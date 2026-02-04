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
    get_url_data,
    remove_illegal_chars
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
    raise RuntimeError("Missing env vars")

API_ID = int(API_ID_STR)

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

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

# 🔥 FIXED DOMAIN SUPPORT
URL_PATTERN = r"(https?://[^\s]+)"

def extract_urls_from_message(message: Message):
    urls = set()

    if message.text:
        urls.update(re.findall(URL_PATTERN, message.text))

    if message.caption:
        urls.update(re.findall(URL_PATTERN, message.caption))

    for ent in (message.entities or []) + (message.caption_entities or []):
        if ent.type == "url":
            src = message.text or message.caption
            urls.add(src[ent.offset: ent.offset + ent.length])

    return list(urls)


def is_valid_bunkr_url(url: str) -> bool:
    return any(d in url for d in (
        "bunkr.", "bunkrr.", "cyberdrop."
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
        f"📤 Uploading [{idx}/{total_items}]\n{file_name}\n{percent}%"
    )


async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    status_msg = await message.reply_text(f"🔍 Processing\n{url[:60]}")

    try:
        logger.info(f"Resolving URL: {url}")

        items = get_items_list(session, url)

        if not items:
            await safe_edit(status_msg, "❌ No downloadable items found")
            return

        album_name = remove_illegal_chars(items[0].get("album", "download"))
        download_path = os.path.join(DOWNLOADS_DIR, album_name)
        os.makedirs(download_path, exist_ok=True)

        await safe_edit(status_msg, f"📥 Found {len(items)} item(s)")

        for idx, item in enumerate(items, 1):
            try:
                real = get_real_download_url(
                    session,
                    item["url"],
                    True,
                    album_name
                )

                if not real or not real.get("url"):
                    logger.error("Failed to resolve real URL")
                    continue

                file_url = real["url"]
                file_name = remove_illegal_chars(real["name"])
                file_path = os.path.join(download_path, file_name)

                await safe_edit(status_msg, f"⬇️ Downloading [{idx}/{len(items)}]\n{file_name}")

                r = session.get(file_url, stream=True, timeout=60)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")

                with open(file_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)

                await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]\n{file_name}")

                await client.send_document(
                    message.chat.id,
                    file_path,
                    caption=f"✅ {file_name}",
                    progress=upload_progress,
                    progress_args=(status_msg, file_name, idx, len(items))
                )

                os.remove(file_path)

            except Exception as e:
                logger.exception(e)
                await message.reply_text(f"⚠️ Failed item {idx}: {str(e)[:80]}")

        await safe_edit(status_msg, "✅ Completed")

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Fatal error: {str(e)[:120]}")


@app.on_message(filters.text | filters.caption)
async def handle_message(client: Client, message: Message):
    urls = extract_urls_from_message(message)

    if not urls:
        return

    session = get_global_session()

    for url in urls:
        if is_valid_bunkr_url(url):
            await download_and_send_file(client, message, url, session)


@app.on_message(filters.command("start"))
async def start_command(client, message):
    await message.reply_text(
        "🤖 Bunkr Downloader Bot\n"
        "Send Bunkr / CyberDrop links"
    )


if __name__ == "__main__":
    logger.info("Starting Telegram Bot...")
    app.run()
