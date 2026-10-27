import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

import net_guard

CRAWL_TIMEOUT = 6

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
    try:
        parsed_base = urllib.parse.urlparse(target_url)
        base_domain = f"{parsed_base.scheme}://{parsed_base.netloc}"
    except ValueError:
        return None

    for path in sub_paths:
        test_url = f"{base_domain.rstrip('/')}{path}"
        try:
            print(f"   [EMAIL HUNTER] Crawling target node: {test_url}")
            response = net_guard.safe_get(test_url, timeout=CRAWL_TIMEOUT)
        except net_guard.UnsafeTarget as error:
            print(f"    -> [BLOCKED] {error}")
            return None
        except (requests.ConnectionError, requests.Timeout):
            # The site is unreachable; the remaining paths share the same host.
            if not path:
                return None
            continue
        except (requests.RequestException, net_guard.FetchError):
            continue
        if response.status_code >= 400 or not (response.is_html or "text/plain" in response.content_type):
            continue
        html_content = response.text

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

    return None