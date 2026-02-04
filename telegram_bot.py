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
from urllib.parse import urljoin, urlparse
import time
from datetime import timedelta
import subprocess
from pathlib import Path
import socket

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
    """Extract URLs and remove duplicates"""
    matches = re.findall(URL_PATTERN, text)
    unique_urls = []
    seen = set()
    for url in matches:
        if url not in seen:
            unique_urls.append(url)
            seen.add(url)
    logger.info(f"[v0] Extracted {len(unique_urls)} unique URLs")
    return unique_urls


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


def replace_domain_in_url(url, original_domain, new_domain):
    """Replace domain in URL"""
    return url.replace(original_domain, new_domain)


async def download_with_fallback_domains(session, file_url, timeout=20):
    """Download file with fallback domains for DNS resolution issues"""
    parsed_url = urlparse(file_url)
    original_domain = parsed_url.netloc
    
    # Fallback domains for bunkr cache
    fallback_domains = [
        ('c.bunkr-cache.se', 'bunkr.sk'),
        ('c.bunkr-cache.se', 'bunkr.cr'),
        ('c.bunkr-cache.se', 'bunkr.su'),
        ('c.bunkr-cache.se', 'cdn.bunkrcdn.ru'),
    ]
    
    # Try original URL first
    try:
        response = session.get(file_url, stream=True, timeout=timeout)
        if response.status_code == 200:
            return response
        logger.warning(f"[v0] Original URL returned {response.status_code}")
    except (requests.exceptions.RequestException, socket.gaierror) as e:
        logger.warning(f"[v0] Original URL failed: {str(e)[:60]}")
    
    # Try fallback domains
    for old_domain, new_domain in fallback_domains:
        if old_domain in file_url:
            fallback_url = replace_domain_in_url(file_url, old_domain, new_domain)
            logger.info(f"[v0] Trying fallback: {fallback_url[:80]}")
            try:
                response = session.get(fallback_url, stream=True, timeout=timeout)
                if response.status_code == 200:
                    logger.info(f"[v0] Fallback domain worked: {new_domain}")
                    return response
                logger.warning(f"[v0] Fallback returned {response.status_code}")
            except (requests.exceptions.RequestException, socket.gaierror) as e:
                logger.warning(f"[v0] Fallback failed: {str(e)[:60]}")
                continue
    
    return None


def is_valid_bunkr_url(url):
    """Validate bunkr URL format"""
    is_valid = bool(
        re.match(r'https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|si|ph|ps|ci|ax|fi|ac|black|la)|bunkrrr\.org|cyberdrop\.me)', url)
    )
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


class UploadTracker:
    """Track upload progress"""
    def __init__(self):
        self.start_time = time.time()


upload_tracker = UploadTracker()


async def upload_progress(current, total, status_msg, file_name, idx, total_items):
    """Update upload progress with speed and ETA"""
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
    """Download files from Bunkr and upload to Telegram"""
    status_msg = None
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""

        is_bunkr = "bunkr" in url or "bunkrrr" in url

        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.su{url}"

        # Fetch main page with retries
        await safe_edit(status_msg, f"⏳ Fetching page data...")
        max_retries = 3
        r = None
        for attempt in range(max_retries):
            try:
                r = session.get(url, timeout=15)
                if r.status_code == 200:
                    break
                elif r.status_code == 404:
                    await safe_edit(status_msg, f"❌ HTTP 404 - File/Album not found")
                    return
                else:
                    logger.warning(f"[v0] Page fetch attempt {attempt + 1}: HTTP {r.status_code}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
            except (requests.exceptions.RequestException, socket.gaierror) as e:
                logger.warning(f"[v0] Page fetch attempt {attempt + 1} failed: {str(e)[:50]}")
                if attempt == max_retries - 1:
                    await safe_edit(status_msg, f"❌ DNS/Network error. Could not reach site.")
                    return
                await asyncio.sleep(2)
        
        if r is None or r.status_code != 200:
            await safe_edit(status_msg, f"❌ Failed to fetch URL")
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

            # Download with retries and fallback domains
            response = None
            for attempt in range(3):
                try:
                    # Use fallback domain function for DNS issues
                    response = await download_with_fallback_domains(session, file_url, timeout=20)
                    if response and response.status_code == 200:
                        break
                    elif response and response.status_code == 404:
                        logger.warning(f"[v0] File not found: {file_url}")
                        break
                    else:
                        logger.warning(f"[v0] Download attempt {attempt + 1} failed")
                        if attempt < 2:
                            await asyncio.sleep(3)
                except Exception as e:
                    logger.warning(f"[v0] Download attempt {attempt + 1} error: {str(e)[:50]}")
                    if attempt < 2:
                        await asyncio.sleep(3)
            
            if response is None or response.status_code != 200:
                logger.warning(f"[v0] Skipping file, download failed: {file_name}")
                await safe_edit(status_msg, f"⚠️  Skipped [{idx}/{len(items)}]: {file_name[:25]} (DNS/Network Error)")
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

            # Cleanup downloaded file
            try:
                os.remove(final_path)
            except:
                pass

        await safe_edit(status_msg, f"✅ Done! {album_name}")

    except Exception as e:
        logger.exception(f"[v0] Exception in download_and_send_file: {e}")
        try:
            if status_msg:
                await safe_edit(status_msg, f"❌ Error: {str(e)[:80]}")
        except:
            await message.reply_text(f"❌ Error: {str(e)[:100]}")


@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    """Handle incoming messages with Bunkr URLs"""
    urls = extract_urls(message.text)
    if not urls:
        return

    session = create_session()
    
    # Process only first URL to prevent spam
    url = urls[0]
    if is_valid_bunkr_url(url):
        logger.info(f"[v0] Processing URL: {url}")
        await download_and_send_file(client, message, url, session)
    else:
        await message.reply_text("❌ Invalid URL format")


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
    logger.info("[v0] Bot starting...")
    app.run()


if __name__ == "__main__":
    start_bot()
