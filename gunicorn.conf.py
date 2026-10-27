"""Production gunicorn settings: gunicorn -c gunicorn.conf.py --chdir src control_gate_api:app"""

import os

import gunicorn.http.wsgi

# gunicorn always sends a Server header; hide its name and version.
gunicorn.http.wsgi.SERVER = "app"

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
# Discovery progress and job guards live in process memory, so keep one
# worker process and use threads for concurrency.
workers = 1
threads = int(os.getenv("GUNICORN_THREADS", "8"))
timeout = 120
