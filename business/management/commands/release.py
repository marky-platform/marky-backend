from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# Orden importa: el esquema primero, luego la tabla de cache, luego los datos de referencia.
STEPS = (
    ("migrate", {"interactive": False}),
    ("createcachetable", {}),
    ("seed", {}),
)


class Command(BaseCommand):
    help = (
        "Paso de release (pre-deploy): migrate, createcachetable y seed. "
        "Idempotente: seguro de ejecutar en cada deploy."
    )

    def handle(self, *args, **options):
        for name, kwargs in STEPS:
            self.stdout.write(f"[release] {name} ...")
            try:
                call_command(name, **kwargs)
            except Exception as exc:
                raise CommandError(f"[release] {name} FALLÓ: {exc}") from exc
            self.stdout.write(self.style.SUCCESS(f"[release] {name} OK"))
        self.stdout.write(self.style.SUCCESS("[release] completo."))
