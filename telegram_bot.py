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
from urllib.parse import urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup
from pyrogram.errors import MessageNotModified
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor
import threading
import random

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

# Enhanced session with connection pooling — NO RETRIES, 10s TIMEOUT
def create_optimized_session():
    """Create session with optimized connection pooling"""
    session = requests.Session()
    
    # Connection pooling + no retries
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=Retry(
            total=0,
            backoff_factor=0,
            status_forcelist=[429, 500, 502, 503, 504, 523, 403],
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

# ⚡ OPTIMIZED UPLOAD PROGRESS WITH SPEED TRACKING
async def optimized_upload_progress(current, total, status_msg, file_name, idx, total_items, last_update_time, start_time):
    if total == 0:
        return
    
    current_time = time.time()
    if current_time - last_update_time[0] < 3:  # Update every 3 seconds for better speed display
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
    url = url.replace("bunkrrr", "bunkrr")  # Correct triple r if any
    url = url.replace("bunkr", "bunkrr")  # Standardize to bunkrr
    return url

def get_alternative_urls(url: str) -> list:
    parsed = urlparse(url)
    netloc = parsed.netloc
    path = parsed.path
    if not netloc:
        return [url]
    
    parts = netloc.split('.')
    if len(parts) < 3:
        return [url]
    
    subdomain = parts[0]
    domain = '.'.join(parts[1:-1])  # Handle bunkrr as domain
    tld = parts[-1]
    
    subdomain_variations = ['', 'c', 'cdn', 'cdn1', 'cdn2', 'cdn3', 'cdn4', 'media', 'stream', 'get', 'dl', 'files', 'media-files']
    domain_variations = ['bunkr', 'bunkrr', 'cyberdrop']
    
    # Expanded to all possible from URL_PATTERN
    possible_tlds = ['su', 'is', 'la', 'ru', 'media', 'sk', 'pk', 'si', 'ph', 'ps', 'ci', 'ax', 'fi', 'ac', 'black', 'red', 'site', 'ws', 'org', 'cat', 'cc', 'com', 'net', 'to', 'cr']
    random.shuffle(possible_tlds)
    
    alternatives = set()
    for sub in subdomain_variations:
        for dom in domain_variations:
            for new_tld in possible_tlds:
                if sub == subdomain and dom == domain and new_tld == tld:
                    continue
                new_netloc = f"{sub}.{dom}.{new_tld}" if sub else f"{dom}.{new_tld}"
                new_parsed = parsed._replace(netloc=new_netloc)
                alternatives.add(urlunparse(new_parsed))
    
    alt_list = list(alternatives)
    random.shuffle(alt_list)
    return [url] + alt_list

def get_direct_download_url(session, page_url, is_file=False, name=''):
    """Custom function to extract direct download URL, prioritizing modern bunkr patterns"""
    try:
        r = session.get(page_url, timeout=10)
        if r.status_code != 200:
            logger.warning(f"Failed to fetch page {page_url}: HTTP {r.status_code}")
            return None
        
        soup = BeautifulSoup(r.content, 'html.parser')
        
        # Priority 1: Download button
        download_btn = soup.find('a', string=re.compile('download', re.I)) or soup.find('a', {'class': re.compile('btn|download|primary')})
        if download_btn and 'href' in download_btn.attrs:
            dl_url = download_btn['href']
            if not dl_url.startswith('https'):
                dl_url = urljoin(page_url, dl_url)
            return {'url': dl_url, 'name': name or soup.find('h1', {'class': ['text-[20px]', 'truncate']}).text.strip() if soup.find('h1', {'class': ['text-[20px]', 'truncate']}) else name}
        
        # Fallback 1: Video source src
        video = soup.find('video')
        if video:
            source = video.find('source')
            if source and 'src' in source.attrs:
                src_url = source['src']
                if not src_url.startswith('https'):
                    src_url = urljoin(page_url, src_url)
                return {'url': src_url, 'name': name or soup.find('h1', {'class': ['text-[20px]', 'truncate']}).text.strip() if soup.find('h1', {'class': ['text-[20px]', 'truncate']}) else name}
        
        # Fallback 2: Any mp4 link
        mp4_link = soup.find('a', href=re.compile(r'https?://.*\.mp4'))
        if mp4_link:
            return {'url': mp4_link['href'], 'name': name or soup.find('h1', {'class': ['text-[20px]', 'truncate']}).text.strip() if soup.find('h1', {'class': ['text-[20px]', 'truncate']}) else name}
        
        logger.warning(f"No download URL found on {page_url}")
        return None
    except Exception as e:
        logger.error(f"Error extracting direct URL from {page_url}: {e}")
        return None

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

def is_valid_video(file_path: str) -> bool:
    """Check if the downloaded file is a valid video"""
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1000:
        return False
    
    duration = get_video_duration_ffprobe(file_path)
    if duration is not None and duration > 0:
        return True
    
    if MOVIEPY_AVAILABLE:
        try:
            clip = VideoFileClip(file_path)
            duration = clip.duration
            clip.close()
            return duration > 0
        except Exception:
            pass
    
    if OPENCV_AVAILABLE:
        try:
            cap = cv2.VideoCapture(file_path)
            ret = cap.isOpened()
            cap.release()
            return ret
        except Exception:
            pass
    
    # Additional check for MP4 header
    try:
        with open(file_path, 'rb') as f:
            header = f.read(12)
            if header[4:8] == b'ftyp':
                return True
    except Exception:
        pass
    
    return False

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
        
        is_bunkr = "bunkr" in url or "bunkrrr" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")
        
        url = url.replace("bunkr.pk", "bunkr.su").replace("bunkr.is", "bunkr.su")
        
        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"
        
        # Fetch album/page
        r = session.get(url, timeout=10)
        
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
            item = get_direct_download_url(session, url, True, album_name)
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
                    direct_item = get_direct_download_url(session, view_url, True, name)
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
            response = None
            
            urls_to_try = get_alternative_urls(file_url)
            
            for try_idx, try_url in enumerate(urls_to_try, 1):
                logger.info(f"Trying download from {try_url} ({try_idx}/{len(urls_to_try)})")
                try:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
                        "Referer": "https://bunkr.su/"
                    }
                    response = session.get(try_url, stream=True, timeout=10, headers=headers)  # 10s timeout
                    
                    if response.status_code == 200:
                        # Download to temp path first
                        temp_path = os.path.join(download_path, f"temp_{file_name}")
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
                        
                        # Check if downloaded size matches
                        actual_size = os.path.getsize(temp_path)
                        if file_size > 0 and actual_size != file_size:
                            logger.warning(f"Incomplete download from {try_url}: {actual_size} / {file_size}, deleting")
                            os.remove(temp_path)
                            continue
                        
                        # Validate the downloaded file
                        if is_valid_video(temp_path):
                            os.rename(temp_path, os.path.join(download_path, file_name))
                            success = True
                            file_url = try_url
                            break
                        else:
                            logger.warning(f"Invalid or corrupted file from {try_url}, deleting")
                            os.remove(temp_path)
                    
                    elif response.status_code == 404:
                        logger.warning(f"HTTP 404 for {try_url}")
                    else:
                        logger.warning(f"HTTP {response.status_code} for {try_url}")
                
                except requests.exceptions.Timeout:
                    logger.warning(f"Timeout (10s) for {try_url}")
                except requests.exceptions.ConnectionError:
                    logger.warning(f"Connection error (e.g., DNS) for {try_url}")
                except Exception as e:
                    logger.warning(f"Failed to download from {try_url}: {str(e)}")
            
            if not success:
                skipped_files.append(file_name)
                await safe_edit(
                    status_msg,
                    f"⚠️ Skipped [{idx}/{len(items)}]: {file_name[:30]} (failed after trying all domains)"
                )
                logger.error(f"Skipped file: {file_name}")
                continue
            
            final_path = os.path.join(download_path, file_name)
            
            # Video metadata extraction
            duration = None
            width = None
            height = None
            is_video = file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))
            
            if is_video:
                logger.info(f"[v0] Getting video metadata for {file_name}")
                duration = get_video_duration(final_path)
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
