"""Detección de tipo por magic bytes para archivos subidos (F-14).

El Content-Type que envía el cliente no es confiable: se inspecciona la
cabecera del archivo y el tipo guardado se deriva de la extensión validada.
"""
import os

# extensión -> (familia detectada por magic bytes, Content-Type a almacenar)
EXTENSION_TYPES = {
    '.jpg': ('jpeg', 'image/jpeg'),
    '.jpeg': ('jpeg', 'image/jpeg'),
    '.png': ('png', 'image/png'),
    '.webp': ('webp', 'image/webp'),
    '.gif': ('gif', 'image/gif'),
    '.mp4': ('isobmff', 'video/mp4'),
    '.mov': ('isobmff', 'video/quicktime'),
    '.webm': ('webm', 'video/webm'),
}

SNIFF_BYTES = 64


def sniff_family(header):
    """Devuelve la familia de formato según los primeros bytes, o None."""
    if header.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'webp'
    if header[4:8] == b'ftyp':
        return 'isobmff'  # MP4 / MOV (QuickTime)
    if header.startswith(b'\x1a\x45\xdf\xa3') and b'webm' in header:
        return 'webm'
    return None


def read_header(value):
    try:
        value.seek(0)
        return value.read(SNIFF_BYTES)
    finally:
        value.seek(0)


def file_extension(value):
    return os.path.splitext(value.name)[1].lower()


def content_matches_extension(value, allowed_extensions):
    """True si la extensión está permitida y los magic bytes coinciden con ella."""
    ext = file_extension(value)
    if ext not in allowed_extensions or ext not in EXTENSION_TYPES:
        return False
    return sniff_family(read_header(value)) == EXTENSION_TYPES[ext][0]


def force_content_type(value):
    """Fija el Content-Type almacenado según la extensión (ya validada)."""
    entry = EXTENSION_TYPES.get(file_extension(value))
    if entry:
        value.content_type = entry[1]
    return value
