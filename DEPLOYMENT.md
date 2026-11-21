# Deployment (Railway)

All deploy logic lives in this repo (`Dockerfile`, `scripts/start.sh`, `python manage.py release`).
`railway.json` was removed: Railway "Config as Code" is deprecated and was never applied to our services.
The container itself runs `python manage.py release` on start, so the dashboard needs no pre-deploy command, no start command and no config file path: only the settings below plus environment variables.

## 1. Dashboard settings (identical in dev, QA and prod)

| Setting | Value |
|---|---|
| Builder | Dockerfile |
| Healthcheck path | `/healthz` |
| Pre-deploy / start command / config file path | **leave empty** (the Dockerfile `CMD` runs `scripts/start.sh`, which runs `release` and then gunicorn) |

`scripts/start.sh` runs `python manage.py release` before gunicorn: `migrate --noinput`, `createcachetable` and `seed`, in that order. It is idempotent; if any step fails the container exits non-zero (the `[release] <step> FALLÓ` line is in the logs) and never serves traffic. Independently, migration `users.0006_create_cache_table` creates the `django_cache` table whenever `CACHES` uses `DatabaseCache`, so any `migrate` guarantees it.

**Multiple replicas:** every replica would run `release` at start, and concurrent migrations can race. In that case set `SKIP_RELEASE=1` on the service and configure `python manage.py release` as the platform pre-deploy command instead (the same applies if you simply prefer a pre-deploy step).

## 2. Environment variables

Never commit real values. The app refuses to start with `DEBUG=False` if a required one is missing (`marky_backend/prod_checks.py`).

| Variable | Required | Differs per env | Notes |
|---|---|---|---|
| `SECRET_KEY` | yes | yes (unique per env) | Long random string |
| `DATABASE_URL` | yes | yes | Reference the env's PostGIS service; forces the PostGIS engine |
| `HOST` | yes | yes | Public hostname of the backend, no `*` |
| `FRONTEND_URL` | yes | yes | URL of the matching frontend (email links, CORS) |
| `DEFAULT_FROM_EMAIL` | yes | usually | Must be a Resend-verified sender |
| `RESEND_API_KEY` | yes | yes | One key per env |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | yes | yes | R2 credentials, scoped to the env's bucket |
| `AWS_STORAGE_BUCKET_NAME` | yes | yes | One R2 bucket per env |
| `AWS_S3_ENDPOINT_URL` | yes | account-wide | R2 endpoint |
| `NUM_PROXIES` | yes | no | `1` on Railway (client IP for throttling) |
| `SENTRY_DSN` | recommended | yes | One Sentry project/DSN per env |
| `VERCEL_TEAM_SLUG` | optional | no | Allows Vercel preview origins in CORS |

Vercel side: `REACT_APP_API_URL` must point to the matching backend (`https://<HOST>/api/v1`).

## 3. Create a new environment

1. Create or duplicate the Railway environment.
2. Add a PostGIS database service from the `postgis/postgis` template (not the stock Postgres plugin).
3. Set the env vars from section 2 (`DATABASE_URL` from the new database).
4. Set the dashboard settings from section 1 (Dockerfile builder, healthcheck path; nothing else).
5. Point the service at the environment's branch.
6. Deploy. The start-time `release` step creates the schema, the cache table and the seed data.
7. Check `https://<HOST>/healthz` returns 200.
8. Run once: `python manage.py cities_light`.
9. Schedule cron jobs: `flushexpiredtokens`, `send_queued_mail` (nothing is emailed until it runs), `purge_mail`, `purge_analytics_events`, `expire_promotions` (every 5 minutes).
10. After the first deploy, run once: `python manage.py generate_thumbnails`.

## 4. Rollback

Redeploy the previous deployment from the Railway dashboard. Migrations are not reverted, so every migration must stay backward compatible with the previous release (add before use, remove only after the code stopped using it).

## 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `relation "django_cache" does not exist` | Should not happen: `release` runs at container start and migration `users.0006` creates the table. Check the logs for `[start] SKIP_RELEASE=1` (then run `python manage.py release` as pre-deploy) or a `[release] <step> FALLÓ` line; as a stopgap run `python manage.py createcachetable` |
| `column ... does not exist` / `relation ... does not exist` | Same: migrations did not run. Check for `SKIP_RELEASE=1` or a failed `[release]` step in the logs, redeploy |
| Container exits right after start | Read the `[release] <step> FALLÓ` line in the logs (usually DB unreachable or a bad migration) |
| App exits at boot with `ImproperlyConfigured` | A required env var from section 2 is missing |
