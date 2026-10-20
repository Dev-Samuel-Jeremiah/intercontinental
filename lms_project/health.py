"""/healthz/ — for an uptime monitor (UptimeRobot, Better Stack…). Says only "ok" or "error"."""
import logging

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache

logger = logging.getLogger(__name__)


@never_cache
def healthz(request):
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
    except Exception:
        logger.exception('Health check: the database is not answering')
        return JsonResponse({'status': 'error'}, status=503)
    return JsonResponse({'status': 'ok'})
