import os
import sys
import logging
from dotenv import load_dotenv

# Setup logging before anything else
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

def main():
    """Main entry point for the bot"""
    logger.info("=" * 60)
    logger.info("BUNKR DOWNLOADER BOT - MAIN ENTRY POINT")
    logger.info("=" * 60)
    
    # Get environment variables
    API_ID = os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    # Log environment info
    logger.info(f"Environment: {'HEROKU' if os.getenv('DYNO') else 'LOCAL'}")
    logger.info(f"API_ID set: {bool(API_ID)}")
    logger.info(f"API_HASH set: {bool(API_HASH)}")
    logger.info(f"BOT_TOKEN set: {bool(BOT_TOKEN)}")
    
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.error("=" * 60)
        logger.error("❌ MISSING REQUIRED ENVIRONMENT VARIABLES!")
        logger.error("=" * 60)
        logger.error("Required variables:")
        logger.error("  - TELEGRAM_API_ID")
        logger.error("  - TELEGRAM_API_HASH")
        logger.error("  - TELEGRAM_BOT_TOKEN")
        logger.error("=" * 60)
        sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("✅ All environment variables found!")
    logger.info("=" * 60)
    
    # Import and run the bot
    try:
        logger.info("Importing telegram_bot module...")
        from telegram_bot import start_bot
        
        logger.info("Starting bot...")
        start_bot()
        
    except ImportError as e:
        logger.error(f"Failed to import telegram_bot: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Bot crashed with error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
