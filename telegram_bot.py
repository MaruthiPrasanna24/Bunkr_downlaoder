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
from pyrogram.errors import MessageNotModified, ImageProcessFailed

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID_STR = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not all([API_ID_STR, API_HASH, BOT_TOKEN]):
    logger.error("Missing required environment variables!")
    raise ValueError("Missing environment variables")

API_ID = int(API_ID_STR)

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")

if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)

_session = None

def get_global_session():
    global _session
    if _session is None:
        _session = create_session()
    return _session

app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

URL_PATTERN = r"(https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|me|so|re|cat|dog|xxx)|cyberdrop\.(?:me|io|to|cc))[^\s]*)"

def extract_urls(text):
    matches = re.findall(URL_PATTERN, text)
    cleaned = []
    for url in matches:
        url = url.rstrip('.,!?;)')
        if url:
            cleaned.append(url)
    return cleaned

def is_valid_bunkr_url(url):
    return bool(re.match(
        r"https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|me|so|re|cat|dog|xxx)|cyberdrop\.(?:me|io|to|cc))",
        url
    ))

async def safe_edit(msg, text):
    try:
        if msg.text != text:
            await msg.edit_text(text)
    except MessageNotModified:
        pass

async def upload_progress(current, total, status_msg, file_name, idx, total_items):
    if total == 0:
        return
    percent = int(current * 100 / total)
    await safe_edit(
        status_msg,
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}\nProgress: {percent}%"
    )

async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        status_msg = await message.reply_text("🔄 Processing link...")

        r = session.get(url, timeout=30)
        if r.status_code != 200:
            await safe_edit(status_msg, "❌ Failed to fetch page")
            return

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "html.parser")

        items = []
        processed = set()

        h1 = soup.find("h1", {"class": "truncate"})
        album_name = h1.text if h1 else "bunkr"

        for theItem in soup.find_all("div", {"class": "theItem"}):
            box = theItem.find("a", {"class": "after:absolute"})
            if box:
                item_url = box["href"]
                if item_url not in processed:
                    processed.add(item_url)
                    items.append({
                        "url": item_url,
                        "name": theItem.find("p").text if theItem.find("p") else "file"
                    })

        if not items:
            item = get_real_download_url(session, url, True, album_name)
            if item:
                items.append(item)

        if not items:
            await safe_edit(status_msg, "❌ No downloadable items found")
            return

        from dump import remove_illegal_chars
        album_name = remove_illegal_chars(album_name)
        download_path = os.path.join(DOWNLOADS_DIR, album_name)

        os.makedirs(download_path, exist_ok=True)

        for idx, item in enumerate(items, 1):
            file_url = item["url"] if isinstance(item, dict) else item
            file_name = item["name"] if isinstance(item, dict) else album_name

            if file_url.startswith("/"):
                file_url = "https://bunkr.cr" + file_url

            file_name = remove_illegal_chars(file_name)
            final_path = os.path.join(download_path, file_name)

            await safe_edit(status_msg, f"⬇️ Downloading {file_name[:30]}")

            resp = session.get(file_url, stream=True)
            if resp.status_code != 200:
                continue

            with open(final_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    if chunk:
                        f.write(chunk)

            if file_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                await client.send_photo(message.chat.id, final_path, caption=f"✅ {file_name}")
            elif file_name.lower().endswith((".mp4", ".mkv", ".mov", ".avi", ".webm")):
                await client.send_video(message.chat.id, final_path, caption=f"✅ {file_name}")
            else:
                await client.send_document(message.chat.id, final_path, caption=f"✅ {file_name}")

            os.remove(final_path)

        await safe_edit(status_msg, "✅ Done")

    except Exception as e:
        logger.exception(e)
        await message.reply_text("❌ Error during download")

# 🔥 FULLY FIXED MESSAGE HANDLER
@app.on_message(filters.text | filters.caption)
async def handle_message(client: Client, message: Message):
    try:
        text = message.text or message.caption or ""

        # 🔥 NEW: extract URLs from entities (preview-only messages)
        if message.entities:
            for ent in message.entities:
                if ent.type == "url":
                    text += " " + message.text[ent.offset: ent.offset + ent.length]
                elif ent.type == "text_link":
                    text += " " + ent.url

        if not text or text.startswith("/"):
            return

        urls = extract_urls(text)
        if not urls:
            return

        session = get_global_session()
        processed = set()

        for url in urls:
            if url in processed:
                continue
            processed.add(url)

            if is_valid_bunkr_url(url):
                await download_and_send_file(client, message, url, session)

    except Exception as e:
        logger.exception(e)
        await message.reply_text("❌ Internal error")

@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 Bunkr Downloader Bot\n\n"
        "Send Bunkr / Cyberdrop links.\n"
        "Preview links supported."
    )

@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    await message.reply_text("Just send a link.")

def start_bot():
    logger.info("Bot starting…")
    app.run()

if __name__ == "__main__":
    start_bot()
