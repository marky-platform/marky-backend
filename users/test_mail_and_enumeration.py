"""Fase E: correo encolado (F-17), respuestas uniformes (F-21) y purga de correo (F-44)."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail as django_mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from post_office import settings as po_settings
from post_office.models import Email, EmailTemplate, STATUS

from utils.tests_base import MarkyAPITestCase

User = get_user_model()

REGISTER_URL = '/api/v1/users/register/'
VERIFY_URL = '/api/v1/users/verify-email/'
RESEND_URL = '/api/v1/users/resend-verification/'
RECOVERY_URL = '/api/v1/users/password-recovery/'
LOGIN_URL = '/api/v1/users/login/'


def _register_payload(email='new@test.com'):
    return {'email': email, 'password': 'TestPass123!', 'business_name': 'Mi Negocio'}


class MailTestBase(MarkyAPITestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for name in ('verify_email', 'password_recovery', 'account_exists'):
            EmailTemplate.objects.create(name=name, subject=f'[Marky] {name}', html_content='{{ verification_code }}')

    def setUp(self):
        super().setUp()
        cache.clear()


class QueuedMailTests(MailTestBase):
    def test_register_queues_mail_instead_of_sending_inline(self):
        response = self.client.post(REGISTER_URL, _register_payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(django_mail.outbox), 0)
        email = Email.objects.get()
        self.assertEqual(email.status, STATUS.queued)
        self.assertEqual(email.to, ['new@test.com'])
        self.assertEqual(email.template.name, 'verify_email')

    def test_verification_mail_expires_with_the_code(self):
        self.client.post(REGISTER_URL, _register_payload())
        email = Email.objects.get()
        self.assertIsNotNone(email.expires_at)
        self.assertLessEqual(email.expires_at, timezone.now() + timedelta(minutes=15))

    def test_resend_and_recovery_are_queued(self):
        User.objects.create_user(username='u@test.com', email='u@test.com', password='TestPass123!',
                                 is_verified=False)
        self.client.post(RESEND_URL, {'email': 'u@test.com'})
        self.client.post(RECOVERY_URL, {'email': 'u@test.com'})
        self.assertEqual(sorted(Email.objects.values_list('template__name', flat=True)),
                         ['password_recovery', 'verify_email'])
        self.assertTrue(all(e.status == STATUS.queued for e in Email.objects.all()))
        self.assertEqual(len(django_mail.outbox), 0)

    def test_failing_queue_does_not_break_registration_and_is_logged(self):
        with patch('users.mailing.mail.send', side_effect=RuntimeError('boom')):
            with self.assertLogs('users.mailing', level='ERROR') as logs:
                response = self.client.post(REGISTER_URL, _register_payload())
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email='new@test.com').exists())
        self.assertIn('verify_email', '\n'.join(logs.output))

    def test_failing_queue_does_not_break_resend_and_is_logged(self):
        User.objects.create_user(username='u@test.com', email='u@test.com', password='TestPass123!',
                                 is_verified=False)
        with patch('users.mailing.mail.send', side_effect=RuntimeError('boom')):
            with self.assertLogs('users.mailing', level='ERROR'):
                response = self.client.post(RESEND_URL, {'email': 'u@test.com'})
        self.assertEqual(response.status_code, 200)

    def test_failing_backend_marks_mail_failed_and_logs_on_dispatch(self):
        self.client.post(REGISTER_URL, _register_payload())
        email = Email.objects.get()
        with patch('post_office.models.Email.email_message', side_effect=RuntimeError('resend down')):
            with self.assertLogs('post_office', level='ERROR'):
                email.dispatch(disconnect_after_delivery=False)
        email.refresh_from_db()
        self.assertEqual(email.status, STATUS.failed)

    def test_retry_settings_are_configured(self):
        self.assertGreaterEqual(po_settings.get_max_retries(), 3)
        self.assertGreater(po_settings.get_retry_timedelta(), timedelta(0))
        self.assertEqual(po_settings.get_default_priority(), 'medium')


class PurgeMailCommandTests(MailTestBase):
    def _mail(self, status, age_hours, expires_in_hours=None):
        email = Email.objects.create(to=['a@test.com'], from_email='x@test.com', subject='s', status=status)
        Email.objects.filter(pk=email.pk).update(
            created=timezone.now() - timedelta(hours=age_hours),
            expires_at=None if expires_in_hours is None else timezone.now() + timedelta(hours=expires_in_hours),
        )
        return email

    def test_deletes_old_sent_failed_and_expired_queued_but_keeps_rest(self):
        old_sent = self._mail(STATUS.sent, 48)
        old_failed = self._mail(STATUS.failed, 48)
        expired_queued = self._mail(STATUS.queued, 48, expires_in_hours=-40)
        fresh_sent = self._mail(STATUS.sent, 1)
        pending = self._mail(STATUS.queued, 48)
        call_command('purge_mail', stdout=StringIO())
        remaining = set(Email.objects.values_list('pk', flat=True))
        self.assertEqual(remaining, {fresh_sent.pk, pending.pk})
        self.assertNotIn(old_sent.pk, remaining)
        self.assertNotIn(old_failed.pk, remaining)
        self.assertNotIn(expired_queued.pk, remaining)

    @override_settings(MAIL_RETENTION_HOURS=100)
    def test_uses_retention_setting(self):
        sent = self._mail(STATUS.sent, 48)
        call_command('purge_mail', stdout=StringIO())
        self.assertTrue(Email.objects.filter(pk=sent.pk).exists())


class RegisterEnumerationTests(MailTestBase):
    def setUp(self):
        super().setUp()
        self.existing = User.objects.create_user(
            username='existing@test.com', email='existing@test.com', password='TestPass123!', is_verified=True)

    def test_duplicate_email_gets_same_success_shape(self):
        new = self.client.post(REGISTER_URL, _register_payload('fresh@test.com'))
        dup = self.client.post(REGISTER_URL, _register_payload('existing@test.com'))
        self.assertEqual(dup.status_code, 201)
        self.assertEqual(dup.json()['message'], new.json()['message'])
        self.assertEqual(set(dup.json()['user'].keys()), set(new.json()['user'].keys()))
        self.assertEqual(dup.json()['user']['email'], 'existing@test.com')

    def test_duplicate_email_id_is_not_distinguishable_from_a_new_user(self):
        new = self.client.post(REGISTER_URL, _register_payload('fresh@test.com'))
        cache.clear()
        dup = self.client.post(REGISTER_URL, _register_payload('existing@test.com'))
        self.assertIsInstance(new.json()['user']['id'], int)
        # Un id nulo delataría que el correo ya existe: debe ser un entero, como en un registro nuevo.
        self.assertIsInstance(dup.json()['user']['id'], int)
        self.assertEqual(
            {k: type(v) for k, v in dup.json()['user'].items()},
            {k: type(v) for k, v in new.json()['user'].items()},
        )
        self.assertFalse(User.objects.filter(pk=dup.json()['user']['id']).exists())

    def test_duplicate_email_changes_nothing_and_notifies_owner(self):
        old_hash = self.existing.password
        self.client.post(REGISTER_URL, {**_register_payload('existing@test.com'), 'password': 'Other456!pass'})
        self.existing.refresh_from_db()
        self.assertEqual(self.existing.password, old_hash)
        self.assertEqual(User.objects.filter(email='existing@test.com').count(), 1)
        email = Email.objects.get()
        self.assertEqual(email.template.name, 'account_exists')
        self.assertEqual(email.to, ['existing@test.com'])
        self.assertEqual(len(django_mail.outbox), 0)

    def test_duplicate_email_with_weak_password_still_400_on_password_only(self):
        response = self.client.post(REGISTER_URL, {**_register_payload('existing@test.com'), 'password': '123'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('email', response.json())


class VerifyResendUniformityTests(MailTestBase):
    def setUp(self):
        super().setUp()
        self.verified = User.objects.create_user(
            username='ver@test.com', email='ver@test.com', password='TestPass123!', is_verified=True)
        self.unverified = User.objects.create_user(
            username='unv@test.com', email='unv@test.com', password='TestPass123!', is_verified=False)
        self.unverified.verification_code = '123456'
        self.unverified.verification_code_expires_at = timezone.now() + timedelta(minutes=15)
        self.unverified.save()

    def test_verify_failure_responses_are_identical(self):
        bodies = set()
        for email, code in [('ghost@test.com', '654321'), ('ver@test.com', '654321'),
                            ('unv@test.com', '654321')]:
            cache.clear()
            response = self.client.post(VERIFY_URL, {'email': email, 'verification_code': code})
            self.assertEqual(response.status_code, 400)
            bodies.add(str(response.json()))
        self.assertEqual(len(bodies), 1)

    def test_resend_responses_are_identical_for_all_states(self):
        results = []
        for email in ('ghost@test.com', 'ver@test.com', 'unv@test.com'):
            cache.clear()
            response = self.client.post(RESEND_URL, {'email': email})
            results.append((response.status_code, str(response.json())))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(results[0][0], 200)

    def test_verify_and_resend_match_email_case_insensitively(self):
        cache.clear()
        self.client.post(RESEND_URL, {'email': 'UNV@test.com'})
        self.assertEqual(Email.objects.count(), 1)
        cache.clear()
        self.unverified.refresh_from_db()
        response = self.client.post(
            VERIFY_URL, {'email': 'Unv@Test.com', 'verification_code': self.unverified.verification_code})
        self.assertEqual(response.status_code, 200, response.content)

    def test_resend_only_mails_unverified_users(self):
        self.client.post(RESEND_URL, {'email': 'ver@test.com'})
        self.client.post(RESEND_URL, {'email': 'ghost@test.com'})
        self.assertEqual(Email.objects.count(), 0)
        self.client.post(RESEND_URL, {'email': 'unv@test.com'})
        self.assertEqual(Email.objects.count(), 1)

    def test_recovery_responses_are_identical(self):
        a = self.client.post(RECOVERY_URL, {'email': 'ghost@test.com'})
        cache.clear()
        b = self.client.post(RECOVERY_URL, {'email': 'ver@test.com'})
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))


class LoginUniformityTests(MailTestBase):
    def setUp(self):
        super().setUp()
        User.objects.create_user(username='ver@test.com', email='ver@test.com', password='TestPass123!',
                                 is_verified=True)
        User.objects.create_user(username='unv@test.com', email='unv@test.com', password='TestPass123!',
                                 is_verified=False)

    def _login(self, username, password):
        cache.clear()
        return self.client.post(LOGIN_URL, {'username': username, 'password': password})

    def test_unknown_wrong_password_and_unverified_wrong_password_look_identical(self):
        responses = [
            self._login('ghost@test.com', 'TestPass123!'),
            self._login('ver@test.com', 'Wrong123!'),
            self._login('unv@test.com', 'Wrong123!'),
        ]
        self.assertEqual({r.status_code for r in responses}, {401})
        self.assertEqual(len({str(r.json()) for r in responses}), 1)

    def test_not_verified_only_revealed_after_correct_password(self):
        response = self._login('unv@test.com', 'TestPass123!')
        self.assertEqual(response.status_code, 401)
        self.assertNotEqual(str(response.json()), str(self._login('ghost@test.com', 'x').json()))
