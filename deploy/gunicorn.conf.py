"""
Gunicorn settings for the school portal (used by deploy/intercontinental.service).

SQLite allows one writer at a time, so a few workers with a few threads each
is the right shape: plenty for a single school, without writers queueing up.
"""
import multiprocessing

# Nginx talks to Gunicorn over this socket (systemd creates /run/intercontinental)
bind = 'unix:/run/intercontinental/gunicorn.sock'

workers = min(3, multiprocessing.cpu_count() + 1)
threads = 4
worker_class = 'gthread'

# Long enough for result-sheet PDFs and Excel exports; anything slower is a bug
timeout = 120
graceful_timeout = 30
keepalive = 5

# Recycle workers now and then so memory stays steady over months of uptime
max_requests = 1000
max_requests_jitter = 100

# Logs go to journald: journalctl -u intercontinental -f
accesslog = '-'
errorlog = '-'
loglevel = 'info'
proc_name = 'intercontinental'
