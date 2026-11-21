"""F-24: miniaturas de imágenes generadas con Pillow al subir."""
import io
import os
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase
from PIL import Image

from products.models import Product, ProductCategory, ProductMedia, ProductVariant
from products.tests_uploads import MediaRootMixin, URL, _upload
from utils.tests_base import MarkyAPITestCase
from utils.thumbnails import THUMBNAIL_MAX_SIZE, make_thumbnail


def _image_bytes(size=(1200, 800), fmt='PNG', mode='RGB'):
    buf = io.BytesIO()
    Image.new(mode, size, color='red').save(buf, format=fmt)
    return buf.getvalue()


def _open(content_file):
    content_file.seek(0)
    return Image.open(io.BytesIO(content_file.read()))


class TestMakeThumbnail(SimpleTestCase):

    def test_large_image_is_bounded_and_keeps_aspect(self):
        thumb = make_thumbnail(SimpleUploadedFile('a.png', _image_bytes((1200, 800))))
        img = _open(thumb)
        self.assertEqual(img.size, (THUMBNAIL_MAX_SIZE, 267))
        self.assertTrue(thumb.name.endswith('.webp'))

    def test_small_image_is_not_upscaled(self):
        img = _open(make_thumbnail(SimpleUploadedFile('a.png', _image_bytes((100, 50)))))
        self.assertEqual(img.size, (100, 50))

    def test_gif_uses_first_frame_and_rgba_is_supported(self):
        frames = [Image.new('P', (800, 800), color=i) for i in (0, 1)]
        buf = io.BytesIO()
        frames[0].save(buf, format='GIF', save_all=True, append_images=frames[1:])
        img = _open(make_thumbnail(SimpleUploadedFile('a.gif', buf.getvalue())))
        self.assertEqual(img.size, (THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE))
        img = _open(make_thumbnail(SimpleUploadedFile('a.png', _image_bytes((900, 900), mode='RGBA'))))
        self.assertEqual(img.size, (THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE))

    def test_corrupt_data_returns_none(self):
        self.assertIsNone(make_thumbnail(SimpleUploadedFile('a.jpg', b'\xff\xd8\xff' + b'garbage' * 20)))

    def test_file_position_is_restored(self):
        f = SimpleUploadedFile('a.png', _image_bytes())
        make_thumbnail(f)
        self.assertEqual(f.tell(), 0)


class TestThumbnailAPI(MediaRootMixin, MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('thumb_owner', 'thumb_owner@test.com')

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _create(self, extra=None):
        data = {
            'name': 'P', 'description': 'D', 'price': '10.00',
            'media[0][file]': _upload('m.png', _image_bytes()),
            'media[0][order]': '0',
            'variants[0][name]': 'V', 'variants[0][price]': '5.00',
            'variants[0][image]': _upload('v.png', _image_bytes()),
        }
        data.update(extra or {})
        return self.client.post(URL, data, format='multipart')

    def test_upload_generates_thumbnails_and_serializes_them(self):
        res = self._create()
        self.assertEqual(res.status_code, 201, res.content)
        media = ProductMedia.objects.get()
        variant = ProductVariant.objects.get()
        for ff in (media.thumbnail, variant.thumbnail):
            self.assertTrue(ff)
            self.assertLessEqual(max(Image.open(ff.path).size), THUMBNAIL_MAX_SIZE)
        self.assertNotEqual(media.thumbnail.name, media.file.name)
        detail = self.client.get(f'{URL}{media.product_id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.data['media'][0]['thumbnail'].endswith(media.thumbnail.name))
        self.assertTrue(detail.data['variants'][0]['thumbnail'].endswith(variant.thumbnail.name))
        self.assertTrue(detail.data['media'][0]['file'].endswith(media.file.name))

    def test_video_has_no_thumbnail(self):
        from products.tests_uploads import MP4_BYTES
        res = self._create({'media[0][file]': _upload('v.mp4', MP4_BYTES)})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(ProductMedia.objects.get().thumbnail)

    def test_thumbnail_failure_never_fails_upload(self):
        # JPEG válido por magic bytes pero no decodificable.
        res = self._create({'media[0][file]': _upload('m.jpg', b'\xff\xd8\xff' + b'x' * 200)})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(ProductMedia.objects.get().thumbnail)
        first_pk = ProductMedia.objects.get().pk
        with mock.patch('utils.thumbnails.ImageOps.exif_transpose', side_effect=RuntimeError('boom')):
            res = self._create()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(ProductMedia.objects.exclude(thumbnail='').exclude(thumbnail__isnull=True).exclude(
            pk=first_pk).exists())

    def _catalog_item(self):
        res = self.client.get('/api/v1/products/product-categories/with_products/')
        self.assertEqual(res.status_code, 200, res.content)
        for group in res.data['results']:
            if group['products']:
                return group['products'][0]
        self.fail('catálogo sin productos')

    def test_catalog_exposes_image_thumbnail_with_fallback(self):
        cat = ProductCategory.objects.create(business=self.profile, name='C', icon='x')
        self._create({'category': cat.pk})
        item = self._catalog_item()
        self.assertTrue(item['image_thumbnail'])
        self.assertNotEqual(item['image_thumbnail'], item['image'])
        ProductMedia.objects.update(thumbnail='')
        item = self._catalog_item()
        self.assertIsNone(item['image_thumbnail'])
        self.assertTrue(item['image'])

    def test_replacing_and_deleting_files_cleans_thumbnails(self):
        self._create()
        media = ProductMedia.objects.get()
        old_thumb = media.thumbnail
        old_name = old_thumb.name
        with self.captureOnCommitCallbacks(execute=True):
            media.file = _upload('new.png', _image_bytes((900, 900)))
            media.save()
        media.refresh_from_db()
        self.assertNotEqual(media.thumbnail.name, old_name)
        self.assertFalse(old_thumb.storage.exists(old_name))
        self.assertTrue(media.thumbnail.storage.exists(media.thumbnail.name))
        thumb = media.thumbnail
        with self.captureOnCommitCallbacks(execute=True):
            media.delete()
        self.assertFalse(thumb.storage.exists(thumb.name))

    def test_unchanged_save_keeps_thumbnail(self):
        self._create()
        media = ProductMedia.objects.get()
        name = media.thumbnail.name
        media.order = 5
        media.save()
        media.refresh_from_db()
        self.assertEqual(media.thumbnail.name, name)

    def test_thumbnail_is_read_only_in_input(self):
        res = self._create({'media[0][thumbnail]': _upload('t.png', _image_bytes((10, 10)))})
        self.assertEqual(res.status_code, 201, res.content)
        self.assertLessEqual(max(Image.open(ProductMedia.objects.get().thumbnail.path).size), THUMBNAIL_MAX_SIZE)


class TestThumbnailRefreshEdgeCases(MediaRootMixin, MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('edge_owner', 'edge_owner@test.com')

    def _product(self):
        return Product.objects.create(name='E', description='D', price=1, business=self.profile)

    def test_clearing_variant_image_clears_thumbnail_and_removes_file(self):
        variant = ProductVariant.objects.create(
            product=self._product(), name='V', price=1, image=_upload('v.png', _image_bytes()))
        old_thumb = variant.thumbnail
        old_name = old_thumb.name
        self.assertTrue(old_name)
        self.assertTrue(old_thumb.storage.exists(old_name))
        with self.captureOnCommitCallbacks(execute=True):
            variant.image = None
            variant.save()
        variant.refresh_from_db()
        self.assertFalse(variant.image)
        self.assertFalse(variant.thumbnail)
        self.assertFalse(old_thumb.storage.exists(old_name))

    def test_clearing_variant_image_with_empty_string(self):
        variant = ProductVariant.objects.create(
            product=self._product(), name='V', price=1, image=_upload('v.png', _image_bytes()))
        old_thumb = variant.thumbnail
        old_name = old_thumb.name
        with self.captureOnCommitCallbacks(execute=True):
            variant.image = ''
            variant.save()
        variant.refresh_from_db()
        self.assertFalse(variant.thumbnail)
        self.assertFalse(old_thumb.storage.exists(old_name))

    def test_media_switched_to_video_clears_committed_thumbnail(self):
        media = ProductMedia.objects.create(
            product=self._product(), file=_upload('m.png', _image_bytes()), media_type='image')
        old_thumb = media.thumbnail
        old_name = old_thumb.name
        self.assertTrue(old_name)
        with self.captureOnCommitCallbacks(execute=True):
            media.media_type = 'video'
            media.save()
        media.refresh_from_db()
        self.assertFalse(media.thumbnail)
        self.assertFalse(old_thumb.storage.exists(old_name))

    def test_variant_update_fields_image_persists_new_thumbnail(self):
        variant = ProductVariant.objects.create(
            product=self._product(), name='V', price=1, image=_upload('v.png', _image_bytes()))
        old_name = variant.thumbnail.name
        variant.image = _upload('v2.png', _image_bytes((900, 900)))
        variant.save(update_fields=['image'])
        variant.refresh_from_db()
        self.assertTrue(variant.thumbnail)
        self.assertNotEqual(variant.thumbnail.name, old_name)

    def test_media_update_fields_file_persists_new_thumbnail(self):
        media = ProductMedia.objects.create(
            product=self._product(), file=_upload('m.png', _image_bytes()), media_type='image')
        old_name = media.thumbnail.name
        media.file = _upload('m2.png', _image_bytes((900, 900)))
        media.save(update_fields=['file'])
        media.refresh_from_db()
        self.assertTrue(media.thumbnail)
        self.assertNotEqual(media.thumbnail.name, old_name)

    def test_update_fields_without_source_does_not_touch_thumbnail(self):
        media = ProductMedia.objects.create(
            product=self._product(), file=_upload('m.png', _image_bytes()), media_type='image')
        name = media.thumbnail.name
        media.save(update_fields=['order'])
        media.refresh_from_db()
        self.assertEqual(media.thumbnail.name, name)


class TestBackfillCommand(MediaRootMixin, MarkyAPITestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user, cls.profile = cls.make_user('bf_owner', 'bf_owner@test.com')

    def test_backfill_generates_missing_and_is_idempotent(self):
        p = Product.objects.create(name='B', description='D', price=1, business=self.profile)
        media = ProductMedia.objects.create(product=p, file=_upload('m.png', _image_bytes()), media_type='image')
        variant = ProductVariant.objects.create(product=p, name='V', price=1, image=_upload('v.png', _image_bytes()))
        ProductMedia.objects.update(thumbnail='')
        ProductVariant.objects.update(thumbnail='')
        out = io.StringIO()
        call_command('generate_thumbnails', stdout=out)
        media.refresh_from_db()
        variant.refresh_from_db()
        self.assertTrue(media.thumbnail)
        self.assertTrue(variant.thumbnail)
        first = media.thumbnail.name
        call_command('generate_thumbnails', stdout=out)
        media.refresh_from_db()
        self.assertEqual(media.thumbnail.name, first)
        self.assertTrue(os.path.exists(media.thumbnail.path))
