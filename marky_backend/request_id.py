import contextvars
import logging
import re
import uuid

from django.core.signals import request_finished

_request_id = contextvars.ContextVar('request_id', default='-')
_VALID_ID = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
HEADER = 'X-Request-ID'


def get_request_id():
    return _request_id.get()


class RequestIdMiddleware:
    """Acepta/genera `X-Request-ID`, lo expone a los logs (contextvar) y lo devuelve en la respuesta."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get('HTTP_X_REQUEST_ID', '')
        rid = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        request.request_id = rid
        # No se restablece aquí: Django registra los 4xx/5xx de `django.request` después de salir de la
        # cadena de middleware. Se limpia en `request_finished` (ver abajo).
        _request_id.set(rid)
        response = self.get_response(request)
        response[HEADER] = rid
        return response


def _clear_request_id(**kwargs):
    _request_id.set('-')


request_finished.connect(_clear_request_id, dispatch_uid='marky_clear_request_id')


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = get_request_id()
        return True
