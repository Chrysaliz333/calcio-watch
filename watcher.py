"""Calcio Storico ticket release watcher.

Fetches the official Calcio Storico page, extracts main content,
detects ticket-release signals, and sends Telegram notifications.
"""

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

URL = "https://cultura.comune.fi.it/calcio-storico-fiorentino"
STATE_FILE = Path(__file__).parent / "state.json"
LOG_FILE = Path(__file__).parent / "runs.log"
LONDON_TZ = ZoneInfo("Europe/London")

TICKET_KEYWORDS = [
    "biglietti", "biglietteria", "vendita", "acquista",
    "ticketone", "vivaticket", "ciaotickets", "tickets", "on sale",
    "prevendita", "prenotazione", "posto", "tribuna",
]

JUNE_DATE_PATTERNS = [
    r"\d{1,2}\s+giugno",
    r"\d{1,2}/0?6/2026",
    r"june\s+\d{1,2}",
    r"giugno\s+2026",
]

TICKETING_DOMAINS = [
    "ticketone.it", "vivaticket.com", "ciaotickets.com",
    "ticketmaster.it", "eventbrite.it", "dice.fm",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("calcio-watch")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "last_hash": None,
        "last_keywords": [],
        "last_links": [],
        "last_check": None,
        "last_notification": None,
        "suppressed_signals": [],
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def append_log(message: str) -> None:
    now = datetime.now(LONDON_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    with LOG_FILE.open("a") as f:
        f.write(f"[{now}] {message}\n")


def fetch_page() -> str:
    """Fetch the official page with a browser-like user agent."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }
    resp = httpx.get(URL, headers=headers, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_main_content(html: str) -> tuple[str, BeautifulSoup]:
    """Extract main content area, stripping nav/footer/scripts/cookie banners."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup.find_all(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": re.compile(r"cookie|gdpr|consent|banner", re.I)}):
        tag.decompose()
    for tag in soup.find_all(["nav", "footer", "header"]):
        tag.decompose()

    # Try to find the main content container
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"class": re.compile(r"content|article|body", re.I)})
        or soup.find("div", attrs={"id": re.compile(r"content|main", re.I)})
    )

    if main is None:
        main = soup.find("body") or soup

    text = main.get_text(separator=" ", strip=True)
    return text, main


def compute_hash(text: str) -> str:
    """Hash the main content text."""
    return hashlib.sha256(text.encode()).hexdigest()


def find_keyword_hits(text: str) -> list[dict]:
    """Find ticket-related keywords in the content."""
    text_lower = text.lower()
    hits = []
    for kw in TICKET_KEYWORDS:
        idx = text_lower.find(kw)
        if idx != -1:
            start = max(0, idx - 100)
            end = min(len(text), idx + len(kw) + 100)
            snippet = text[start:end].strip()
            hits.append({"keyword": kw, "snippet": snippet})
    return hits


def find_date_hits(text: str) -> list[dict]:
    """Find June 2026 date references."""
    hits = []
    for pattern in JUNE_DATE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 80)
            snippet = text[start:end].strip()
            hits.append({"pattern": pattern, "match": match.group(), "snippet": snippet})
    return hits


def find_ticketing_links(soup: BeautifulSoup) -> list[dict]:
    """Find outbound links to known ticketing platforms."""
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        for domain in TICKETING_DOMAINS:
            if domain in href:
                links.append({"domain": domain, "url": href, "text": a.get_text(strip=True)[:100]})
    return links


def build_signals(
    content_hash: str,
    keyword_hits: list[dict],
    date_hits: list[dict],
    ticketing_links: list[dict],
    state: dict,
) -> list[dict]:
    """Build a list of signals worth notifying about."""
    signals = []

    # New ticketing links (not seen before)
    old_link_urls = {l["url"] for l in state.get("last_links", [])}
    new_links = [l for l in ticketing_links if l["url"] not in old_link_urls]
    if new_links:
        signals.append({
            "type": "new_ticketing_links",
            "detail": new_links,
            "priority": "HIGH",
        })

    # New keyword hits (keywords not previously present)
    old_keywords = set(state.get("last_keywords", []))
    new_kw_hits = [h for h in keyword_hits if h["keyword"] not in old_keywords]
    if new_kw_hits:
        signals.append({
            "type": "new_keywords",
            "detail": new_kw_hits,
            "priority": "HIGH",
        })

    # Content hash change (only if there are no more specific signals, to reduce noise)
    if state.get("last_hash") and content_hash != state["last_hash"] and not signals:
        signals.append({
            "type": "content_changed",
            "detail": "Main content hash differs from previous check",
            "priority": "LOW",
        })

    return signals


def should_suppress(signal: dict, state: dict) -> bool:
    """Suppress if the same signal type fired on the previous run."""
    return signal["type"] in state.get("suppressed_signals", [])


def format_telegram_message(signals: list[dict], now: str) -> str:
    """Format signals into a Telegram notification message."""
    lines = ["🏟 *Calcio Storico — Ticket Signal Detected*", ""]

    for sig in signals:
        if sig["type"] == "new_ticketing_links":
            lines.append("*New ticketing links found:*")
            for link in sig["detail"]:
                lines.append(f"  • [{link['domain']}]({link['url']}) — {link['text']}")
        elif sig["type"] == "new_keywords":
            lines.append("*New ticket keywords detected:*")
            for hit in sig["detail"]:
                lines.append(f"  • `{hit['keyword']}`: ...{hit['snippet']}...")
        elif sig["type"] == "content_changed":
            lines.append("*Page content has changed* (no specific keyword/link signal)")

    lines.extend([
        "",
        f"🔗 [View page]({URL})",
        f"🕐 {now}",
    ])
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Send a message via Telegram bot."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    resp.raise_for_status()
    log.info("Telegram notification sent")


def send_error_notification(error: str) -> None:
    """Notify about fetch/parse errors (once, then back off)."""
    now = datetime.now(LONDON_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    message = (
        "⚠️ *Calcio Watch — Error*\n\n"
        f"`{error}`\n\n"
        f"Will back off until next day.\n"
        f"🕐 {now}"
    )
    try:
        send_telegram(message)
    except Exception as e:
        log.error(f"Failed to send error notification: {e}")


def run(dry_run: bool = False) -> None:
    """Main watcher logic."""
    state = load_state()
    now = datetime.now(LONDON_TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %Z")

    # Back off after errors: if last check was an error today, skip
    last_check = state.get("last_check")
    if last_check and state.get("last_error"):
        last_dt = datetime.fromisoformat(last_check)
        if last_dt.date() == now.date():
            log.info("Backing off after previous error today")
            append_log("SKIP — backing off after error")
            return

    # Fetch and parse
    try:
        html = fetch_page()
        content_text, content_soup = extract_main_content(html)
    except Exception as e:
        log.error(f"Fetch/parse error: {e}")
        append_log(f"ERROR — {e}")
        state["last_check"] = now.isoformat()
        state["last_error"] = str(e)
        save_state(state)
        if not dry_run:
            send_error_notification(str(e))
        return

    # Clear error flag on successful fetch
    state["last_error"] = None

    # Analyse
    content_hash = compute_hash(content_text)
    keyword_hits = find_keyword_hits(content_text)
    date_hits = find_date_hits(content_text)
    ticketing_links = find_ticketing_links(content_soup)

    signals = build_signals(content_hash, keyword_hits, date_hits, ticketing_links, state)

    # Filter suppressed signals
    active_signals = [s for s in signals if not should_suppress(s, state)]

    log.info(f"Hash: {content_hash[:16]}... | Keywords: {len(keyword_hits)} | "
             f"Dates: {len(date_hits)} | Links: {len(ticketing_links)} | "
             f"Signals: {len(signals)} ({len(active_signals)} active)")

    if dry_run:
        print("\n--- DRY RUN RESULTS ---")
        print(f"Content hash: {content_hash}")
        print(f"Previous hash: {state.get('last_hash', 'none')}")
        print(f"Keyword hits: {json.dumps(keyword_hits, indent=2)}")
        print(f"Date hits: {json.dumps(date_hits, indent=2)}")
        print(f"Ticketing links: {json.dumps(ticketing_links, indent=2)}")
        print(f"Signals: {json.dumps(signals, indent=2)}")
        print(f"Active (unsuppressed): {len(active_signals)}")
        if active_signals:
            print(f"\nWould send:\n{format_telegram_message(active_signals, now_str)}")
        else:
            print("\nNo notification would be sent.")
        return

    # Notify
    if active_signals:
        message = format_telegram_message(active_signals, now_str)
        send_telegram(message)
        state["last_notification"] = now.isoformat()
        append_log(f"NOTIFY — {len(active_signals)} signals: "
                   f"{', '.join(s['type'] for s in active_signals)}")
    else:
        append_log("OK — no new signals")

    # Update state
    state["last_hash"] = content_hash
    state["last_keywords"] = [h["keyword"] for h in keyword_hits]
    state["last_links"] = ticketing_links
    state["last_check"] = now.isoformat()
    state["suppressed_signals"] = [s["type"] for s in signals]
    save_state(state)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run(dry_run=dry)
