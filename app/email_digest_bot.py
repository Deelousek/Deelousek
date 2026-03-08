#!/usr/bin/env python3
"""Daily important-email summary bot for Raspberry Pi.

Flow:
1. Read config from environment variables.
2. Connect to IMAP and fetch today's emails.
3. Score/filter important messages.
4. Summarize with local Ollama model (good fit for Pi + AI accelerator).
5. Post report to Discord channel using Bot token.
6. Repeat every day at configured local time.
"""

from __future__ import annotations

import datetime as dt
import email
import hashlib
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from html import unescape
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple
from urllib import request

from apscheduler.schedulers.blocking import BlockingScheduler
from zoneinfo import ZoneInfo


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOGGER = logging.getLogger("email-digest-bot")


@dataclass
class Config:
    imap_host: str
    imap_port: int
    email_address: str
    email_password: str
    mailbox: str
    timezone: str
    run_hour: int
    run_minute: int
    subject_keywords: List[str]
    sender_allowlist: List[str]
    exclude_subject_keywords: List[str]
    unread_only: bool
    report_on_startup: bool
    discord_bot_token: str
    discord_channel_id: str
    ollama_url: str
    ollama_model: str
    max_messages: int
    state_path: str


@dataclass
class EmailItem:
    sender: str
    subject: str
    date: str
    snippet: str
    has_attachment: bool


@dataclass
class ScoredEmail:
    item: EmailItem
    score: int


def env_list(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    return Config(
        imap_host=os.environ["IMAP_HOST"],
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        email_address=os.environ["EMAIL_ADDRESS"],
        email_password=os.environ["EMAIL_PASSWORD"],
        mailbox=os.getenv("IMAP_MAILBOX", "INBOX"),
        timezone=os.getenv("TZ", "UTC"),
        run_hour=int(os.getenv("REPORT_HOUR", "18")),
        run_minute=int(os.getenv("REPORT_MINUTE", "0")),
        subject_keywords=env_list(
            "IMPORTANT_SUBJECT_KEYWORDS",
            "urgent,invoice,deadline,action required,meeting,security,failed",
        ),
        sender_allowlist=env_list("IMPORTANT_SENDER_ALLOWLIST", ""),
        exclude_subject_keywords=env_list(
            "EXCLUDE_SUBJECT_KEYWORDS",
            "newsletter,promo,advertisement",
        ),
        unread_only=env_bool("UNREAD_ONLY", default=False),
        report_on_startup=env_bool("REPORT_ON_STARTUP", default=False),
        discord_bot_token=os.environ["DISCORD_BOT_TOKEN"],
        discord_channel_id=os.environ["DISCORD_CHANNEL_ID"],
        ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate"),
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
        max_messages=int(os.getenv("MAX_MESSAGES", "30")),
        state_path=os.getenv("STATE_PATH", ".state/processed_hashes.json"),
    )


def decode_mime(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded).strip()


def strip_html(text: str) -> str:
    text = re.sub(r"<script.*?>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in content_disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                return re.sub(r"\s+", " ", text).strip()
            if content_type == "text/html":
                return strip_html(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return strip_html(text)
            return re.sub(r"\s+", " ", text).strip()
    return ""


def message_has_attachment(msg: Message) -> bool:
    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in content_disposition:
            return True
    return False


def score_email(item: EmailItem, config: Config) -> int:
    sender_l = item.sender.lower()
    subject_l = item.subject.lower()
    score = 0

    if any(keyword and keyword in subject_l for keyword in config.exclude_subject_keywords):
        return -999

    if any(allowed and allowed in sender_l for allowed in config.sender_allowlist):
        score += 5
    for keyword in config.subject_keywords:
        if keyword and keyword in subject_l:
            score += 2

    if item.has_attachment:
        score += 1

    return score


def fingerprint(item: EmailItem) -> str:
    raw = f"{item.sender}|{item.subject}|{item.date}|{item.snippet[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_processed_hashes(path: str) -> Set[str]:
    state_file = Path(path)
    if not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return set(data.get("hashes", []))
    except (json.JSONDecodeError, OSError, TypeError):
        LOGGER.warning("Could not read state file, continuing with empty state")
        return set()


def save_processed_hashes(path: str, hashes: Set[str]) -> None:
    state_file = Path(path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hashes": sorted(hashes)[-3000:]}
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_today_emails(config: Config) -> List[EmailItem]:
    tz = ZoneInfo(config.timezone)
    today = dt.datetime.now(tz).strftime("%d-%b-%Y")
    collected: List[EmailItem] = []

    LOGGER.info("Connecting to IMAP server %s", config.imap_host)
    with imaplib.IMAP4_SSL(config.imap_host, config.imap_port) as client:
        client.login(config.email_address, config.email_password)
        client.select(config.mailbox)

        query = f'(SINCE "{today}")'
        if config.unread_only:
            query = f'(SINCE "{today}" UNSEEN)'

        status, data = client.search(None, query)
        if status != "OK":
            raise RuntimeError("IMAP search failed")

        ids = data[0].split()
        for message_id in ids[-config.max_messages :]:
            status, payload = client.fetch(message_id, "(RFC822)")
            if status != "OK" or not payload or payload[0] is None:
                continue

            raw = payload[0][1]
            message = email.message_from_bytes(raw)

            subject = decode_mime(message.get("Subject", "(No Subject)"))
            sender = decode_mime(message.get("From", "Unknown"))
            date = decode_mime(message.get("Date", ""))
            body = extract_body(message)[:800]

            collected.append(
                EmailItem(
                    sender=sender,
                    subject=subject,
                    date=date,
                    snippet=body,
                    has_attachment=message_has_attachment(message),
                )
            )

    LOGGER.info("Fetched %s emails for today", len(collected))
    return collected


def format_emails(items: Iterable[ScoredEmail]) -> str:
    blocks = []
    for i, entry in enumerate(items, start=1):
        priority = "HIGH" if entry.score >= 6 else "MEDIUM" if entry.score >= 3 else "LOW"
        attachment_note = "Yes" if entry.item.has_attachment else "No"
        blocks.append(
            f"{i}. Priority: {priority} (score {entry.score})\n"
            f"   From: {entry.item.sender}\n"
            f"   Subject: {entry.item.subject}\n"
            f"   Date: {entry.item.date}\n"
            f"   Attachment: {attachment_note}\n"
            f"   Snippet: {entry.item.snippet[:260]}"
        )
    return "\n\n".join(blocks)


def summarize_with_ollama(config: Config, important_emails: List[ScoredEmail]) -> str:
    if not important_emails:
        return "No important emails found today ✅"

    prompt = (
        "You are an executive assistant. Create a concise daily report from the emails below. "
        "Use bullet points grouped by theme, include deadlines, requested actions, and risks. "
        "Highlight HIGH priority emails first. At the end add a short 'Top 3 actions' section.\n\n"
        f"EMAILS:\n{format_emails(important_emails)}"
    )

    payload = json.dumps(
        {
            "model": config.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2},
        }
    ).encode("utf-8")

    req = request.Request(
        config.ollama_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))

    return data.get("response", "(No summary returned)").strip()


def post_to_discord(config: Config, report: str, important_count: int, total_count: int) -> None:
    tz = ZoneInfo(config.timezone)
    stamp = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M %Z")
    content = (
        f"📬 **Daily Important Email Report** ({stamp})\n"
        f"Scanned: **{total_count}** | Important: **{important_count}**\n\n{report}"
    )

    chunks = [content[i : i + 1900] for i in range(0, len(content), 1900)]
    for chunk in chunks:
        payload = json.dumps({"content": chunk}).encode("utf-8")
        req = request.Request(
            f"https://discord.com/api/v10/channels/{config.discord_channel_id}/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {config.discord_bot_token}",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=30) as response:
            if response.status >= 300:
                raise RuntimeError(f"Discord bot API failed with status {response.status}")


def prepare_important(config: Config, emails: List[EmailItem]) -> Tuple[List[ScoredEmail], Set[str]]:
    processed_hashes = load_processed_hashes(config.state_path)
    new_hashes: Set[str] = set()

    scored: List[ScoredEmail] = []
    for item in emails:
        fp = fingerprint(item)
        new_hashes.add(fp)
        if fp in processed_hashes:
            continue

        score = score_email(item, config)
        if score >= 2:
            scored.append(ScoredEmail(item=item, score=score))

    scored.sort(key=lambda x: x.score, reverse=True)
    return scored, processed_hashes.union(new_hashes)


def run_once(config: Config) -> None:
    emails = fetch_today_emails(config)
    important, merged_hashes = prepare_important(config, emails)
    report = summarize_with_ollama(config, important)
    post_to_discord(config, report, len(important), len(emails))
    save_processed_hashes(config.state_path, merged_hashes)
    LOGGER.info("Daily report sent to Discord (bot API)")


def main() -> None:
    config = load_config()

    if config.report_on_startup:
        LOGGER.info("Running startup report before scheduler")
        run_once(config)

    scheduler = BlockingScheduler(timezone=ZoneInfo(config.timezone))
    scheduler.add_job(
        run_once,
        "cron",
        hour=config.run_hour,
        minute=config.run_minute,
        args=[config],
    )

    LOGGER.info(
        "Scheduler started. Daily run at %02d:%02d (%s)",
        config.run_hour,
        config.run_minute,
        config.timezone,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
