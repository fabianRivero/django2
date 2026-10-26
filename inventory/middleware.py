from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class RequireLoginMiddleware:
    #Exige sesión activa para todo request salvo las rutas públicas.

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not self._is_public(request.path):
            from urllib.parse import quote
            login_url = reverse(settings.LOGIN_URL)

            return redirect(f'{login_url}?next={quote(request.get_full_path(), safe="/")}')

        return self.get_response(request)

    def _is_public(self, path: str) -> bool:
        """¿Esta path está en la lista de públicas?"""
        normalized = '/' + path.lstrip('/')

        for public in getattr(settings, 'PUBLIC_URLS', []):
            if normalized == '/' + public.lstrip('/'):
                return True

        for prefix in getattr(settings, 'PUBLIC_URL_PREFIXES', []):
            if normalized.startswith('/' + prefix.lstrip('/')):
                return True

        return False
