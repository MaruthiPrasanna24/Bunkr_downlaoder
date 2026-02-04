import requests
import json
import argparse
import sys
import os
import re
import logging
from tenacity import retry, wait_fixed, retry_if_exception_type, stop_after_attempt
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from tqdm import tqdm
from base64 import b64decode
from math import floor
from urllib.parse import unquote
from datetime import datetime

logger = logging.getLogger(__name__)

BUNKR_VS_API_URL_FOR_SLUG = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"

MAX_RETRIES = 10

session = None


def get_items_list(session, url, extensions, only_export, custom_path=None, is_last_page=True, date_before=None, date_after=None):
    """Get list of items from Bunkr/Cyberdrop URL"""
    extensions_list = extensions.split(',') if extensions is not None else []
    
    logger.info(f"[GET ITEMS LIST] Fetching: {url}")
    r = session.get(url)
    if r.status_code != 200:
        logger.error(f"[GET ITEMS LIST] HTTP error {r.status_code}")
        raise Exception(f"[-] HTTP error {r.status_code}")

    soup = BeautifulSoup(r.content, 'html.parser')
    is_bunkr = "| Bunkr" in soup.find('title').text if soup.find('title') else False

    direct_link = False
    
    if is_bunkr:
        items = []
        soup = BeautifulSoup(r.content, 'html.parser')

        direct_link = soup.find('span', {'class': 'ic-videos'}) is not None or soup.find('div', {'class': 'lightgallery'}) is not None
        
        if direct_link:
            logger.info("[GET ITEMS LIST] Direct link detected")
            album_name = soup.find('h1', {'class': 'text-[20px]'})
            if album_name is None:
                album_name = soup.find('h1', {'class': 'truncate'})

            album_name = remove_illegal_chars(album_name.text) if album_name else "file"
            items.append(get_real_download_url(session, url, True, album_name))
        else:
            logger.info("[GET ITEMS LIST] Album detected")
            theItems = soup.find_all('div', {'class': 'theItem'})
            logger.info(f"[GET ITEMS LIST] Found {len(theItems)} items")
            
            for theItem in theItems:
                if date_before is not None or date_after is not None:
                    date_span = theItem.find('span', {'class': 'ic-clock'})
                    if not is_date_in_range(date_span.text, date_before, date_after):
                        continue
                box = theItem.find('a', {'class': 'after:absolute'})
                if box:
                    items.append({'url': box['href'], 'size': -1, 'name': theItem.find('p').text if theItem.find('p') else 'file'})
            
            h1 = soup.find('h1', {'class': 'truncate'})
            album_name = remove_illegal_chars(h1.text) if h1 else "album"
    else:
        logger.info("[GET ITEMS LIST] Cyberdrop detected")
        items = []
        items_dom = soup.find_all('a', {'class': 'image'})
        for item_dom in items_dom:
            items.append({'url': f"https://cyberdrop.me{item_dom['href']}", 'size': -1})
        
        h1 = soup.find('h1', {'id': 'title'})
        album_name = remove_illegal_chars(h1.text) if h1 else "cyberdrop_album"

    download_path = get_and_prepare_download_path(custom_path, album_name)
    already_downloaded_url = get_already_downloaded_url(download_path)

    for item in items:
        if not direct_link:
            item = get_real_download_url(session, item['url'], is_bunkr, item['name'])
            if item is None:
                logger.warning("[GET ITEMS LIST] Unable to find download link for item")
                print(f"\t\t[-] Unable to find a download link")
                continue

        extension = get_url_data(item['url'])['extension']
        if ((extension in extensions_list or len(extensions_list) == 0) and (item['url'] not in already_downloaded_url)):
            if only_export:
                write_url_to_list(item['url'], download_path)
            else:
                download(session, item['url'], download_path, is_bunkr, item['name'])
        
    pagination = soup.find('nav', {'class': 'pagination'})
    if pagination is not None:
        try:
            current_page = int(pagination.find('span', {'class': 'active'}).text)
            last_page = int(pagination.find_all('a')[-2].text)

            if int(current_page) < int(last_page):
                url_next_page = None
                logger.info(f"[GET ITEMS LIST] Pagination: page {int(current_page)+1}/{last_page}")
                print(f"[!] Downloading page ({int(current_page)+1}/{last_page})")
                
                if re.search(r'([?&])page=\d+', url):
                    url_next_page = re.sub(r'([?&])page=\d+', r'\1page={}'.format(current_page+1), url)
                else:
                    url_next_page = f"{url}{'&' if '?' in url else '?'}page={(current_page+1)}"
            
                get_items_list(session, url_next_page, extensions, only_export, custom_path=custom_path, is_last_page=(int(current_page) == int(last_page)), date_before=date_before, date_after=date_after)
        except Exception as e:
            logger.warning(f"[GET ITEMS LIST] Pagination error: {e}")

    if is_last_page:
        msg = f"\t[+] File list exported in {os.path.join(download_path, 'url_list.txt')}" if only_export else f"\t[+] Download completed"
        logger.info(f"[GET ITEMS LIST] {msg}")
        print(msg)
    return
    

def get_real_download_url(session, url, is_bunkr=True, item_name=None):
    """Get the actual downloadable URL"""
    try:
        if is_bunkr:
            url = url if 'https' in url else f'https://bunkr.sk{url}'
        else:
            url = url.replace('/f/', '/api/f/')

        logger.info(f"[GET REAL URL] Fetching: {url}")
        r = session.get(url)
        if r.status_code != 200:
            logger.error(f"[GET REAL URL] HTTP error {r.status_code} for {url}")
            print(f"\t[-] HTTP error {r.status_code} getting real url for {url}")
            return None
               
        if is_bunkr:
            slug = unquote(re.search(r'\/f\/(.*?)$', url).group(1))
            decrypted_url = decrypt_encrypted_url(get_encryption_data(slug))
            logger.info(f"[GET REAL URL] Got decrypted URL for {item_name}")
            return {'url': decrypted_url, 'size': -1, 'name': item_name}
        else:
            item_data = json.loads(r.content)
            logger.info(f"[GET REAL URL] Got Cyberdrop URL for {item_data.get('name')}")
            return {'url': item_data['url'], 'size': -1, 'name': item_data['name']}
    except Exception as e:
        logger.error(f"[GET REAL URL] Error: {e}")
        return None

        
@retry(retry=retry_if_exception_type(requests.exceptions.ConnectionError), wait=wait_fixed(2), stop=stop_after_attempt(MAX_RETRIES))
def download(session, item_url, download_path, is_bunkr=False, file_name=None):
    """Download file with retries"""
    try:
        file_name = get_url_data(item_url)['file_name'] if file_name is None else file_name
        final_path = os.path.join(download_path, file_name)

        logger.info(f"[DOWNLOAD] Starting: {file_name}")
        print(f"\t[+] Downloading {item_url} ({file_name})")
        
        with session.get(item_url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                logger.error(f"[DOWNLOAD] HTTP {r.status_code} for {file_name}")
                print(f"\t[-] Error downloading \"{file_name}\": {r.status_code}")
                return
            
            if r.url == "https://bnkr.b-cdn.net/maintenance.mp4":
                logger.warning(f"[DOWNLOAD] Server maintenance for {file_name}")
                print(f"\t[-] Error downloading \"{file_name}\": Server is down for maintenance")
                return

            file_size = int(r.headers.get('content-length', -1))
            with open(final_path, 'wb') as f:
                with tqdm(total=file_size, unit='iB', unit_scale=True, desc=file_name, leave=False) as pbar:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk is not None:
                            f.write(chunk)
                            pbar.update(len(chunk))

        if is_bunkr and file_size > -1:
            downloaded_file_size = os.stat(final_path).st_size
            if downloaded_file_size != file_size:
                logger.warning(f"[DOWNLOAD] Size mismatch for {file_name}: {downloaded_file_size} vs {file_size}")
                print(f"\t[-] {file_name} size check failed, file could be broken\n")
                return

        logger.info(f"[DOWNLOAD] Completed: {file_name}")
        mark_as_downloaded(item_url, download_path)
        return
        
    except Exception as e:
        logger.error(f"[DOWNLOAD] Error downloading {file_name}: {e}")
        raise


def create_session():
    """Create HTTP session with proper headers"""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.sk/',
    })
    logger.info("[SESSION] Created new session")
    return session


def get_url_data(url):
    """Extract data from URL"""
    parsed_url = urlparse(url)
    return {
        'file_name': os.path.basename(parsed_url.path),
        'extension': os.path.splitext(parsed_url.path)[1],
        'hostname': parsed_url.hostname
    }


def get_and_prepare_download_path(custom_path, album_name):
    """Prepare download directory"""
    final_path = 'downloads' if custom_path is None else custom_path
    final_path = os.path.join(final_path, album_name) if album_name is not None else 'downloads'
    final_path = final_path.replace('\n', '')

    if not os.path.isdir(final_path):
        os.makedirs(final_path, exist_ok=True)
        logger.info(f"[DOWNLOAD PATH] Created: {final_path}")

    already_downloaded_path = os.path.join(final_path, 'already_downloaded.txt')
    if not os.path.isfile(already_downloaded_path):
        open(already_downloaded_path, 'w', encoding='utf-8').close()

    return final_path


def write_url_to_list(item_url, download_path):
    """Write URL to list file"""
    list_path = os.path.join(download_path, 'url_list.txt')

    with open(list_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    logger.info(f"[URL LIST] Written to {list_path}")
    return


def get_already_downloaded_url(download_path):
    """Get list of already downloaded URLs"""
    file_path = os.path.join(download_path, 'already_downloaded.txt')

    if not os.path.isfile(file_path):
        return []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        urls = f.read().splitlines()
        logger.info(f"[ALREADY DOWNLOADED] Found {len(urls)} previously downloaded URLs")
        return urls


def mark_as_downloaded(item_url, download_path):
    """Mark URL as downloaded"""
    file_path = os.path.join(download_path, 'already_downloaded.txt')
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    return


def remove_illegal_chars(string):
    """Remove illegal characters from filename"""
    return re.sub(r'[<>:"/\\|?*\']|[\0-\31]', "-", string).strip()


def get_encryption_data(slug=None):
    """Get encryption data for Bunkr URLs"""
    global session
    if session is None:
        session = create_session()
    
    try:
        logger.info(f"[ENCRYPTION] Getting data for slug: {slug}")
        r = session.post(BUNKR_VS_API_URL_FOR_SLUG, json={'slug': slug}, timeout=10)
        if r.status_code != 200:
            logger.error(f"[ENCRYPTION] HTTP ERROR {r.status_code}")
            print(f"\t\t[-] HTTP ERROR {r.status_code} getting encryption data")
            return None
        
        data = json.loads(r.content)
        logger.info(f"[ENCRYPTION] Got encryption data")
        return data
    except Exception as e:
        logger.error(f"[ENCRYPTION] Error: {e}")
        return None


def decrypt_encrypted_url(encryption_data):
    """Decrypt Bunkr encrypted URL"""
    try:
        secret_key = f"{SECRET_KEY_BASE}{floor(encryption_data['timestamp'] / 3600)}"
        encrypted_url_bytearray = list(b64decode(encryption_data['url']))
        secret_key_byte_array = list(secret_key.encode('utf-8'))

        decrypted_url = ""

        for i in range(len(encrypted_url_bytearray)):
            decrypted_url += chr(encrypted_url_bytearray[i] ^ secret_key_byte_array[i % len(secret_key_byte_array)])

        logger.info(f"[DECRYPTION] Successfully decrypted URL")
        return decrypted_url
    except Exception as e:
        logger.error(f"[DECRYPTION] Error: {e}")
        return None


def date_argument(date_string):
    """Parse date argument"""
    try:
        return datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid date format. Use: yyyy-mm-ddThh:mm:ss")

    
def is_date_in_range(date_string, date_before, date_after):
    """Check if date is in range"""
    try:
        bunkr_date = datetime.strptime(date_string, '%H:%M:%S %d/%m/%Y')
        date_before = datetime.max if date_before is None else date_before
        date_after = datetime.min if date_after is None else date_after

        return bunkr_date <= date_before and bunkr_date >= date_after

    except ValueError:
        logger.warning(f"[DATE] Invalid file date {date_string}")
        print(f"\t[-] Invalid file date {date_string}")
        return False

    
if __name__ == '__main__':
    parser = argparse.ArgumentParser(sys.argv[1:])
    parser.add_argument("-u", help="Url to fetch", type=str, required=False, default=None)
    parser.add_argument("-f", help="File to list of URLs to download", required=False, type=str, default=None)
    parser.add_argument("-r", help="Amount of retries in case the connection fails", type=int, required=False, default=10)
    parser.add_argument("-e", help="Extensions to download (comma separated)", type=str)
    parser.add_argument("-p", help="Path to custom downloads folder")
    parser.add_argument("-w", help="Export url list (ex: for wget)", action="store_true")
    parser.add_argument("--before", help="Export only files before this date", type=date_argument, default=None)
    parser.add_argument("--after", help="Export only files after this date", type=date_argument, default=None)

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')

    if args.u is None and args.f is None:
        print("[-] No URL or file provided")
        sys.exit(1)

    if args.u is not None and args.f is not None:
        print("[-] Please provide only one URL or file")
        sys.exit(1)

    session = create_session()

    MAX_RETRIES = args.r

    if args.f is not None:
        with open(args.f, 'r', encoding='utf-8') as f:
            urls = f.read().splitlines()
        for url in urls:
            print(f"\t[-] Processing \"{url}\"...")
            get_items_list(session, url, args.e, args.w, args.p, date_before=args.before, date_after=args.after)
        sys.exit(0)
    else:
        get_items_list(session, args.u, args.e, args.w, args.p, date_before=args.before, date_after=args.after)
        
    sys.exit(0)
