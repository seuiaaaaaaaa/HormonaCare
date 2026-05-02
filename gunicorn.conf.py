import os

port = (os.getenv("PORT") or "10000").strip()
bind = f"0.0.0.0:{port}"
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = 4
timeout = 120
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = "info"

print(f"[gunicorn] binding to {bind}", flush=True)
