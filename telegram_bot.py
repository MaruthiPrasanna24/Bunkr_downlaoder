# telegram_bot.py
import os
import re
import time
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# ────────────────────────────────────────────────
# Import scraper functions from dump.py
# ────────────────────────────────────────────────
from dump import (
    create_session,
    get_and_prepare_download_path,
    get_real_download_url,
    get_url_data,
    remove_illegal_chars
)

try:
    from moviepy.editor import VideoFileClip
    MOVIEPY_AVAILABLE = True
except ImportError:
    MOVIEPY_AVAILABLE = False

# ────────────────────────────────────────────────
# Config & Logging
# ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Client(
    "bunkr_downloader_bot",
    api_id=int(os.getenv("TELEGRAM_API_ID")),
    api_hash=os.getenv("TELEGRAM_API_HASH"),
    bot_token=os.getenv("TELEGRAM_BOT_TOKEN")
)

DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', 'downloads')

URL_PATTERN = r'(https?://(?:bunkr\.(?:is|su|la|ac|fi|ax|ci|ps|ph|si|is|pk|ru|cr|sk)|bunkrrr\.org|cyberdrop\.me)[^\s]+)'

def extract_urls(text):
    return re.findall(URL_PATTERN, text, re.IGNORECASE)

def is_valid_url(url):
    return bool(re.match(URL_PATTERN, url, re.IGNORECASE))

async def safe_edit(msg: Message, text: str):
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"safe_edit failed: {e}")

def human_bytes(size):
    for unit in ['B','KiB','MiB','GiB','TiB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}" if unit != 'B' else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{size:.2f} PiB"

async def download_progress(curr, total, msg, fname, idx, total_files, last_t, stime):
    now = time.time()
    if now - last_t[0] < 5:
        return
    last_t[0] = now

    if total <= 0:
        return

    pct = int(curr * 100 / total)
    elapsed = now - stime
    speed = curr / elapsed if elapsed > 0 else 0
    eta_sec = (total - curr) / speed if speed > 0 else 0

    bar = '█' * (pct // 5) + '░' * (20 - (pct // 5))

    txt = (
        f"⬇️ [{idx}/{total_files}] {fname[:28]}\n"
        f"[{bar}] {pct}%\n"
        f"{human_bytes(curr)} / {human_bytes(total)}\n"
        f"↑ {human_bytes(int(speed))}/s  •  ETA {int(eta_sec//60)}m {int(eta_sec%60)}s"
    )
    await safe_edit(msg, txt)

async def upload_progress(curr, total, msg, fname, idx, total_files, last_t, stime):
    now = time.time()
    if now - last_t[0] < 5:
        return
    last_t[0] = now

    if total <= 0:
        return

    pct = int(curr * 100 / total)
    elapsed = now - stime
    speed = curr / elapsed if elapsed > 0 else 0
    eta_sec = (total - curr) / speed if speed > 0 else 0

    bar = '█' * (pct // 5) + '░' * (20 - (pct // 5))

    txt = (
        f"📤 [{idx}/{total_files}] {fname[:28]}\n"
        f"[{bar}] {pct}%\n"
        f"{human_bytes(curr)} / {human_bytes(total)}\n"
        f"↑ {human_bytes(int(speed))}/s  •  ETA {int(eta_sec//60)}m {int(eta_sec%60)}s"
    )
    await safe_edit(msg, txt)

async def download_and_send_file(client: Client, message: Message, url: str):
    status = None
    try:
        status = await message.reply(f"🔄 Processing {url[:65]}{'…' if len(url)>65 else ''}")

        # Normalize domain
        url = re.sub(r'^http://', 'https://', url)
        url = url.replace("bunkr.pk", "bunkr.su").replace("bunkr.is", "bunkr.su")

        sess = create_session()
        r = sess.get(url, timeout=25)
        if r.status_code != 200:
            await safe_edit(status, f"❌ HTTP {r.status_code}")
            return

        soup = BeautifulSoup(r.content, 'html.parser')

        is_single = bool(
            soup.find('span', class_='ic-videos') or
            soup.find('div', class_='lightgallery')
        )

        items = []
        album_name = "download"

        h1 = soup.find('h1', class_=['truncate', 'text-[20px]'])
        if h1:
            album_name = remove_illegal_chars(h1.get_text(strip=True))

        if is_single:
            dl_item = get_real_download_url(sess, url, item_name=album_name)
            if dl_item:
                items.append(dl_item)
        else:
            for div in soup.find_all('div', class_='theItem'):
                a = div.find('a', class_='after:absolute')
                if not a:
                    continue
                view_url = urljoin(url, a['href'])
                name = div.find('p')
                name = name.get_text(strip=True) if name else "file"
                dl_item = get_real_download_url(sess, view_url, item_name=name)
                if dl_item:
                    items.append(dl_item)

        if not items:
            await safe_edit(status, "❌ No files found")
            return

        path = get_and_prepare_download_path(DOWNLOADS_DIR, album_name)
        await safe_edit(status, f"Found {len(items)} file(s). Downloading...")

        for i, item in enumerate(items, 1):
            file_url = item['url'] if isinstance(item, dict) else item
            fname = item.get('name', album_name) if isinstance(item, dict) else album_name

            await safe_edit(status, f"⬇️ [{i}/{len(items)}] {fname[:32]} …")

            r = sess.get(file_url, stream=True, timeout=45)
            if r.status_code != 200:
                await safe_edit(status, f"Download failed HTTP {r.status_code}")
                continue

            fsize = int(r.headers.get('content-length', 0))
            out_path = os.path.join(path, fname)

            curr = 0
            st = time.time()
            last = [st]

            with open(out_path, 'wb') as f:
                for chunk in r.iter_content(16384):
                    if chunk:
                        f.write(chunk)
                        curr += len(chunk)
                        await download_progress(curr, fsize, status, fname, i, len(items), last, st)

            # thumbnail
            thumb = None
            if MOVIEPY_AVAILABLE and fname.lower().endswith(('.mp4','.mkv','.webm','.mov','.avi')):
                try:
                    thumb = os.path.join(path, f"thumb_{fname}.jpg")
                    clip = VideoFileClip(out_path)
                    clip.save_frame(thumb, t=1.8)
                    clip.close()
                except Exception as e:
                    logger.warning(f"thumb failed: {e}")
                    thumb = None

            # upload
            await safe_edit(status, f"📤 Uploading [{i}/{len(items)}] {fname[:32]} …")

            ust = time.time()
            ulast = [ust]

            with open(out_path, 'rb') as f:
                if fname.lower().endswith(('.mp4','.mkv','.webm','.mov','.avi')):
                    await client.send_video(
                        message.chat.id, f,
                        caption=f"**{fname}**",
                        thumb=thumb,
                        progress=upload_progress,
                        progress_args=(status, fname, i, len(items), ulast, ust)
                    )
                elif fname.lower().endswith(('.jpg','.jpeg','.png','.gif','.webp')):
                    await client.send_photo(
                        message.chat.id, f,
                        caption=f"**{fname}**",
                        progress=upload_progress,
                        progress_args=(status, fname, i, len(items), ulast, ust)
                    )
                else:
                    await client.send_document(
                        message.chat.id, f,
                        caption=f"**{fname}**",
                        progress=upload_progress,
                        progress_args=(status, fname, i, len(items), ulast, ust)
                    )

            try:
                os.remove(out_path)
                if thumb and os.path.exists(thumb):
                    os.remove(thumb)
            except:
                pass

        await safe_edit(status, f"**Done** — {album_name} ({len(items)} files)")

    except Exception as e:
        logger.exception(e)
        txt = f"❌ Error: {str(e)[:200]}"
        if status:
            await safe_edit(status, txt)
        else:
            await message.reply(txt)

@app.on_message(filters.text & ~filters.command)
async def on_text(client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return

    for url in urls:
        if is_valid_url(url):
            await download_and_send_file(client, message, url)

@app.on_message(filters.command("start"))
async def cmd_start(_, m):
    await m.reply("**Bunkr / Cyberdrop Downloader Bot**\n\nSend link(s) to download & upload here.")

@app.on_message(filters.command("help"))
async def cmd_help(_, m):
    await m.reply("Just send bunkr or cyberdrop link(s).\nBot will process albums and single files.")

def run_bot():
    logger.info("Telegram bot is running...")
    app.run()

if __name__ == "__main__":
    run_bot()
