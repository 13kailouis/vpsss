"""
KLIEN FIRESTORE
===============

Satu klien, dibuat sekali, dipakai semua thread (klien google-cloud-firestore
aman untuk thread). Dibuat malas supaya kontainer tetap bisa hidup dan
`/api/health` tetap menjawab walau kredensialnya salah - dulu kegagalan init
cuma jadi `print` lalu semuanya mengembalikan konteks kosong tanpa penjelasan.
"""

import json
import threading

import firebase_admin
from firebase_admin import credentials

from . import config
from .logging_setup import get

log = get(__name__)

# RLock, BUKAN Lock. get_db() memegang kunci ini lalu memanggil init_admin_app()
# yang mengambil kunci yang sama. Dengan Lock biasa itu deadlock: permintaan
# pertama yang menyentuh Firestore menggantung selamanya, dan karena gunicorn
# punya batas waktu 120 detik, gejalanya di produksi bukan error melainkan
# "AI support diam". Ditemukan oleh tests/test_ai_support.py.
_lock = threading.RLock()
_db = None
_db_error = None
_admin_app = None


def _service_account_info():
    raw = config.FIREBASE_SERVICE_ACCOUNT
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # Penyebab paling sering: value di .env dibungkus kutip, atau private_key
        # dipecah jadi baris asli. Docker env_file memperlakukan kutip sebagai
        # karakter literal, jadi JSON-nya jadi rusak dan pesannya tidak jelas.
        raise RuntimeError(
            "FIREBASE_SERVICE_ACCOUNT bukan JSON yang sah. Harus 1 baris, tanpa "
            "kutip pembungkus, \n di private_key dibiarkan literal. (%s)" % exc
        ) from exc


def init_admin_app():
    """Firebase Admin dipakai untuk verify_id_token. Terpisah dari klien Firestore."""
    global _admin_app
    if _admin_app is not None:
        return _admin_app
    with _lock:
        if _admin_app is not None:
            return _admin_app
        if firebase_admin._apps:
            _admin_app = firebase_admin.get_app()
            return _admin_app
        info = _service_account_info()
        if info:
            _admin_app = firebase_admin.initialize_app(credentials.Certificate(info))
        else:
            _admin_app = firebase_admin.initialize_app()
        return _admin_app


def get_db():
    """Klien Firestore, atau None kalau init gagal (alasannya ada di db_error())."""
    global _db, _db_error
    if _db is not None:
        return _db
    with _lock:
        if _db is not None:
            return _db
        try:
            init_admin_app()
            from google.cloud import firestore as gfs

            info = _service_account_info()
            if info:
                from google.oauth2 import service_account

                cred = service_account.Credentials.from_service_account_info(info)
                _db = gfs.Client(
                    credentials=cred,
                    project=info.get("project_id"),
                    database=config.FIRESTORE_DATABASE,
                )
            else:
                _db = gfs.Client(database=config.FIRESTORE_DATABASE)
            _db_error = None
            log.info("firestore.ready", extra={"database": config.FIRESTORE_DATABASE})
            return _db
        except Exception as exc:  # noqa: BLE001 - dilaporkan lewat db_error()
            _db_error = str(exc)
            log.error("firestore.init_failed", exc_info=True)
            return None


def db_error():
    return _db_error


def server_timestamp():
    from google.cloud import firestore as gfs

    return gfs.SERVER_TIMESTAMP


def increment(n):
    from google.cloud import firestore as gfs

    return gfs.Increment(n)


def descending():
    from google.cloud import firestore as gfs

    return gfs.Query.DESCENDING
