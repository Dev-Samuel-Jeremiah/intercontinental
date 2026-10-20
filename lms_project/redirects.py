"""Redirects to an address the user supplied (?next=, a form's "next"): only ever within this site."""
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def is_safe(request, url):
    return bool(url) and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                                         require_https=request.is_secure())


def safe_redirect(request, url, default):
    """Redirect to `url` if it stays on this site, otherwise to `default` (a URL or a URL name)."""
    return redirect(url if is_safe(request, url) else default)
