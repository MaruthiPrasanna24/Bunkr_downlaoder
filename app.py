import os
import sys
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    API_ID = os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.error("Missing required environment variables!")
        logger.error("Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    
    logger.info("Starting Telegram Bot...")
    logger.info(f"API_ID: {API_ID}")
    logger.info(f"Environment: Heroku" if os.getenv('DYNO') else "Environment: Local")
    
    # Create downloads directory if it doesn't exist
    downloads_dir = os.getenv("DOWNLOADS_DIR", "downloads")
    if not os.path.exists(downloads_dir):
        os.makedirs(downloads_dir)
        logger.info(f"Created downloads directory: {downloads_dir}")
    
    try:
        # Run the bot directly
        from telegram_bot import start_bot
        start_bot()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Error starting bot: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
