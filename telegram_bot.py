import os
import re
import asyncio
import time
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
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from pyrogram.errors import MessageNotModified

try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

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

URL_PATTERN = r'(https?://(?:bunkr(?:r)?\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la|media|red|site|ws|org|cat|cc|com|net|to)|bunkrrr\.org|cyberdrop\.(?:me|cr|to|cc|nl))[^\s]+)'

def extract_urls(text):
    matches = re.findall(URL_PATTERN, text)
    logger.info(f"[v0] URL_PATTERN matches: {matches}")
    return matches

def is_valid_bunkr_url(url):
    is_valid = bool(
        re.match(r'https?://(?:bunkr(?:r)?\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la|media|red|site|ws|org|cat|cc|com|net|to)|bunkrrr\.org|cyberdrop\.(?:me|cr|to|cc|nl))', url)
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
    current_time = time.time()
    if current_time - last_update_time[0] < 5:
        return
    last_update_time[0] = current_time
    percent = int(current * 100 / total)
    elapsed = current_time - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    bar = '█' * int(percent / 5) + '░' * (20 - int(percent / 5))
    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}...\n"
        f"[{bar}] {percent}%\n"
        f"{human_bytes(current)} / {human_bytes(total)}\n"
        f"ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )
    await safe_edit(status_msg, text)

def fix_bunkr_url(url: str) -> str:
    """Fix unstable Bunkr CDN domains"""
    url = url.replace("c.bunkr-cache.se", "c.bunkr.su")
    url = url.replace("bunkr-cache.se", "bunkr.su")
    url = url.replace("c.bunkr.is", "c.bunkr.su")
    url = url.replace("bunkr.media", "bunkr.su")
    url = url.replace("bunkr.red", "bunkr.su")
    return url

async def generate_video_thumbnail_and_duration(video_path: str, output_path: str) -> tuple[bool, int]:
    """
    Try to generate thumbnail and get duration using moviepy or opencv
    Returns (success, duration)
    """
    success = False
    duration = 0

    if MOVIEPY_AVAILABLE:
        try:
            clip = VideoFileClip(video_path)
            duration = int(clip.duration)
            clip.save_frame(output_path, t=1)
            clip.close()
            success = True
        except Exception as e:
            logger.warning(f"MoviePy failed: {e}")

    if not success and OPENCV_AVAILABLE:
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                logger.warning("Cannot open video with OpenCV")
                return False, 0

            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if fps > 0:
                duration = int(frame_count / fps)

            # Try to get frame at ~1 second
            frame_pos = int(fps * 1) if fps > 0 else 30
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(output_path, frame)
                success = True
            else:
                # Fallback to first frame
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()
                if ret:
                    cv2.imwrite(output_path, frame)
                    success = True
            cap.release()
        except Exception as e:
            logger.warning(f"OpenCV failed: {e}")

    if not success:
        logger.warning("No thumbnail/duration extracted")

    return success, duration

async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""
        is_bunkr = "bunkr" in url or "bunkrrr" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")

        # Enhanced domain fallback for album URL
        url = fix_bunkr_url(url)
        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"

        # Try fetching album with fallback domains if 404
        domains_to_try = ['bunkr.su', 'bunkr.red', 'bunkr.is']
        r = None
        for domain in domains_to_try:
            try_url = url.replace('bunkr.media', domain)  # Example replacement
            r = session.get(try_url, timeout=30)
            if r.status_code == 200:
                url = try_url  # Update to working URL
                break
            logger.warning(f"Failed domain {domain} with {r.status_code}")

        if not r or r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code if r else 'Failed all domains'} on album page")
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

        skipped_files = []

        for idx, item in enumerate(items, 1):
            if isinstance(item, dict):
                file_url = item.get("url")
                file_name = item.get("name", album_name)
            else:
                file_url = item
                file_name = album_name

            file_url = fix_bunkr_url(file_url)

            await safe_edit(
                status_msg,
                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            success = False
            max_retries = 4

            for attempt in range(max_retries):
                try:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://bunkr.su/"
                    }
                    response = session.get(file_url, stream=True, timeout=60, headers=headers)
                    if response.status_code == 200:
                        success = True
                        break
                    else:
                        logger.warning(f"HTTP {response.status_code} on attempt {attempt+1}")
                except Exception as e:
                    logger.warning(f"Attempt {attempt+1} failed: {str(e)}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2 ** attempt)

            if not success:
                skipped_files.append(file_name)
                await safe_edit(
                    status_msg,
                    f"⚠️ Skipped [{idx}/{len(items)}]: {file_name[:30]} (failed after retries)"
                )
                logger.error(f"Skipped file: {file_name} - could not download from {file_url}")
                continue

            file_size = int(response.headers.get("content-length", 0))
            final_path = os.path.join(download_path, file_name)
            downloaded = 0
            start_time = time.time()
            last_update = start_time

            try:
                with open(final_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        current_time = time.time()
                        if current_time - last_update >= 5 and file_size > 0:
                            percent = int((downloaded / file_size) * 100)
                            elapsed = current_time - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            eta = (file_size - downloaded) / speed if speed > 0 else 0
                            bar = '█' * int(percent / 5) + '░' * (20 - int(percent / 5))
                            text = (
                                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:25]}\n"
                                f"[{bar}] {percent}%\n"
                                f"{human_bytes(downloaded)} / {human_bytes(file_size)}\n"
                                f"ETA: {int(eta // 60)}m {int(eta % 60)}s"
                            )
                            if text != last_status:
                                await safe_edit(status_msg, text)
                                last_status = text
                            last_update = current_time
            except Exception as download_err:
                skipped_files.append(file_name)
                await safe_edit(
                    status_msg,
                    f"⚠️ Skipped [{idx}/{len(items)}]: {file_name[:30]} (download error)"
                )
                logger.exception(f"Download failed for {file_name}: {download_err}")
                if os.path.exists(final_path):
                    os.remove(final_path)
                continue

            # Thumbnail and duration extraction
            thumb_path = None
            video_duration = 0
            if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                thumb_filename = f"{file_name}_thumb.jpg"
                thumb_path = os.path.join(download_path, thumb_filename)

                success, video_duration = await generate_video_thumbnail_and_duration(final_path, thumb_path)
                if not success or not os.path.exists(thumb_path):
                    logger.warning(f"Thumbnail not created for {file_name}")
                    thumb_path = None

            await safe_edit(
                status_msg,
                f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            upload_start_time = time.time()
            last_update_time = [upload_start_time]

            try:
                with open(final_path, "rb") as f:
                    if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                        await client.send_video(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                            duration=video_duration,
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items), last_update_time, upload_start_time)
                        )
                    elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        await client.send_photo(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items), last_update_time, upload_start_time)
                        )
                    else:
                        await client.send_document(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items), last_update_time, upload_start_time)
                        )
            except Exception as upload_err:
                logger.exception(f"Upload failed for {file_name}: {upload_err}")
                await safe_edit(status_msg, f"⚠️ Upload failed for {file_name[:30]} (but file was downloaded)")

            # Cleanup
            if os.path.exists(final_path):
                os.remove(final_path)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

        # Final summary
        summary = f"✅ Done! {album_name}\n"
        if skipped_files:
            summary += f"⚠️ Skipped {len(skipped_files)} file(s): {', '.join(skipped_files[:3])}"
            if len(skipped_files) > 3:
                summary += f" + {len(skipped_files)-3} more"
        await safe_edit(status_msg, summary)

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Critical error (album aborted): {str(e)[:100]}")

@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    # Anti-flood: ignore messages sent by the bot itself to prevent loops
    bot = await client.get_me()
    if message.from_user and message.from_user.id == bot.id:
        return

    urls = extract_urls(message.text)
    if not urls:
        return
    session = create_session()
    retry_strategy = Retry(
        total=7,
        backoff_factor=1.5,
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
