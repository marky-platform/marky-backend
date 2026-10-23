import json

from rest_framework import status
from rest_framework.exceptions import APIException, ParseError
from rest_framework.parsers import BaseParser, JSONParser


class PayloadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = 'El cuerpo de la solicitud es demasiado grande.'
    default_code = 'payload_too_large'


def _read_limited(stream, max_bytes):
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PayloadTooLarge()
    return data


class LimitedJSONParser(JSONParser):
    """JSON estándar con tope explícito de tamaño."""

    def parse(self, stream, media_type=None, parser_context=None):
        from django.conf import settings
        data = _read_limited(stream, settings.ANALYTICS_MAX_BODY_BYTES)
        try:
            return json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError(f'JSON inválido: {exc}')


class PlainTextJSONParser(BaseParser):
    """JSON enviado como `text/plain;charset=UTF-8`.

    El tracker usa este content-type (CORS-simple, sin preflight) para que `fetch keepalive`
    y `sendBeacon` sobrevivan al cierre de página entre orígenes distintos.
    """

    media_type = 'text/plain'

    def parse(self, stream, media_type=None, parser_context=None):
        from django.conf import settings
        data = _read_limited(stream, settings.ANALYTICS_MAX_BODY_BYTES)
        try:
            return json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseError(f'JSON inválido: {exc}')
