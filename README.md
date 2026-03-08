# Raspberry Pi 5 Daily Important Email Reporter (Discord Bot)

This project runs on a Raspberry Pi 5 (including AI HAT setups) and sends one daily Discord report with important emails from your inbox.

## What it does
- Connects to your IMAP mailbox (Gmail/Outlook/custom IMAP).
- Reads today's emails.
- Scores/filter emails by:
  - subject keywords,
  - sender allow-list,
  - attachment presence (small score bonus),
  - optional exclude keywords.
- Prevents duplicate reporting using a local state file.
- Uses a local LLM via **Ollama** to summarize important messages.
- Sends the report to a Discord channel using a **Discord Bot token** (not webhook).
- Runs every day at a fixed local time.

## 1) Install
```bash
sudo apt update
sudo apt install -y python3 python3-pip
python3 -m pip install -r requirements.txt
```

## 2) Prepare config
```bash
cp .env.example .env
nano .env
```

Set at minimum:
- `IMAP_HOST`, `EMAIL_ADDRESS`, `EMAIL_PASSWORD`
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`
- `TZ`, `REPORT_HOUR`, `REPORT_MINUTE`

> For Gmail, use an **App Password** (not your normal login password).

## 3) Discord bot setup
1. Create a bot in Discord Developer Portal.
2. Enable bot token and invite it to your server.
3. Give it permission to send messages in your target channel.
4. Copy Bot Token and Channel ID into `.env`.

## 4) (Recommended for AI HAT) Run local model with Ollama
Install Ollama and pull a model (example):
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b
ollama serve
```

## 5) Run bot
```bash
set -a
source .env
set +a
python3 app/email_digest_bot.py
```

## Useful features you can toggle
- `UNREAD_ONLY=true` → only process unread emails.
- `REPORT_ON_STARTUP=true` → send one report immediately on startup.
- `EXCLUDE_SUBJECT_KEYWORDS=...` → skip newsletters/spam-like subjects.
- `STATE_PATH=.state/processed_hashes.json` → avoids duplicate summaries.

## Optional: autostart with systemd
Create `/etc/systemd/system/email-digest.service`:
```ini
[Unit]
Description=Daily important email digest bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/workspace/Deelousek
ExecStart=/usr/bin/bash -lc 'set -a; source /workspace/Deelousek/.env; set +a; python3 app/email_digest_bot.py'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now email-digest.service
sudo systemctl status email-digest.service
```

## Notes
- Discord messages are chunked to avoid the 2000-character limit.
- Email importance logic is easy to tune in `.env`.
- The script uses only standard Python modules plus APScheduler.
