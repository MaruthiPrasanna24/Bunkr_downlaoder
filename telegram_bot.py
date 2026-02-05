import os
import re
import asyncio
import time
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urljoin, urlparse, unquote
from bs4 import BeautifulSoup
from pyrogram.errors import MessageNotModified
import json
from math import floor
from base64 import b64decode

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
    return url

def get_video_duration(video_path: str) -> int:
    """
    Returns video duration in seconds (int) or 0 if failed
    """
    duration = 0
    if MOVIEPY_AVAILABLE:
        try:
            clip = VideoFileClip(video_path)
            duration = int(clip.duration + 0.5)  # round to nearest second
            clip.close()
            if duration > 0:
                return duration
        except Exception as e:
            logger.warning(f"MoviePy duration read failed: {e}")

    if OPENCV_AVAILABLE:
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return 0
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if fps > 0 and frame_count > 0:
                duration = int(frame_count / fps + 0.5)
            cap.release()
            if duration > 0:
                return duration
        except Exception as e:
            logger.warning(f"OpenCV duration read failed: {e}")

    logger.warning("Could not determine video duration")
    return duration

async def generate_video_thumbnail(video_path: str, output_path: str) -> bool:
    """
    Try to generate thumbnail using moviepy or opencv
    Returns True if successful, False otherwise
    """
    if MOVIEPY_AVAILABLE:
        try:
            clip = VideoFileClip(video_path)
            clip.save_frame(output_path, t=1)
            clip.close()
            return True
        except Exception as e:
            logger.warning(f"MoviePy thumbnail failed: {e}")

    if OPENCV_AVAILABLE:
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                logger.warning("Cannot open video with OpenCV")
                return False
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_pos = int(fps * 1) if fps > 0 else 30
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_pos)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(output_path, frame)
                cap.release()
                return True
            # Fallback: first frame
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(output_path, frame)
                cap.release()
                return True
            cap.release()
        except Exception as e:
            logger.warning(f"OpenCV thumbnail failed: {e}")

    logger.warning("No thumbnail generated - neither moviepy nor opencv worked")
    return False

# Functions from dump.py (integrated and fixed)

BUNKR_VS_API_URL_FOR_SLUG = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"

def create_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.su/',
    })
    return session

def get_and_prepare_download_path(custom_path, album_name):
    album_name = re.sub(r'[<>:"/\\|?*\']|[\0-\31]', "-", album_name).strip() if album_name else "album"
    final_path = custom_path if custom_path else DOWNLOADS_DIR
    final_path = os.path.join(final_path, album_name)
    if not os.path.isdir(final_path):
        os.makedirs(final_path)
    return final_path

def get_real_download_url(session, url, is_bunkr=True, item_name=None):
    if is_bunkr:
        url = url if url.startswith('https') else f'https://bunkr.su{url}'
    else:
        url = url.replace('/f/','/api/f/')

    r = session.get(url)
    if r.status_code != 200:
        logger.warning(f"HTTP {r.status_code} getting real url for {url}")
        return None

    if is_bunkr:
        # Fixed: use basename instead of faulty regex
        slug = unquote(os.path.basename(urlparse(url).path))
        encryption_data = get_encryption_data(session, slug)
        if not encryption_data:
            return None
        decrypted_url = decrypt_encrypted_url(encryption_data)
        return {'url': decrypted_url, 'name': item_name}
    else:
        try:
            item_data = json.loads(r.content)
            return {'url': item_data['url'], 'name': item_data['name']}
        except:
            return None

def get_encryption_data(session, slug=None):
    r = session.post(BUNKR_VS_API_URL_FOR_SLUG, json={'slug': slug})
    if r.status_code != 200:
        logger.warning(f"HTTP {r.status_code} getting encryption data")
        return None
    return json.loads(r.content)

def decrypt_encrypted_url(encryption_data):
    secret_key = f"{SECRET_KEY_BASE}{floor(encryption_data['timestamp'] / 3600)}"
    encrypted_url_bytearray = list(b64decode(encryption_data['url']))
    secret_key_byte_array = list(secret_key.encode('utf-8'))
    decrypted_url = ""
    for i in range(len(encrypted_url_bytearray)):
        decrypted_url += chr(encrypted_url_bytearray[i] ^ secret_key_byte_array[i % len(secret_key_byte_array)])
    return decrypted_url

# End of integrated functions

async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""
        is_bunkr = "bunkr" in url or "bunkrrr" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")
        url = url.replace("bunkr.pk", "bunkr.su").replace("bunkr.is", "bunkr.su")
        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code} on album page")
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
        seen_urls = set()  # To prevent duplicate downloads

        for idx, item in enumerate(items, 1):
            if isinstance(item, dict):
                file_url = item.get("url")
                file_name = item.get("name", album_name)
            else:
                file_url = item
                file_name = album_name

            if file_url in seen_urls:
                logger.info(f"Skipping duplicate file_url: {file_url}")
                continue
            seen_urls.add(file_url)

            file_url = fix_bunkr_url(file_url)

            await safe_edit(
                status_msg,
                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            success = False
            max_retries = 10  # Increased for better reliability
            for attempt in range(max_retries):
                try:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://bunkr.su/"
                    }
                    response = session.get(file_url, stream=True, timeout=60, headers=headers)
                    if response.status_code != 200:
                        if response.status_code == 404:
                            break
                        logger.warning(f"HTTP {response.status_code} on attempt {attempt+1}")
                        await asyncio.sleep(2 ** attempt)
                        continue

                    file_size = int(response.headers.get("content-length", 0))
                    final_path = os.path.join(download_path, file_name)
                    downloaded = 0
                    start_time = time.time()
                    last_update = start_time

                    with open(final_path, "wb") as f:
                        for chunk in response.iter_content(chunk_size=8192 * 4):  # Larger chunk for large files
                            if chunk:
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

                    # Size check
                    downloaded_size = os.path.getsize(final_path)
                    if file_size > 0 and downloaded_size != file_size:
                        raise ValueError(f"Size mismatch: expected {file_size}, got {downloaded_size}")

                    success = True
                    break
                except Exception as e:
                    logger.warning(f"Attempt {attempt+1} failed for {file_name}: {str(e)}")
                    if os.path.exists(final_path):
                        os.remove(final_path)
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

            # Get duration, width, height
            duration = 0
            width = 0
            height = 0

            is_video = file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))

            if is_video:
                duration = get_video_duration(final_path)

                # Get resolution
                if duration > 0:  # Only if duration succeeded, try resolution
                    if MOVIEPY_AVAILABLE:
                        try:
                            clip = VideoFileClip(final_path)
                            width, height = clip.size
                            clip.close()
                        except Exception as e:
                            logger.warning(f"MoviePy resolution failed: {e}")
                    if width == 0 and OPENCV_AVAILABLE:
                        try:
                            cap = cv2.VideoCapture(final_path)
                            if cap.isOpened():
                                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                            cap.release()
                        except Exception as e:
                            logger.warning(f"OpenCV resolution failed: {e}")

            # Thumbnail generation
            thumb_path = None
            if is_video:
                thumb_filename = f"{file_name}_thumb.jpg"
                thumb_path = os.path.join(download_path, thumb_filename)
                success_thumb = await generate_video_thumbnail(final_path, thumb_path)
                if not success_thumb or not os.path.exists(thumb_path):
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
                    if is_video:
                        await client.send_video(
                            message.chat.id,
                            f,
                            caption=f"✅ {file_name}",
                            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                            duration=duration,
                            width=width if width > 0 else 0,
                            height=height if height > 0 else 0,
                            supports_streaming=True,
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
    urls = extract_urls(message.text)
    unique_urls = list(set(urls))  # Deduplicate URLs to prevent multiple processing
    if not unique_urls:
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

    for url in unique_urls:
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

# Add dummy Flask server for Render deployment (use background worker if possible)
from flask import Flask
import threading

flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bunkr Downloader Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=flask_app.run, kwargs={"host": "0.0.0.0", "port": port, "threaded": True, "use_reloader": False}).start()
    start_bot()
