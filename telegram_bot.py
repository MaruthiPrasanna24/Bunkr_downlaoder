import os
import re
import time
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified
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
from bs4 import BeautifulSoup
from urllib.parse import urljoin

try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False

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
    logger.info(f"Extracted URLs: {matches}")
    return matches

def is_valid_bunkr_url(url):
    return bool(re.match(r'https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)', url))

async def safe_edit(msg: Message, text: str):
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"edit_text failed: {e}")

def human_bytes(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}" if unit != 'B' else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"

async def download_progress(current: int, total: int, status_msg: Message, file_name: str, idx: int, total_items: int, last_update: list, start_time: float):
    current_time = time.time()
    if current_time - last_update[0] < 4:
        return
    last_update[0] = current_time

    if total <= 0:
        return

    percent = int(current * 100 / total)
    elapsed = current_time - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    bar = '█' * (percent // 5) + '░' * (20 - (percent // 5))

    text = (
        f"⬇️ Downloading [{idx}/{total_items}]: {file_name[:28]}\n"
        f"[{bar}] {percent}%\n"
        f"{human_bytes(current)} / {human_bytes(total)}\n"
        f"Speed: {human_bytes(int(speed))}/s   ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )
    await safe_edit(status_msg, text)

async def upload_progress(current: int, total: int, status_msg: Message, file_name: str, idx: int, total_items: int, last_update: list, start_time: float):
    current_time = time.time()
    if current_time - last_update[0] < 4:
        return
    last_update[0] = current_time

    if total <= 0:
        return

    percent = int(current * 100 / total)
    elapsed = current_time - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    bar = '█' * (percent // 5) + '░' * (20 - (percent // 5))

    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:28]}\n"
        f"[{bar}] {percent}%\n"
        f"{human_bytes(current)} / {human_bytes(total)}\n"
        f"Speed: {human_bytes(int(speed))}/s   ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )
    await safe_edit(status_msg, text)

async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"Processing: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:60]}...")

        # Normalize domain
        url = url.replace("bunkr.pk", "bunkr.su").replace("bunkr.is", "bunkr.su")
        if not url.startswith("https://"):
            url = "https://" + url.lstrip("/")

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code} – cannot access page")
            return

        soup = BeautifulSoup(r.content, 'html.parser')

        is_direct_file_page = (
            soup.find('span', {'class': 'ic-videos'}) is not None or
            soup.find('div', {'class': 'lightgallery'}) is not None or
            "media" in r.url.lower()
        )

        items = []
        album_name = "file"

        h1 = soup.find('h1', class_=['text-[20px]', 'truncate'])
        if h1:
            album_name = h1.get_text(strip=True) or "file"

        if is_direct_file_page:
            item = get_real_download_url(session, url, is_direct=True, name=album_name)
            if item:
                items.append(item)
        else:
            for theItem in soup.find_all('div', class_='theItem'):
                a_tag = theItem.find('a', class_='after:absolute')
                if not a_tag:
                    continue
                view_url = urljoin(url, a_tag['href'])
                name_tag = theItem.find("p")
                name = name_tag.get_text(strip=True) if name_tag else "file"
                direct_item = get_real_download_url(session, view_url, is_direct=True, name=name)
                if direct_item:
                    items.append(direct_item)

        if not items:
            await safe_edit(status_msg, "❌ No downloadable files found")
            return

        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📦 Found {len(items)} item(s). Starting download...")

        for idx, item in enumerate(items, 1):
            if isinstance(item, dict):
                file_url = item.get("url")
                file_name = item.get("name", album_name)
            else:
                file_url = item
                file_name = album_name

            await safe_edit(status_msg, f"⬇️ [{idx}/{len(items)}] {file_name[:35]} ...")

            resp = session.get(file_url, stream=True, timeout=40)
            if resp.status_code != 200:
                await safe_edit(status_msg, f"❌ HTTP {resp.status_code} on file {idx}")
                continue

            file_size = int(resp.headers.get("Content-Length", 0))
            final_path = os.path.join(download_path, file_name)

            downloaded = 0
            start_time = time.time()
            last_update = [start_time]

            with open(final_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    await download_progress(
                        downloaded, file_size, status_msg,
                        file_name, idx, len(items), last_update, start_time
                    )

            # Thumbnail for videos
            thumb_path = None
            if MOVIEPY_AVAILABLE and file_name.lower().endswith(('.mp4', '.mkv', '.mov', '.webm', '.avi')):
                try:
                    thumb_path = os.path.join(download_path, f"{os.path.splitext(file_name)[0]}_thumb.jpg")
                    clip = VideoFileClip(final_path)
                    clip.save_frame(thumb_path, t="00:00:01.5")
                    clip.close()
                except Exception as e:
                    logger.warning(f"Thumbnail failed for {file_name}: {e}")
                    thumb_path = None

            await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]: {file_name[:35]}")

            upload_start = time.time()
            last_upload_update = [upload_start]

            with open(final_path, "rb") as f:
                if file_name.lower().endswith(('.mp4', '.mkv', '.mov', '.webm', '.avi')):
                    await client.send_video(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        thumb=thumb_path,
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items), last_upload_update, upload_start)
                    )
                elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    await client.send_photo(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items), last_upload_update, upload_start)
                    )
                else:
                    await client.send_document(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items), last_upload_update, upload_start)
                    )

            try:
                os.remove(final_path)
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except:
                pass

        await safe_edit(status_msg, f"✅ Finished — {album_name} ({len(items)} file(s))")

    except Exception as e:
        logger.exception(e)
        await safe_edit(status_msg, f"❌ Error: {str(e)[:120]}")

# ────────────────────────────────────────────────
#  FIXED: list your actual commands here
# ────────────────────────────────────────────────
@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return

    session = create_session()

    retry = Retry(
        total=6,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    for url in urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply("🤖 **Bunkr / Cyberdrop Downloader**\n\nJust send album or file link(s).")

@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    await message.reply("Send any bunkr.su / bunkrrr.org / cyberdrop.me link.\nBot downloads → uploads with progress.")

def main():
    logger.info("Bot starting...")
    app.run()

if __name__ == "__main__":
    main()
