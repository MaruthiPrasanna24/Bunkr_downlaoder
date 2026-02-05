import os
import sys
import asyncio
from dotenv import load_dotenv
from pyrogram import idle

load_dotenv()

def check_env():
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not all([api_id, api_hash, bot_token]):
        print("❌ Missing required environment variables!")
        sys.exit(1)

    return int(api_id), api_hash, bot_token


async def main():
    print("🚀 Starting Telegram Bot...")

    check_env()

    from telegram_bot import app

    await app.start()
    print("✅ Bot started successfully and listening")

    await idle()

    print("🛑 Bot stopped")
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
