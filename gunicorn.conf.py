import os

bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = 4
timeout = 120
accesslog = "-"
errorlog = "-"
capture_output = True
loglevel = "debug"

print(f"[gunicorn] binding to {bind}", flush=True)
