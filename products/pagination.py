from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class AllResultsPagination(PageNumberPagination):
    """Devuelve TODOS los resultados en una sola "página" (F-28).

    Mantiene el sobre `{count, next, previous, results}` que el frontend ya lee
    (`.results`), pero sin truncar por `PAGE_SIZE`. Pensado para las listas de
    categorías, acotadas por `settings.MAX_CATEGORIES_PER_BUSINESS`. Ignora
    `?page` y `?page_size`.
    """

    def paginate_queryset(self, queryset, request, view=None):
        self.request = request
        self._items = list(queryset)
        return self._items

    def get_paginated_response(self, data):
        return Response({
            'count': len(self._items),
            'next': None,
            'previous': None,
            'results': data,
        })

    def get_paginated_response_schema(self, schema):
        return {
            'type': 'object',
            'required': ['count', 'results'],
            'properties': {
                'count': {'type': 'integer'},
                'next': {'type': 'string', 'nullable': True},
                'previous': {'type': 'string', 'nullable': True},
                'results': schema,
            },
        }
