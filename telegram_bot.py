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
import subprocess

load_dotenv()

API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', 'downloads')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
# OPTIMIZED PYROGRAM CLIENT
app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=".",
)

def create_optimized_session():
    """Create session with optimized connection pooling"""
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=Retry(
            total=7,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
        )
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

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

async def optimized_upload_progress(current, total, status_msg, file_name, idx, total_items, last_update_time, start_time):
    if total == 0:
        return
    current_time = time.time()
    if current_time - last_update_time[0] < 3:
        return
    last_update_time[0] = current_time
    percent = int(current * 100 / total)
    elapsed = current_time - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    bar = '█' * int(percent / 5) + '░' * (20 - int(percent / 5))
    speed_mbps = (speed / 1024 / 1024)
    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}...\n"
        f"[{bar}] {percent}%\n"
        f"{human_bytes(current)} / {human_bytes(total)}\n"
        f"⚡ Speed: {speed_mbps:.2f} MB/s | ETA: {int(eta // 60)}m {int(eta % 60)}s"
    )
    await safe_edit(status_msg, text)

def fix_bunkr_url(url: str) -> str:
    """Fix unstable Bunkr CDN domains"""
    url = url.replace("c.bunkr-cache.se", "c.bunkr.su")
    url = url.replace("bunkr-cache.se", "bunkr.su")
    url = url.replace("c.bunkr.is", "c.bunkr.su")
    return url

def get_video_duration_ffprobe(video_path: str) -> int:
    """
    Get video duration in seconds using ffprobe.
    Returns integer seconds (rounded). Returns 0 on failure.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1:noprint_indexes=1",
                video_path
            ],
            capture_output=True,
            text=True,
            timeout=12
        )
        if result.returncode != 0:
            logger.warning(f"[v0] ffprobe returned non-zero exit code: {result.returncode}")
            return 0

        output = result.stdout.strip()
        if not output:
            logger.warning("[v0] ffprobe returned empty duration output")
            return 0

        try:
            duration_float = float(output)
            duration = int(round(duration_float))  # better rounding
            logger.info(f"[v0] ffprobe duration: {duration}s (raw: {output})")
            return duration
        except ValueError:
            logger.warning(f"[v0] ffprobe duration is not a number: '{output}'")
            return 0

    except subprocess.TimeoutExpired:
        logger.warning("[v0] ffprobe timed out after 12 seconds")
        return 0
    except FileNotFoundError:
        logger.error("[v0] ffprobe not found on the system")
        return 0
    except Exception as e:
        logger.warning(f"[v0] ffprobe failed: {type(e).__name__}: {e}")
        return 0

def get_video_resolution_ffprobe(video_path: str) -> tuple:
    """Get video resolution using ffprobe - returns (width, height) or (None, None)"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                video_path
            ],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split('x')
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                width, height = int(parts[0]), int(parts[1])
                if width > 0 and height > 0:
                    logger.info(f"[v0] ffprobe resolution: {width}x{height}")
                    return (width, height)
    except Exception as e:
        logger.warning(f"[v0] ffprobe resolution failed: {e}")
    return (None, None)

async def generate_video_thumbnail_ffmpeg(video_path: str, output_path: str) -> bool:
    """Generate thumbnail using ffmpeg"""
    try:
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-ss", "00:00:01.000", "-vframes", "1", "-y", output_path],
            capture_output=True,
            timeout=15
        )
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            logger.info(f"[v0] ffmpeg thumbnail generated successfully → {output_path}")
            return True
    except Exception as e:
        logger.warning(f"[v0] ffmpeg thumbnail failed: {e}")
    return False

# ────────────────────────────────────────────────
# MAIN DOWNLOAD + UPLOAD LOGIC
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
            album_name = h1.text.strip() if h1 else "file"
            item = get_real_download_url(session, url, True, album_name)
            if item:
                items.append(item)
        else:
            h1 = soup.find('h1', {'class': 'truncate'})
            album_name = h1.text.strip() if h1 else "album"
            for theItem in soup.find_all('div', {'class': 'theItem'}):
                box = theItem.find('a', {'class': 'after:absolute'})
                if box:
                    view_url = urljoin(url, box["href"])
                    name = theItem.find("p").text.strip() if theItem.find("p") else "file"
                    direct_item = get_real_download_url(session, view_url, True, name)
                    if direct_item:
                        items.append(direct_item)

        if not items:
            await safe_edit(status_msg, "❌ No downloadable items found")
            return

        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📥 Found {len(items)} items. Starting...")

        skipped_files = []
        seen_urls = set()

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
                    elif response.status_code == 404:
                        logger.warning(f"HTTP 404 for {file_url} on attempt {attempt+1}")
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
                logger.error(f"Skipped file: {file_name}")
                continue

            file_size = int(response.headers.get("content-length", 0))
            final_path = os.path.join(download_path, file_name)
            downloaded = 0
            start_time = time.time()
            last_update = start_time

            try:
                with open(final_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=524288):
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
                            speed_mbps = speed / 1024 / 1024

                            text = (
                                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:25]}\n"
                                f"[{bar}] {percent}%\n"
                                f"{human_bytes(downloaded)} / {human_bytes(file_size)}\n"
                                f"⚡ Speed: {speed_mbps:.2f} MB/s | ETA: {int(eta // 60)}m {int(eta % 60)}s"
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

            # ────────────────────────────────────────────────
            # VIDEO METADATA
            duration = 0
            width = None
            height = None
            thumb_path = None
            is_video = file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))

            if is_video:
                logger.info(f"[v0] Getting video metadata for {file_name}")
                duration = get_video_duration_ffprobe(final_path)
                width, height = get_video_resolution_ffprobe(final_path)

                thumb_filename = f"{file_name}_thumb.jpg"
                thumb_path = os.path.join(download_path, thumb_filename)
                logger.info(f"[v0] Generating thumbnail for {file_name}")
                success_thumb = await generate_video_thumbnail_ffmpeg(final_path, thumb_path)
                if not success_thumb or not os.path.exists(thumb_path) or os.path.getsize(thumb_path) < 2000:
                    thumb_path = None

            # ────────────────────────────────────────────────
            # UPLOAD
            await safe_edit(
                status_msg,
                f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            upload_start_time = time.time()
            last_update_time = [upload_start_time]

            try:
                with open(final_path, "rb") as f:
                    if is_video:
                        send_kwargs = {
                            "chat_id": message.chat.id,
                            "video": f,
                            "caption": f" {file_name}",
                            "supports_streaming": True,
                            "progress": optimized_upload_progress,
                            "progress_args": (status_msg, file_name, idx, len(items), last_update_time, upload_start_time),
                            # Always include duration (Telegram likes it better even if 0)
                            "duration": duration,
                        }

                        if thumb_path and os.path.exists(thumb_path):
                            send_kwargs["thumb"] = thumb_path
                        if width is not None and width > 0:
                            send_kwargs["width"] = width
                        if height is not None and height > 0:
                            send_kwargs["height"] = height

                        logger.info(f"[v0] Sending video → duration={duration}s  res={width}x{height}")
                        await client.send_video(**send_kwargs)

                    elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                        await client.send_photo(
                            message.chat.id,
                            f,
                            caption=f" {file_name}",
                            progress=optimized_upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items), last_update_time, upload_start_time)
                        )

                    else:
                        await client.send_document(
                            message.chat.id,
                            f,
                            caption=f" {file_name}",
                            progress=optimized_upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items), last_update_time, upload_start_time)
                        )

                total_upload_time = time.time() - upload_start_time
                file_size_mb = os.path.getsize(final_path) / 1024 / 1024
                upload_speed_mbps = file_size_mb / total_upload_time if total_upload_time > 0 else 0
                logger.info(f"[v0] Upload complete for {file_name}: {upload_speed_mbps:.2f} MB/s")

            except Exception as upload_err:
                logger.exception(f"Upload failed for {file_name}: {upload_err}")
                await safe_edit(status_msg, f"⚠️ Upload failed for {file_name[:30]}")

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

# ────────────────────────────────────────────────
# HANDLERS
@app.on_message(filters.text & (filters.private | filters.group))
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    unique_urls = list(set(urls))

    if not unique_urls:
        return

    session = create_optimized_session()

    for url in unique_urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 **Bunkr Downloader Bot**\n\n"
        "Send Bunkr or Cyberdrop links.\n"
        "The bot will download & upload automatically.\n\n"
        "⚡ **Optimized for fast upload**"
    )

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    await message.reply_text(
        "Send any Bunkr / Cyberdrop link.\n"
        "Progress updates + auto upload supported.\n\n"
        "⚡ **Features:**\n"
        "• Fast upload\n"
        "• Video duration & thumbnail support\n"
        "• Connection pooling\n"
        "• Progress with speed & ETA"
    )
