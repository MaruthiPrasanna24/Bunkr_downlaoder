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

URL_PATTERN = r'(https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is)|cyberdrop\.me)[^\s]+)'


def extract_urls(text):
    matches = re.findall(URL_PATTERN, text)
    logger.info(f"[v0] URL_PATTERN matches: {matches}")
    return matches


def is_valid_bunkr_url(url):
    is_valid = bool(
        re.match(r'https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is)|cyberdrop\.me)', url)
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


# =======================
# ✅ ADDED: UPLOAD PROGRESS
# =======================
async def upload_progress(current, total, status_msg, file_name, idx, total_items):
    if total == 0:
        return
    percent = int(current * 100 / total)
    text = (
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}\n"
        f"Progress: {percent}%"
    )
    await safe_edit(status_msg, text)


async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")
        last_status = ""

        is_bunkr = "bunkr" in url
        logger.info(f"[v0] is_bunkr: {is_bunkr}")

        if is_bunkr and not url.startswith("https"):
            url = f"https://bunkr.sk{url}"

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code}")
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
            for theItem in soup.find_all('div', {'class': 'theItem'}):
                box = theItem.find('a', {'class': 'after:absolute'})
                if box:
                    items.append({
                        "url": box["href"],
                        "name": theItem.find("p").text if theItem.find("p") else "file"
                    })
            h1 = soup.find('h1', {'class': 'truncate'})
            album_name = h1.text if h1 else "album"

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

            response = session.get(file_url, stream=True, timeout=30)
            if response.status_code != 200:
                continue

            file_size = int(response.headers.get("content-length", 0))
            final_path = os.path.join(download_path, file_name)

            downloaded = 0
            with open(final_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)

                    if file_size > 0:
                        percent = int((downloaded / file_size) * 100)
                        text = (
                            f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:25]}\n"
                            f"Progress: {percent}%"
                        )
                        if text != last_status:
                            await safe_edit(status_msg, text)
                            last_status = text

            await safe_edit(
                status_msg,
                f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}"
            )

            with open(final_path, "rb") as f:
                if file_name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
                    await client.send_video(
                        message.chat.id,
                        f,
                        caption=f"✅ {file_name}",
                        progress=upload_progress,
                        progress_args=(status_msg, file_name, idx, len(items))
                    )
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

            os.remove(final_path)

        await safe_edit(status_msg, f"✅ Done! {album_name}")

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Error: {str(e)[:100]}")


@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    urls = extract_urls(message.text)
    if not urls:
        return

    session = create_session()
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
