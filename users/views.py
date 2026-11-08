import hmac
import logging
import secrets

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import generics
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenRefreshView, )

from users.mailing import queue_mail, VERIFICATION_MAIL_TTL
from users.serializers import UserRegisterSerializer, UserLoginSerializer, VerifyEmailSerializer, \
    PasswordRecoverySerializer, PasswordChangeSerializer, ResendVerificationSerializer, UserSerializer, \
    ActiveUserTokenRefreshSerializer, LogoutSerializer

User = get_user_model()
logger = logging.getLogger(__name__)

MAX_VERIFICATION_ATTEMPTS = 5
# Respuesta única para cualquier fallo de verificación (cuenta inexistente, ya verificada, código malo).
INVALID_CODE_RESPONSE = {"message": "Código inválido o expirado."}


def generate_verification_code():
    """Código de 6 dígitos generado con un CSPRNG."""
    return ''.join(secrets.choice('0123456789') for _ in range(6))


REGISTER_OK_MESSAGE = "Cuenta creada con éxito. Se ha enviado una verificación a su email."


def _issue_verification_code(user):
    """Genera y guarda un código nuevo (15 min) y encola el correo de verificación."""
    code = generate_verification_code()
    user.verification_code = code
    user.verification_code_expires_at = timezone.now() + VERIFICATION_MAIL_TTL
    user.verification_attempts = 0
    user.save(update_fields=['verification_code', 'verification_code_expires_at', 'verification_attempts'])
    queue_mail(
        user.email,
        'verify_email',
        {'verification_link': f"{settings.FRONTEND_URL}/verify-email/", 'verification_code': code},
        expires_in=VERIFICATION_MAIL_TTL,
    )


@extend_schema(tags=['Auth'])
class RegisterView(generics.CreateAPIView):
    """Registro sin enumeración (F-21): un correo ya registrado recibe la misma respuesta de éxito
    y el dueño de la cuenta recibe un aviso por correo; no se crea ni modifica nada."""
    serializer_class = UserRegisterSerializer
    permission_classes = [AllowAny]
    throttle_scope = 'auth'

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']

        if User.objects.filter(email__iexact=email).exists():
            queue_mail(email, 'account_exists', {'login_link': f"{settings.FRONTEND_URL}/login"})
            data = serializer.validated_data
            # Un id nulo delataría que el correo ya existe: se devuelve el siguiente id libre,
            # igual que vería un registro nuevo (nada se crea con él).
            last_id = User.objects.order_by('-id').values_list('id', flat=True).first() or 0
            return Response(
                {"message": REGISTER_OK_MESSAGE,
                 "user": {
                     'id': last_id + 1,
                     'business_name': data.get('business_name', ''),
                     'email': email,
                     'phone_number': data.get('phone_number', ''),
                     'has_configuration': False,
                 }},
                status=status.HTTP_201_CREATED
            )

        try:
            with transaction.atomic():
                user = serializer.save()

                business_group = Group.objects.filter(name='business').first()
                if business_group:
                    business_group.user_set.add(user)

                _issue_verification_code(user)
        except Exception:
            logger.exception("Error during user registration")
            return Response(
                {"error": "Ha ocurrido un error inesperado durante el registro."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(
            {"message": REGISTER_OK_MESSAGE, "user": UserSerializer(user).data},
            status=status.HTTP_201_CREATED
        )


@extend_schema(tags=['Auth'])
class LoginView(generics.GenericAPIView):
    serializer_class = UserLoginSerializer
    permission_classes = [AllowAny]
    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        username = serializer.validated_data['username']
        password = serializer.validated_data['password']
        user = authenticate(username=username, password=password)

        if user is None:
            raise AuthenticationFailed(_("Invalid credentials"))

        if not user.is_verified:
            raise AuthenticationFailed(_("User not verified"))

        refresh = RefreshToken.for_user(user)
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': UserSerializer(user).data
        })


@extend_schema(tags=['Auth'])
class CustomTokenRefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    throttle_scope = 'refresh'
    serializer_class = ActiveUserTokenRefreshSerializer


@extend_schema(tags=['Auth'], responses={204: None})
class LogoutView(generics.GenericAPIView):
    """Pone el refresh token en la blacklist. Idempotente: un token inválido o ya revocado también responde 204."""
    permission_classes = [AllowAny]
    throttle_scope = 'auth'
    serializer_class = LogoutSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data['refresh']).blacklist()
        except TokenError:
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Auth'])
class VerifyEmailView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    throttle_scope = 'verify'
    serializer_class = VerifyEmailSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        verification_code = serializer.validated_data['verification_code']

        # Mismo criterio que el registro (email__iexact): no distingue mayúsculas.
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            return Response(INVALID_CODE_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        if user.is_verified:
            return Response(INVALID_CODE_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        stored_code = user.verification_code
        if (not stored_code
                or user.verification_code_expires_at is None
                or user.verification_code_expires_at < now
                or user.verification_attempts >= MAX_VERIFICATION_ATTEMPTS):
            return Response(INVALID_CODE_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        if not hmac.compare_digest(stored_code.encode(), verification_code.encode()):
            user.verification_attempts += 1
            if user.verification_attempts >= MAX_VERIFICATION_ATTEMPTS:
                # Demasiados intentos fallidos: se invalida el código (hay que pedir uno nuevo)
                user.verification_code = None
                user.verification_code_expires_at = None
            user.save(update_fields=['verification_attempts', 'verification_code',
                                     'verification_code_expires_at'])
            return Response(INVALID_CODE_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        user.is_verified = True
        user.verification_code = None
        user.verification_code_expires_at = None
        user.verification_attempts = 0
        user.save(update_fields=['is_verified', 'verification_code', 'verification_code_expires_at',
                                 'verification_attempts'])

        return Response({"message": "Correo verificado con éxito."}, status=status.HTTP_200_OK)


@extend_schema(tags=['Auth'])
class ResendVerificationView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    throttle_scope = 'resend'
    serializer_class = ResendVerificationSerializer

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        # Respuesta idéntica exista o no la cuenta y esté o no verificada (F-21).
        user = User.objects.filter(email__iexact=email, is_verified=False).first()
        if user is not None:
            _issue_verification_code(user)

        return Response(
            {"message": "Se ha enviado un nuevo correo de verificación."},
            status=status.HTTP_200_OK
        )


@extend_schema(tags=['Auth'])
class PasswordRecoveryView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    throttle_scope = 'recovery'
    serializer_class = PasswordRecoverySerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
        return Response({"message": "Se ha enviado un correo de recuperación."}, status=status.HTTP_200_OK)


@extend_schema(tags=['Auth'])
class PasswordChangeView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    throttle_scope = 'password_change'
    serializer_class = PasswordChangeSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": "La contraseña fue cambiada con éxito."}, status=status.HTTP_200_OK)
