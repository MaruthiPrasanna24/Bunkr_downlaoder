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
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ==================
# VALIDATION
# ==================
if not all([API_ID, API_HASH, BOT_TOKEN]):
    logger.error("Missing required environment variables!")
    logger.error("Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN")
    sys.exit(1)

try:
    API_ID = int(API_ID)
except ValueError:
    logger.error("TELEGRAM_API_ID must be an integer")
    sys.exit(1)

# ==================
# PYROGRAM CLIENT
# ==================
app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=DOWNLOADS_DIR,
    in_memory=False
)

# ==================
# URL PATTERNS
# ==================
URL_PATTERNS = [
    r'https?://bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)(?:/[^\s]*)?',
    r'https?://bunkrrr\.org(?:/[^\s]*)?',
    r'https?://cyberdrop\.me(?:/[^\s]*)?',
    r'bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)(?:/[^\s]*)',
    r'bunkrrr\.org(?:/[^\s]*)',
    r'cyberdrop\.me(?:/[^\s]*)'
]


def extract_urls(text):
    """Extract URLs from text with multiple patterns"""
    if not text:
        logger.debug("[URL EXTRACTION] No text provided")
        return []
    
    urls = []
    seen = set()
    
    for pattern in URL_PATTERNS:
        try:
            matches = re.findall(pattern, text)
            for match in matches:
                if match and match not in seen:
                    url = match if match.startswith('http') else f'https://{match}'
                    urls.append(url)
                    seen.add(match)
                    logger.info(f"[URL EXTRACTION] Found URL: {url}")
        except Exception as e:
            logger.warning(f"[URL EXTRACTION] Pattern error: {e}")
    
    logger.info(f"[URL EXTRACTION] Total unique URLs found: {len(urls)}")
    return urls


def is_valid_bunkr_url(url):
    """Validate if URL is from supported domains"""
    if not url:
        return False
    
    url_lower = url.lower()
    
    valid_domains = [
        'bunkr.sk', 'bunkr.cr', 'bunkr.ru', 'bunkr.su',
        'bunkr.pk', 'bunkr.is', 'bunkr.si', 'bunkr.ph',
        'bunkr.ps', 'bunkr.ci', 'bunkr.ax', 'bunkr.fi',
        'bunkr.ac', 'bunkr.black', 'bunkr.la',
        'bunkrrr.org', 'cyberdrop.me'
    ]
    
    is_valid = any(domain in url_lower for domain in valid_domains)
    
    if is_valid:
        logger.info(f"[URL VALIDATION] ✓ Valid URL: {url}")
    else:
        logger.warning(f"[URL VALIDATION] ✗ Invalid domain: {url}")
    
    return is_valid


async def safe_edit(msg, text):
    """Safely edit Telegram message without crashing"""
    try:
        if msg and msg.text != text:
            await msg.edit_text(text)
            logger.debug(f"[MESSAGE EDIT] Updated: {text[:50]}...")
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[MESSAGE EDIT] Failed: {e}")


async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    """Download files from URL and send to Telegram"""
    status_msg = None
    try:
        logger.info(f"[DOWNLOAD START] Processing URL: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""

        is_bunkr = "bunkr" in url.lower() or "bunkrrr" in url.lower()
        logger.info(f"[URL TYPE] Bunkr detected: {is_bunkr}")

        # Fix URL format - ensure HTTPS
        if not url.startswith("http"):
            url = f"https://{url}"
        
        logger.info(f"[HTTP REQUEST] Fetching URL: {url}")
        r = session.get(url, timeout=30)
        
        if r.status_code != 200:
            error_msg = f"❌ HTTP {r.status_code} - Failed to fetch page"
            logger.error(f"[HTTP ERROR] {error_msg}")
            await safe_edit(status_msg, error_msg)
            return

        # Parse HTML
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, 'html.parser')

        # Check if it's a direct file or album
        is_direct = (
            soup.find('span', {'class': 'ic-videos'}) is not None or
            soup.find('div', {'class': 'lightgallery'}) is not None
        )

        items = []

        if is_direct:
            logger.info("[PARSING] Direct file detected")
            h1 = soup.find('h1', {'class': 'text-[20px]'}) or soup.find('h1', {'class': 'truncate'})
            album_name = h1.text if h1 else "file"
            item = get_real_download_url(session, url, True, album_name)
            if item:
                items.append(item)
                logger.info(f"[PARSING] Added direct file: {album_name}")
        else:
            logger.info("[PARSING] Album/folder detected")
            h1 = soup.find('h1', {'class': 'truncate'})
            album_name = h1.text if h1 else "album"
            
            theItems = soup.find_all('div', {'class': 'theItem'})
            logger.info(f"[PARSING] Found {len(theItems)} items in album")
            
            for theItem in theItems:
                box = theItem.find('a', {'class': 'after:absolute'})
                if box:
                    view_url = urljoin(url, box["href"])
                    name = theItem.find("p").text if theItem.find("p") else "file"
                    direct_item = get_real_download_url(session, view_url, True, name)
                    if direct_item:
                        items.append(direct_item)
                        logger.info(f"[PARSING] Added album item: {name}")

        if not items:
            error_msg = "❌ No downloadable items found"
            logger.warning(f"[PARSING] {error_msg}")
            await safe_edit(status_msg, error_msg)
            return

        logger.info(f"[DOWNLOAD PREP] Found {len(items)} items, preparing download")
        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📥 Found {len(items)} items. Starting download...")

        # Download and upload each item
        for idx, item in enumerate(items, 1):
            try:
                if isinstance(item, dict):
                    file_url = item.get("url")
                    file_name = item.get("name", album_name)
                else:
                    file_url = item
                    file_name = album_name

                logger.info(f"[ITEM {idx}/{len(items)}] Downloading: {file_name}")
                await safe_edit(status_msg, f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}")

                # Download file
                response = session.get(file_url, stream=True, timeout=30)
                if response.status_code != 200:
                    logger.warning(f"[DOWNLOAD ERROR] HTTP {response.status_code}: {file_name}")
                    continue

                file_size = int(response.headers.get("content-length", 0))
                final_path = os.path.join(download_path, file_name)

                logger.info(f"[FILE SAVE] Saving to: {final_path} ({file_size} bytes)")
                downloaded = 0
                
                with open(final_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)

                        if file_size > 0:
                            percent = int((downloaded / file_size) * 100)
                            text = (
                                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:25]}\n"
                                f"Progress: {percent}%"
                            )
                            if text != last_status:
                                await safe_edit(status_msg, text)
                                last_status = text

                logger.info(f"[DOWNLOAD OK] {file_name} - {downloaded} bytes downloaded")

                # Upload to Telegram
                await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}")

                with open(final_path, "rb") as f:
                    if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                        logger.info(f"[UPLOAD VIDEO] {file_name}")
                        await client.send_video(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                        )
                    elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        logger.info(f"[UPLOAD PHOTO] {file_name}")
                        await client.send_photo(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                        )
                    else:
                        logger.info(f"[UPLOAD DOCUMENT] {file_name}")
                        await client.send_document(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                        )

                # Clean up file
                try:
                    os.remove(final_path)
                    logger.info(f"[CLEANUP] Removed temp file: {final_path}")
                except Exception as e:
                    logger.warning(f"[CLEANUP] Failed to remove {final_path}: {e}")

            except Exception as e:
                logger.exception(f"[ITEM ERROR] Error processing item {idx}: {e}")
                await message.reply_text(f"⚠️ Error processing item {idx}: {str(e)[:100]}")
                continue

        await safe_edit(status_msg, f"✅ Done! {album_name}")
        logger.info(f"[SUCCESS] Completed: {album_name}")

    except Exception as e:
        logger.exception(f"[FATAL ERROR] {e}")
        if status_msg:
            await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")
        else:
            await message.reply_text(f"❌ Error: {str(e)[:100]}")


@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Handle /start command"""
    logger.info(f"[COMMAND] /start")
    await message.reply_text(
        "🤖 **Bunkr Downloader Bot**\n\n"
        "Send Bunkr or Cyberdrop links.\n"
        "The bot will download & upload automatically.\n\n"
        "Supported domains:\n"
        "• bunkr.sk, bunkr.cr, bunkr.ru, bunkr.su\n"
        "• bunkrrr.org\n"
        "• cyberdrop.me"
    )


@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    """Handle /help command"""
    logger.info(f"[COMMAND] /help")
    await message.reply_text(
        "📖 **How to use:**\n\n"
        "1. Send any Bunkr or Cyberdrop link\n"
        "2. The bot will automatically:\n"
        "   • Detect the link\n"
        "   • Download all files\n"
        "   • Upload to Telegram\n\n"
        "Examples:\n"
        "• bunkr.sk/album-id\n"
        "• cyberdrop.me/a/album-id"
    )


@app.on_message(filters.text & ~filters.bot & ~filters.command(["start", "help"]))
async def handle_message(client: Client, message: Message):
    """
    MAIN MESSAGE HANDLER - Process all text messages looking for URLs
    This is the ONLY text message handler to avoid conflicts
    """
    try:
        msg_text = message.text if message.text else ""
        
        logger.info(f"[MESSAGE] Received from chat {message.chat.id}")
        logger.info(f"[MESSAGE] Content: {msg_text[:100]}")
        
        if not msg_text or len(msg_text.strip()) == 0:
            logger.debug("[MESSAGE] Empty message, ignoring")
            return
        
        # Extract URLs from message
        urls = extract_urls(msg_text)
        
        if not urls or len(urls) == 0:
            logger.info("[MESSAGE] No URLs detected in message")
            await message.reply_text("❌ No Bunkr or Cyberdrop links detected. Please send a valid link.")
            return

        logger.info(f"[MESSAGE] Found {len(urls)} URL(s) to process")
        
        # Create session once for all URLs
        session = create_session()
        logger.info("[MESSAGE] Session created")
        
        # Process each URL
        for idx, url in enumerate(urls, 1):
            logger.info(f"[URL {idx}/{len(urls)}] Validating: {url}")
            
            if is_valid_bunkr_url(url):
                logger.info(f"[URL {idx}/{len(urls)}] ✓ Valid, starting download")
                try:
                    await download_and_send_file(client, message, url, session)
                except Exception as item_error:
                    logger.error(f"[URL {idx}] Error: {item_error}")
                    await message.reply_text(f"❌ Error processing URL {idx}: {str(item_error)[:100]}")
            else:
                logger.warning(f"[URL {idx}/{len(urls)}] ✗ Invalid domain: {url}")
                await message.reply_text(f"❌ Invalid URL: {url}\nSupported: bunkr.sk, cyberdrop.me, etc.")
                
    except Exception as e:
        logger.exception(f"[HANDLER ERROR] {e}")
        try:
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
        except:
            logger.error("[HANDLER] Failed to send error message")


def start_bot():
    """Start the bot with proper error handling"""
    logger.info("=" * 70)
    logger.info("BUNKR DOWNLOADER BOT STARTING")
    logger.info("=" * 70)
    logger.info(f"API_ID: {API_ID}")
    logger.info(f"API_HASH: {'✓ SET' if API_HASH else '✗ NOT SET'}")
    logger.info(f"BOT_TOKEN: {'✓ SET' if BOT_TOKEN else '✗ NOT SET'}")
    logger.info(f"Downloads Dir: {DOWNLOADS_DIR}")
    logger.info("=" * 70)
    
    try:
        logger.info("[BOT] Connecting to Telegram...")
        with app:
            logger.info("[BOT] ✓ Connected to Telegram!")
            logger.info("[BOT] Bot is running and listening for messages...")
            app.run()
            
    except KeyboardInterrupt:
        logger.info("[BOT] Shutdown requested")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"[BOT] Critical error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    start_bot()
