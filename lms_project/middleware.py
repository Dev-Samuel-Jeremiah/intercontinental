from django.contrib.auth import logout

class OneSessionPerBrowserMiddleware:
    """
    Middleware to ensure session is valid per browser session.
    Logs out user if session does not exist or is invalid.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only for authenticated users
        if request.user.is_authenticated:
            # Check if session key exists
            if not request.session.session_key:
                logout(request)
        response = self.get_response(request)
        return response
