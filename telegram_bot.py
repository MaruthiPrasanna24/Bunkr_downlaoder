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
# More flexible URL pattern that captures various formats
URL_PATTERNS = [
    r'https?://bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)(?:/[^\s]*)?',
    r'https?://bunkrrr\.org(?:/[^\s]*)?',
    r'https?://cyberdrop\.me(?:/[^\s]*)?',
    r'bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)(?:/[^\s]*)?',  # without https
    r'(?:bunkrrr\.org)(?:/[^\s]*)?',
    r'(?:cyberdrop\.me)(?:/[^\s]*)?'
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
                    # Ensure URL has protocol
                    url = match if match.startswith('http') else f'https://{match}'
                    urls.append(url)
                    seen.add(match)
                    logger.debug(f"[URL EXTRACTION] Found: {url}")
        except Exception as e:
            logger.warning(f"[URL EXTRACTION] Pattern error: {e}")
    
    logger.info(f"[URL EXTRACTION] Total URLs found: {len(urls)}")
    return urls


def is_valid_bunkr_url(url):
    """Validate if URL is from supported domains"""
    if not url:
        return False
    
    url_lower = url.lower()
    
    is_valid = any([
        'bunkr.sk' in url_lower,
        'bunkr.cr' in url_lower,
        'bunkr.ru' in url_lower,
        'bunkr.su' in url_lower,
        'bunkr.pk' in url_lower,
        'bunkr.is' in url_lower,
        'bunkr.si' in url_lower,
        'bunkr.ph' in url_lower,
        'bunkr.ps' in url_lower,
        'bunkr.ci' in url_lower,
        'bunkr.ax' in url_lower,
        'bunkr.fi' in url_lower,
        'bunkr.ac' in url_lower,
        'bunkr.black' in url_lower,
        'bunkr.la' in url_lower,
        'bunkrrr.org' in url_lower,
        'cyberdrop.me' in url_lower,
    ])
    
    if is_valid:
        logger.info(f"[URL VALIDATION] ✓ Valid: {url}")
    else:
        logger.warning(f"[URL VALIDATION] ✗ Invalid: {url}")
    
    return is_valid


async def safe_edit(msg, text):
    """Safely edit Telegram message without crashing"""
    try:
        if msg and msg.text != text:
            await msg.edit_text(text)
            logger.debug(f"[MESSAGE EDIT] Updated message: {text[:50]}...")
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[MESSAGE EDIT] Failed to edit message: {e}")


async def upload_progress(current, total, status_msg, file_name, idx, total_items):
    """Handle upload progress updates"""
    if total == 0:
        return
    percent = int(current * 100 / total)
    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}\n"
        f"Progress: {percent}%"
    )
    await safe_edit(status_msg, text)


async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    """Download files from URL and send to Telegram"""
    status_msg = None
    try:
        logger.info(f"[DOWNLOAD START] Processing URL: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        logger.info("[DOWNLOAD START] Status message sent successfully")
        last_status = ""

        is_bunkr = "bunkr" in url or "bunkrrr" in url
        logger.info(f"[URL TYPE] Bunkr: {is_bunkr}")

        # Fix URL format
        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"

        # Fetch page
        logger.info(f"[HTTP REQUEST] Fetching: {url}")
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            error_msg = f"❌ HTTP {r.status_code} - Failed to fetch page"
            logger.error(f"[HTTP ERROR] {error_msg}")
            await safe_edit(status_msg, error_msg)
            return

        # Parse HTML
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, 'html.parser')

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

        if not items:
            error_msg = "❌ No downloadable items found"
            logger.warning(f"[PARSING] {error_msg}")
            await safe_edit(status_msg, error_msg)
            return

        logger.info(f"[DOWNLOAD PREP] Found {len(items)} items to download")
        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📥 Found {len(items)} items. Starting...")

        # Download and upload each item
        for idx, item in enumerate(items, 1):
            try:
                if isinstance(item, dict):
                    file_url = item.get("url")
                    file_name = item.get("name", album_name)
                else:
                    file_url = item
                    file_name = album_name

                logger.info(f"[DOWNLOAD ITEM {idx}/{len(items)}] {file_name}")
                await safe_edit(status_msg, f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}")

                # Download file
                response = session.get(file_url, stream=True, timeout=30)
                if response.status_code != 200:
                    logger.warning(f"[DOWNLOAD ERROR] HTTP {response.status_code} for {file_name}")
                    continue

                file_size = int(response.headers.get("content-length", 0))
                final_path = os.path.join(download_path, file_name)

                logger.info(f"[FILE SAVE] Saving to: {final_path}")
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

                logger.info(f"[DOWNLOAD COMPLETE] {file_name} - {downloaded} bytes")

                # Upload to Telegram
                await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}")

                with open(final_path, "rb") as f:
                    if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                        logger.info(f"[UPLOAD VIDEO] {file_name}")
                        await client.send_video(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items))
                        )
                    elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        logger.info(f"[UPLOAD PHOTO] {file_name}")
                        await client.send_photo(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items))
                        )
                    else:
                        logger.info(f"[UPLOAD DOCUMENT] {file_name}")
                        await client.send_document(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items))
                        )

                # Clean up file from /tmp
                try:
                    os.remove(final_path)
                    logger.info(f"[CLEANUP] Removed {final_path}")
                except Exception as e:
                    logger.warning(f"[CLEANUP] Failed to remove {final_path}: {e}")

            except Exception as e:
                logger.exception(f"[ITEM ERROR] Error processing item {idx}: {e}")
                await message.reply_text(f"⚠️ Error processing item {idx}: {str(e)[:100]}")
                continue

        await safe_edit(status_msg, f"✅ Done! {album_name}")
        logger.info(f"[DOWNLOAD SUCCESS] Completed processing {album_name}")

    except Exception as e:
        logger.exception(f"[FATAL ERROR] {e}")
        await message.reply_text(f"❌ Error: {str(e)[:100]}")


@app.on_message(filters.text & ~filters.bot)
async def handle_message(client: Client, message: Message):
    """Handle incoming text messages"""
    try:
        # Get message text safely
        msg_text = message.text if message.text else ""
        
        logger.info(f"[MESSAGE RECEIVED] Chat ID: {message.chat.id}, Type: {message.chat.type}")
        logger.info(f"[MESSAGE CONTENT] {msg_text[:150] if msg_text else 'No text'}")
        
        if not msg_text or len(msg_text) == 0:
            logger.debug("[NO TEXT] Message has no text content")
            return
        
        urls = extract_urls(msg_text)
        logger.info(f"[URL DETECTION] Extracted {len(urls)} URLs from message")
        
        if not urls or len(urls) == 0:
            logger.debug("[NO URLS] No URLs found in message")
            return

        logger.info(f"[PROCESSING] Starting download for {len(urls)} URLs")
        session = create_session()
        
        for idx, url in enumerate(urls, 1):
            logger.info(f"[URL CHECK {idx}/{len(urls)}] Checking: {url}")
            if is_valid_bunkr_url(url):
                logger.info(f"[VALID URL {idx}/{len(urls)}] Starting download: {url}")
                try:
                    await download_and_send_file(client, message, url, session)
                except Exception as item_error:
                    logger.error(f"[ITEM ERROR] Failed to process URL {idx}: {item_error}")
                    await message.reply_text(f"❌ Error processing URL: {str(item_error)[:100]}")
            else:
                logger.warning(f"[INVALID URL {idx}] Domain not supported: {url}")
                
    except Exception as e:
        logger.exception(f"[MESSAGE HANDLER ERROR] {e}")
        try:
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
        except:
            pass


@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Handle /start command"""
    logger.info(f"[COMMAND] /start from {message.from_user.id if message.from_user else 'Unknown'}")
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
    logger.info(f"[COMMAND] /help from {message.from_user.id if message.from_user else 'Unknown'}")
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


@app.on_message()
async def debug_all_messages(client: Client, message: Message):
    """Debug handler - logs ALL messages"""
    try:
        if message.text:
            logger.debug(f"[DEBUG ALL] Message: {message.text[:100]}")
    except:
        pass
    # IMPORTANT: Don't return, let other handlers process


@app.on_message(filters.text & ~filters.bot & ~filters.command(["start", "help"]))
async def handle_any_message(client: Client, message: Message):
    """Catch-all handler for text messages that might contain URLs"""
    try:
        msg_text = message.text if message.text else ""
        logger.info(f"[CATCH-ALL] Received message from chat {message.chat.id}: {msg_text[:100]}")
        
        # Check if it looks like a URL
        if "://" in msg_text or "bunkr" in msg_text.lower() or "cyberdrop" in msg_text.lower():
            logger.info("[CATCH-ALL] Message looks like it contains a link, processing...")
            urls = extract_urls(msg_text)
            
            if urls:
                logger.info(f"[CATCH-ALL] Found {len(urls)} URLs: {urls}")
                session = create_session()
                
                for idx, url in enumerate(urls, 1):
                    if is_valid_bunkr_url(url):
                        logger.info(f"[CATCH-ALL] Processing URL: {url}")
                        await download_and_send_file(client, message, url, session)
                    else:
                        logger.warning(f"[CATCH-ALL] Invalid domain: {url}")
            else:
                logger.warning("[CATCH-ALL] No URLs extracted from message")
        else:
            logger.debug(f"[CATCH-ALL] Message does not contain URLs")
            
    except Exception as e:
        logger.exception(f"[CATCH-ALL ERROR] {e}")
        try:
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
        except:
            logger.error("[CATCH-ALL] Failed to send error message")


def start_bot():
    """Start the bot with proper error handling"""
    logger.info("=" * 70)
    logger.info("BUNKR DOWNLOADER BOT STARTING")
    logger.info("=" * 70)
    logger.info(f"API_ID: {API_ID}")
    logger.info(f"API_HASH: {'SET' if API_HASH else 'NOT SET'}")
    logger.info(f"BOT_TOKEN: {'SET' if BOT_TOKEN else 'NOT SET'}")
    logger.info(f"Downloads Directory: {DOWNLOADS_DIR}")
    logger.info(f"Python Version: {sys.version}")
    logger.info("=" * 70)
    
    try:
        logger.info("[BOT] Connecting to Telegram...")
        with app:
            logger.info("[BOT] ✓ Connected successfully!")
            logger.info("[BOT] Starting to listen for messages...")
            app.run()
            
    except KeyboardInterrupt:
        logger.info("[BOT] Shutdown requested by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"[BOT ERROR] Bot crashed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    start_bot()
