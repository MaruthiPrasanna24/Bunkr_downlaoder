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
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[v0] edit_text failed: {e}")

def human_bytes(size):
    if size < 1024: return f"{size} B"
    elif size < 1024**2: return f"{size / 1024:.2f} KB"
    elif size < 1024**3: return f"{size / 1024**2:.2f} MB"
    else: return f"{size / 1024**3:.2f} GB"

async def optimized_upload_progress(current, total, status_msg, file_name, idx, total_items, last_update_time, start_time):
    if total == 0: return
    current_time = time.time()
    if current_time - last_update_time[0] < 3: return
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
    url = url.replace("c.bunkr-cache.se", "c.bunkr.su")
    url = url.replace("bunkr-cache.se", "bunkr.su")
    url = url.replace("c.bunkr.is", "c.bunkr.su")
    return url

def get_video_duration_ffprobe(video_path: str) -> int:
    """
    Robust duration detection for fragmented MP4 / Reels / Shorts / bunkr videos.
    Tries stream → format → frame count estimation.
    Never returns 0 (Telegram hates it).
    """
    # 1. Try stream duration (often works better for fragmented files)
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            val = r.stdout.strip()
            if val and val != "N/A":
                try:
                    duration = int(float(val))
                    if duration > 0:
                        logger.info(f"[v0] Duration from stream: {duration}s")
                        return duration
                except ValueError:
                    pass
    except Exception as e:
        logger.debug(f"[v0] Stream duration probe failed: {e}")

    # 2. Try format duration
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            val = r.stdout.strip()
            if val and val != "N/A":
                try:
                    duration = int(float(val))
                    if duration > 0:
                        logger.info(f"[v0] Duration from format: {duration}s")
                        return duration
                except ValueError:
                    pass
    except Exception as e:
        logger.debug(f"[v0] Format duration probe failed: {e}")

    # 3. Last resort: estimate from frame count + fps
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-count_frames",
                "-show_entries", "stream=nb_read_frames,r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0:
            lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
            if len(lines) >= 2:
                try:
                    frames = int(lines[0])
                    num, den = map(int, lines[1].split('/'))
                    fps = num / den
                    if fps > 0:
                        duration = int(frames / fps) + 1  # small safety margin
                        if duration > 0:
                            logger.info(f"[v0] Estimated duration from frames: {duration}s")
                            return duration
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"[v0] Frame count estimation failed: {e}")

    logger.warning(f"[v0] All duration probes failed for {os.path.basename(video_path)} → using fallback 1s")
    return 1

def get_video_resolution_ffprobe(video_path: str) -> tuple:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                video_path
            ],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split('x')
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    logger.info(f"[v0] Resolution: {w}x{h}")
                    return (w, h)
    except Exception as e:
        logger.warning(f"[v0] Resolution probe failed: {e}")
    return (None, None)

async def generate_video_thumbnail_ffmpeg(video_path: str, output_path: str) -> bool:
    try:
        # Better thumbnail for Telegram: small square-ish, good quality
        cmd = [
            "ffmpeg", "-i", video_path,
            "-ss", "00:00:01",
            "-vframes", "1",
            "-vf", "scale=320:320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2",
            "-q:v", "2",
            "-y", output_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
        if os.path.exists(output_path) and os.path.getsize(output_path) >= 8000:  # Telegram likes > ~10KB
            logger.info(f"[v0] Thumbnail generated OK → {output_path} ({os.path.getsize(output_path)} bytes)")
            return True
    except Exception as e:
        logger.warning(f"[v0] Thumbnail generation failed: {e}")
    return False

# ────────────────────────────────────────────────
# MAIN LOGIC
async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Processing: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")

        is_bunkr = "bunkr" in url or "bunkrrr" in url
        url = url.replace("bunkr.pk", "bunkr.su").replace("bunkr.is", "bunkr.su")
        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code}")
            return

        soup = BeautifulSoup(r.content, 'html.parser')
        is_direct = (
            soup.find('span', {'class': 'ic-videos'}) or
            soup.find('div', {'class': 'lightgallery'})
        )

        items = []
        if is_direct:
            h1 = soup.find('h1', {'class': 'text-[20px]'}) or soup.find('h1', {'class': 'truncate'})
            album_name = h1.get_text(strip=True) if h1 else "file"
            item = get_real_download_url(session, url, True, album_name)
            if item: items.append(item)
        else:
            h1 = soup.find('h1', {'class': 'truncate'})
            album_name = h1.get_text(strip=True) if h1 else "album"
            for theItem in soup.find_all('div', {'class': 'theItem'}):
                box = theItem.find('a', {'class': 'after:absolute'})
                if box:
                    view_url = urljoin(url, box["href"])
                    name = theItem.find("p").get_text(strip=True) if theItem.find("p") else "file"
                    direct = get_real_download_url(session, view_url, True, name)
                    if direct: items.append(direct)

        if not items:
            await safe_edit(status_msg, "❌ No files found")
            return

        download_path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status_msg, f"📥 Found {len(items)} items. Starting...")

        skipped = []
        seen = set()

        for idx, item in enumerate(items, 1):
            file_url = item["url"] if isinstance(item, dict) else item
            file_name = item.get("name", album_name) if isinstance(item, dict) else album_name

            if file_url in seen: continue
            seen.add(file_url)
            file_url = fix_bunkr_url(file_url)

            await safe_edit(status_msg, f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}")

            success = False
            for attempt in range(4):
                try:
                    headers = {"User-Agent": "Mozilla/5.0 ...", "Referer": "https://bunkr.su/"}
                    resp = session.get(file_url, stream=True, timeout=60, headers=headers)
                    if resp.status_code == 200:
                        success = True
                        break
                except Exception:
                    if attempt < 3: await asyncio.sleep(2 ** attempt)

            if not success:
                skipped.append(file_name)
                await safe_edit(status_msg, f"⚠️ Skipped {file_name[:30]}")
                continue

            final_path = os.path.join(download_path, file_name)
            file_size = int(resp.headers.get("content-length", 0))

            downloaded = 0
            start_t = time.time()
            last_up = start_t
            last_status = ""

            try:
                with open(final_path, "wb") as f:
                    for chunk in resp.iter_content(524288):
                        if not chunk: continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_up >= 5 and file_size > 0:
                            # progress code (same as before)
                            percent = int(downloaded * 100 / file_size)
                            elapsed = now - start_t
                            speed = downloaded / elapsed if elapsed else 0
                            eta = (file_size - downloaded) / speed if speed else 0
                            bar = '█' * (percent // 5) + '░' * (20 - percent // 5)
                            speed_mb = speed / 1024 / 1024
                            text = f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:25]}\n[{bar}] {percent}%\n{human_bytes(downloaded)} / {human_bytes(file_size)}\n⚡ {speed_mb:.2f} MB/s | ETA {int(eta//60)}m {int(eta%60)}s"
                            if text != last_status:
                                await safe_edit(status_msg, text)
                                last_status = text
                            last_up = now
            except Exception as e:
                logger.exception(f"Download failed: {file_name}")
                skipped.append(file_name)
                if os.path.exists(final_path): os.remove(final_path)
                continue

            # ── Metadata ───────────────────────────────────────
            duration = 0
            width = None
            height = None
            thumb_path = None
            is_video = file_name.lower().endswith(('.mp4','.mkv','.avi','.mov','.webm'))

            if is_video:
                logger.info(f"[v0] Metadata for {file_name}")
                duration = get_video_duration_ffprobe(final_path)
                width, height = get_video_resolution_ffprobe(final_path)

                thumb_file = f"{file_name}_thumb.jpg"
                thumb_path = os.path.join(download_path, thumb_file)
                if await generate_video_thumbnail_ffmpeg(final_path, thumb_path):
                    if os.path.getsize(thumb_path) < 8000:
                        os.remove(thumb_path)
                        thumb_path = None
                else:
                    thumb_path = None

            # ── Upload ─────────────────────────────────────────
            await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}")

            upload_start = time.time()
            last_up_time = [upload_start]

            try:
                with open(final_path, "rb") as f:
                    if is_video:
                        send_kwargs = {
                            "chat_id": message.chat.id,
                            "video": f,
                            "caption": f" {file_name}",
                            "supports_streaming": True,
                            "progress": optimized_upload_progress,
                            "progress_args": (status_msg, file_name, idx, len(items), last_up_time, upload_start),
                            "duration": max(1, duration),           # ← fixed: never 0
                            "width":  width  if width  and width  > 0 else 720,
                            "height": height if height and height > 0 else 1280,
                        }
                        if thumb_path and os.path.exists(thumb_path):
                            send_kwargs["thumb"] = thumb_path

                        logger.info(f"[v0] send_video → dur={send_kwargs['duration']}s  {send_kwargs['width']}×{send_kwargs['height']}")
                        await client.send_video(**send_kwargs)

                    elif file_name.lower().endswith(('.jpg','.jpeg','.png','.gif','.webp')):
                        await client.send_photo(message.chat.id, f, caption=f" {file_name}",
                                                progress=optimized_upload_progress,
                                                progress_args=(status_msg, file_name, idx, len(items), last_up_time, upload_start))

                    else:
                        await client.send_document(message.chat.id, f, caption=f" {file_name}",
                                                   progress=optimized_upload_progress,
                                                   progress_args=(status_msg, file_name, idx, len(items), last_up_time, upload_start))

                upload_time = time.time() - upload_start
                mb = os.path.getsize(final_path) / 1024 / 1024
                logger.info(f"[v0] Uploaded {file_name} @ {mb/upload_time:.2f} MB/s")
            except Exception as e:
                logger.exception(f"Upload failed: {file_name}")
                await safe_edit(status_msg, f"⚠️ Upload failed: {file_name[:30]}")

            # Cleanup
            if os.path.exists(final_path): os.remove(final_path)
            if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)

        # Summary
        summary = f"✅ Done! {album_name}"
        if skipped:
            summary += f"\n⚠️ Skipped {len(skipped)}: {', '.join(skipped[:3])}"
            if len(skipped) > 3: summary += f" +{len(skipped)-3} more"
        await safe_edit(status_msg, summary)

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Error: {str(e)[:100]}")

# Handlers (unchanged)
@app.on_message(filters.text & (filters.private | filters.group))
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    unique_urls = list(set(urls))
    if not unique_urls: return
    session = create_optimized_session()
    for url in unique_urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text("🤖 Bunkr / Cyberdrop Downloader Bot\n\nSend link → auto download & upload")

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    await message.reply_text("Send Bunkr / Cyberdrop link.\nSupports albums, videos, photos.\nShows progress + speed.")

