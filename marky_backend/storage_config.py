"""Configuración del storage S3/R2 (F-15), separada de settings para poder probarla."""

# Los nombres de archivo llevan sufijo único (file_overwrite=False): son inmutables.
CACHE_CONTROL = 'public, max-age=31536000'


def build_r2_storage(get):
    """Arma la entrada STORAGES['default'] para R2. `get(nombre)` lee una variable de entorno."""
    options = {
        "access_key": get("AWS_ACCESS_KEY_ID"),
        "secret_key": get("AWS_SECRET_ACCESS_KEY"),
        "bucket_name": get("AWS_STORAGE_BUCKET_NAME"),
        "endpoint_url": get("AWS_S3_ENDPOINT_URL"),
        "region_name": get("AWS_S3_REGION_NAME") or "auto",
        "signature_version": "s3v4",
        "file_overwrite": False,
        "default_acl": None,
        "object_parameters": {"CacheControl": CACHE_CONTROL},
    }
    custom_domain = get("AWS_S3_CUSTOM_DOMAIN")
    if custom_domain:
        # Dominio público de R2: URLs estables, sin firma, cacheables por el CDN.
        options["custom_domain"] = custom_domain
        options["querystring_auth"] = False
    return {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage", "OPTIONS": options}
