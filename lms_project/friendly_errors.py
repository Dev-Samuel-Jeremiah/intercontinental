"""
lms_project/friendly_errors.py
──────────────────────────────
When a form is sent with something the database will not take (a name that is
too long, a number that already exists, a required box left empty, a value in
the wrong format), send the person back to the page with a plain message
instead of the "Server error" page, which makes people think the site is down.

Only for forms (POST and the like): a page that fails to open is a real fault
and still gets the server error page. Every case is logged with a reference,
so it can still be looked into.
"""
import logging
import re
import uuid

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError
from django.http import HttpResponseRedirect, JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme

from .error_views import _wants_json

logger = logging.getLogger('django.request')

FORM_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def explain(exception):
    """A message for the person who sent the form, or None if this is not an input problem."""
    text = str(exception)
    low = text.lower()
    if isinstance(exception, ValidationError):
        return ' '.join(exception.messages) or 'Some of what you entered is not valid. Check it and try again.'
    if isinstance(exception, DataError):
        size = re.search(r'character varying\((\d+)\)', low)
        if size:
            return (f'Something you typed is too long: that box takes at most {size.group(1)} characters. '
                    'Shorten it and try again.')
        if 'out of range' in low or 'too large' in low:
            return 'A number you entered is too large. Check it and try again.'
        return ('One of the values you entered is not in the right format (for example a date or a number). '
                'Check what you typed and try again.')
    if isinstance(exception, IntegrityError):
        if 'unique' in low or 'duplicate' in low:
            return ('That already exists: something with the same name or number is already saved. '
                    'Use a different one, or change the one that is already there.')
        if 'not null' in low or 'null value' in low:
            return 'A required box was left empty. Fill in every required box and try again.'
        if 'foreign key' in low:
            return ('This could not be saved because it is linked to something that no longer exists, '
                    'or other records still depend on it.')
        return 'This could not be saved because it clashes with a record already in the system.'
    if type(exception) is ValueError:
        return ('One of the values you entered is not in the right format (for example letters where a number '
                'is expected). Check what you typed and try again.')
    return None


def _back(request):
    """The page the form was on: the referring page if it is on this site, otherwise the same address."""
    referer = request.META.get('HTTP_REFERER', '')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
        return referer
    return request.get_full_path()


class FriendlyFormErrorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if request.method not in FORM_METHODS:
            return None
        message = explain(exception)
        if message is None:
            return None
        ref = uuid.uuid4().hex[:8].upper()
        tenant = getattr(request, 'tenant', None)
        logger.warning('Form refused ref=%s path=%s school=%s: %s', ref, request.path,
                       getattr(tenant, 'subdomain', '-'), exception, exc_info=True)
        if _wants_json(request):
            return JsonResponse({'ok': False, 'status': 'invalid', 'message': message, 'detail': message,
                                 'reference': ref}, status=400)
        try:
            messages.error(request, f'{message} (Reference {ref}.)')
        except Exception:                  # no message storage (e.g. a request without a session)
            return None
        return HttpResponseRedirect(_back(request))
