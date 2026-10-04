"""Parsing/validation helpers for the optional informational product fields
(featured ingredients, presentation, allergens, SIN TACC)."""
import json
import math
from decimal import Decimal

from rest_framework import serializers

MAX_FEATURED_INGREDIENTS = 8
MAX_INGREDIENT_LENGTH = 50
MAX_JSON_LENGTH = 5000
# Sane bounds so stored values round-trip through the UI (no exponent notation).
MIN_MEASURE = 0.001
MAX_MEASURE = 1_000_000
MAX_PEOPLE = 1000

ALLERGEN_IDS = (
    'milk', 'egg', 'gluten_cereals', 'tree_nuts', 'peanut', 'soy', 'sesame',
    'fish', 'crustaceans', 'mollusks', 'mustard', 'celery', 'lupin', 'sulfites',
)

AMOUNT_UNITS = {'units': (None,), 'weight': ('g', 'kg'), 'volume': ('ml', 'l')}
DIMENSION_SHAPES = {
    'round': {'required': ('diameterCm',), 'optional': ('heightCm',)},
    'rectangular': {'required': ('lengthCm', 'widthCm'), 'optional': ('heightCm',)},
}
DIMENSION_KEYS = ('diameterCm', 'lengthCm', 'widthCm', 'heightCm')
CELIAC_FLAGS = ('crossContaminationControl', 'glutenFreeGrains', 'certifiedProtocol')


def _is_measure(value):
    """Positive real within [MIN_MEASURE, MAX_MEASURE]; bools are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    # bound ints before math.isfinite, which would overflow on huge ones
    if isinstance(value, int) and abs(value) > MAX_MEASURE:
        return False
    return math.isfinite(value) and MIN_MEASURE <= value <= MAX_MEASURE


def _is_positive_int(value, maximum=MAX_MEASURE):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


def _error(message):
    return serializers.ValidationError(message)


def load_json_value(value):
    """Multipart sends JSON as a string; JSON requests send a real object."""
    if isinstance(value, str):
        if len(value) > MAX_JSON_LENGTH:
            raise _error('JSON demasiado largo.')
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            raise _error('JSON inválido.')
    return value


def _check_keys(obj, allowed, label):
    if not isinstance(obj, dict):
        raise _error(f'{label} debe ser un objeto.')
    unknown = set(obj) - set(allowed)
    if unknown:
        raise _error(f'{label}: claves no permitidas: {", ".join(sorted(unknown))}.')


def _csv_to_list(value):
    return [part.strip() for part in value.split(',') if part.strip()] if value else []


def normalize_featured_ingredients(value):
    """Validate a CSV string (or list) and return the canonical CSV or None."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(',')] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = [str(i).strip() for i in value]
    else:
        raise _error('Formato de ingredientes inválido.')
    if any(not i for i in items):
        raise _error('Los ingredientes no pueden estar vacíos.')
    if any(any(ord(ch) < 32 for ch in i) for i in items):
        raise _error('Los ingredientes no pueden contener caracteres de control.')
    if any(',' in i for i in items):
        raise _error('Los ingredientes no pueden contener comas.')
    if any(len(i) > MAX_INGREDIENT_LENGTH for i in items):
        raise _error(f'Cada ingrediente admite hasta {MAX_INGREDIENT_LENGTH} caracteres.')
    if len({i.casefold() for i in items}) != len(items):
        raise _error('Hay ingredientes duplicados.')
    if len(items) > MAX_FEATURED_INGREDIENTS:
        raise _error(f'Máximo {MAX_FEATURED_INGREDIENTS} ingredientes destacados.')
    return ','.join(items) or None


def normalize_allergens(value):
    if value is None:
        return None
    items = [p.strip() for p in value.split(',')] if isinstance(value, str) and value.strip() else (
        [] if isinstance(value, str) else [str(i).strip() for i in value]
    )
    if any(not i for i in items):
        raise _error('Los alérgenos no pueden estar vacíos.')
    unknown = [i for i in items if i not in ALLERGEN_IDS]
    if unknown:
        raise _error(f'Alérgenos desconocidos: {", ".join(unknown)}.')
    if len(set(items)) != len(items):
        raise _error('Hay alérgenos duplicados.')
    ordered = [a for a in ALLERGEN_IDS if a in items]
    return ','.join(ordered) or None


def validate_presentation(value):
    value = load_json_value(value)
    if value is None:
        return None
    _check_keys(value, ('version', 'amount', 'dimensions', 'approximateYield'), 'presentation')
    if type(value.get('version')) is not int or value['version'] != 1:
        raise _error('presentation.version debe ser 1.')
    result = {'version': 1, 'amount': None, 'dimensions': None, 'approximateYield': None}

    amount = value.get('amount')
    if amount is not None:
        _check_keys(amount, ('type', 'value', 'unit'), 'amount')
        a_type = amount.get('type')
        if not isinstance(a_type, str) or a_type not in AMOUNT_UNITS:
            raise _error('amount.type inválido.')
        unit = amount.get('unit')
        if not (unit is None or isinstance(unit, str)) or unit not in AMOUNT_UNITS[a_type]:
            raise _error('amount.unit inválido para el tipo de cantidad.')
        qty = amount.get('value')
        if a_type == 'units':
            if not _is_positive_int(qty):
                raise _error('amount.value debe ser un entero positivo.')
        elif not _is_measure(qty):
            raise _error('amount.value debe ser un número positivo.')
        result['amount'] = {'type': a_type, 'value': qty, 'unit': unit}

    dims = value.get('dimensions')
    if dims is not None:
        _check_keys(dims, ('shape',) + DIMENSION_KEYS, 'dimensions')
        shape = dims.get('shape')
        if not isinstance(shape, str) or shape not in DIMENSION_SHAPES:
            raise _error('dimensions.shape inválido.')
        spec = DIMENSION_SHAPES[shape]
        out = {'shape': shape}
        for key in DIMENSION_KEYS:
            v = dims.get(key)
            if key in spec['required']:
                if not _is_measure(v):
                    raise _error(f'dimensions.{key} es requerido y debe ser positivo.')
            elif key in spec['optional']:
                if v is not None and not _is_measure(v):
                    raise _error(f'dimensions.{key} debe ser positivo.')
            elif v is not None:
                raise _error(f'dimensions.{key} no aplica a la forma {shape}.')
            out[key] = v
        result['dimensions'] = out

    yld = value.get('approximateYield')
    if yld is not None:
        _check_keys(yld, ('minPeople', 'maxPeople'), 'approximateYield')
        min_p, max_p = yld.get('minPeople'), yld.get('maxPeople')
        if not _is_positive_int(min_p, MAX_PEOPLE):
            raise _error('approximateYield.minPeople debe ser un entero positivo.')
        if max_p is not None and (not _is_positive_int(max_p, MAX_PEOPLE) or max_p < min_p):
            raise _error('approximateYield.maxPeople debe ser un entero >= minPeople.')
        result['approximateYield'] = {'minPeople': min_p, 'maxPeople': max_p}

    if not any(result[k] for k in ('amount', 'dimensions', 'approximateYield')):
        return None
    return result


def validate_celiac_info(value):
    value = load_json_value(value)
    if value is None:
        return None
    _check_keys(value, ('version',) + CELIAC_FLAGS, 'celiac_info')
    if type(value.get('version')) is not int or value['version'] != 1:
        raise _error('celiac_info.version debe ser 1.')
    out = {'version': 1}
    for flag in CELIAC_FLAGS:
        v = value.get(flag)
        if not isinstance(v, bool):
            raise _error(f'celiac_info.{flag} debe ser booleano.')
        out[flag] = v
    return out
