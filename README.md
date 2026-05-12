# Calcio Storico Ticket Watcher

Monitors the [official Calcio Storico page](https://cultura.comune.fi.it/calcio-storico-fiorentino) for ticket release signals and sends Telegram notifications when something changes.

## What it detects

- New ticket-related keywords appearing on the page (biglietti, vendita, acquista, etc.)
- New outbound links to ticketing platforms (TicketOne, Vivaticket, etc.)
- June 2026 date references
- General content changes (lower priority, only fires if no specific signal found)

## Setup

1. Create a new GitHub repo (e.g. `calcio-watch`)
2. Push this code to it
3. Add two repository secrets in Settings > Secrets and variables > Actions:
   - `TELEGRAM_BOT_TOKEN` — your Telegram bot token
   - `TELEGRAM_CHAT_ID` — your Telegram chat ID
4. GitHub Actions will run automatically every 3 hours

## Local testing

```bash
pip install -r requirements.txt
python watcher.py --dry-run
```

The `--dry-run` flag fetches the page and analyses it but skips sending notifications. It prints what it found and what it would have sent.

## Tuning

**Schedule**: Edit `.github/workflows/check.yml`, change the cron expression. `0 */3 * * *` = every 3 hours. `0 */2 * * *` = every 2 hours.

**Keywords**: Edit `TICKET_KEYWORDS` in `watcher.py` to add or remove terms.

**Ticketing domains**: Edit `TICKETING_DOMAINS` in `watcher.py` to watch for links to additional ticket sellers.

## How notifications work

- First detection of a new signal triggers a Telegram message
- If the same signal fires on the next run, it's suppressed (no duplicate alerts)
- On fetch errors, one error notification is sent, then the watcher backs off until the next day
- All runs are logged to `runs.log` (committed to the repo so you can check history)
