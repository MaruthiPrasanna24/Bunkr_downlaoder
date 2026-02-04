# app.py
import os
import sys
from dotenv import load_dotenv

load_dotenv()

def main():
    API_ID = os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

    if not all([API_ID, API_HASH, BOT_TOKEN]):
        print("Missing required environment variables!")
        print("Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    print("Starting Telegram Bot...")

    # Import and run the bot
    from telegram_bot import run_bot
    run_bot()

if __name__ == "__main__":
    main()
