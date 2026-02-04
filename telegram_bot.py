import os
import re
import time
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
    if matches:
        logger.info(f"Found URLs: {matches}")
    return matches

def is_valid_bunkr_url(url):
    pattern = r'https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)'
    return bool(re.match(pattern, url))

async def safe_edit(msg: Message, text: str):
    """Safely edit message, ignore MessageNotModified"""
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"safe_edit failed: {e}")

def human_bytes(size):
    """Convert bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.2f} {unit}" if unit != 'B' else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

# ────────────────────────────────────────────────
# Progress callback - DOWNLOAD
# ────────────────────────────────────────────────
async def download_progress(current, total, status_msg, file_name, idx, total_items, last_update, start_time):
    now = time.time()
    if now - last_update[0] < 5:
        return
    last_update[0] = now

    if total <= 0:
        return

    percent = int(current * 100 / total)
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    bar = '█' * (percent // 5) + '░' * (20 - (percent // 5))

    text = (
        f"⬇️ Downloading [{idx}/{total_items}]: {file_name[:30]}\n"
        f"[{bar}] {percent}%\n"
        f"Size: {human_bytes(current)} / {human_bytes(total)}\n"
        f"Speed: {human_bytes(int(speed))}/s   ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )
    await safe_edit(status_msg, text)

# ────────────────────────────────────────────────
# Progress callback - UPLOAD
# ────────────────────────────────────────────────
async def upload_progress(current, total, status_msg, file_name, idx, total_items, last_update, start_time):
    now = time.time()
    if now - last_update[0] < 5:
        return
    last_update[0] = now

    if total <= 0:
        return

    percent = int(current * 100 / total)
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0

    bar = '█' * (percent // 5) + '░' * (20 - (percent // 5))

    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:30]}\n"
        f"[{bar}] {percent}%\n"
        f"Size: {human_bytes(current)} / {human_bytes(total)}\n"
        f"Speed: {human_bytes(int(speed))}/s   ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )
    await safe_edit(status_msg, text)

# ────────────────────────────────────────────────
# Main download & upload logic
# ────────────────────────────────────────────────
async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    status_msg = None
    try:
        logger.info(f"Starting process: {url}")
        status_msg = await message.reply_text(f"🔄 Processing link: {url[:60]}{'...' if len(url) > 60 else ''}")

        # Normalize domain
        url = url.replace("bunkr.pk", "bunkr.su").replace("bunkr.is", "bunkr.su")
        if not url.startswith("https://"):
            url = "https://" + url.lstrip("/")

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ Failed to access page (HTTP {r.status_code})")
            return

        soup = BeautifulSoup(r.content, 'html.parser')

        # Detect if single file or album
        is_single_file = (
            soup.find('span', class_='ic-videos') is not None or
            soup.find('div', class_='lightgallery') is not None or
            "media" in r.url.lower()
        )

        items = []
        album_name = "file"

        h1 = soup.find('h1', class_=['text-[20px]', 'truncate'])
        if h1:
            album_name = h1.get_text(strip=True) or "file"

        if is_single_file:
            # Single file page
            item = get_real_download_url(session, url, name=album_name)
            if item:
                items.append(item)
        else:
            # Album
            for item_div in soup.find_all('div', class_='theItem'):
                link_tag = item_div.find('a', class_='after:absolute')
                if not link_tag:
                    continue

                view_url = urljoin(url, link_tag["href"])
                name_tag = item_div.find("p")
                name = name_tag.get_text(strip=True) if name_tag else "file"

                direct_link = get_real_download_url(session, view_url, name=name)
                if direct_link:
                    items.append(direct_link)

        if not items:
            await safe_edit(status_msg, "❌ No downloadable files found")
            return

        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📦 Found {len(items)} file(s). Starting download...")

        for idx, item in enumerate(items, 1):
            if isinstance(item, dict):
                file_url = item.get("url")
                file_name = item.get("name", album_name)
            else:
                file_url = item
                file_name = album_name

            # Download
            await safe_edit(status_msg, f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:35]} ...")

            resp = session.get(file_url, stream=True, timeout=45)
            if resp.status_code != 200:
                await safe_edit(status_msg, f"❌ Download failed (HTTP {resp.status_code}) - {file_name}")
                continue

            file_size = int(resp.headers.get("Content-Length", 0))
            final_path = os.path.join(download_path, file_name)

            downloaded = 0
            start_time = time.time()
            last_update_time = [start_time]

            with open(final_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=16384):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        await download_progress(
                            downloaded, file_size, status_msg,
                            file_name, idx, len(items),
                            last_update_time, start_time
                        )

            # Generate thumbnail for videos
            thumb_path = None
            if MOVIEPY_AVAILABLE and file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.mov', '.avi')):
                try:
                    thumb_path = os.path.join(download_path, f"thumb_{file_name}.jpg")
                    clip = VideoFileClip(final_path)
                    clip.save_frame(thumb_path, t="00:00:01.500")
                    clip.close()
                except Exception as e:
                    logger.warning(f"Could not generate thumbnail: {e}")
                    thumb_path = None

            # Upload
            await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]: {file_name[:35]}")

            upload_start = time.time()
            last_upload_time = [upload_start]

            with open(final_path, "rb") as f:
                if file_name.lower().endswith(('.mp4', '.mkv', '.webm', '.mov', '.avi')):
                    await client.send_video(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        thumb=thumb_path,
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items), last_upload_time, upload_start)
                    )
                elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    await client.send_photo(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items), last_upload_time, upload_start)
                    )
                else:
                    await client.send_document(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items), last_upload_time, upload_start)
                    )

            # Cleanup
            try:
                os.remove(final_path)
                if thumb_path and os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except:
                pass

        await safe_edit(status_msg, f"✅ **Completed**\n\nAlbum: {album_name}\nFiles: {len(items)}")

    except Exception as e:
        logger.exception("Error in download_and_send_file")
        msg = f"❌ Error occurred: {str(e)[:180]}"
        if status_msg:
            await safe_edit(status_msg, msg)
        else:
            await message.reply_text(msg)

# ────────────────────────────────────────────────
# Message Handler (non-command text)
# ────────────────────────────────────────────────
@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return

    session = create_session()

    # Add retry strategy
    retry_strategy = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    for url in urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)

# ────────────────────────────────────────────────
# Commands
# ────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def start_command(client, message):
    await message.reply_text(
        "👋 **Bunkr / Cyberdrop Downloader Bot**\n\n"
        "Just send me any bunkr or cyberdrop link (single file or album).\n"
        "I'll download and upload everything here with progress!"
    )

@app.on_message(filters.command("help"))
async def help_command(client, message):
    await message.reply_text(
        "📌 **How to use**\n\n"
        "• Send any bunkr.su / bunkrrr.org / cyberdrop.me link\n"
        "• Works with albums and single files\n"
        "• Shows download & upload progress\n"
        "• Generates thumbnails for videos (if possible)\n\n"
        "Enjoy! 🚀"
    )

def main():
    logger.info("Bot is starting...")
    app.run()

if __name__ == "__main__":
    main()
