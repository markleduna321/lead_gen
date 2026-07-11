import re
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup

# Optimized regex pattern to extract standard email formats from raw text blocks
EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

def extract_emails_from_html(text):
    """Scans a block of text and returns a list of unique, lowercase email strings."""
    if not text:
        return []
    found = re.findall(EMAIL_REGEX, text)
    return list(set(email.lower() for email in found if not email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg'))))

def attempt_domain_email_crawl(target_url):
    """
    Attempts to crawl a business domain home page and common sub-pages,
    returning the first verified email string found.
    """
    if not target_url or not str(target_url).startswith('http'):
        return None

    # Common URL variations where local businesses drop contact data handles
    sub_paths = ["", "/contact", "/about", "/contact-us"]
    parsed_base = urllib.parse.urlparse(target_url)
    base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"

    # Use a standard browser User-Agent header to prevent getting blocked by basic firewalls
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for path in sub_paths:
        test_url = f"{base_domain.rstrip('/')}{path}"
        try:
            print(f"   [EMAIL HUNTER] Crawling target node: {test_url}")
            req = urllib.request.Request(test_url, headers=headers)
            
            # Timeout safely at 4 seconds to prevent stalling your discovery queue
            with urllib.request.urlopen(req, timeout=4) as response:
                html_content = response.read().decode('utf-8', errors='ignore')
                
                # Extract from raw page source
                emails = extract_emails_from_html(html_content)
                if emails:
                    print(f"    -> [SUCCESS] Found contact link: {emails[0]}")
                    return emails[0]
                    
                # Secondary pass: Scan anchor 'mailto:' tags explicitly via BeautifulSoup
                soup = BeautifulSoup(html_content, 'html.parser')
                for anchor in soup.find_all('a', href=True):
                    href = anchor['href']
                    if href.startswith('mailto:'):
                        extracted = href.replace('mailto:', '').split('?')[0].strip().lower()
                        if extracted:
                            print(f"    -> [SUCCESS] Found mailto anchor: {extracted}")
                            return extracted
        except Exception:
            # Silently pass on individual path network failures (e.g., 404 on /contact-us)
            continue

    return None