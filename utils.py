# utils.py
import os
import time
import logging
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from moviepy.editor import VideoFileClip
from pyrogram import Client
from pyrogram.types import Message

logger = logging.getLogger(__name__)

def create_session_with_retries():
    session = requests.Session()
    retries = Retry(total=5,
                    backoff_factor=1,
                    status_forcelist=[403, 429, 500, 502, 503, 504],
                    allowed_methods=["HEAD", "GET", "OPTIONS", "POST"])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def get_real_download_url(session, url, is_direct=False, name="file"):
    # Assuming this is the function from dump.py, updated with retries
    # If you have the full dump.py, integrate accordingly
    # For demonstration, placeholder with updates
    try:
        # Update domain if needed
        if 'bunkr.pk' in url:
            url = url.replace('bunkr.pk', 'bunkr.ac')  # Use a working domain from search
        response = session.get(url, timeout=60)  # Increased timeout
        if response.status_code == 200:
            # Parse to get direct url, etc.
            # Add your logic here
            return {"url": url, "name": name}  # Placeholder
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error: {e}")
        raise
    return None

def progress_bar(percent):
    filled = int(percent / 5)
    empty = 20 - filled
    return '█' * filled + '░' * empty

async def download_file(session, file_url, file_name, download_path, status_msg, idx, total_items):
    try:
        response = session.get(file_url, stream=True, timeout=60)
        if response.status_code != 200:
            return None

        file_size = int(response.headers.get("content-length", 0))
        final_path = os.path.join(download_path, file_name)
        downloaded = 0
        start_time = time.time()
        last_update_time = start_time

        with open(final_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    current_time = time.time()
                    if current_time - last_update_time >= 5 and file_size > 0:
                        elapsed = current_time - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        remaining = file_size - downloaded
                        eta = remaining / speed if speed > 0 else 0
                        percent = int(downloaded * 100 / file_size)
                        size_str = f"{downloaded / 1024**2:.2f}MB / {file_size / 1024**2:.2f}MB"
                        eta_str = f"{eta:.0f}s"
                        bar = progress_bar(percent)
                        text = (
                            f"⬇️ Downloading [{idx}/{total_items}]: {file_name[:25]}\n"
                            f"[{bar}] {percent}% | {size_str} | ETA: {eta_str}"
                        )
                        await status_msg.edit_text(text)
                        last_update_time = current_time

        return final_path
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

async def upload_file(client: Client, chat_id, file_path, file_name, status_msg, idx, total_items, is_video=False):
    try:
        start_time = time.time()
        last_update_time = [start_time]  # Mutable for callback

        async def upload_progress(current, total):
            current_time = time.time()
            if current_time - last_update_time[0] < 5:
                return
            last_update_time[0] = current_time
            elapsed = current_time - start_time
            speed = current / elapsed if elapsed > 0 else 0
            remaining = total - current
            eta = remaining / speed if speed > 0 else 0
            percent = int(current * 100 / total) if total > 0 else 0
            size_str = f"{current / 1024**2:.2f}MB / {total / 1024**2:.2f}MB"
            eta_str = f"{eta:.0f}s"
            bar = progress_bar(percent)
            text = (
                f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}\n"
                f"[{bar}] {percent}% | {size_str} | ETA: {eta_str}"
            )
            await status_msg.edit_text(text)

        thumb_path = None
        if is_video:
            clip = VideoFileClip(file_path)
            thumb_path = os.path.splitext(file_path)[0] + "_thumb.jpg"
            clip.save_frame(thumb_path, t=1)  # Extract frame at 1 second
            # Simple design: no extra, just the frame

        if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
            await client.send_video(
                chat_id,
                file_path,
                caption=f"✅ {file_name}",
                thumb=thumb_path,
                progress=upload_progress,
            )
        elif file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            await client.send_photo(
                chat_id,
                file_path,
                caption=f"✅ {file_name}",
                progress=upload_progress,
            )
        else:
            await client.send_document(
                chat_id,
                file_path,
                caption=f"✅ {file_name}",
                progress=upload_progress,
            )

        if thumb_path:
            os.remove(thumb_path)
        os.remove(file_path)
    except Exception as e:
        logger.error(f"Upload error: {e}")
