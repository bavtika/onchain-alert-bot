import asyncio
from datetime import datetime, timezone

from telegram import Bot
from telegram.constants import ParseMode

import config
from utils.logger import log

_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    return _bot


URGENCY_EMOJI = {
    "critical": "\U0001F534",  # red circle
    "warning": "\U0001F7E0",   # orange circle
    "info": "\U0001F535",      # blue circle
}


def _escape_md(text: str) -> str:
    special = r"_*[]()~`>#+-=|{}.!"
    out = []
    for ch in text:
        if ch in special:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def format_alert(strategy: str, title: str, body: str, urgency: str = "warning") -> str:
    emoji = URGENCY_EMOJI.get(urgency, URGENCY_EMOJI["info"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{emoji} *{_escape_md(strategy.upper())}*\n\n"
        f"*{_escape_md(title)}*\n\n"
        f"{_escape_md(body)}\n\n"
        f"\u23F0 {_escape_md(now)}"
    )


async def send_alert(strategy: str, title: str, body: str, urgency: str = "warning") -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured, skipping alert")
        return False

    message = format_alert(strategy, title, body, urgency)

    for attempt in range(3):
        try:
            bot = get_bot()
            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            log.info(f"Alert sent: [{strategy}] {title}")
            return True
        except Exception as e:
            log.error(f"Telegram send failed (attempt {attempt + 1}): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return False


async def send_raw(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        bot = get_bot()
        await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)
        return True
    except Exception as e:
        log.error(f"Telegram raw send failed: {e}")
        return False
