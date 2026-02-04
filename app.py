import os
import sys
import logging
import traceback
from dotenv import load_dotenv

# Setup logging before anything else
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)

# Load .env first
logger.info("[APP] Loading .env file...")
load_dotenv()
logger.info("[APP] .env loaded")

def main():
    """Main entry point for the bot"""
    logger.info("=" * 70)
    logger.info("BUNKR DOWNLOADER BOT - STARTING")
    logger.info("=" * 70)
    logger.info(f"[APP] Environment: {'HEROKU' if os.getenv('DYNO') else 'LOCAL'}")
    logger.info(f"[APP] Python: {sys.version}")
   
    # Get environment variables
    API_ID = os.getenv('TELEGRAM_API_ID')
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
   
    # Log environment info
    logger.info(f"[APP] API_ID set: {bool(API_ID)}")
    logger.info(f"[APP] API_HASH set: {bool(API_HASH)}")
    logger.info(f"[APP] BOT_TOKEN set: {bool(BOT_TOKEN)}")
   
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.error("=" * 70)
        logger.error("MISSING REQUIRED ENVIRONMENT VARIABLES!")
        logger.error("=" * 70)
        logger.error("Required variables:")
        logger.error(" - TELEGRAM_API_ID")
        logger.error(" - TELEGRAM_API_HASH")
        logger.error(" - TELEGRAM_BOT_TOKEN")
        logger.error("=" * 70)
        sys.exit(1)
   
    logger.info("=" * 70)
    logger.info("All environment variables found!")
    logger.info("=" * 70)
   
    # Import and run the bot
    try:
        logger.info("[APP] Importing telegram_bot module...")
        from telegram_bot import start_bot
        logger.info("[APP] Successfully imported telegram_bot")
       
        logger.info("[APP] Calling start_bot()...")
        start_bot()
        logger.info("[APP] Bot has stopped")
       
    except ImportError as e:
        logger.error(f"[APP] Failed to import telegram_bot: {e}")
        traceback.print_exc()
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("[APP] Received KeyboardInterrupt, shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"[APP] Bot crashed with error: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
