from django.db import connection
from django.http import JsonResponse


def healthz(request):
    """Liveness: el proceso responde. No toca la base de datos."""
    return JsonResponse({'status': 'ok'})


def readyz(request):
    """Readiness: la base de datos responde."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})
