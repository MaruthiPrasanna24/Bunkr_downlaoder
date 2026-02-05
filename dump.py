import requests
import json
import argparse
import sys
import os
import re
from tenacity import retry, wait_fixed, retry_if_exception_type, stop_after_attempt
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from tqdm import tqdm
from base64 import b64decode
from math import floor
from urllib.parse import unquote
from datetime import datetime

BUNKR_VS_API_URL_FOR_SLUG = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"

MAX_RETRIES = 10

session = None

# Alternative Bunkr domains for fallback
BUNKR_DOMAINS = [
    "https://bunkr.cr",
    "https://bunkr.sk", 
    "https://bunkr.su",
    "https://bunkr.ru",
    "https://bunkr.is",
]

def get_items_list(session, url, extensions, only_export, custom_path=None, is_last_page=True, date_before=None, date_after=None):
    extensions_list = extensions.split(',') if extensions is not None else []
       
    r = session.get(url)
    if r.status_code != 200:
        raise Exception(f"[-] HTTP error {r.status_code}")

    soup = BeautifulSoup(r.content, 'html.parser')
    is_bunkr = "| Bunkr" in soup.find('title').text

    direct_link = False
    
    if is_bunkr:
        items = []
        soup = BeautifulSoup(r.content, 'html.parser')

        direct_link = soup.find('span', {'class': 'ic-videos'}) is not None or soup.find('div', {'class': 'lightgallery'}) is not None
        if direct_link:
            album_name = soup.find('h1', {'class': 'text-[20px]'})
            if album_name is None:
                album_name = soup.find('h1', {'class': 'truncate'})

            album_name = remove_illegal_chars(album_name.text)
            item_data = get_real_download_url(session, url, True, album_name)
            if item_data is not None:
                items.append(item_data)
        else:
            theItems = soup.find_all('div', {'class': 'theItem'})
            for theItem in theItems:
                if date_before is not None or date_after is not None:
                    date_span = theItem.find('span', {'class': 'ic-clock'})
                    if not is_date_in_range(date_span.text, date_before, date_after):
                        continue
                box = theItem.find('a', {'class': 'after:absolute'})
                items.append({'url': box['href'], 'size': -1, 'name': theItem.find('p').text})
            
            album_name = soup.find('h1', {'class': 'truncate'}).text
            album_name = remove_illegal_chars(album_name)
    else:
        items = []
        items_dom = soup.find_all('a', {'class': 'image'})
        for item_dom in items_dom:
            items.append({'url': f"https://cyberdrop.me{item_dom['href']}", 'size': -1})
        album_name = remove_illegal_chars(soup.find('h1', {'id': 'title'}).text)

    download_path = get_and_prepare_download_path(custom_path, album_name)
    already_downloaded_url = get_already_downloaded_url(download_path)

    for item in items:
        if not direct_link:
            item = get_real_download_url(session, item['url'], is_bunkr, item['name'])
            if item is None:
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
        current_page = int(pagination.find('span', {'class': 'active'}).text)
        last_page = int(pagination.find_all('a')[-2].text)

        if int(current_page) < int(last_page):
            url_next_page = None
            print(f"[!] Downloading page ({int(current_page)+1}/{last_page})")
            if re.search(r'([?&])page=\d+', url):
                url_next_page = re.sub(r'([?&])page=\d+', r'\1page={}'.format(current_page+1), url)
            else:
                url_next_page = f"{url}{'&' if '?' in url else '?'}page={(current_page+1)}"
        
            get_items_list(session, url_next_page, extensions, only_export, custom_path=custom_path, is_last_page=(int(current_page) == int(last_page)), date_before=date_before, date_after=date_after)

    if is_last_page:
        print(f"\t[+] File list exported in {os.path.join(download_path, 'url_list.txt')}" if only_export else f"\t[+] Download completed")
    return

def extract_slug_from_url(url):
    """
    Extract slug from Bunkr URL, handling both /f/ and /v/ formats.
    Returns None if slug cannot be extracted.
    """
    # Try to match both /f/ (file) and /v/ (video) patterns
    match = re.search(r'\/([fv])\/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(2)
    
    # Fallback: try to get anything after last slash
    match = re.search(r'\/([a-zA-Z0-9_-]+)$', url)
    if match:
        return match.group(1)
    
    return None

def get_real_download_url(session, url, is_bunkr=True, item_name=None):
    """
    Get the real download URL from Bunkr with proper error handling and domain rotation.
    """
    if is_bunkr:
        url = url if 'https' in url else f'https://bunkr.sk{url}'
    else:
        url = url.replace('/f/', '/api/f/')

    try:
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            print(f"\t[-] HTTP error {r.status_code} getting real url for {url}")
            return None
    except Exception as e:
        print(f"\t[-] Error fetching URL {url}: {str(e)}")
        return None
           
    if is_bunkr:
        # Extract slug safely
        slug = extract_slug_from_url(url)
        
        if slug is None:
            print(f"\t[-] Could not extract slug from URL: {url}")
            return None
        
        slug = unquote(slug)
        
        try:
            decrypted_url = decrypt_encrypted_url(get_encryption_data(slug))
            if decrypted_url is None:
                print(f"\t[-] Failed to decrypt URL for slug: {slug}")
                return None
            return {'url': decrypted_url, 'size': -1, 'name': item_name}
        except Exception as e:
            print(f"\t[-] Error decrypting URL for slug {slug}: {str(e)}")
            return None
    else:
        try:
            item_data = json.loads(r.content)
            return {'url': item_data['url'], 'size': -1, 'name': item_data['name']}
        except Exception as e:
            print(f"\t[-] Error parsing response: {str(e)}")
            return None

@retry(
    retry=retry_if_exception_type(requests.exceptions.ConnectionError),
    wait=wait_fixed(3),
    stop=stop_after_attempt(MAX_RETRIES)
)
def download(session, item_url, download_path, is_bunkr=False, file_name=None):
    """
    Download file with automatic retry and domain fallback on HTTP errors.
    """
    file_name = get_url_data(item_url)['file_name'] if file_name is None else file_name
    final_path = os.path.join(download_path, file_name)

    domains_to_try = BUNKR_DOMAINS if is_bunkr else [None]
    
    for domain_idx, domain in enumerate(domains_to_try):
        try:
            # Replace domain if using alternative
            download_url = item_url
            if is_bunkr and domain_idx > 0:
                # Replace the domain in the URL
                download_url = re.sub(r'https?://[^/]+', domain, item_url)
                print(f"\t[*] Trying alternative domain: {domain}")
            
            with session.get(download_url, stream=True, timeout=15) as r:
                if r.status_code == 410 or r.status_code == 401:
                    print(f"\t[-] HTTP {r.status_code} for {file_name}, trying next domain...")
                    if domain_idx < len(domains_to_try) - 1:
                        continue
                    else:
                        print(f"\t[-] All domains exhausted for {file_name}")
                        return None
                
                if r.status_code != 200:
                    print(f"\t[-] Error downloading \"{file_name}\": HTTP {r.status_code}")
                    return None
                
                if r.url == "https://bnkr.b-cdn.net/maintenance.mp4":
                    print(f"\t[-] Error downloading \"{file_name}\": Server is down for maintenance")
                    return None

                print(f"\t[+] Downloading {file_name}")
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
                        print(f"\t[-] {file_name} size check failed, file could be broken")
                        # Don't return, mark as downloaded anyway
                
                mark_as_downloaded(item_url, download_path)
                return True
                
        except requests.exceptions.Timeout:
            print(f"\t[-] Timeout downloading {file_name}, trying next domain...")
            if domain_idx < len(domains_to_try) - 1:
                continue
            else:
                print(f"\t[-] All domains exhausted for {file_name}")
                return None
        except requests.exceptions.ConnectionError:
            print(f"\t[-] Connection error for {file_name}, retrying...")
            raise
        except Exception as e:
            print(f"\t[-] Error downloading {file_name}: {str(e)}")
            if domain_idx < len(domains_to_try) - 1:
                continue
            else:
                return None
    
    return None

def create_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.sk/',
    })
    return session

def get_url_data(url):
    parsed_url = urlparse(url)
    return {'file_name': os.path.basename(parsed_url.path), 'extension': os.path.splitext(parsed_url.path)[1], 'hostname': parsed_url.hostname}

def get_and_prepare_download_path(custom_path, album_name):

    final_path = 'downloads' if custom_path is None else custom_path
    final_path = os.path.join(final_path, album_name) if album_name is not None else 'downloads'
    final_path = final_path.replace('\n', '')

    if not os.path.isdir(final_path):
        os.makedirs(final_path)

    already_downloaded_path = os.path.join(final_path, 'already_downloaded.txt')
    if not os.path.isfile(already_downloaded_path):
        open(already_downloaded_path, 'w', encoding='utf-8').close()

    return final_path

def write_url_to_list(item_url, download_path):

    list_path = os.path.join(download_path, 'url_list.txt')

    with open(list_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    return

def get_already_downloaded_url(download_path):

    file_path = os.path.join(download_path, 'already_downloaded.txt')

    if not os.path.isfile(file_path):
        return []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read().splitlines()

def mark_as_downloaded(item_url, download_path):

    file_path = os.path.join(download_path, 'already_downloaded.txt')
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    return

def remove_illegal_chars(string):
    return re.sub(r'[<>:"/\\|?*\']|[\0-\31]', "-", string).strip()

def get_encryption_data(slug=None):
    global session
    if session is None:
        session = create_session()
    
    try:
        r = session.post(BUNKR_VS_API_URL_FOR_SLUG, json={'slug': slug}, timeout=10)
        if r.status_code != 200:
            print(f"\t\t[-] HTTP ERROR {r.status_code} getting encryption data for slug: {slug}")
            return None
        
        return json.loads(r.content)
    except Exception as e:
        print(f"\t\t[-] Error getting encryption data: {str(e)}")
        return None

def decrypt_encrypted_url(encryption_data):

    if encryption_data is None:
        return None
    
    try:
        secret_key = f"{SECRET_KEY_BASE}{floor(encryption_data['timestamp'] / 3600)}"
        encrypted_url_bytearray = list(b64decode(encryption_data['url']))
        secret_key_byte_array = list(secret_key.encode('utf-8'))

        decrypted_url = ""

        for i in range(len(encrypted_url_bytearray)):
            decrypted_url += chr(encrypted_url_bytearray[i] ^ secret_key_byte_array[i % len(secret_key_byte_array)])

        return decrypted_url
    except Exception as e:
        print(f"\t\t[-] Error decrypting URL: {str(e)}")
        return None

def date_argument(date_string):
    try:
        return datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid date format. Use: yyyy-mm-ddThh:mm:ss")
    
def is_date_in_range(date_string, date_before, date_after):
    try:
        bunkr_date = datetime.strptime(date_string, '%H:%M:%S %d/%m/%Y')
        date_before = datetime.max if date_before is None else date_before
        date_after = datetime.min if date_after is None else date_after

        return bunkr_date <= date_before and bunkr_date >= date_after

    except ValueError:
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
