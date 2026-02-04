import os
import re
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
from dump import (
    create_session,
    get_real_download_url,
    remove_illegal_chars
)
import aiohttp

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bunkr-bot")

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

app = Client(
    "bunkr_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# UPDATED DOMAIN SUPPORT
BUNKR_DOMAINS = (
    "bunkr.", "bunkrr.", "cyberdrop."
)

URL_REGEX = re.compile(r"https?://[^\s]+")


def extract_all_urls(message: Message):
    urls = set()

    if message.text:
        urls.update(URL_REGEX.findall(message.text))

    if message.caption:
        urls.update(URL_REGEX.findall(message.caption))

    for ent in (message.entities or []) + (message.caption_entities or []):
        if ent.type == "url":
            urls.add(message.text[ent.offset: ent.offset + ent.length])

    return list(urls)


def is_bunkr(url: str) -> bool:
    return any(domain in url for domain in BUNKR_DOMAINS)


async def download_and_send(client: Client, message: Message, url: str):
    status = await message.reply_text("🔍 Resolving link...")

    try:
        session = create_session()
        item = get_real_download_url(session, url, True, "file")

        if not item or not item.get("url"):
            await status.edit("❌ Failed to resolve media URL")
            return

        media_url = item["url"]
        filename = remove_illegal_chars(item.get("name", "file"))

        path = os.path.join(DOWNLOADS_DIR, filename)

        await status.edit("⬇️ Downloading...")

        async with aiohttp.ClientSession() as s:
            async with s.get(media_url) as r:
                if r.status != 200:
                    await status.edit(f"❌ HTTP {r.status}")
                    return

                with open(path, "wb") as f:
                    while True:
                        chunk = await r.content.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

        await status.edit("📤 Uploading...")

        await client.send_document(
            message.chat.id,
            path,
            caption=f"✅ {filename}"
        )

        os.remove(path)
        await status.edit("✅ Done")

    except Exception as e:
        logger.exception(e)
        await status.edit(f"❌ Error: {str(e)[:80]}")


@app.on_message(filters.text | filters.caption)
async def handle_message(client: Client, message: Message):
    urls = extract_all_urls(message)

    if not urls:
        return

    for url in urls:
        if is_bunkr(url):
            await download_and_send(client, message, url)


@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text(
        "🤖 Bunkr Downloader Bot\n\n"
        "Send Bunkr / CyberDrop links.\n"
        "Albums + single files supported."
    )


if __name__ == "__main__":
    logger.info("Bot started")
    app.run()
