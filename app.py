"""
Desktop launcher for AsuraTECH Lead Gen.
Starts the Flask server in a background thread, then opens a native
OS window (Edge WebView2 on Windows) pointing at it.

Run:  python app.py
Build to .exe:  run build.bat
"""

import sys
import os
import threading
import time

# --------------------------------------------------------------------------
# When packaged with PyInstaller, all source files land inside sys._MEIPASS.
# We point the import path there; when running normally it's just the src/ dir.
# --------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SRC_DIR = os.path.join(BASE_DIR, 'src')
sys.path.insert(0, SRC_DIR)

import webview
import requests as _req  # alias to avoid shadowing Flask's internal imports

SERVER_URL = 'http://127.0.0.1:5000'
STARTUP_TIMEOUT = 20  # seconds to wait for Flask before giving up


def _wait_for_flask(url: str, timeout: int) -> bool:
    """Polls the Flask server until it responds or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _req.get(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _start_flask():
    """Runs the Flask dev server (reloader disabled — we manage the process)."""
    from control_gate_api import app
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)


if __name__ == '__main__':
    # Start Flask in a daemon thread so it dies when the window closes
    flask_thread = threading.Thread(target=_start_flask, daemon=True)
    flask_thread.start()

    print('[LAUNCHER] Waiting for Flask to initialize...')
    if not _wait_for_flask(SERVER_URL, STARTUP_TIMEOUT):
        print('[LAUNCHER ERROR] Flask failed to start within timeout. Exiting.')
        sys.exit(1)

    print('[LAUNCHER] Server ready — opening desktop window.')
    window = webview.create_window(
        title='AsuraTECH Lead Gen',
        url=SERVER_URL,
        width=1440,
        height=900,
        resizable=True,
        min_size=(1024, 700),
        text_select=True,
    )
    # gui=None lets pywebview pick the best backend for the OS
    # On Windows 10/11 this is Edge WebView2 (already installed)
    webview.start(debug=False)
