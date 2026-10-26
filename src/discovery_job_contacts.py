import ipaddress
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from discovery_job_filters import normalize_text
from email_hunter import extract_emails_from_html
from scrapingdog_client import fetch as scrapingdog_fetch
serpapi_fetch = scrapingdog_fetch


_SKIP_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "monster.com", "careerbuilder.com", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "wikipedia.org", "yelp.com", "instagram.com", "google.com",
    "crunchbase.com", "bloomberg.com", "wellfound.com", "builtinnyc.com",
    "built.in", "lever.co", "greenhouse.io", "workday.com", "bamboohr.com",
    "recruitee.com", "gmail.com", "outlook.com", "yahoo.com",
}


def web_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"} or not host or "." not in host
        or parsed.username or parsed.password or port not in {None, 80, 443}
        or any(character.isspace() or ord(character) < 32 for character in value)
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        pass
    return value


def website_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _company_evidence(company: str, text: str) -> bool:
    name = normalize_text(company)
    return len(name) >= 3 and f" {name} " in f" {normalize_text(text)} "


def _fetch_page(url: str, host: str):
    for _ in range(3):
        response = requests.get(url, timeout=8, allow_redirects=False,
                                headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code in {301, 302, 303, 307, 308}:
            redirected = web_url(urljoin(url, response.headers.get("Location", "")))
            if not redirected or website_host(redirected) != host:
                raise ValueError("Website redirected outside the matched host")
            url = redirected
            continue
        if response.status_code in {404, 410}:
            return None
        response.raise_for_status()
        if "html" not in response.headers.get("Content-Type", "").lower():
            raise ValueError("Company page did not return HTML")
        return url, BeautifulSoup(response.text, "html.parser")
    raise ValueError("Too many company-page redirects")


def find_company_contact(company: str, location: str, api_key: str, log) -> dict:
    if not api_key:
        raise RuntimeError("Company contact lookup requires SERPAPI_API_KEY / SCRAPINGDOG_API_KEY.")
    evidence = {
        "status": "unconfirmed", "checked_at": datetime.now(timezone.utc).isoformat(),
        "email_status": "unconfirmed",
        "matching_basis": "Company name in search title and website title/heading",
        "emails": [], "phones": [], "pages_checked": [],
    }
    result = {"website_url": None, "email": None, "contact_evidence": evidence, "errors": []}
    query_str = f'"{company}" official website contact {location}'.strip()
    response = serpapi_fetch({
        "engine": "google", "query": query_str, "q": query_str,
        "api_key": api_key, "num": 5,
    }, timeout=20, log=log, label=f"Contact search for '{company}'")
    data = response.json()
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError(data.get("error") if isinstance(data, dict) else "Invalid company search response")
    organic = data.get("organic_results", [])
    if not isinstance(organic, list):
        raise ValueError("Invalid company search results")
    fetched = set()
    for item in organic[:5]:
        if not isinstance(item, dict):
            raise ValueError("Invalid company search result")
        url = web_url(item.get("link"))
        title = item.get("title") or ""
        if not isinstance(title, str):
            raise ValueError("Company result title must be text")
        if not url or not _company_evidence(company, title):
            log(f"[CONTACT MATCH] Skipped search candidate without company-name evidence: {item.get('link')}")
            continue
        host = website_host(url)
        if host in fetched or any(host == domain or host.endswith("." + domain) for domain in _SKIP_DOMAINS):
            continue
        fetched.add(host)
        if len(fetched) > 3:
            break
        try:
            page = _fetch_page(url, host)
            if page is None:
                log(f"[CONTACT MATCH] Company candidate page unavailable: {url}")
                continue
            page_url, soup = page
            page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
            headings = " ".join(heading.get_text(" ", strip=True) for heading in soup.find_all("h1"))
            if not (_company_evidence(company, page_title) or _company_evidence(company, headings)):
                log(f"[CONTACT MATCH] Skipped {url}: company name absent from page title/heading.")
                continue
        except (requests.RequestException, ValueError) as error:
            message = f"Company page {url}: {error}"
            log(f"[CONTACT ERROR] {message}")
            result["errors"].append(message)
            continue
        result["website_url"] = page_url
        evidence.update({
            "status": "matched", "website_source_url": page_url,
            "search_result_url": url, "page_title": page_title,
        })
        pages = [(page_url, soup)]
        origin = urlsplit(page_url)
        base = f"{origin.scheme}://{origin.netloc}"
        for path in ("/contact", "/about", "/contact-us"):
            contact_url = base + path
            if contact_url == page_url:
                continue
            try:
                contact_page = _fetch_page(contact_url, host)
                if contact_page:
                    pages.append(contact_page)
            except (requests.RequestException, ValueError) as error:
                message = f"Contact page {contact_url}: {error}"
                log(f"[CONTACT ERROR] {message}")
                result["errors"].append(message)
        for source_url, document in pages:
            evidence["pages_checked"].append(source_url)
            for tag in document(["script", "style", "noscript"]):
                tag.decompose()
            text = document.get_text(" ", strip=True) + " " + " ".join(
                anchor.get("href", "") for anchor in document.find_all("a", href=True)
                if anchor.get("href", "").lower().startswith("mailto:")
            )
            for email in sorted(extract_emails_from_html(text)):
                email_host = email.rsplit("@", 1)[1]
                if email_host != host or email.split("@")[0] in {"noreply", "no-reply", "example", "test"}:
                    log(f"[CONTACT MATCH] Ignored off-domain or placeholder email on {source_url}.")
                    continue
                if not any(entry["email"] == email for entry in evidence["emails"]):
                    evidence["emails"].append({"email": email, "source_url": source_url})
            for anchor in document.find_all("a", href=True):
                href = anchor.get("href", "")
                if href.lower().startswith("tel:"):
                    phone = href[4:].split("?")[0].strip()
                    if phone and not any(entry["phone"] == phone for entry in evidence["phones"]):
                        evidence["phones"].append({"phone": phone, "source_url": source_url})
        if evidence["emails"]:
            result["email"] = evidence["emails"][0]["email"]
            evidence["email_source_url"] = evidence["emails"][0]["source_url"]
            evidence["email_status"] = "public_domain_match"
        log(f"[CONTACT MATCH] Public website matched for {company}; mailbox deliverability is not verified.")
        return result
    log(f"[CONTACT MATCH] No confirmed public website for {company}; contact fields left empty.")
    return result
