import os
import re
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
import logging
from dump import (
    get_items_list,
    create_session,
    get_and_prepare_download_path,
    get_real_download_url,
    get_url_data
)
import requests
from tqdm import tqdm
from pyrogram.errors import MessageNotModified
from urllib.parse import urljoin
import sys

load_dotenv()

# ==================
# CONFIG - HEROKU COMPATIBLE
# ==================
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Heroku uses /tmp for temporary files
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', '/tmp/downloads')

# Create downloads directory if it doesn't exist
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ==================
# LOGGING SETUP
# ==================
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)

# ==================
# VALIDATION
# ==================
logger.info("[INIT] Validating credentials...")
if not all([API_ID, API_HASH, BOT_TOKEN]):
    logger.error("[INIT] Missing credentials!")
    sys.exit(1)
logger.info("[INIT] Credentials OK")

# ==================
# PYROGRAM CLIENT
# ==================
logger.info("[INIT] Creating Pyrogram Client...")
app = Client(
    "bunkr_downloader_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=DOWNLOADS_DIR
)
logger.info("[INIT] Pyrogram Client created")

# ==================
# URL PATTERNS
# ==================
URL_PATTERNS = [
    r'https?://bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)(?:/[^\s]*)?',
    r'https?://bunkrrr\.org(?:/[^\s]*)?',
    r'https?://cyberdrop\.me(?:/[^\s]*)?',
]

def extract_urls(text):
    """Extract URLs from text"""
    if not text:
        return []
    
    urls = []
    seen = set()
    
    for pattern in URL_PATTERNS:
        try:
            matches = re.findall(pattern, text)
            for match in matches:
                if match and match not in seen:
                    urls.append(match)
                    seen.add(match)
                    logger.debug(f"[URL EXTRACT] Found: {match}")
        except Exception as e:
            logger.warning(f"[URL EXTRACT] Pattern error: {e}")
    
    logger.info(f"[URL EXTRACT] Total {len(urls)} URLs found")
    return urls

def is_valid_bunkr_url(url):
    """Check if URL is valid Bunkr/Cyberdrop"""
    if not url:
        return False
    
    url_lower = url.lower()
    valid = any([
        'bunkr.' in url_lower,
        'bunkrrr.org' in url_lower,
        'cyberdrop.me' in url_lower,
    ])
    
    if valid:
        logger.info(f"[URL VALID] OK: {url}")
    else:
        logger.warning(f"[URL VALID] FAIL: {url}")
    
    return valid

# ==================
# DOWNLOAD HANDLER
# ==================
async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    """Download and upload files"""
    status_msg = None
    try:
        logger.info(f"[DOWNLOAD] START: {url}")
        
        # Send status
        status_msg = await message.reply_text(f"Processing: {url[:50]}...")
        logger.info("[DOWNLOAD] Status message sent")
        
        # Get items from URL
        logger.info("[DOWNLOAD] Getting items list...")
        items = get_items_list(session, url, None, False, DOWNLOADS_DIR)
        
        if not items:
            logger.warning("[DOWNLOAD] No items found")
            await status_msg.edit_text("No files found in this link")
            return
        
        logger.info(f"[DOWNLOAD] Found {len(items)} items")
        
        # Download each file
        for idx, item in enumerate(items, 1):
            try:
                file_path = get_and_prepare_download_path(item, DOWNLOADS_DIR)
                logger.info(f"[DOWNLOAD {idx}/{len(items)}] File: {file_path}")
                
                # Update status
                await status_msg.edit_text(
                    f"Downloading {idx}/{len(items)}...\n{os.path.basename(file_path)[:40]}"
                )
                
                # Get download URL
                dl_url = get_real_download_url(session, item['link'])
                if not dl_url:
                    logger.warning(f"[DOWNLOAD] Could not get URL for {file_path}")
                    continue
                
                # Download file
                logger.info(f"[DOWNLOAD] Downloading from: {dl_url}")
                response = session.get(dl_url, timeout=60, stream=True)
                
                if response.status_code != 200:
                    logger.error(f"[DOWNLOAD] HTTP {response.status_code}")
                    continue
                
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                
                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                logger.info(f"[DOWNLOAD] Saved: {file_path}")
                
                # Upload to Telegram
                logger.info(f"[UPLOAD] Uploading to Telegram...")
                await message.reply_document(file_path)
                logger.info(f"[UPLOAD] Success")
                
                # Clean up
                try:
                    os.remove(file_path)
                except:
                    pass
                    
            except Exception as e:
                logger.error(f"[DOWNLOAD] Item error: {e}")
                continue
        
        await status_msg.edit_text("Download complete!")
        logger.info("[DOWNLOAD] ALL COMPLETE")
        
    except Exception as e:
        logger.exception(f"[DOWNLOAD] ERROR: {e}")
        try:
            if status_msg:
                await status_msg.edit_text(f"Error: {str(e)[:100]}")
        except:
            pass

# ==================
# MESSAGE HANDLERS
# ==================
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Handle /start command"""
    logger.info("[COMMAND] /start")
    await message.reply_text(
        "Bunkr Downloader Bot\n\n"
        "Send any Bunkr or Cyberdrop link.\n"
        "Bot will download and upload files automatically."
    )

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    """Handle /help command"""
    logger.info("[COMMAND] /help")
    await message.reply_text(
        "How to use:\n"
        "1. Send a Bunkr or Cyberdrop link\n"
        "2. Wait for download to complete\n"
        "3. Files will be uploaded automatically"
    )

@app.on_message(filters.text & ~filters.bot)
async def handle_message(client: Client, message: Message):
    """Handle text messages"""
    try:
        msg_text = message.text or ""
        logger.info(f"[MESSAGE] From {message.chat.id}: {msg_text[:100]}")
        
        # Extract URLs
        urls = extract_urls(msg_text)
        
        if not urls:
            logger.debug("[MESSAGE] No URLs found")
            return
        
        logger.info(f"[MESSAGE] Found {len(urls)} URLs")
        
        # Process each URL
        session = create_session()
        
        for url in urls:
            if is_valid_bunkr_url(url):
                logger.info(f"[MESSAGE] Processing: {url}")
                await download_and_send_file(client, message, url, session)
            else:
                logger.warning(f"[MESSAGE] Invalid: {url}")
        
    except Exception as e:
        logger.exception(f"[MESSAGE] ERROR: {e}")
        try:
            await message.reply_text(f"Error: {str(e)[:100]}")
        except:
            pass

# ==================
# BOT START
# ==================
def start_bot():
    """Start the bot"""
    logger.info("=" * 70)
    logger.info("BUNKR DOWNLOADER BOT STARTING")
    logger.info("=" * 70)
    logger.info(f"API_ID: {API_ID}")
    logger.info(f"Downloads: {DOWNLOADS_DIR}")
    logger.info("=" * 70)
    
    try:
        logger.info("[BOT] Connecting...")
        app.run()
        logger.info("[BOT] Stopped")
        
    except KeyboardInterrupt:
        logger.info("[BOT] Shutdown")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"[BOT] ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    start_bot()
