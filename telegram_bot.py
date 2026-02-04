# telegram_bot.py (main file)
import os
import re
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
import logging
from utils import (  # Import from separate file
    create_session_with_retries,
    get_real_download_url,
    download_file,
    upload_file,
    get_and_prepare_download_path,
)
from bs4 import BeautifulSoup
from urllib.parse import urljoin

load_dotenv()
API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', 'downloads')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)
URL_PATTERN = r'(https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)[^\s]+)'  # Updated with more domains
def extract_urls(text):
    matches = re.findall(URL_PATTERN, text)
    logger.info(f"[v0] URL_PATTERN matches: {matches}")
    return matches
def is_valid_bunkr_url(url):
    is_valid = bool(
        re.match(r'https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)', url)
    )
    logger.info(f"[v0] is_valid_bunkr_url({url}) = {is_valid}")
    return is_valid
async def safe_edit(msg, text):
    """Safely edit Telegram message without crashing"""
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[v0] edit_text failed: {e}")

async def download_and_send_file(client: Client, message: Message, url: str, session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        is_bunkr = "bunkr" in url or "bunkrrr" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")
        if is_bunkr:
            url = url.replace(url.split('://')[1].split('/')[0], 'bunkr.ac')  # Force to working domain

        r = session.get(url, timeout=60)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code}")
            return
        soup = BeautifulSoup(r.content, 'html.parser')
        is_direct = (
            soup.find('span', {'class': 'ic-videos'}) is not None or
            soup.find('div', {'class': 'lightgallery'}) is not None
        )
        items = []
        if is_direct:
            h1 = soup.find('h1', {'class': 'text-[20px]'}) or soup.find('h1', {'class': 'truncate'})
            album_name = h1.text if h1 else "file"
            item = get_real_download_url(session, url, True, album_name)
            if item:
                items.append(item)
        else:
            h1 = soup.find('h1', {'class': 'truncate'})
            album_name = h1.text if h1 else "album"
            for theItem in soup.find_all('div', {'class': 'theItem'}):
                box = theItem.find('a', {'class': 'after:absolute'})
                if box:
                    view_url = urljoin(url, box["href"])
                    name = theItem.find("p").text if theItem.find("p") else "file"
                    direct_item = get_real_download_url(session, view_url, True, name)
                    if direct_item:
                        items.append(direct_item)
        if not items:
            await safe_edit(status_msg, "❌ No downloadable items found")
            return
        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📥 Found {len(items)} items. Starting...")
        for idx, item in enumerate(items, 1):
            if isinstance(item, dict):
                file_url = item.get("url")
                file_name = item.get("name", album_name)
            else:
                file_url = item
                file_name = album_name
            file_path = await download_file(session, file_url, file_name, download_path, status_msg, idx, len(items))
            if file_path:
                is_video = file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
                await upload_file(client, message.chat.id, file_path, file_name, status_msg, idx, len(items), is_video)
        await safe_edit(status_msg, f"✅ Done! {album_name}")
    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Error: {str(e)[:100]}")

@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return
    session = create_session_with_retries()
    for url in urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 **Bunkr Downloader Bot**\n\n"
        "Send Bunkr or Cyberdrop links.\n"
        "The bot will download & upload automatically."
    )

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    await message.reply_text(
        "Send any Bunkr / Cyberdrop link.\n"
        "Progress updates + auto upload supported."
    )

def start_bot():
    logger.info("Bot starting...")
    app.run()

if __name__ == "__main__":
    start_bot()
