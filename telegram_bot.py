import os
import re
import asyncio
import time
import socket
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm
from pyrogram.errors import MessageNotModified
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from requests.exceptions import ConnectionError
from urllib3.exceptions import NameResolutionError

try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False

# =========================
# 🔥 ADDED: FORCE IPV4 (DNS FIX)
# =========================
orig_getaddrinfo = socket.getaddrinfo
def force_ipv4(*args, **kwargs):
    return orig_getaddrinfo(*args, **kwargs, family=socket.AF_INET)
socket.getaddrinfo = force_ipv4

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

URL_PATTERN = r'(https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)[^\s]+)'

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
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[v0] edit_text failed: {e}")

def human_bytes(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024**2:
        return f"{size / 1024:.2f} KB"
    elif size < 1024**3:
        return f"{size / 1024**2:.2f} MB"
    else:
        return f"{size / 1024**3:.2f} GB"

async def upload_progress(current, total, status_msg, file_name, idx, total_items, last_update_time, start_time):
    if total == 0:
        return
    now = time.time()
    if now - last_update_time[0] < 5:
        return
    last_update_time[0] = now

    percent = int(current * 100 / total)
    speed = current / (now - start_time) if now > start_time else 0
    eta = (total - current) / speed if speed else 0
    bar = '█' * int(percent / 5) + '░' * (20 - int(percent / 5))

    await safe_edit(
        status_msg,
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}\n"
        f"[{bar}] {percent}%\n"
        f"{human_bytes(current)} / {human_bytes(total)}\n"
        f"ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )

# =========================
# 🔥 ADDED: SAFE FILE DOWNLOAD
# =========================
def fix_bunkr_cdn(url):
    parsed = urlparse(url)
    if "bunkr-cache" in parsed.netloc or "b-cache" in parsed.netloc:
        return url.replace(parsed.netloc, "media.bunkr.su")
    return url

async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code}")
            return

        soup = BeautifulSoup(r.content, 'html.parser')
        items = []

        for theItem in soup.find_all('div', {'class': 'theItem'}):
            box = theItem.find('a', {'class': 'after:absolute'})
            if box:
                view_url = urljoin(url, box["href"])
                name = theItem.find("p").text if theItem.find("p") else "file"
                item = get_real_download_url(session, view_url, True, name)
                if item:
                    items.append(item)

        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, "album")
        await safe_edit(status_msg, f"📥 Found {len(items)} items")

        for idx, item in enumerate(items, 1):
            try:
                file_url = fix_bunkr_cdn(item["url"])
                file_name = item["name"]

                response = session.get(file_url, stream=True, timeout=30)
                if response.status_code != 200:
                    raise ConnectionError("Bad status")

                final_path = os.path.join(download_path, file_name)
                with open(final_path, "wb") as f:
                    for chunk in response.iter_content(8192):
                        if chunk:
                            f.write(chunk)

                await client.send_document(message.chat.id, final_path, caption=f"✅ {file_name}")
                os.remove(final_path)

            except (ConnectionError, NameResolutionError, socket.gaierror) as e:
                logger.error(f"SKIPPED FILE: {file_name} | {e}")
                await safe_edit(status_msg, f"⚠️ Skipped broken file: {file_name}")
                continue

        await safe_edit(status_msg, "✅ Album completed")

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Error: {str(e)[:100]}")

@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return

    session = create_session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    for url in urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 **Bunkr Downloader Bot**\n\n"
        "Send Bunkr or Cyberdrop links.\n"
        "Auto download + upload."
    )

def start_bot():
    logger.info("Bot starting...")
    app.run()

if __name__ == "__main__":
    start_bot()
