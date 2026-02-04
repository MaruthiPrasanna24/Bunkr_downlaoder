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
import time
from datetime import timedelta
import subprocess
from pathlib import Path

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


def format_bytes(bytes_val):
    """Convert bytes to human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} TB"


def get_progress_bar(percent, width=20):
    """Create a text-based progress bar"""
    filled = int(width * percent / 100)
    bar = '█' * filled + '░' * (width - filled)
    return f"[{bar}] {percent}%"


def calculate_eta(downloaded, file_size, elapsed_time):
    """Calculate estimated time remaining"""
    if elapsed_time == 0 or downloaded == 0:
        return "calculating..."
    speed = downloaded / elapsed_time
    remaining = (file_size - downloaded) / speed if speed > 0 else 0
    return str(timedelta(seconds=int(remaining)))


def get_video_thumbnail(video_path, output_path):
    """Generate thumbnail for video using ffmpeg"""
    try:
        cmd = [
            'ffmpeg',
            '-i', video_path,
            '-ss', '00:00:01',
            '-vframes', '1',
            '-vf', 'scale=320:240',
            '-y',
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return output_path if os.path.exists(output_path) else None
    except Exception as e:
        logger.warning(f"[v0] Thumbnail generation failed: {e}")
        return None


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


# =======================
# ✅ ENHANCED: UPLOAD PROGRESS WITH SPEED & ETA
# =======================
class UploadTracker:
    def __init__(self):
        self.start_time = time.time()
        self.last_update = time.time()

upload_tracker = UploadTracker()

async def upload_progress(current, total, status_msg, file_name, idx, total_items):
    if total == 0:
        return
    percent = int(current * 100 / total)
    elapsed = time.time() - upload_tracker.start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = calculate_eta(current, total, elapsed)
    
    file_size_str = format_bytes(total)
    current_str = format_bytes(current)
    speed_str = f"{format_bytes(speed)}/s" if speed > 0 else "calculating..."
    
    progress_bar = get_progress_bar(percent)
    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:20]}\n"
        f"{progress_bar}\n"
        f"Size: {current_str}/{file_size_str} | Speed: {speed_str}\n"
        f"ETA: {eta}"
    )
    await safe_edit(status_msg, text)


async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""

        is_bunkr = "bunkr" in url or "bunkrrr" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")

        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"  # Updated to a common domain

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code}")
            return

        from bs4 import BeautifulSoup
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

            await safe_edit(
                status_msg,
                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            response = session.get(file_url, stream=True, timeout=30)
            if response.status_code != 200:
                continue

            file_size = int(response.headers.get("content-length", 0))
            final_path = os.path.join(download_path, file_name)

            downloaded = 0
            start_time = time.time()
            with open(final_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    if file_size > 0:
                        percent = int((downloaded / file_size) * 100)
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        eta = calculate_eta(downloaded, file_size, elapsed)
                        
                        file_size_str = format_bytes(file_size)
                        downloaded_str = format_bytes(downloaded)
                        speed_str = f"{format_bytes(speed)}/s" if speed > 0 else "calculating..."
                        
                        progress_bar = get_progress_bar(percent)
                        text = (
                            f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:20]}\n"
                            f"{progress_bar}\n"
                            f"Size: {downloaded_str}/{file_size_str} | Speed: {speed_str}\n"
                            f"ETA: {eta}"
                        )
                        if text != last_status:
                            await safe_edit(status_msg, text)
                            last_status = text

            await safe_edit(
                status_msg,
                f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            upload_tracker.start_time = time.time()
            with open(final_path, "rb") as f:
                if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                    # Generate thumbnail for video
                    thumb_path = None
                    try:
                        thumb_path = final_path.replace(os.path.splitext(final_path)[1], '_thumb.jpg')
                        thumb = get_video_thumbnail(final_path, thumb_path)
                    except:
                        thumb = None
                    
                    await client.send_video(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        thumb=thumb,
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items))
                    )
                    
                    # Cleanup thumbnail
                    if thumb_path and os.path.exists(thumb_path):
                        try:
                            os.remove(thumb_path)
                        except:
                            pass
                elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                    await client.send_photo(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items))
                    )
                else:
                    await client.send_document(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items))
                    )

            os.remove(final_path)

        await safe_edit(status_msg, f"✅ Done! {album_name}")

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Error: {str(e)[:100]}")


@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return

    session = create_session()
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
