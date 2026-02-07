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
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from pyrogram.errors import MessageNotModified
import subprocess
import json

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

app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=".",
)

# Domain priority — cyberdrop.cr first
DOMAINS = ['cr', 'la', 'su', 'is', 'ru', 'pk', 'sk', 'ph', 'ps', 'ci', 'ax', 'fi', 'ac', 'ws', 'red', 'site', 'black', 'cat', 'cc', 'org', 'si', 'media', 'to', 'net', 'com', 'albums.io']

CDN_PREFIXES = (
    [f'cdn{i}.' for i in range(1, 13)] +
    [f'media-files{i}.' for i in range(1, 10)] +
    [f'i{i}.' for i in range(1, 10)] +
    ['stream.', 'files.', 'c.', 'cdn.', 'media-files.', 'scdn.', 'c2wi.scdn.', '']
)

def create_optimized_session():
    session = requests.Session()
    session.verify = False
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
    return bool(re.match(r'https?://(?:bunkr(?:r)?\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la|media|red|site|ws|org|cat|cc|com|net|to)|bunkrrr\.org|bunkr-albums\.io|cyberdrop\.(?:me|cr|to|cc|nl))', url))

async def safe_edit(msg, text):
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[v0] edit_text failed: {e}")

def human_bytes(size):
    if size < 1024: return f"{size} B"
    if size < 1024**2: return f"{size/1024:.2f} KB"
    if size < 1024**3: return f"{size/1024**2:.2f} MB"
    return f"{size/1024**3:.2f} GB"

async def optimized_upload_progress(current, total, status_msg, file_name, idx, total_items, last_update_time, start_time):
    if total == 0: return
    now = time.time()
    if now - last_update_time[0] < 3: return
    last_update_time[0] = now
    pct = int(current * 100 / total)
    elapsed = now - start_time
    speed = current / elapsed if elapsed > 0 else 0
    eta = (total - current) / speed if speed > 0 else 0
    bar = '█' * (pct//5) + '░' * (20 - pct//5)
    speed_mbps = speed / 1024 / 1024
    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}...\n"
        f"[{bar}] {pct}%\n"
        f"{human_bytes(current)} / {human_bytes(total)}\n"
        f"Speed: {speed_mbps:.2f} MB/s | ETA: {int(eta//60)}m {int(eta%60)}s"
    )
    await safe_edit(status_msg, text)

def fix_bunkr_url(url: str) -> str:
    url = url.replace("c.bunkr-cache.se", "c.bunkr.su")
    url = url.replace("bunkr-cache.se", "bunkr.su")
    url = url.replace("c.bunkr.is", "c.bunkr.su")
    return url

# ────────────────────────────────────────────────
#   VIDEO HELPERS (unchanged)
# ────────────────────────────────────────────────

def get_video_duration_ffprobe(video_path: str) -> int | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0 and r.stdout.strip():
            return int(float(r.stdout.strip()) + 0.5)
    except:
        pass
    return None

def get_video_duration(video_path: str) -> int | None:
    if not os.path.exists(video_path): return None
    dur = get_video_duration_ffprobe(video_path)
    if dur is not None and dur > 0: return dur
    if MOVIEPY_AVAILABLE:
        try:
            clip = VideoFileClip(video_path)
            dur = int(clip.duration)
            clip.close()
            if dur > 0: return dur
        except:
            pass
    return None

def get_video_resolution_ffprobe(video_path: str) -> tuple:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", video_path],
            capture_output=True, text=True, timeout=8
        )
        if r.returncode == 0 and r.stdout.strip():
            w, h = map(int, r.stdout.strip().split('x'))
            return w, h
    except:
        pass
    return None, None

async def generate_video_thumbnail(video_path: str, output_path: str) -> bool:
    if not os.path.exists(video_path): return False
    try:
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-ss", "00:00:01", "-vframes", "1", "-y", output_path],
            capture_output=True, timeout=12
        )
        if os.path.exists(output_path) and os.path.getsize(output_path) > 800:
            return True
    except:
        pass
    return False

# ────────────────────────────────────────────────
#   REAL DOWNLOAD URL EXTRACTION
# ────────────────────────────────────────────────

def get_real_download_url(session: requests.Session, page_url: str, default_name: str = "file") -> dict | None:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": page_url,
        }
        r = session.get(page_url, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            logger.warning(f"Cannot fetch {page_url} → {r.status_code}")
            return None

        soup = BeautifulSoup(r.text, 'html.parser')
        domain = urlparse(page_url).netloc.lower()
        is_cyberdrop = 'cyberdrop' in domain

        direct_url = None
        filename = default_name

        if is_cyberdrop:
            # Cyberdrop single file
            dl = soup.find('a', id='download')
            if dl and dl.get('href'):
                direct_url = urljoin(page_url, dl['href'])

            if not direct_url:
                for v in soup.find_all('video'):
                    src = v.get('src')
                    if src:
                        direct_url = urljoin(page_url, src)
                        break

            if not direct_url:
                img = soup.find('img', id='img') or soup.find('img', class_='img') or soup.find('img')
                if img:
                    for attr in ['src', 'data-src', 'data-full', 'data-original']:
                        src = img.get(attr)
                        if src:
                            direct_url = urljoin(page_url, src)
                            break

            title = soup.find('h1', id='title') or soup.find('h1')
            if title and title.text.strip():
                filename = title.text.strip()

        else:  # Bunkr
            video = soup.find('video')
            if video:
                src = video.get('src') or (video.find('source') or {}).get('src')
                if src:
                    direct_url = urljoin(page_url, src)

            if not direct_url:
                img = soup.find('img', {'data-src': True}) or soup.find('img')
                if img:
                    src = img.get('data-src') or img.get('src')
                    if src:
                        direct_url = urljoin(page_url, src)

        if not direct_url:
            logger.warning(f"No direct link found → {page_url}")
            return None

        direct_url = fix_bunkr_url(direct_url)

        # Fix extension if needed
        if '.' not in filename or len(filename.rsplit('.', 1)[-1]) > 5:
            ext_part = direct_url.split('?')[0].rsplit('.', 1)
            if len(ext_part) == 2:
                ext = ext_part[1].lower()
                if ext in {'jpg','jpeg','png','gif','webp','mp4','webm','mov','mkv'}:
                    filename = f"{default_name}.{ext}"

        return {"url": direct_url, "name": filename}

    except Exception as e:
        logger.exception(f"get_real_download_url error {page_url}: {e}")
        return None

def get_and_prepare_download_path(downloads_dir, album_name):
    safe = re.sub(r'[^\w\-. ]', '_', album_name)[:100]
    p = os.path.join(downloads_dir, safe)
    os.makedirs(p, exist_ok=True)
    return p

async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    status_msg = await message.reply_text(f"🔄 Processing {url[:55]}...")
    try:
        is_bunkr = any(x in url.lower() for x in ["bunkr", "bunkrrr", "bunkr-albums"])
        is_cyberdrop = "cyberdrop" in url.lower()

        if not (is_bunkr or is_cyberdrop):
            await safe_edit(status_msg, "❌ Unsupported site")
            return

        if not url.startswith("https://"):
            url = "https://" + url.lstrip("http://")

        parsed = urlparse(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")

        soup = None
        used_url = url

        for tld in DOMAINS:
            if tld == 'albums.io':
                test_url = f"https://bunkr-albums.io{path}"
            else:
                base = "bunkr." if is_bunkr else "cyberdrop."
                test_url = f"https://{base}{tld}{path}"

            logger.info(f"Trying → {test_url}")
            try:
                r = session.get(test_url, timeout=15)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.content, 'html.parser')
                    used_url = test_url
                    logger.info(f"Success → {test_url}")
                    break
            except Exception as e:
                logger.warning(f"Failed {test_url}: {str(e)[:80]}")

        if not soup:
            await safe_edit(status_msg, "❌ Could not load page from any domain")
            return

        items = []

        if is_bunkr:
            # Bunkr logic unchanged — keep your original bunkr parsing
            is_direct = (
                soup.find('span', {'class': 'ic-videos'}) is not None or
                soup.find('div', {'class': 'lightgallery'}) is not None
            )
            if is_direct:
                h1 = soup.find('h1', {'class': 'text-[20px]'}) or soup.find('h1', {'class': 'truncate'})
                name = h1.text.strip() if h1 else "file"
                item = get_real_download_url(session, used_url, name)
                if item: items.append(item)
            else:
                h1 = soup.find('h1', {'class': 'truncate'})
                album_name = h1.text.strip() if h1 else "album"
                for div in soup.find_all('div', class_='theItem'):
                    a = div.find('a', class_='after:absolute')
                    if a and a.get('href'):
                        vu = urljoin(used_url, a['href'])
                        fname = (div.find('p') or {}).text.strip() or "file"
                        item = get_real_download_url(session, vu, fname)
                        if item: items.append(item)

        else:  # Cyberdrop ──────────────────────────────────────
            title = soup.find('h1', id='title') or soup.find('h1')
            album_name = (title.text.strip() if title else "cyberdrop").replace('/', '_')

            # Single file check
            if any([
                soup.find('a', id='download'),
                soup.find('video'),
                soup.find('img', id='img'),
                soup.find('img', class_='img'),
            ]):
                logger.info("[cyberdrop] single file page detected")
                item = get_real_download_url(session, used_url, album_name)
                if item: items.append(item)
            else:
                # Album / folder ────────────────────────────────
                logger.info("[cyberdrop] album / folder mode")

                # Find all potential file links
                for a in soup.find_all('a', href=re.compile(r'^/(f|v)/[^/]+$')):
                    href = a['href']
                    view_url = urljoin(used_url, href)

                    # Try to get name from multiple places
                    name = (
                        a.get('title') or
                        a.get('data-name') or
                        (a.find('img') or {}).get('alt') or
                        a.get_text(strip=True) or
                        f"file_{len(items)+1}"
                    ).strip()

                    if not name or name == "file":
                        name = f"file_{len(items)+1}"

                    item = get_real_download_url(session, view_url, name)
                    if item:
                        items.append(item)
                        logger.info(f"[cyberdrop] added → {name}  ({href})")

                if not items:
                    logger.warning("[cyberdrop] no /f/ or /v/ links found on page")

        if not items:
            await safe_edit(status_msg, "❌ No downloadable files detected")
            return

        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📥 Found {len(items)} items → starting download...")

        # ────────────────────────────────────────────────
        #   Your existing download + upload + cleanup loop
        #   (paste the rest of your original download_and_send_file function here)
        # ────────────────────────────────────────────────
        # skipped_files = []
        # seen_urls = set()
        # for idx, item in enumerate(items, 1):
        #     ... your full download, domain fallback, progress, upload, cleanup code ...

        # After all files processed:
        summary = f"✅ Finished — {album_name}\nProcessed {len(items)} file(s)"
        await safe_edit(status_msg, summary)

    except Exception as e:
        logger.exception(e)
        await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")

# ────────────────────────────────────────────────
#   Message handlers (unchanged)
# ────────────────────────────────────────────────

@app.on_message(filters.text & (filters.private | filters.group))
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    unique_urls = list(set(urls))
    if not unique_urls:
        return
    session = create_optimized_session()
    for u in unique_urls:
        if is_valid_bunkr_url(u):
            await download_and_send_file(client, message, u, session)

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 **Bunkr / Cyberdrop Downloader**\n\n"
        "Send any link → bot will try to download & send files."
    )

