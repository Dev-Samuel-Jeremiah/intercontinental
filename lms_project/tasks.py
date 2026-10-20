"""
lms_project/tasks.py
────────────────────
Run slow work (bulk email/SMS, PDF batches) outside the request/response cycle,
so a bursar sending 600 fee reminders doesn't stare at a spinner until the
request times out.

No extra infrastructure: work runs on a small thread pool inside the web
process. That is enough for a school-sized platform. If you later add Celery
or RQ, only `run_in_background` needs to change.

Threads do not inherit the thread-local current tenant, so pass `tenant=` —
anything the task touches through the tenant-scoped managers depends on it.

    from lms_project.tasks import run_in_background
    run_in_background(send_fee_reminders, channel='BOTH', tenant=request.tenant)

Set RUN_TASKS_EAGERLY=True (tests, management commands) to run inline instead.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections, transaction

logger = logging.getLogger(__name__)

_executor = None
_executor_lock = threading.Lock()


def _get_executor():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=getattr(settings, 'BACKGROUND_WORKERS', 4),
                thread_name_prefix='school-bg',
            )
    return _executor


def _run(func, args, kwargs, tenant, inline=False):
    from tenants.middleware import clear_current_tenant, set_current_tenant

    name = getattr(func, '__name__', str(func))
    # A worker thread tidies its own database connection. Run inline (eagerly) it shares the
    # request's connection, which may be inside a transaction: closing it there would end the
    # transaction (PostgreSQL really closes it).
    if not inline:
        close_old_connections()
    try:
        if tenant is not None:
            set_current_tenant(tenant)
        result = func(*args, **kwargs)
        logger.info('Background task %s finished (school=%s): %s',
                    name, getattr(tenant, 'subdomain', '-'), result)
        return result
    except Exception:
        logger.exception('Background task %s failed (school=%s)',
                         name, getattr(tenant, 'subdomain', '-'))
    finally:
        clear_current_tenant()
        if not inline:
            close_old_connections()


def run_in_background(func, *args, tenant=None, **kwargs):
    """Queue `func` to run after the response is sent.

    Returns the result when running eagerly, otherwise None.
    """
    if getattr(settings, 'RUN_TASKS_EAGERLY', False):
        return _run(func, args, kwargs, tenant, inline=True)
    # Start only once the request's transaction has committed, otherwise the
    # worker thread can look for rows that aren't visible to it yet (and, on
    # SQLite, trip over a locked table). Outside a transaction this runs
    # immediately.
    transaction.on_commit(
        lambda: _get_executor().submit(_run, func, args, kwargs, tenant))
    return None
