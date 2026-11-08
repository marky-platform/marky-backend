# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

Django 5.1 + Django REST Framework backend for "Marky", a multi-tenant platform where each business (commercial or entrepreneur) manages a profile, branches, products, and promotions. Primary language of the domain/UI strings is Spanish; `LANGUAGE_CODE = 'es'`, timezone `America/Asuncion`.

## Commands

The virtualenv lives in `env/`. Activate it first: `source env/bin/activate`.

```bash
python manage.py runserver              # dev server (port from env or 8000)
python manage.py migrate
python manage.py makemigrations
python manage.py createsuperuser
python manage.py test                   # run all tests
python manage.py test products          # single app
python manage.py test products.tests.SomeTest.test_method   # single test
python manage.py cities_light           # download/import city & country data (PY, VE)
python manage.py send_queued_mail       # flush the post_office email queue
python manage.py createcachetable       # one-off: creates the `django_cache` table (DatabaseCache; idempotent)
```

Note: test files are currently stubs — there is no real test suite yet.

## Environment & database

- Config is read from `.env` via `django-environ` (see `.env.example`).
- **No `DATABASE_URL` set → SQLite** (`db.sqlite3`). When `DATABASE_URL` is set, the engine is forced to **PostGIS** (`django.contrib.gis.db.backends.postgis`), not plain Postgres. GIS features (the `Branch.location` PointField, distance queries) only work against PostGIS — see `README.md` for the `apt-get install binutils gdal-bin libgdal-dev` + `CREATE EXTENSION postgis` setup.
- **`DEBUG=False` fails fast:** `marky_backend/prod_checks.py::validate_production_env` raises `ImproperlyConfigured` unless `DATABASE_URL`, `HOST` (no `*`), `FRONTEND_URL`, `RESEND_API_KEY` and the R2 vars (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL`) are set. Local `.env` uses `DEBUG=1`, so dev and tests are unaffected. With `DEBUG=False` settings also enable SSL redirect (exempting `/healthz` and `/readyz`), HSTS (`SECURE_HSTS_SECONDS`, default 3600), secure cookies and `CSRF_TRUSTED_ORIGINS`. Optional env: `SECURE_SSL_REDIRECT`, `SECURE_HSTS_*`, `CSRF_TRUSTED_ORIGINS`, `CONN_MAX_AGE` (60), `CONN_HEALTH_CHECKS` (True), `CACHE_URL`, `VERCEL_TEAM_SLUG` (allows Vercel preview origins in CORS). `check --deploy` leaves only W005/W021 (HSTS subdomains/preload), deliberate with a short max-age.
- **Cache / throttling:** `CACHES['default']` is `DatabaseCache` (table `django_cache`, run `createcachetable` once; the test runner creates it itself) so DRF throttle counters are shared across workers. Set `CACHE_URL` (e.g. `rediscache://...`) to use Redis instead. Client IP for throttles depends on `NUM_PROXIES` (Railway = 1).
- `marky_backend/local_settings.py` is imported last if present and overrides everything (gitignored, currently empty).

## Architecture

Three Django apps, all routed under `/api/v1/` (see `marky_backend/urls.py`):

- **`users`** — custom `AUTH_USER_MODEL = users.User` (extends `AbstractUser`, adds `business_name`, `phone_number`, `is_verified`). Handles the full auth flow: register, email verification, login, JWT refresh, password recovery/change. Auth endpoints under `/api/v1/users/`.
- **`business`** — the tenant model. `BusinessProfile` (OneToOne with `User`), `Branch` (with PostGIS `PointField`), `Currency`, `SocialMediaLink`, plus aggregate endpoints (`home-page/`, `account-info/`). Under `/api/v1/business/`.
- **`products`** — `ProductCategory`, `Product`, `ProductVariant`, `ProductAddon`, `ProductMedia`, all owned by a `BusinessProfile`. Under `/api/v1/products/`.

**Top-level directories that are NOT Django apps:** `business_profiles/` and `product_variants/` are media/upload targets; `cities/` is the `CITIES_LIGHT_DATA_DIR` (downloaded geonames data); `media/` is `MEDIA_ROOT`; `utils/` holds shared helpers (no app config). Only `users`, `business`, `products` are in `INSTALLED_APPS`.

### Tenancy & queryset scoping

Every business-scoped view filters its queryset by the requesting user's profile, e.g. `Product.objects.filter(business=user.business_profile)`, and returns `.none()` for users without a `business_profile`. When adding endpoints that touch business data, **always scope through `request.user.business_profile`** — there is no row-level DB enforcement. `User.has_configuration` / `hasattr(user, 'business_profile')` is the standard "is this user set up" check.

### Auth & permissions

- JWT via `djangorestframework-simplejwt` (`Authorization: Bearer ...`). Only JWT auth is enabled in DRF (no `SessionAuthentication`; Django admin keeps its own session login).
- Tokens: access 30 min, refresh 30 days with rotation + `token_blacklist`. `POST /users/logout/` blacklists the refresh token (access tokens stay valid until they expire). `CHECK_REVOKE_TOKEN: True` embeds a `hash_password` claim, so a password change (`PasswordChangeView`) also kills previously issued access tokens (401) and blacklists all outstanding refresh tokens; tokens minted via `RefreshToken.for_user` carry the claim automatically.
- Email verification uses a 6-digit code stored on the user (`User.verification_code`, with `verification_attempts` and a lockout after 5 failed attempts) — see `users/views.py` `RegisterView`/`VerifyEmailView`. Emails go through `users/mailing.py::queue_mail` into `post_office` (DB-queued, priority `medium`, retries via `POST_OFFICE`; failures to queue are logged, never raised). Nothing is sent until `send_queued_mail` runs; `purge_mail` removes processed rows (they hold codes). Auth responses are uniform (no email enumeration): register with an existing email answers 201 like a new one and mails the owner (`account_exists` template). Every response carries `X-Request-ID`, included in log lines.
- `utils/permissions.py::IsBusinessOrSuperAdmin` gates business/product endpoints — it checks membership in the Django **group named `business`** (users are auto-added to this group on register) or `is_superuser`.

### Multi-currency pricing

`BusinessProfile` carries `primary_currency`, optional `secondary_currency`, `exchange_rate`, and `is_primary_to_secondary` (direction flag). `Product`, `ProductVariant`, and `ProductAddon` each implement `get_primary_secondary_amounts()` which returns `(primary, secondary, primary_currency, secondary_currency)` as Decimals. Reuse this method rather than re-deriving conversions; keep arithmetic in `Decimal`.

### Promotions

Both `ProductCategory` and `Product` carry the same promotion fields (`multibuy_option` 2x1/3x2, `discount_percentage`, `promotion_starts_at/ends_at`). `products/filters.py::ProductCategoryFilter.filter_has_promotion` defines what "active promotion" means (date-window + Exists subquery over child products) — match that logic when querying promotions elsewhere.

### File uploads / nested forms

DRF is configured globally with `drf_nested_forms` parsers (`NestedMultiPartParser`, `NestedJSONParser`) before the standard `FormParser`. This lets multipart requests carry nested structures (e.g. a product plus its variants/media/addons and their image files in one request). Product write views use `ProductInputSerializer` for create/update and `ProductSerializer` for reads (`get_serializer_class` switches by action). Image uploads are validated by `business/validators.py` (≤2MB, jpg/jpeg/png/gif). Product media/variant images are sniffed by magic bytes (`utils/uploads.py`, `products/validators.py`) and the stored Content-Type and `media_type` derive from the validated extension, never from the client. Nested lists per product are capped (`MAX_VARIANTS`/`MAX_ADDONS`/`MAX_MEDIA_ITEMS` = 20 rows; gallery 3 images + 1 video, computed over the final state). Files of deleted/replaced Product media, variants, 3D models and business profile images are removed after commit by `utils/storage_cleanup.py` (errors logged, files still referenced elsewhere are kept). R2 storage options live in `marky_backend/storage_config.py` (`AWS_S3_CUSTOM_DOMAIN` gives unsigned URLs).

## API docs

In production (`DEBUG=False`) the schema/Swagger/ReDoc URLs return 404 unless the visitor is a staff user logged in via `/admin/` (see `docs_gate` in `urls.py`). Probes: `/healthz` (process up, no DB) and `/readyz` (`SELECT 1`), both unauthenticated and unthrottled.

Spectacular schema at `/api/schema/`, Swagger UI at `/api/schema/swagger/`, ReDoc at `/api/schema/redoc/`. Tag new endpoints with `@extend_schema(tags=[...])` using the existing tags (`Auth`, `Business`, `Cities`, `Products`).
