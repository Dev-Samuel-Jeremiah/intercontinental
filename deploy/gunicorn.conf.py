"""
Gunicorn settings for the school portal (used by deploy/intercontinental.service).

PostgreSQL handles many writers at once, so it gets the usual 2 x CPUs + 1
workers. SQLite allows one writer at a time, so it keeps a few workers with a
few threads each: plenty for a single school, without writers queueing up.
"""
import multiprocessing

from decouple import Config, RepositoryEnv

_env = Config(RepositoryEnv(__import__('pathlib').Path(__file__).resolve().parent.parent / '.env'))
_postgres = _env('DB_ENGINE', default='sqlite') == 'postgresql'

# Nginx talks to Gunicorn over this socket (systemd creates /run/intercontinental)
bind = 'unix:/run/intercontinental/gunicorn.sock'

# Each worker uses roughly 150–200 MB of memory; the cap keeps a large machine from running out.
workers = min(9, multiprocessing.cpu_count() * 2 + 1) if _postgres else min(3, multiprocessing.cpu_count() + 1)
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
