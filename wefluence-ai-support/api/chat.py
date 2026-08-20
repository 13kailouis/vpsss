"""
PENERUS LAMA
============

Sampai versi 1, seluruh service ada di berkas ini dan gunicorn dijalankan dengan
target `api.chat:app`. Isinya sudah pindah ke `api/app.py` dan modul-modul di
sebelahnya. Berkas ini sengaja dibiarkan hidup supaya perintah lama, skrip lama,
atau kontainer yang belum di-rebuild tetap menemukan `app`.

Boleh dihapus setelah dipastikan tidak ada lagi yang menyebut `api.chat`.
"""

from .app import app  # noqa: F401
