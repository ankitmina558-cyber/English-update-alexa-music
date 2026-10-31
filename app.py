import asyncio
import threading

import aiohttp
import uvicorn
from fastapi import FastAPI
from pyrogram import idle
from pyrogram.types import BotCommand

import config
import db
import botstate
from clients import bot, assistant, call_py, LOGGER
from helpers import smallcaps_title, expandable_quote, strip_quotes, DIVIDER

# Importing is required so the handlers get registered
import play  # noqa: F401

web = FastAPI()


@web.get("/")
async def root():
    return {"status": "running"}


def run_web():
    uvicorn.run(web, host="0.0.0.0", port=config.PORT, log_level="warning")


async def keep_alive():
    """
    Render free web services go to sleep after 15 min of inactivity, so the
    bot periodically pings its own URL to stay online. RENDER_EXTERNAL_URL is
    provided by Render at deploy time — no need to set it manually.
    """
    if not config.RENDER_EXTERNAL_URL:
        LOGGER.info("RENDER_EXTERNAL_URL is not set — skipping keep-alive ping.")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(config.RENDER_EXTERNAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    LOGGER.info(f"Keep-alive ping: {resp.status}")
            except Exception as e:
                LOGGER.warning(f"Keep-alive ping fail: {e}")
            # Ping immediately on start, then every PING_INTERVAL seconds —
            # Render's free tier sleeps after ~15 min of inactivity.
            await asyncio.sleep(config.PING_INTERVAL)


async def register_bot_commands():
    """Sets the bot commands so the '/' menu shows up in groups."""
    try:
        await bot.set_bot_commands(
            [
                BotCommand("start", smallcaps_title("Start the bot")),
                BotCommand("play", smallcaps_title("Play a song")),
                BotCommand("skip", smallcaps_title("Play the next song")),
                BotCommand("pause", smallcaps_title("Pause the music")),
                BotCommand("resume", smallcaps_title("Resume the music")),
                BotCommand("stop", smallcaps_title("Stop the music")),
                BotCommand("reload", smallcaps_title("Refresh the bot (admin only)")),
                BotCommand("id", smallcaps_title("Show your / the group ID")),
            ]
        )
    except Exception as e:
        LOGGER.warning(f"Could not set bot commands: {e}")


async def _run_once():
    await bot.start()
    LOGGER.info("✅ Bot started")

    await assistant.start()
    LOGGER.info("✅ Assistant started")

    # IMPORTANT: fetch the assistant's dialogs once so pyrogram caches the
    # peer + access_hash of every group/channel. Without this,
    # "ValueError: Peer id invalid: ..." shows up a bit later when the bot
    # tries change_stream/leave_group_call on a chat whose peer is not in the
    # assistant's local cache.
    try:
        async for _ in assistant.get_dialogs():
            pass
        LOGGER.info("✅ Assistant peers cached")
    except Exception as e:
        LOGGER.warning(f"Error while caching dialogs: {e}")

    await call_py.start()
    LOGGER.info("✅ PyTgCalls started — the bot is now ready to play music")

    # Load the status the owner last set with /on /off
    try:
        botstate.set_enabled(await db.get_bot_status())
        LOGGER.info(f"✅ Bot status loaded: {'ON' if botstate.is_enabled() else 'OFF'}")
    except Exception as e:
        LOGGER.warning(f"Could not load bot status, keeping default ON: {e}")

    await register_bot_commands()

    if config.LOG_GROUP_ID:
        log_text = (
            f"✅ {smallcaps_title('Bot Restarted')}..!! ✦\n\n"
            + expandable_quote(
                f"⌾ {smallcaps_title('The bot has restarted and is now online')}.\n\n{DIVIDER}"
            )
        )
        try:
            try:
                await bot.send_message(config.LOG_GROUP_ID, log_text)
            except Exception:
                await bot.send_message(config.LOG_GROUP_ID, strip_quotes(log_text))
        except Exception as e:
            LOGGER.warning(f"Could not send message to the log group: {e}")

    keep_alive_task = asyncio.create_task(keep_alive())

    try:
        await idle()
    finally:
        keep_alive_task.cancel()
        try:
            await bot.stop()
        except Exception:
            pass
        try:
            await assistant.stop()
        except Exception:
            pass
        LOGGER.info("🛑 Bot stopped")


async def main():
    """
    Wraps _run_once() so that no unexpected crash (network drop, connection
    reset, etc.) can take the bot offline permanently — the process restarts
    itself after a short delay, until Render kills the process.
    """
    while True:
        try:
            await _run_once()
            break  # idle() only returns when the process is stopped normally
        except Exception as e:
            LOGGER.error(f"Bot crashed, restarting in 10 seconds: {e}")
            await asyncio.sleep(10)


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.get_event_loop().run_until_complete(main())
