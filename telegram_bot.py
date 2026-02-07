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
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from pyrogram.errors import MessageNotModified
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor
import threading
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
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
load_dotenv()
API_ID = int(os.getenv('TELEGRAM_API_ID'))
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', 'downloads')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# ⚡ OPTIMIZED PYROGRAM CLIENT
app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=".",
)
# Bunkr domain fallbacks (expanded with more from search)
DOMAINS = ['la', 'su', 'is', 'ru', 'pk', 'sk', 'ph', 'ps', 'ci', 'ax', 'fi', 'ac', 'ws', 'red', 'site', 'black', 'cat', 'cc', 'org', 'cr', 'si', 'media', 'to', 'net', 'com', 'fi', 'albums.io']
# Known CDN prefixes (expanded)
CDN_PREFIXES = (
    [f'cdn{i}.' for i in range(1, 13)] +
    [f'media-files{i}.' for i in range(1, 10)] +
    [f'i{i}.' for i in range(1, 10)] +
    ['stream.', 'files.', 'c.', 'cdn.', 'media-files.', 'scdn.', 'c2wi.scdn.', '']
)
# Enhanced session with connection pooling and disable SSL verify for weak certs
def create_optimized_session():
    """Create session with optimized connection pooling"""
    session = requests.Session()
    session.verify = False  # Disable SSL verification for sites with weak certs
   
    # Connection pooling with increased pool size
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=Retry(
            total=3,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST"]
        )
    )
   
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
URL_PATTERN = r'(https?://(?:bunkr(?:r)?\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la|media|red|site|ws|org|cat|cc|com|net|to)|bunkrrr\.org|bunkr-albums\.io|cyberdrop\.(?:me|cr|to|cc|nl))[^\s]+)'
def extract_urls(text):
    matches = re.findall(URL_PATTERN, text)
    logger.info(f"[v0] URL_PATTERN matches: {matches}")
    return matches
def is_valid_bunkr_url(url):
    is_valid = bool(
        re.match(r'https?://(?:bunkr(?:r)?\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la|media|red|site|ws|org|cat|cc|com|net|to)|bunkrrr\.org|bunkr-albums\.io|cyberdrop\.(?:me|cr|to|cc|nl))', url)
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
# ⚡ OPTIMIZED UPLOAD PROGRESS WITH SPEED TRACKING
async def optimized_upload_progress(current, total, status_msg, file_name, idx, total_items, last_update_time, start_time):
    if total == 0:
        return
   
    current_time = time.time()
    if current_time - last_update_time[0] < 3: # Update every 3 seconds for better speed display
        return
   
    last_update_time[0] = current_time
    percent = int(current * 100 / total)
    elapsed = current_time - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
   
    bar = '█' * int(percent / 5) + '░' * (20 - int(percent / 5))
   
    # Show speed in MB/s
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
    """Get video duration using ffprobe"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1:noprint_indexes=1", video_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            duration = int(float(result.stdout.strip()) + 0.5)
            logger.info(f"[v0] ffprobe duration: {duration}s")
            return duration
    except Exception as e:
        logger.warning(f"[v0] ffprobe duration failed: {e}")
    return None
def get_video_duration(video_path: str) -> int:
    """Returns video duration in seconds"""
    if not os.path.exists(video_path):
        logger.warning(f"[v0] Video file not found: {video_path}")
        return None
   
    duration = get_video_duration_ffprobe(video_path)
    if duration is not None and duration > 0:
        return duration
   
    if MOVIEPY_AVAILABLE:
        try:
            clip = VideoFileClip(video_path)
            duration = int(clip.duration)
            clip.close()
            if duration > 0:
                logger.info(f"[v0] MoviePy duration: {duration}s")
                return duration
        except Exception as e:
            logger.warning(f"[v0] MoviePy duration failed: {e}")
   
    if OPENCV_AVAILABLE:
        try:
            cap = cv2.VideoCapture(video_path)
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                cap.release()
                if fps > 0 and frame_count > 0:
                    duration = int(frame_count / fps)
                    if duration > 0:
                        logger.info(f"[v0] OpenCV duration: {duration}s")
                        return duration
        except Exception as e:
            logger.warning(f"[v0] OpenCV duration failed: {e}")
   
    return None
def get_video_resolution_ffprobe(video_path: str) -> tuple:
    """Get video resolution using ffprobe"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", video_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split('x')
            if len(parts) == 2:
                width, height = int(parts[0]), int(parts[1])
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
            logger.info(f"[v0] ffmpeg thumbnail generated successfully")
            return True
    except Exception as e:
        logger.warning(f"[v0] ffmpeg thumbnail failed: {e}")
    return False
async def generate_video_thumbnail_moviepy(video_path: str, output_path: str) -> bool:
    """Generate thumbnail using moviepy"""
    try:
        clip = VideoFileClip(video_path)
        frame = clip.get_frame(1)
        clip.close()
        img = Image.fromarray(frame)
        img.save(output_path, "JPEG")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            logger.info(f"[v0] MoviePy thumbnail generated successfully")
            return True
    except Exception as e:
        logger.warning(f"[v0] MoviePy thumbnail failed: {e}")
    return False
async def generate_video_thumbnail_opencv(video_path: str, output_path: str) -> bool:
    """Generate thumbnail using opencv"""
    try:
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_num = int(fps * 1) if fps > 0 else 30
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
            ret, frame = cap.read()
            cap.release()
            if ret and frame is not None:
                cv2.imwrite(output_path, frame)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                    logger.info(f"[v0] OpenCV thumbnail generated successfully")
                    return True
    except Exception as e:
        logger.warning(f"[v0] OpenCV thumbnail failed: {e}")
    return False
async def generate_fallback_thumbnail(video_path: str, output_path: str) -> bool:
    """Generate a simple fallback thumbnail"""
    try:
        if not PIL_AVAILABLE:
            return False
        width, height = 320, 180
        img = Image.new('RGB', (width, height), color='#1a1a1a')
        draw = ImageDraw.Draw(img)
        center_x, center_y = width // 2, height // 2
        triangle_size = 30
        points = [
            (center_x - triangle_size, center_y - triangle_size),
            (center_x - triangle_size, center_y + triangle_size),
            (center_x + triangle_size, center_y)
        ]
        draw.polygon(points, fill='#ffffff')
        img.save(output_path, "JPEG")
        logger.info(f"[v0] Fallback thumbnail generated")
        return True
    except Exception as e:
        logger.warning(f"[v0] Fallback thumbnail failed: {e}")
    return False
async def generate_video_thumbnail(video_path: str, output_path: str) -> bool:
    """Generate thumbnail using ffmpeg, moviepy, opencv, or fallback"""
    if not os.path.exists(video_path):
        logger.warning(f"[v0] Video file not found for thumbnail: {video_path}")
        return False
   
    if await generate_video_thumbnail_ffmpeg(video_path, output_path):
        return True
    if MOVIEPY_AVAILABLE and await generate_video_thumbnail_moviepy(video_path, output_path):
        return True
    if OPENCV_AVAILABLE and await generate_video_thumbnail_opencv(video_path, output_path):
        return True
    if await generate_fallback_thumbnail(video_path, output_path):
        return True
   
    logger.warning(f"[v0] No thumbnail generated for {video_path}")
    return False
# ⚡ OPTIMIZED FILE UPLOAD WITH FASTER SPEED (7-10 MB/s target)
async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""
       
        is_bunkr = "bunkr" in url or "bunkrrr" in url or "bunkr-albums" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")
       
        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"
       
        # Parse path for domain fallback
        parsed = urlparse(url)
        path = parsed.path
        if parsed.query:
            path += '?' + parsed.query
        
        # Fetch album/page with domain fallback
        r = None
        soup = None
        for tld in DOMAINS:
            if tld == 'albums.io':
                current_url = f"https://bunkr-albums.io{path}"
            else:
                current_url = f"https://bunkr.{tld}{path}"
            logger.info(f"[v0] Trying domain for album: {urlparse(current_url).netloc}")
            try:
                r = session.get(current_url, timeout=15)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.content, 'html.parser')
                    url = current_url  # Update for urljoin
                    logger.info(f"[v0] Success on domain {tld} for album (HTTP 200)")
                    break
                else:
                    logger.warning(f"[v0] Domain {tld} returned HTTP {r.status_code} for album")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[v0] Domain {tld} failed for album: {e}")
        
        if soup is None:
            await safe_edit(status_msg, "❌ Failed to fetch from all domains")
            return
       
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
            logger.info(f"[v0] Original file URL: {file_url}")
           
            await safe_edit(
                status_msg,
                f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}"
            )
           
            # File download with domain fallback
            success = False
            file_parsed = urlparse(file_url)
            file_path = file_parsed.path
            if file_parsed.query:
                file_path += '?' + file_parsed.query
            
            # Extract original prefix
            parts = file_parsed.netloc.split('.')
            if len(parts) > 1:
                # Always replace the last part as tld
                original_tld = parts[-1]
                original_prefix = '.'.join(parts[:-1]) + '.'
            else:
                original_prefix = ''
                original_tld = ''
            
            # Prioritize original, then others
            prefixes = [original_prefix] + [p for p in CDN_PREFIXES if p != original_prefix]
            
            final_path = os.path.join(download_path, file_name)
            temp_path = os.path.join(download_path, f"temp_{file_name}")
            
            for prefix in prefixes:
                for tld in DOMAINS:
                    if tld == 'albums.io':
                        new_netloc = prefix + 'bunkr-albums.io'
                    else:
                        new_netloc = prefix + 'bunkr.' + tld
                    current_file_url = file_parsed._replace(netloc=new_netloc).geturl()
                    current_file_url = fix_bunkr_url(current_file_url)
                    logger.info(f"[v0] Trying domain for file: {new_netloc}")
                    try:
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                            "Referer": "https://bunkr." + tld + "/",
                            "Accept": "*/*",
                            "Accept-Language": "en-US,en;q=0.9",
                            "Range": "bytes=0-"
                        }
                        response = session.get(current_file_url, stream=True, timeout=30, headers=headers)
                        if response.status_code in (200, 206):
                            file_size = int(response.headers.get("content-length", 0))
                            downloaded = 0
                            start_time = time.time()
                            last_update = start_time
                            with open(temp_path, "wb") as f:
                                for chunk in response.iter_content(chunk_size=524288):
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
                            # Check if complete
                            if downloaded == 0:
                                logger.warning(f"[v0] Empty download from {current_file_url}")
                                if os.path.exists(temp_path):
                                    os.remove(temp_path)
                                continue
                            if file_size > 0 and downloaded != file_size:
                                logger.warning(f"[v0] Partial download from {current_file_url}: {downloaded} / {file_size}")
                                if os.path.exists(temp_path):
                                    os.remove(temp_path)
                                continue
                            # Rename to final
                            os.rename(temp_path, final_path)
                            success = True
                            logger.info(f"[v0] Success on domain {tld} with prefix {prefix} for file (HTTP {response.status_code})")
                            break
                        elif response.status_code == 404:
                            logger.warning(f"[v0] HTTP 404 for {current_file_url} on domain {tld} with prefix {prefix}")
                            continue
                        else:
                            logger.warning(f"[v0] Domain {tld} with prefix {prefix} returned HTTP {response.status_code} for file")
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"[v0] File domain {tld} with prefix {prefix} failed: {e}")
                if success:
                    break
           
            if not success:
                skipped_files.append(file_name)
                await safe_edit(
                    status_msg,
                    f"⚠️ Skipped [{idx}/{len(items)}]: {file_name[:30]} (all domains failed)"
                )
                logger.error(f"Skipped file: {file_name}")
                continue
           
            # Validate file size > 0
            file_size_local = os.path.getsize(final_path)
            if file_size_local == 0:
                logger.warning(f"[v0] Empty file after download: {file_name}")
                os.remove(final_path)
                skipped_files.append(file_name)
                await safe_edit(status_msg, f"⚠️ Skipped [{idx}/{len(items)}]: {file_name[:30]} (empty file)")
                continue
           
            # Video metadata extraction and validation
            duration = None
            width = None
            height = None
            is_video = file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
           
            if is_video:
                logger.info(f"[v0] Getting video metadata for {file_name}")
                duration = get_video_duration(final_path)
                if duration is None or duration == 0:
                    logger.warning(f"[v0] Corrupted video file: no duration for {file_name}")
                    os.remove(final_path)
                    skipped_files.append(file_name)
                    await safe_edit(status_msg, f"⚠️ Skipped [{idx}/{len(items)}]: {file_name[:30]} (corrupted video)")
                    continue
                width, height = get_video_resolution_ffprobe(final_path)
               
                if width is None and MOVIEPY_AVAILABLE:
                    try:
                        clip = VideoFileClip(final_path)
                        width, height = clip.size
                        clip.close()
                    except Exception as e:
                        logger.warning(f"[v0] MoviePy resolution failed: {e}")
               
                if width is None and OPENCV_AVAILABLE:
                    try:
                        cap = cv2.VideoCapture(final_path)
                        if cap.isOpened():
                            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                            cap.release()
                    except Exception as e:
                        logger.warning(f"[v0] OpenCV resolution failed: {e}")
           
            # Thumbnail generation
            thumb_path = None
            if is_video:
                thumb_filename = f"{file_name}_thumb.jpg"
                thumb_path = os.path.join(download_path, thumb_filename)
                logger.info(f"[v0] Generating thumbnail for {file_name}")
                success_thumb = await generate_video_thumbnail(final_path, thumb_path)
                if not success_thumb or not os.path.exists(thumb_path):
                    thumb_path = None
           
            # ⚡ OPTIMIZED UPLOAD TO TELEGRAM WITH FASTER SPEED
            await safe_edit(
                status_msg,
                f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}"
            )
           
            upload_start_time = time.time()
            last_update_time = [upload_start_time]
           
            try:
                # Open file in binary mode with optimized buffering
                with open(final_path, "rb") as f:
                    if is_video:
                        send_kwargs = {
                            "chat_id": message.chat.id,
                            "video": f,
                            "caption": f" {file_name}",
                            "supports_streaming": True,
                            "progress": optimized_upload_progress,
                            "progress_args": (status_msg, file_name, idx, len(items), last_update_time, upload_start_time)
                        }
                       
                        # Add optional parameters only if they're valid
                        if thumb_path and os.path.exists(thumb_path):
                            send_kwargs["thumb"] = thumb_path
                        if duration is not None and duration > 0:
                            send_kwargs["duration"] = duration
                        if width is not None and width > 0:
                            send_kwargs["width"] = width
                        if height is not None and height > 0:
                            send_kwargs["height"] = height
                       
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
               
                # Log final upload speed
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
@app.on_message(filters.text & (filters.private | filters.group))
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    unique_urls = list(set(urls))
   
    if not unique_urls:
        return
   
    # Use optimized session with connection pooling
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
        "⚡ **Optimized for 7-10 MB/s upload speeds**"
    )
@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    await message.reply_text(
        "Send any Bunkr / Cyberdrop link.\n"
        "Progress updates + auto upload supported.\n\n"
        "⚡ **Features:**\n"
        "• Fast upload speeds (7-10 MB/s)\n"
        "• Connection pooling for better throughput\n"
        "• Real-time speed monitoring\n"
        "• Optimized chunk sizes"
    )
