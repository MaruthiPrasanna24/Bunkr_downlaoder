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
URL_PATTERN = r'(https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)[^\s]+)'


def extract_urls(text):
    """Extract URLs from text"""
    if not text:
        return []
    matches = re.findall(URL_PATTERN, text)
    logger.info(f"[URL EXTRACTION] Found {len(matches)} URLs in message")
    for url in matches:
        logger.info(f"[URL EXTRACTION] URL: {url}")
    return matches


def is_valid_bunkr_url(url):
    """Validate if URL is from supported domains"""
    is_valid = bool(
        re.match(r'https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)', url)
    )
    logger.info(f"[URL VALIDATION] {url} -> Valid: {is_valid}")
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
    try:
        logger.info(f"[DOWNLOAD START] Processing URL: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
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


@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    """Handle incoming text messages"""
    try:
        logger.info(f"[MESSAGE RECEIVED] From: {message.from_user.id if message.from_user else 'Unknown'}")
        logger.info(f"[MESSAGE CONTENT] {message.text[:100] if message.text else 'No text'}")
        
        urls = extract_urls(message.text)
        
        if not urls:
            logger.info("[NO URLS] No URLs found in message")
            return

        logger.info(f"[PROCESSING] {len(urls)} URLs detected")
        session = create_session()
        
        for url in urls:
            if is_valid_bunkr_url(url):
                logger.info(f"[VALID URL] Starting download for: {url}")
                await download_and_send_file(client, message, url, session)
            else:
                logger.warning(f"[INVALID URL] Skipping: {url}")
                
    except Exception as e:
        logger.exception(f"[MESSAGE HANDLER ERROR] {e}")


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


def start_bot():
    """Start the bot"""
    logger.info("=" * 50)
    logger.info("BUNKR DOWNLOADER BOT STARTING")
    logger.info("=" * 50)
    logger.info(f"API_ID: {API_ID}")
    logger.info(f"Downloads Directory: {DOWNLOADS_DIR}")
    logger.info(f"Python Version: {sys.version}")
    logger.info("=" * 50)
    
    try:
        app.run()
    except Exception as e:
        logger.exception(f"[BOT ERROR] Bot crashed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    start_bot()
