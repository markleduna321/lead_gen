import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

def capture_business_site_snapshot(target_url, google_place_id):
    """
    Spins up a headless background browser instance to capture a high-fidelity
    screenshot of a prospect's website, saving it natively to disk storage.
    """
    if not target_url or not str(target_url).startswith('http'):
        return None

    # Setup native local asset tracking paths
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    storage_dir = os.path.join(base_dir, "static", "screenshots")
    os.makedirs(storage_dir, exist_ok=True)
    
    filename = f"snap_{google_place_id}.png"
    destination_path = os.path.join(storage_dir, filename)

    # Configure background execution engine switches
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") # Core silent operation rule
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    
    # Emulate a standard desktop monitor layout framework
    chrome_options.add_argument("--window-size=1280,800")

    driver = None
    try:
        print(f"   📸 [SCREENSHOT AGENT] Launching browser container for: {target_url}")
        driver = webdriver.Chrome(options=chrome_options)
        
        # Set a hard page-load limit to prevent hanging forever on slow servers
        driver.set_page_load_timeout(10)
        driver.get(target_url)
        
        # Allow a brief 2-second window for JavaScript assets or animations to settle
        time.sleep(2)
        
        # Write asset file directly to local server workspace tree
        driver.save_screenshot(destination_path)
        print(f"    -> [SUCCESS] Snapshot saved cleanly: static/screenshots/{filename}")
        return f"/static/screenshots/{filename}"
        
    except Exception as e:
        print(f"    ⚠️ [SCREENSHOT FAILURE] Could not render page capture: {e}")
        return None
    finally:
        if driver:
            driver.quit() # Always terminate process to prevent memory leaks