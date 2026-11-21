"""Fase D: subidas y almacenamiento (F-14, F-15, F-16, F-18)."""
import io
import tempfile
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import SimpleTestCase, override_settings
from PIL import Image

from products.models import Product, ProductCategory, ProductMedia, ProductVariant
from utils.tests_base import MarkyAPITestCase

URL = '/api/v1/products/products/'


def _png_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='red').save(buf, format='PNG')
    return buf.getvalue()


def _jpg_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='red').save(buf, format='JPEG')
    return buf.getvalue()


def _webp_bytes():
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), color='red').save(buf, format='WEBP')
    return buf.getvalue()


MP4_BYTES = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * 32
MOV_BYTES = b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ' + b'\x00' * 32
WEBM_BYTES = b'\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1fB\x82\x84webm' + b'\x00' * 32
HTML_BYTES = b'<html><script>alert(1)</script></html>' + b' ' * 64


def _upload(name, content, content_type='application/octet-stream'):
    return SimpleUploadedFile(name, content, content_type=content_type)


class SpySave:
    """Registra el content_type con que el storage recibe el archivo."""

    def __init__(self):
        self.seen = {}

    def __enter__(self):
        from django.core.files.storage import FileSystemStorage
        original = FileSystemStorage.save
        seen = self.seen

        def spy(storage, name, content, *a, **k):
            seen['ct'] = getattr(content, 'content_type', None)
            return original(storage, name, content, *a, **k)

        self.patcher = mock.patch.object(FileSystemStorage, 'save', spy)
        self.patcher.start()
        return self.seen

    def __exit__(self, *exc):
        self.patcher.stop()


class MediaRootMixin:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = override_settings(MEDIA_ROOT=tmp.name)
        override.enable()
        self.addCleanup(override.disable)


class TestMagicByteSniffing(SimpleTestCase):

    def test_valid_files_pass(self):
        from products.validators import validate_media_content
        for name, content in [
            ('a.jpg', _jpg_bytes()), ('a.jpeg', _jpg_bytes()), ('a.png', _png_bytes()),
            ('a.webp', _webp_bytes()), ('a.mp4', MP4_BYTES), ('a.mov', MOV_BYTES),
            ('a.mov', MP4_BYTES), ('a.webm', WEBM_BYTES),
        ]:
            f = _upload(name, content)
            validate_media_content(f)
            self.assertEqual(f.read(4), content[:4])  # rebobinado

    def test_html_named_mp4_rejected_in_spanish(self):
        from products.validators import validate_media_content
        with self.assertRaises(ValidationError) as ctx:
            validate_media_content(_upload('x.mp4', HTML_BYTES, 'video/mp4'))
        self.assertIn('no coincide', str(ctx.exception))

    def test_png_named_jpg_and_video_named_png_rejected(self):
        from products.validators import validate_media_content
        with self.assertRaises(ValidationError):
            validate_media_content(_upload('x.jpg', _png_bytes()))
        with self.assertRaises(ValidationError):
            validate_media_content(_upload('x.png', MP4_BYTES))


class TestMediaUploadSecurityAPI(MediaRootMixin, MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('upl_owner', 'upl_owner@test.com')

    def setUp(self):
        super().setUp()
        self.client_a = self.auth_client(self.user)

    def _post(self, **extra):
        data = {'name': 'P', 'description': 'D', 'price': '10.00'}
        data.update(extra)
        return self.client_a.post(URL, data, format='multipart')

    def test_html_named_mp4_with_spoofed_content_type_rejected(self):
        response = self._post(**{
            'media[0][file]': _upload('x.mp4', HTML_BYTES, 'text/html'),
            'media[0][media_type]': 'video',
        })
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Product.objects.filter(name='P').exists())

    def test_stored_content_type_is_forced_from_extension(self):
        with SpySave() as seen:
            response = self._post(**{
                'media[0][file]': _upload('x.png', _png_bytes(), 'text/html'),
                'media[0][media_type]': 'image',
            })
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(seen['ct'], 'image/png')

    def test_media_type_derived_from_file_not_client(self):
        response = self._post(**{
            'media[0][file]': _upload('x.mp4', MP4_BYTES, 'video/mp4'),
            'media[0][media_type]': 'image',
        })
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Product.objects.get(name='P').media.get().media_type, ProductMedia.VIDEO)

    def test_new_media_without_file_rejected(self):
        response = self._post(**{'media[0][media_type]': 'image'})
        self.assertEqual(response.status_code, 400, response.content)

    def test_oversized_variant_image_rejected(self):
        big = _png_bytes() + b'\x00' * (5 * 1024 * 1024)
        response = self._post(**{
            'variants[0][name]': 'V', 'variants[0][price]': '1.00',
            'variants[0][image]': _upload('v.png', big, 'image/png'),
        })
        self.assertEqual(response.status_code, 400, response.content)

    def test_variant_image_content_type_forced(self):
        with SpySave() as seen:
            response = self._post(**{
                'variants[0][name]': 'V', 'variants[0][price]': '1.00',
                'variants[0][image]': _upload('v.png', _png_bytes(), 'text/html'),
            })
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(seen['ct'], 'image/png')

    def test_too_many_variants_addons_media_rejected(self):
        from products.serializers import MAX_ADDONS, MAX_MEDIA_ITEMS, MAX_VARIANTS
        too_many_addons = {f'addons[{i}][name]': 'a' for i in range(MAX_ADDONS + 1)}
        too_many_addons.update({f'addons[{i}][price]': '1.00' for i in range(MAX_ADDONS + 1)})
        r = self._post(**too_many_addons)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('addons', r.data)

        v = {}
        for i in range(MAX_VARIANTS + 1):
            v[f'variants[{i}][name]'] = 'v'
            v[f'variants[{i}][price]'] = '1.00'
        r = self._post(**v)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('variants', r.data)

        m = {}
        for i in range(MAX_MEDIA_ITEMS + 1):
            m[f'media[{i}][_delete]'] = 'true'
            m[f'media[{i}][id]'] = str(i + 1)
        r = self._post(**m)
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('media', r.data)

    def test_patch_cannot_exceed_gallery_cap_by_adding_to_existing(self):
        product = Product.objects.create(name='G', description='D', price=1, business=self.profile)
        for i in range(3):
            ProductMedia.objects.create(product=product, file=f'product_media/e{i}.png', media_type='image', order=i)
        response = self.client_a.patch(f'{URL}{product.id}/', {
            'media[0][file]': _upload('n.png', _png_bytes(), 'image/png'),
            'media[0][media_type]': 'image',
        }, format='multipart')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(product.media.count(), 3)

    def test_patch_can_swap_when_tombstone_frees_a_slot(self):
        product = Product.objects.create(name='G2', description='D', price=1, business=self.profile)
        rows = [ProductMedia.objects.create(product=product, file=f'product_media/e{i}.png', media_type='image', order=i)
                for i in range(3)]
        response = self.client_a.patch(f'{URL}{product.id}/', {
            'media[0][id]': str(rows[0].id), 'media[0][_delete]': 'true',
            'media[1][file]': _upload('n.png', _png_bytes(), 'image/png'),
        }, format='multipart')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(product.media.count(), 3)

    def test_create_ignores_client_id_and_tombstones(self):
        other = Product.objects.create(name='O', description='D', price=1, business=self.profile)
        victim = ProductMedia.objects.create(product=other, file='product_media/v.png', media_type='image', order=0)
        response = self._post(**{
            'media[0][id]': str(victim.id),
            'media[0][file]': _upload('n.png', _png_bytes(), 'image/png'),
            'media[1][_delete]': 'true',
            'media[1][file]': _upload('m.png', _png_bytes(), 'image/png'),
        })
        self.assertEqual(response.status_code, 201, response.content)
        victim.refresh_from_db()
        self.assertEqual(victim.product_id, other.id)
        self.assertEqual(Product.objects.get(name='P').media.count(), 1)

    def test_tombstone_without_file_or_id_is_ignored_not_rejected(self):
        response = self._post(**{
            'media[0][_delete]': 'true',
            'media[1][file]': _upload('n.png', _png_bytes(), 'image/png'),
        })
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Product.objects.get(name='P').media.count(), 1)


class TestBusinessImageUpload(MediaRootMixin, MarkyAPITestCase):
    URL = '/api/v1/business/profile-image/'

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('bimg_owner', 'bimg_owner@test.com')

    def test_content_type_forced_and_html_rejected(self):
        client = self.auth_client(self.user)
        r = client.patch(self.URL, {'profile_image': _upload('x.png', HTML_BYTES, 'image/png')}, format='multipart')
        self.assertEqual(r.status_code, 400, r.content)

        with SpySave() as seen:
            r = client.patch(self.URL, {'profile_image': _upload('x.png', _png_bytes(), 'text/html')}, format='multipart')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(seen['ct'], 'image/png')


class TestStorageOptions(SimpleTestCase):

    def _opts(self, **env):
        from marky_backend.storage_config import build_r2_storage
        base = {'AWS_ACCESS_KEY_ID': 'k', 'AWS_SECRET_ACCESS_KEY': 's', 'AWS_STORAGE_BUCKET_NAME': 'b',
                'AWS_S3_ENDPOINT_URL': 'https://x.r2.cloudflarestorage.com'}
        base.update(env)
        return build_r2_storage(base.get)['OPTIONS']

    def test_custom_domain_gives_unsigned_urls_and_cache_control(self):
        o = self._opts(AWS_S3_CUSTOM_DOMAIN='media.example.com')
        self.assertEqual(o['custom_domain'], 'media.example.com')
        self.assertFalse(o['querystring_auth'])
        self.assertEqual(o['object_parameters']['CacheControl'], 'public, max-age=31536000')

    def test_without_custom_domain_urls_stay_signed(self):
        o = self._opts()
        self.assertNotIn('custom_domain', o)
        self.assertTrue(o.get('querystring_auth', True))
        self.assertEqual(o['object_parameters']['CacheControl'], 'public, max-age=31536000')

    def test_gunicorn_flags_consistent(self):
        import pathlib
        base = pathlib.Path(__file__).resolve().parent.parent
        flags = ['--worker-class gthread', '--threads 4', '--timeout 120',
                 '--max-requests 1000', '--max-requests-jitter 100']
        # El Dockerfile delega en scripts/start.sh (fuente única de los flags de gunicorn).
        self.assertIn('scripts/start.sh', (base / 'Dockerfile').read_text())
        script = ' '.join((base / 'scripts' / 'start.sh').read_text().replace('\\\n', ' ').split())
        for flag in flags:
            self.assertIn(flag, script)
        self.assertIn('python manage.py release', script)
        self.assertIn('SKIP_RELEASE', script)
        self.assertLess(script.index('python manage.py release'), script.index('exec gunicorn'))


class TestOrphanFileCleanup(MediaRootMixin, MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('orph_owner', 'orph_owner@test.com')

    def _exists(self, ff):
        return ff.storage.exists(ff.name)

    def _product(self):
        p = Product.objects.create(name='Orf', description='D', price=1, business=self.profile)
        media = ProductMedia.objects.create(
            product=p, file=_upload('m.png', _png_bytes()), media_type='image', order=0)
        variant = ProductVariant.objects.create(
            product=p, name='V', price=1, image=_upload('v.png', _png_bytes()))
        return p, media, variant

    def test_deleting_media_and_variant_removes_files(self):
        p, media, variant = self._product()
        mf, vf = media.file, variant.image
        with self.captureOnCommitCallbacks(execute=True):
            media.delete()
            variant.delete()
        self.assertFalse(self._exists(mf))
        self.assertFalse(self._exists(vf))

    def test_deleting_product_cascades_files(self):
        p, media, variant = self._product()
        mf, vf = media.file, variant.image
        with self.captureOnCommitCallbacks(execute=True):
            p.delete()
        self.assertFalse(self._exists(mf))
        self.assertFalse(self._exists(vf))

    def test_category_hard_delete_cascades_product_files(self):
        cat = ProductCategory.objects.create(business=self.profile, name='C', icon='x')
        p, media, variant = self._product()
        p.category = cat
        p.save()
        mf = media.file
        with self.captureOnCommitCallbacks(execute=True):
            p.delete()
            cat.delete()
        self.assertFalse(self._exists(mf))

    def test_replacing_file_removes_old_one(self):
        p, media, variant = self._product()
        old = media.file
        old_name = old.name
        with self.captureOnCommitCallbacks(execute=True):
            media.file = _upload('new.png', _png_bytes())
            media.save()
        self.assertFalse(old.storage.exists(old_name))
        self.assertTrue(self._exists(media.file))

    def test_unchanged_save_keeps_file(self):
        p, media, variant = self._product()
        with self.captureOnCommitCallbacks(execute=True):
            media.order = 5
            media.save()
        self.assertTrue(self._exists(media.file))

    def test_file_still_referenced_elsewhere_is_kept(self):
        p, media, variant = self._product()
        twin = ProductMedia.objects.create(product=p, file=media.file.name, media_type='image', order=1)
        with self.captureOnCommitCallbacks(execute=True):
            media.delete()
        self.assertTrue(self._exists(twin.file))

    def test_rollback_keeps_file(self):
        p, media, variant = self._product()
        mf = media.file
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    media.delete()
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertTrue(self._exists(mf))

    def test_storage_error_is_logged_not_raised(self):
        p, media, variant = self._product()
        with mock.patch('django.core.files.storage.FileSystemStorage.delete', side_effect=OSError('r2 down')):
            with self.assertLogs('utils.storage_cleanup', level='ERROR'):
                with self.captureOnCommitCallbacks(execute=True):
                    media.delete()

    def test_business_profile_image_replace_and_default_kept(self):
        profile = self.profile
        default = profile.profile_image
        with self.captureOnCommitCallbacks(execute=True):
            profile.profile_image = _upload('a.png', _png_bytes())
            profile.save()
        first = profile.profile_image
        first_name = first.name
        with self.captureOnCommitCallbacks(execute=True):
            profile.profile_image = _upload('b.png', _png_bytes())
            profile.save()
        self.assertFalse(first.storage.exists(first_name))
        self.assertEqual(default.name, 'business_profiles/default.png')
