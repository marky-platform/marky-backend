from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from utils.tests_base import MarkyAPITestCase

User = get_user_model()


class TestRegister(MarkyAPITestCase):

    def setUp(self):
        super().setUp()
        cache.clear()

    @patch('users.views.mail.send')
    def test_register_creates_unverified_user(self, mock_mail):
        response = self.client.post('/api/v1/users/register/', {
            'username': 'newuser',
            'email': 'newuser@test.com',
            'password': 'TestPass123!',
            'business_name': 'My Business',
        })
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='newuser@test.com')
        self.assertFalse(user.is_verified)
        self.assertIsNotNone(user.verification_code)

    @patch('users.views.mail.send')
    def test_register_trims_business_name_whitespace(self, mock_mail):
        response = self.client.post('/api/v1/users/register/', {
            'username': 'trimuser',
            'email': 'trimuser@test.com',
            'password': 'TestPass123!',
            'business_name': '  Dulce Momento  ',
        })
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='trimuser@test.com')
        self.assertEqual(user.business_name, 'Dulce Momento')

    @patch('users.views.mail.send')
    def test_register_adds_user_to_business_group(self, mock_mail):
        self.client.post('/api/v1/users/register/', {
            'username': 'groupuser',
            'email': 'groupuser@test.com',
            'password': 'TestPass123!',
            'business_name': 'My Business',
        })
        user = User.objects.get(email='groupuser@test.com')
        self.assertTrue(user.groups.filter(name='business').exists())

    @patch('users.views.mail.send')
    def test_register_sends_verification_email(self, mock_mail):
        self.client.post('/api/v1/users/register/', {
            'username': 'emailuser',
            'email': 'emailuser@test.com',
            'password': 'TestPass123!',
            'business_name': 'My Business',
        })
        mock_mail.assert_called_once()

    def test_register_duplicate_email_returns_400(self):
        User.objects.create_user(
            username='existing', email='existing@test.com', password='TestPass123!',
        )
        response = self.client.post('/api/v1/users/register/', {
            'username': 'other',
            'email': 'existing@test.com',
            'password': 'TestPass123!',
            'business_name': 'My Business',
        })
        self.assertEqual(response.status_code, 400)

    @patch('users.views.mail.send')
    def test_register_accepts_business_name_at_22_char_boundary(self, mock_mail):
        response = self.client.post('/api/v1/users/register/', {
            'username': 'boundaryuser',
            'email': 'boundaryuser@test.com',
            'password': 'TestPass123!',
            'business_name': 'a' * 22,
        })
        self.assertEqual(response.status_code, 201)

    def test_register_business_name_over_22_chars_returns_400(self):
        response = self.client.post('/api/v1/users/register/', {
            'username': 'toolonguser',
            'email': 'toolonguser@test.com',
            'password': 'TestPass123!',
            'business_name': 'a' * 23,
        })
        self.assertEqual(response.status_code, 400)

    @patch('users.views.mail.send')
    def test_register_weak_password_returns_400_with_password_key(self, mock_mail):
        response = self.client.post('/api/v1/users/register/', {
            'email': 'weak@test.com',
            'password': '12345678',
            'business_name': 'Weak',
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.json())
        self.assertFalse(User.objects.filter(email='weak@test.com').exists())
        mock_mail.assert_not_called()


class TestEmailVerification(MarkyAPITestCase):

    def _make_unverified_user(self, username='unverified', email='unverified@test.com'):
        user = User.objects.create_user(
            username=username, email=email, password='TestPass123!', is_verified=False,
        )
        user.verification_code = '123456'
        user.verification_code_expires_at = timezone.now() + timedelta(minutes=15)
        user.save(update_fields=['verification_code', 'verification_code_expires_at'])
        return user

    def setUp(self):
        super().setUp()
        cache.clear()  # los throttles usan cache; evita interferencia entre tests

    def _verify(self, user, code):
        return self.client.post('/api/v1/users/verify-email/', {
            'email': user.email,
            'verification_code': code,
        })

    def test_code_invalidated_after_five_failures(self):
        user = self._make_unverified_user()
        for _ in range(5):
            self.assertEqual(self._verify(user, '000000').status_code, 400)
            cache.clear()  # aislar el contador de intentos del throttle por IP
        response = self._verify(user, '123456')
        self.assertIn(response.status_code, (400, 429))
        user.refresh_from_db()
        self.assertFalse(user.is_verified)
        self.assertIsNone(user.verification_code)

    def test_correct_code_after_lockout_fails_until_resend(self):
        user = self._make_unverified_user()
        for _ in range(5):
            self._verify(user, '000000')
            cache.clear()
        self.assertNotEqual(self._verify(user, '123456').status_code, 200)

    @patch('users.views.mail.send')
    def test_resend_resets_attempts_counter(self, mock_mail):
        user = self._make_unverified_user()
        for _ in range(3):
            self._verify(user, '000000')
        cache.clear()
        user.refresh_from_db()
        self.assertEqual(user.verification_attempts, 3)
        self.client.post('/api/v1/users/resend-verification/', {'email': user.email})
        user.refresh_from_db()
        self.assertEqual(user.verification_attempts, 0)
        self.assertEqual(self._verify(user, user.verification_code).status_code, 200)

    def test_verify_endpoint_is_throttled(self):
        user = self._make_unverified_user()
        statuses = [self._verify(user, '000000').status_code for _ in range(6)]
        self.assertEqual(statuses[-1], 429)

    def test_code_cannot_be_reused_after_success(self):
        user = self._make_unverified_user()
        self.assertEqual(self._verify(user, '123456').status_code, 200)
        self.assertEqual(self._verify(user, '123456').status_code, 400)
        user.refresh_from_db()
        self.assertIsNone(user.verification_code)

    def test_generated_code_is_six_digits(self):
        from users.views import generate_verification_code
        for _ in range(50):
            code = generate_verification_code()
            self.assertEqual(len(code), 6)
            self.assertTrue(code.isdigit())

    def test_views_do_not_use_random_module(self):
        import users.views as views
        self.assertFalse(hasattr(views, 'random'))

    def test_valid_code_verifies_user(self):
        user = self._make_unverified_user()
        response = self.client.post('/api/v1/users/verify-email/', {
            'email': user.email,
            'verification_code': '123456',
        })
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)

    def test_wrong_code_returns_400(self):
        user = self._make_unverified_user('unv2', 'unv2@test.com')
        response = self.client.post('/api/v1/users/verify-email/', {
            'email': user.email,
            'verification_code': '000000',
        })
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.is_verified)

    def test_expired_code_returns_400(self):
        user = self._make_unverified_user('unv3', 'unv3@test.com')
        user.verification_code_expires_at = timezone.now() - timedelta(minutes=1)
        user.save(update_fields=['verification_code_expires_at'])
        response = self.client.post('/api/v1/users/verify-email/', {
            'email': user.email,
            'verification_code': '123456',
        })
        self.assertEqual(response.status_code, 400)

    def test_already_verified_user_returns_400(self):
        user, _ = self.make_user('already_ver', 'already_ver@test.com')
        response = self.client.post('/api/v1/users/verify-email/', {
            'email': user.email,
            'verification_code': '123456',
        })
        self.assertEqual(response.status_code, 400)

    def test_nonexistent_email_returns_400(self):
        response = self.client.post('/api/v1/users/verify-email/', {
            'email': 'ghost@test.com',
            'verification_code': '123456',
        })
        self.assertEqual(response.status_code, 400)


class TestLogin(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.verified_user, _ = cls.make_user('login_user', 'login@test.com')
        cls.unverified_user = User.objects.create_user(
            username='unverified_login', email='unverified_login@test.com',
            password='TestPass123!', is_verified=False,
        )

    def test_verified_user_receives_tokens(self):
        response = self.client.post('/api/v1/users/login/', {
            'username': 'login_user',
            'password': 'TestPass123!',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    def test_unverified_user_cannot_login(self):
        response = self.client.post('/api/v1/users/login/', {
            'username': 'unverified_login',
            'password': 'TestPass123!',
        })
        self.assertEqual(response.status_code, 401)

    def test_wrong_password_returns_401(self):
        response = self.client.post('/api/v1/users/login/', {
            'username': 'login_user',
            'password': 'WrongPassword!',
        })
        self.assertEqual(response.status_code, 401)


class TestResendVerification(MarkyAPITestCase):

    @patch('users.views.mail.send')
    def test_unverified_user_receives_new_code(self, mock_mail):
        user = User.objects.create_user(
            username='resend_user', email='resend@test.com',
            password='TestPass123!', is_verified=False,
        )
        old_code = '000000'
        user.verification_code = old_code
        user.save(update_fields=['verification_code'])

        response = self.client.post('/api/v1/users/resend-verification/', {'email': user.email})
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertNotEqual(user.verification_code, old_code)
        mock_mail.assert_called_once()

    @patch('users.views.mail.send')
    def test_nonexistent_email_returns_200_without_leaking(self, mock_mail):
        # Security: always return 200 regardless of whether email exists
        response = self.client.post('/api/v1/users/resend-verification/', {'email': 'ghost@test.com'})
        self.assertEqual(response.status_code, 200)
        mock_mail.assert_not_called()

    def test_already_verified_user_returns_400(self):
        user, _ = self.make_user('resend_verified', 'resend_verified@test.com')
        response = self.client.post('/api/v1/users/resend-verification/', {'email': user.email})
        self.assertEqual(response.status_code, 400)


class TestTokenRevocation(MarkyAPITestCase):
    """F-03: revocación de refresh tokens (blacklist, logout, cambio de contraseña, is_active)."""

    REFRESH_URL = '/api/v1/users/token/refresh/'
    LOGOUT_URL = '/api/v1/users/logout/'

    def setUp(self):
        cache.clear()
        self.user, _ = self.make_user('revoke_user', 'revoke@test.com')

    def _login(self):
        response = self.client.post('/api/v1/users/login/', {
            'username': 'revoke_user', 'password': 'TestPass123!',
        })
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_old_refresh_token_rejected_after_rotation(self):
        tokens = self._login()
        first = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
        self.assertEqual(first.status_code, 200)
        self.assertNotEqual(first.data['refresh'], tokens['refresh'])
        replay = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
        self.assertEqual(replay.status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        tokens = self._login()
        response = self.client.post(self.LOGOUT_URL, {'refresh': tokens['refresh']})
        self.assertEqual(response.status_code, 204)
        refreshed = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
        self.assertEqual(refreshed.status_code, 401)

    def test_logout_with_invalid_token_is_idempotent(self):
        response = self.client.post(self.LOGOUT_URL, {'refresh': 'no-es-un-token'})
        self.assertEqual(response.status_code, 204)

    def test_logout_without_token_returns_400(self):
        response = self.client.post(self.LOGOUT_URL, {})
        self.assertEqual(response.status_code, 400)

    def test_password_change_blacklists_all_outstanding_tokens(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        first = self._login()
        second = self._login()
        response = self.client.post('/api/v1/users/password-change/', {
            'uid': urlsafe_base64_encode(force_bytes(self.user.pk)),
            'token': default_token_generator.make_token(self.user),
            'new_password': 'N3wStrongPass!987',
        })
        self.assertEqual(response.status_code, 200)
        for tokens in (first, second):
            refreshed = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
            self.assertEqual(refreshed.status_code, 401)

    def _change_password(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        response = self.client.post('/api/v1/users/password-change/', {
            'uid': urlsafe_base64_encode(force_bytes(self.user.pk)),
            'token': default_token_generator.make_token(self.user),
            'new_password': 'N3wStrongPass!987',
        })
        self.assertEqual(response.status_code, 200)

    def test_access_token_rejected_after_password_change(self):
        # CHECK_REVOKE_TOKEN: el access token emitido antes del cambio deja de valer.
        from rest_framework.test import APIClient

        tokens = self._login()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(client.get('/api/v1/business/account-info/').status_code, 200)

        self._change_password()

        self.assertEqual(client.get('/api/v1/business/account-info/').status_code, 401)

    def test_refresh_token_rejected_after_password_change(self):
        tokens = self._login()
        self._change_password()
        refreshed = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
        self.assertEqual(refreshed.status_code, 401)

    def test_tokens_issued_after_password_change_work(self):
        from rest_framework.test import APIClient

        self._change_password()
        response = self.client.post('/api/v1/users/login/', {
            'username': 'revoke_user', 'password': 'N3wStrongPass!987',
        })
        self.assertEqual(response.status_code, 200)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(client.get('/api/v1/business/account-info/').status_code, 200)
        refreshed = self.client.post(self.REFRESH_URL, {'refresh': response.data['refresh']})
        self.assertEqual(refreshed.status_code, 200)

    def test_refresh_token_of_deleted_user_rejected(self):
        tokens = self._login()
        self.user.delete()
        response = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
        self.assertEqual(response.status_code, 401)

    def test_refresh_token_of_inactive_user_rejected(self):
        # simplejwt 5.5.1 ya rechaza usuarios inactivos; el override de
        # ActiveUserTokenRefreshSerializer protege sobre todo el caso del usuario eliminado.
        tokens = self._login()
        self.user.is_active = False
        self.user.save()
        response = self.client.post(self.REFRESH_URL, {'refresh': tokens['refresh']})
        self.assertEqual(response.status_code, 401)

    def test_refresh_endpoint_is_throttled(self):
        statuses = [
            self.client.post(self.REFRESH_URL, {'refresh': 'invalido'}).status_code
            for _ in range(40)
        ]
        self.assertIn(429, statuses)
