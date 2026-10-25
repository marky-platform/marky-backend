## PostGIS Setup

1. **Install system dependencies**  
   ```bash
   sudo apt-get install binutils gdal-bin libgdal-dev
   ```
   
2.**Configure Django**  
   ```
INSTALLED_APPS += [
    'django.contrib.gis',
    # … other apps …
]

DATABASES = {
    'default': {
        'ENGINE': 'django.contrib.gis.db.backends.postgis',
        'NAME': '<your_database>',
        'USER': '<your_user>',
        'PASSWORD': '<your_password>',
        'HOST': '<your_host>',
        'PORT': '<your_port>',
    }
}
   ```
3.**Enable PostGIS extension** 

```
sudo -i -u postgres
psql -d <your_database>
CREATE EXTENSION IF NOT EXISTS postgis;
\q
exit
```

## Modelos 3D (GLB/USDZ)

Los productos pueden tener un modelo 3D opcional (`Product3DModel`): un archivo **GLB** (obligatorio si existe el modelo) y un archivo **USDZ** (opcional, mejora la realidad aumentada en iPhone/iPad).

### Límite de tamaño

El máximo es **25 MB por archivo** (GLB y USDZ). La constante vive en dos lugares y **deben mantenerse sincronizadas**:

- Backend: `MAX_MODEL_3D_SIZE` en `products/validators.py`.
- Frontend: `MAX_MODEL_3D_SIZE_BYTES` en `marky-admin/src/utils/model3dValidation.ts`.

El backend además valida la extensión (`.glb` / `.usdz`) y los bytes mágicos del archivo (`glTF` para GLB, `PK\x03\x04` para USDZ), porque el MIME declarado por el navegador no es confiable.

### Content-Type

Al guardar, el servidor normaliza el content type del archivo subido, sin depender de lo que declare el navegador:

- GLB: `model/gltf-binary`
- USDZ: `model/vnd.usdz+zip`

Esto es necesario para que iOS Quick Look abra el USDZ y para que `<model-viewer>` cargue el GLB. Los archivos ya subidos antes de este cambio no se modifican.

### Limpieza de storage

Al reemplazar un archivo, quitar el USDZ, eliminar el modelo o eliminar el producto (cascada), los archivos anteriores se borran del storage (`products/signals.py` y `_apply_model_3d`), una vez confirmada la transacción (`transaction.on_commit`).

### CORS en Cloudflare R2 (obligatorio)

`<model-viewer>` descarga el GLB con `fetch` desde el origen del frontend, así que el bucket de R2 debe permitir CORS y peticiones `Range`. Esto es independiente de `CORS_ALLOWED_ORIGINS` de Django (que solo aplica a la API). En local con `DEBUG` Django sirve `/media/` con CORS abierto y no hace falta nada.

Dónde configurarlo: Cloudflare Dashboard > R2 > (bucket de media) > **Settings** > **CORS Policy** > **Add CORS policy** (pestaña JSON) y pegar:

```json
[
  {
    "AllowedOrigins": [
      "https://marky.one",
      "https://www.marky.one",
      "https://marky-admin.vercel.app",
      "https://marky-admin-*.vercel.app",
      "http://localhost:3000"
    ],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["Range", "*"],
    "ExposeHeaders": ["Content-Length", "Content-Range", "Content-Type"],
    "MaxAgeSeconds": 3600
  }
]
```

Notas:

- Los orígenes salen de `CORS_ALLOWED_ORIGIN_REGEXES` en `marky_backend/settings.py` (`marky-admin*.vercel.app` y `marky.one` con/sin `www`). R2 solo admite un `*` por origen, por eso se listan los patrones de Vercel de esta forma; si hay otros dominios de preview o un dominio propio, agregarlos a la lista.
- Si el bucket se sirve por dominio personalizado (`AWS_S3_CUSTOM_DOMAIN`), la política igual se configura en el bucket; verificar las cabeceras contra ese dominio.

### Verificar cabeceras

Reemplazar `<R2_PUBLIC_URL>` por la URL pública del archivo (por ejemplo `https://<AWS_S3_CUSTOM_DOMAIN>/product_3d/<archivo>.glb`) y `<ORIGEN>` por un origen permitido (por ejemplo `https://marky.one`).

```bash
# 1. Cabeceras básicas: 200, HTTPS, Content-Type y Accept-Ranges
curl -I -H "Origin: <ORIGEN>" "<R2_PUBLIC_URL>"

# 2. Petición con Range: debe responder 206 Partial Content
curl -I -H "Origin: <ORIGEN>" -H "Range: bytes=0-99" "<R2_PUBLIC_URL>"
```

Resultado esperado en ambas:

- `Content-Type: model/gltf-binary` (GLB) o `model/vnd.usdz+zip` (USDZ).
- `Access-Control-Allow-Origin: <ORIGEN>` (o `*`).
- En la segunda, `HTTP/2 206` y `Content-Range: bytes 0-99/<total>`.

Si `Content-Type` es `application/octet-stream`, el archivo se subió antes de la normalización: volver a subirlo desde el formulario del producto.
