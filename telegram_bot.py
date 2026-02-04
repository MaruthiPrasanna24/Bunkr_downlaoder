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
    logger.error("Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN")
    raise ValueError("Missing environment variables")

try:
    API_ID = int(API_ID_STR)
except ValueError:
    logger.error(f"TELEGRAM_API_ID must be an integer, got: {API_ID_STR}")
    raise ValueError("Invalid TELEGRAM_API_ID format")

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")

# Ensure downloads directory exists
if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)
    logger.info(f"Created downloads directory: {DOWNLOADS_DIR}")

# Global session for connection pooling
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
    logger.info(f"[v0] URL_PATTERN matches: {matches}")
    # Clean up URLs by removing trailing punctuation that's not part of the URL
    cleaned_matches = []
    for url in matches:
        url = url.rstrip('.,!?;:)')
        if url:
            cleaned_matches.append(url)
    logger.info(f"[v0] Cleaned URLs: {cleaned_matches}")
    return cleaned_matches


def is_valid_bunkr_url(url):
    is_valid = bool(
        re.match(r"https?://(?:bunkr\.(?:sk|cr|ru|su|pk|is|me|so|re|cat|dog|xxx)|cyberdrop\.(?:me|io|to|cc))", url)
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
        logger.warning(f"edit failed: {e}")


async def upload_progress(current, total, status_msg, file_name, idx, total_items):
    if total == 0:
        return
    percent = int(current * 100 / total)
    await safe_edit(
        status_msg,
        f"📤 Uploading [{idx}/{total_items}]: {file_name[:25]}\n"
        f"Progress: {percent}%"
    )


async def download_and_send_file(client: Client, message: Message, url: str, session: requests.Session):
    try:
        logger.info(f"[v0] Starting download_and_send_file for: {url}")
        status_msg = await message.reply_text(f"🔄 Processing: {url[:50]}...")

        if "bunkr" in url and not url.startswith("https"):
            url = "https://bunkr.cr" + url
        
        logger.info(f"[v0] Fetching URL: {url}")
        r = session.get(url, timeout=30)
        logger.info(f"[v0] Response status: {r.status_code}")
        
        if r.status_code != 200:
            await safe_edit(status_msg, f"❌ HTTP {r.status_code}")
            return

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.content, "html.parser")

        is_direct = soup.find("div", {"class": "lightgallery"}) or soup.find("span", {"class": "ic-videos"})

        items = []
        processed_urls = set()

        if is_direct:
            h1 = soup.find("h1", {"class": "truncate"})
            album_name = h1.text if h1 else "file"
            item = get_real_download_url(session, url, True, album_name)
            if item:
                items.append(item)
        else:
            for theItem in soup.find_all("div", {"class": "theItem"}):
                box = theItem.find("a", {"class": "after:absolute"})
                if box:
                    item_url = box["href"]
                    # Prevent duplicate processing
                    if item_url not in processed_urls:
                        processed_urls.add(item_url)
                        items.append({
                            "url": item_url,
                            "name": theItem.find("p").text if theItem.find("p") else "file"
                        })
            h1 = soup.find("h1", {"class": "truncate"})
            album_name = h1.text if h1 else "album"

        if not items:
            await safe_edit(status_msg, "❌ No items found")
            return

        # Sanitize album name to prevent path issues
        from dump import remove_illegal_chars
        album_name = remove_illegal_chars(album_name)
        download_path = os.path.join(DOWNLOADS_DIR, album_name)
        
        if not os.path.exists(download_path):
            os.makedirs(download_path)
        
        logger.info(f"[v0] Download path: {download_path}")
        await safe_edit(status_msg, f"📥 Found {len(items)} items")

        for idx, item in enumerate(items, 1):
            file_url = item["url"] if isinstance(item, dict) else item
            file_name = item["name"] if isinstance(item, dict) else album_name

            if file_url.startswith("/"):
                file_url = "https://bunkr.cr" + file_url

            # Sanitize filename to prevent nested paths
            file_name = remove_illegal_chars(file_name)
            
            logger.info(f"[v0] Processing item {idx}/{len(items)}: {file_name}")

            await safe_edit(status_msg, f"⬇️ Downloading [{idx}/{len(items)}]: {file_name[:30]}")

            try:
                response = session.get(file_url, stream=True, timeout=30)
                if response.status_code != 200:
                    logger.warning(f"[v0] Failed to download {file_name}: HTTP {response.status_code}")
                    continue

                final_path = os.path.join(download_path, file_name)
                
                # Ensure we don't create nested directories
                if os.path.isdir(final_path):
                    logger.warning(f"[v0] Path is a directory, skipping: {final_path}")
                    continue
                
                logger.info(f"[v0] Saving file to: {final_path}")
                
                with open(final_path, "wb") as f:
                    for chunk in response.iter_content(8192):
                        if chunk:
                            f.write(chunk)

                await safe_edit(status_msg, f"📤 Uploading [{idx}/{len(items)}]: {file_name[:30]}")

                try:
                    logger.info(f"[v0] Uploading {file_name} (size: {os.path.getsize(final_path)} bytes)")
                    
                    if file_name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                        logger.info(f"[v0] Sending as photo: {file_name}")
                        await client.send_photo(
                            message.chat.id,
                            final_path,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items))
                        )

                    elif file_name.lower().endswith((".mp4", ".mkv", ".mov", ".avi", ".webm")):
                        logger.info(f"[v0] Sending as video: {file_name}")
                        await client.send_video(
                            message.chat.id,
                            final_path,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items))
                        )

                    else:
                        logger.info(f"[v0] Sending as document: {file_name}")
                        await client.send_document(
                            message.chat.id,
                            final_path,
                            caption=f"✅ {file_name}",
                            progress=upload_progress,
                            progress_args=(status_msg, file_name, idx, len(items))
                        )
                    
                    logger.info(f"[v0] Successfully uploaded: {file_name}")

                except ImageProcessFailed:
                    logger.warning(f"Image failed: {file_name} — skipped, continuing album")
                except Exception as e:
                    logger.exception(f"[v0] Upload error for {file_name}: {e}")
                    await safe_edit(status_msg, f"⚠️ Upload failed for {file_name[:20]}: {str(e)[:50]}")
                
                # Clean up file after upload
                try:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                        logger.info(f"[v0] Deleted file: {final_path}")
                except Exception as e:
                    logger.warning(f"[v0] Failed to delete {final_path}: {e}")

            except Exception as e:
                logger.exception(f"[v0] Error processing item {idx}: {e}")
                continue

        await safe_edit(status_msg, f"✅ Done! {album_name}")

    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"❌ Error: {str(e)[:120]}")


@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    try:
        logger.info(f"[v0] Received message from {message.chat.id}: {message.text[:100]}")
        
        # Skip commands
        if message.text.startswith('/'):
            logger.info("[v0] Skipping command message")
            return
            
        urls = extract_urls(message.text)
        logger.info(f"[v0] Extracted URLs: {urls}")
        
        if not urls:
            logger.info("[v0] No URLs found in message")
            return
        
        session = get_global_session()
        valid_urls_found = False
        processed_urls = set()
        
        for url in urls:
            # Skip duplicate URLs
            if url in processed_urls:
                logger.info(f"[v0] Skipping duplicate URL: {url}")
                continue
            
            processed_urls.add(url)
            logger.info(f"[v0] Checking URL: {url}")
            
            if is_valid_bunkr_url(url):
                logger.info(f"[v0] Valid Bunkr URL detected: {url}, starting download")
                valid_urls_found = True
                await download_and_send_file(client, message, url, session)
            else:
                logger.info(f"[v0] Invalid Bunkr URL: {url}")
        
        if not valid_urls_found:
            logger.info(f"[v0] No valid Bunkr URLs found in message")
            
    except Exception as e:
        logger.exception(f"[v0] Error in handle_message: {e}")
        try:
            await message.reply_text(f"❌ Error: {str(e)[:100]}")
        except:
            logger.exception("Failed to send error message")


@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 Bunkr Downloader Bot\n\n"
        "Send Bunkr or Cyberdrop links.\n"
        "Photos → photo\nVideos → video\nAlbums supported."
    )


@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    await message.reply_text("Send a link. Album-safe uploads enabled.")


def start_bot():
    try:
        logger.info("[v0] Bot starting...")
        logger.info(f"[v0] API_ID: {API_ID}")
        logger.info(f"[v0] API_HASH: {API_HASH[:20]}...")
        logger.info(f"[v0] BOT_TOKEN: {BOT_TOKEN[:20]}...")
        
        logger.info("[v0] Registering message handlers...")
        logger.info("[v0] Connecting to Telegram...")
        
        app.run()
        logger.info("[v0] Bot started successfully")
    except Exception as e:
        logger.exception(f"[v0] Fatal error in start_bot: {e}")
        raise


if __name__ == "__main__":
    start_bot()
