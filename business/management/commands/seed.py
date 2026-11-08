from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group
from business.models import BusinessCategory, Currency
from post_office.models import EmailTemplate


CURRENCIES = [
    {"code": "PYG", "name": "Guaraní Paraguayo"},
    {"code": "VES", "name": "Bolívar Venezolano"},
    {"code": "USD", "name": "Dólar Americano"},
]

BUSINESS_CATEGORIES = [
    "Restaurante",
    "Pizzería",
    "Cafetería",
    "Heladería",
    "Parrillada / Asados",
    "Panadería",
    "Pastelería",
]

GROUPS = ["business"]

EMAIL_TEMPLATES = [
    {
        "name": "verify_email",
        "subject": "[Marky] Verificación de Correo",
        "content": "",
        "html_content": """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Verificación de correo - Marky</title>
</head>
<body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #f4f4f4;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #f4f4f4; padding: 20px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" max-width="600px" cellspacing="0" cellpadding="0" border="0" style="background-color: #ffffff; border-radius: 10px; padding: 20px; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                    <tr>
                        <td align="center" style="padding: 20px;">
                            <h1 style="font-size: 24px; margin: 0; color: #000;">Verificación de correo</h1>
                            <p style="font-size: 16px; color: #555; font-style: italic;">Tu seguridad es importante</p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 10px 30px;">
                            <p style="font-size: 14px; color: #333; text-align: center;">
                                Gracias por registrarte con nosotros, por favor verifica tu email clickeando en el link debajo.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 10px 30px;">
                            <p style="font-size: 16px; color: #000; font-weight: bold;">
                                Tu código de verificación es: <strong>{{ verification_code }}</strong>
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 20px;">
                            <a href="{{ verification_link }}" style="background-color: #2b63d9; color: #ffffff; padding: 12px 25px; text-decoration: none; font-size: 16px; font-weight: bold; border-radius: 5px; display: inline-block;">
                                Verificar Email
                            </a>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 10px 30px;">
                            <p style="font-size: 14px; color: #333; text-align: center;">
                                Si no creaste una cuenta, no realices ninguna acción.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="border-top: 1px solid #ddd; padding: 20px;">
                            <p style="font-size: 12px; color: #666;">©2025 Marky</p>
                            <p style="font-size: 12px; color: #666;">Crea, gestiona, comparte y muestra tus hermosas creaciones.</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>""",
    },
    {
        "name": "password_recovery",
        "subject": "[Marky] Recuperación de contraseña",
        "content": "",
        "html_content": """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Cambio de contraseña - Marky</title>
</head>
<body style="margin: 0; padding: 0; font-family: Arial, sans-serif; background-color: #f4f4f4;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color: #f4f4f4; padding: 20px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" max-width="600px" cellspacing="0" cellpadding="0" border="0" style="background-color: #ffffff; border-radius: 10px; padding: 20px; box-shadow: 0 0 10px rgba(0,0,0,0.1);">
                    <tr>
                        <td align="center" style="padding: 20px;">
                            <h1 style="font-size: 24px; margin: 0; color: #000;">Cambio de contraseña</h1>
                            <p style="font-size: 16px; color: #555; font-style: italic;">Protege tu cuenta</p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 10px 30px;">
                            <p style="font-size: 14px; color: #333; text-align: center;">
                                Nosotros recibimos una solicitud de recuperación de contraseña. Por favor haz click en el botón de abajo para iniciar el cambio.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 20px;">
                            <a href="{{ verification_link }}" style="background-color: #2b63d9; color: #ffffff; padding: 12px 25px; text-decoration: none; font-size: 16px; font-weight: bold; border-radius: 5px; display: inline-block;">
                                Resetear contraseña
                            </a>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 10px 30px;">
                            <p style="font-size: 14px; color: #333; text-align: center;">
                                Si tú no realizaste esta solicitud, puedes ignorar este correo.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 10px;">
                            <p style="font-size: 14px; color: #333;">Muchas gracias,</p>
                            <p style="font-size: 14px; font-weight: bold; color: #000;">El equipo Marky</p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="border-top: 1px solid #ddd; padding: 20px;">
                            <p style="font-size: 12px; color: #666;">©2025 Marky</p>
                            <p style="font-size: 12px; color: #666;">Crea, gestiona, comparte y muestra tus hermosas creaciones.</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>""",
    },
    {
        "name": "account_exists",
        "subject": "[Marky] Ya tienes una cuenta",
        "content": "Alguien intentó registrar una cuenta con este correo, pero ya tienes una en Marky. "
                   "Inicia sesión en {{ login_link }}; si olvidaste tu contraseña, usa la opción de recuperación. "
                   "Si no fuiste tú, puedes ignorar este correo.",
        "html_content": """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Ya tienes una cuenta - Marky</title></head>
<body style="margin: 0; padding: 20px; font-family: Arial, sans-serif; background-color: #f4f4f4;">
    <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; padding: 30px; text-align: center;">
        <h1 style="font-size: 24px; margin: 0 0 16px; color: #000;">Ya tienes una cuenta</h1>
        <p style="font-size: 14px; color: #333;">
            Alguien intentó registrar una cuenta en Marky con este correo, pero ya tienes una.
            Puedes iniciar sesión o, si olvidaste tu contraseña, recuperarla desde la pantalla de acceso.
        </p>
        <p style="margin: 24px 0;">
            <a href="{{ login_link }}" style="background-color: #2b63d9; color: #ffffff; padding: 12px 25px; text-decoration: none; font-size: 16px; font-weight: bold; border-radius: 5px; display: inline-block;">Iniciar sesión</a>
        </p>
        <p style="font-size: 14px; color: #333;">Si no fuiste tú, puedes ignorar este correo.</p>
        <p style="font-size: 12px; color: #666; border-top: 1px solid #ddd; padding-top: 16px;">©2025 Marky</p>
    </div>
</body>
</html>""",
    },
]


class Command(BaseCommand):
    help = "Seed reference data: auth groups, currencies, business categories, email templates. Safe to rerun."

    def handle(self, *args, **kwargs):
        self._seed_groups()
        self._seed_currencies()
        self._seed_categories()
        self._seed_email_templates()
        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def _seed_groups(self):
        for name in GROUPS:
            _, created = Group.objects.get_or_create(name=name)
            if created:
                self.stdout.write(f"  [+] group: {name}")
            else:
                self.stdout.write(f"  [=] group already exists: {name}")

    def _seed_currencies(self):
        for data in CURRENCIES:
            _, created = Currency.objects.get_or_create(
                code=data["code"],
                defaults={"name": data["name"]},
            )
            if created:
                self.stdout.write(f"  [+] currency: {data['code']}")
            else:
                self.stdout.write(f"  [=] currency already exists: {data['code']}")

    def _seed_categories(self):
        for name in BUSINESS_CATEGORIES:
            _, created = BusinessCategory.objects.get_or_create(name=name)
            if created:
                self.stdout.write(f"  [+] category: {name}")
            else:
                self.stdout.write(f"  [=] category already exists: {name}")

    def _seed_email_templates(self):
        for data in EMAIL_TEMPLATES:
            _, created = EmailTemplate.objects.get_or_create(
                name=data["name"],
                language="",
                defaults={
                    "subject": data["subject"],
                    "content": data["content"],
                    "html_content": data["html_content"],
                },
            )
            if created:
                self.stdout.write(f"  [+] email template: {data['name']}")
            else:
                self.stdout.write(f"  [=] email template already exists: {data['name']}")
