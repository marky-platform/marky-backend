from django.test import TestCase
from utils.tests_base import MarkyAPITestCase
from business.management.commands.seed import Command
from business.models import Currency, BusinessProfile, SocialMediaLink


class TestBusinessProfileTenancy(MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user_a, cls.profile_a = cls.make_user('biz_a', 'biz_a@test.com')
        cls.user_b, cls.profile_b = cls.make_user('biz_b', 'biz_b@test.com')

    def test_user_can_retrieve_own_profile(self):
        client = self.auth_client(self.user_a)
        response = client.get(f'/api/v1/business/business_profile/{self.profile_a.id}/')
        self.assertEqual(response.status_code, 200)

    def test_user_cannot_retrieve_other_profile(self):
        client = self.auth_client(self.user_a)
        response = client.get(f'/api/v1/business/business_profile/{self.profile_b.id}/')
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_request_returns_401(self):
        response = self.client.get(f'/api/v1/business/business_profile/{self.profile_a.id}/')
        self.assertEqual(response.status_code, 401)

    def test_superuser_can_retrieve_any_profile(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        superuser = User.objects.create_superuser(
            username='super', email='super@test.com', password='TestPass123!',
        )
        client = self.auth_client(superuser)
        response = client.get(f'/api/v1/business/business_profile/{self.profile_a.id}/')
        self.assertEqual(response.status_code, 200)


class TestBusinessProfileCreateExchangeDirection(MarkyAPITestCase):
    """Regression test: BusinessProfileWriteSerializer must not silently
    default is_primary_to_secondary to True on creation."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, _ = cls.make_user('biz_new', 'biz_new@test.com', with_profile=False)

    def test_create_without_is_primary_to_secondary_is_rejected(self):
        client = self.auth_client(self.user)
        response = client.post('/api/v1/business/business_profile/', {
            'business_id': 'biz-new',
            'primary_currency': self.primary_currency.id,
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('is_primary_to_secondary', response.data)

    def test_create_persists_explicit_direction_false(self):
        client = self.auth_client(self.user)
        response = client.post('/api/v1/business/business_profile/', {
            'business_id': 'biz-new',
            'primary_currency': self.primary_currency.id,
            'secondary_currency': self.secondary_currency.id,
            'exchange_rate': '6000',
            'is_primary_to_secondary': False,
        }, format='json')
        self.assertEqual(response.status_code, 201)
        profile = BusinessProfile.objects.get(business_id='biz-new')
        self.assertFalse(profile.is_primary_to_secondary)

    def test_partial_update_without_is_primary_to_secondary_still_works(self):
        client = self.auth_client(self.user)
        create_response = client.post('/api/v1/business/business_profile/', {
            'business_id': 'biz-new',
            'primary_currency': self.primary_currency.id,
            'is_primary_to_secondary': True,
        }, format='json')
        self.assertEqual(create_response.status_code, 201)
        profile_id = BusinessProfile.objects.get(business_id='biz-new').id

        response = client.patch(
            f'/api/v1/business/business_profile/{profile_id}/',
            {'business_id': 'biz-new-renamed'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)


class TestBusinessIdReservedWords(MarkyAPITestCase):
    """Regression: business_id doubles as the public profile slug
    (marky.one/<business_id>); it must never collide with a static
    marky-admin route (login, home, ...), or that business's public page
    becomes permanently unreachable (the static route always wins over the
    `/:businessId` catch-all)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, _ = cls.make_user('biz_reserved', 'biz_reserved@test.com', with_profile=False)

    def test_create_rejects_a_reserved_business_id(self):
        client = self.auth_client(self.user)
        response = client.post('/api/v1/business/business_profile/', {
            'business_id': 'home',
            'primary_currency': self.primary_currency.id,
            'is_primary_to_secondary': True,
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('business_id', response.data)

    def test_rename_rejects_a_reserved_business_id(self):
        user, profile = self.make_user('biz_reserved_rename', 'biz_reserved_rename@test.com')
        client = self.auth_client(user)
        response = client.patch('/api/v1/business/account-info/', {
            'business_id': 'login',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        profile.refresh_from_db()
        self.assertEqual(profile.business_id, 'biz_reserved_rename')


class SocialMediaLinksReplaceTests(MarkyAPITestCase):
    """Coverage for POST /business/social-media-links/bulk-update/, the
    full-replacement endpoint backing the Canales modal."""

    URL = '/api/v1/business/social-media-links/bulk-update/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('biz_channels', 'biz_channels@test.com')

    def setUp(self):
        self.client_ = self.auth_client(self.user)

    def test_full_replacement(self):
        SocialMediaLink.objects.create(
            business=self.profile, platform='whatsapp', label='Viejo', url='595981111111'
        )
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'instagram', 'url': 'https://www.instagram.com/dulce_momento'},
                {'platform': 'whatsapp', 'label': 'Pedidos', 'url': '+595 981 234 567'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        links = SocialMediaLink.objects.filter(business=self.profile)
        self.assertEqual(links.count(), 2)
        self.assertFalse(links.filter(label='Viejo').exists())

    def test_three_whatsapps_ok(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'label': 'Pedidos', 'url': '+595 981 111 111'},
                {'platform': 'whatsapp', 'label': 'Atención', 'url': '+595 981 222 222'},
                {'platform': 'whatsapp', 'label': 'Eventos', 'url': '+595 981 333 333'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            SocialMediaLink.objects.filter(business=self.profile, platform='whatsapp').count(), 3
        )

    def test_four_whatsapps_rejected(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'label': 'A', 'url': '+595 981 111 111'},
                {'platform': 'whatsapp', 'label': 'B', 'url': '+595 981 222 222'},
                {'platform': 'whatsapp', 'label': 'C', 'url': '+595 981 333 333'},
                {'platform': 'whatsapp', 'label': 'D', 'url': '+595 981 444 444'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_instagram_url_must_be_valid(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'instagram', 'url': 'not a url at all'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_missing_label_for_multi_entry_platform_rejected(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'url': '+595 981 111 111'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_more_than_three_platforms_rejected(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'instagram', 'url': 'https://www.instagram.com/a'},
                {'platform': 'facebook', 'url': 'https://www.facebook.com/a'},
                {'platform': 'tiktok', 'url': 'https://www.tiktok.com/@a'},
                {'platform': 'whatsapp', 'label': 'Pedidos', 'url': '+595 981 111 111'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_link_url_normalized_without_scheme(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'link', 'label': 'Cómo llegar', 'url': 'maps.google.com/xyz'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        link = SocialMediaLink.objects.get(business=self.profile, platform='link')
        self.assertEqual(link.url, 'https://maps.google.com/xyz')

    def test_whatsapp_normalized_to_digits(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'label': 'Pedidos', 'url': '+595 981 234 567'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        link = SocialMediaLink.objects.get(business=self.profile, platform='whatsapp')
        self.assertEqual(link.url, '595981234567')

    def test_duplicate_url_within_platform_rejected(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'label': 'Pedidos', 'url': '+595 981 234 567'},
                {'platform': 'whatsapp', 'label': 'Otro', 'url': '595981234567'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_order_assigned_by_index(self):
        response = self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'label': 'Primero', 'url': '+595 981 111 111'},
                {'platform': 'whatsapp', 'label': 'Segundo', 'url': '+595 981 222 222'},
            ],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        links = SocialMediaLink.objects.filter(
            business=self.profile, platform='whatsapp'
        ).order_by('order')
        self.assertEqual([l.label for l in links], ['Primero', 'Segundo'])
        self.assertEqual([l.order for l in links], [0, 1])

    def test_single_entry_platform_db_constraint(self):
        from django.db import IntegrityError, transaction

        SocialMediaLink.objects.create(
            business=self.profile, platform='instagram', url='https://www.instagram.com/a'
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SocialMediaLink.objects.create(
                    business=self.profile, platform='instagram', url='https://www.instagram.com/b', order=1
                )

    def test_home_page_returns_label_and_order(self):
        self.client_.post(self.URL, {
            'channels': [
                {'platform': 'whatsapp', 'label': 'Pedidos', 'url': '+595 981 234 567'},
            ],
        }, format='json')
        response = self.client_.get('/api/v1/business/home-page/')
        self.assertEqual(response.status_code, 200)
        link = response.data['social_links'][0]
        self.assertIn('label', link)
        self.assertIn('order', link)
        self.assertEqual(link['label'], 'Pedidos')


class PublicBusinessProfileAPITests(MarkyAPITestCase):
    """Coverage for GET /api/v1/public/business/<business_id>/, the
    unauthenticated business-profile endpoint backing the public page."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('biz_public', 'biz_public@test.com')

    def test_anonymous_client_reaches_the_endpoint(self):
        response = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['business_id'], self.profile.business_id)

    def test_unknown_slug_404s(self):
        response = self.client.get('/api/v1/public/business/does-not-exist/')
        self.assertEqual(response.status_code, 404)

    def test_response_does_not_leak_private_fields(self):
        response = self.client.get(f'/api/v1/public/business/{self.profile.business_id}/')
        self.assertEqual(response.status_code, 200)
        for leaked_field in ('user', 'email', 'phone_number', 'exchange_rate'):
            self.assertNotIn(leaked_field, response.data)

    def test_slug_lookup_is_case_insensitive(self):
        response = self.client.get(f'/api/v1/public/business/{self.profile.business_id.upper()}/')
        self.assertEqual(response.status_code, 200)


class TestSeedCurrencyNames(TestCase):
    """Regression test for the Asana ticket 'Step 3 ... Ajuste de textos':
    currency names must be the full display name shown in the frontend
    dropdown, not the abbreviated form."""

    def test_seed_creates_currencies_with_full_names(self):
        Command()._seed_currencies()
        self.assertEqual(Currency.objects.get(code='PYG').name, 'Guaraní Paraguayo')
        self.assertEqual(Currency.objects.get(code='VES').name, 'Bolívar Venezolano')
        self.assertEqual(Currency.objects.get(code='USD').name, 'Dólar Americano')
