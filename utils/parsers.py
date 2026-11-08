import re

from drf_nested_forms.parsers import NestedMultiPartParser
from rest_framework.parsers import DataAndFiles

# Claves de texto libre (de primer nivel, incl. allergens/featured_ingredients, o dentro de variants/addons) cuyo valor
# nunca debe coercionarse: drf_nested_forms convierte "007" en 7, "true" en True
# y "null" en None cuando el request trae claves anidadas.
_TEXT_KEY = re.compile(r'^(?:(?:variants|addons)\[(?P<index>\d+)\]\[(?P<nested>name|description)\]|(?P<top>name|description|allergens|featured_ingredients))$')
_NESTED_LISTS = ('variants', 'addons')


class TextPreservingNestedMultiPartParser(NestedMultiPartParser):
    """NestedMultiPartParser que devuelve los textos libres tal como se enviaron (F-36)."""

    def parse(self, stream, media_type=None, parser_context=None):
        result = super().parse(stream, media_type, parser_context)
        original = getattr(self, '_full_data', None)
        if original is None or isinstance(result, DataAndFiles):
            return result  # sin claves anidadas: no hubo coerción
        for key in original:
            match = _TEXT_KEY.match(key)
            if not match:
                continue
            raw = original[key]
            if not isinstance(raw, str):
                continue
            if match.group('top'):
                result[key] = raw
            else:
                prefix = key.split('[', 1)[0]
                rows = result.get(prefix)
                index = int(match.group('index'))
                if isinstance(rows, list) and index < len(rows) and isinstance(rows[index], dict):
                    rows[index][match.group('nested')] = raw
        return result
