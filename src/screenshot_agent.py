import time

import net_guard
import r2_storage


def chrome_options(proxy_url):
    from selenium.webdriver.chrome.options import Options
    options = Options()
    # Chrome keeps its sandbox: the app must run as a non-root user (see README).
    for argument in (
        "--headless=new",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--window-size=1280,800",
        f"--proxy-server={proxy_url}",
        # Loopback normally bypasses the proxy; force it through the filter too.
        "--proxy-bypass-list=<-loopback>",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
        "--no-first-run",
        "--disable-extensions",
        "--disable-background-networking",
    ):
        options.add_argument(argument)
    return options


def capture_business_site_snapshot(target_url, google_place_id=None):
    """Captures a prospect's website and stores it privately in Cloudflare R2.

    Returns the R2 object key, or None when storage is not configured, the URL
    is not a public website, or the capture fails.
    """
    if not target_url or not str(target_url).startswith('http'):
        return None
    if not r2_storage.configured():
        return None
    try:
        net_guard.check_url(target_url)
    except net_guard.UnsafeTarget as error:
        print(f"    ⚠️ [SCREENSHOT SKIPPED] {error}")
        return None

    from selenium import webdriver
    driver = None
    try:
        print(f"   📸 [SCREENSHOT AGENT] Capturing: {target_url}")
        driver = webdriver.Chrome(options=chrome_options(net_guard.shared_proxy().url))
        driver.set_page_load_timeout(10)
        driver.get(target_url)
        time.sleep(2)
        if not str(driver.current_url).startswith(("http://", "https://")):
            return None
        key = r2_storage.upload_png(driver.get_screenshot_as_png())
        print("    -> [SUCCESS] Snapshot stored.")
        return key
    except Exception as e:
        print(f"    ⚠️ [SCREENSHOT FAILURE] Could not render page capture: {e}")
        return None
    finally:
        if driver:
            driver.quit()
