"""
KONTEKS PENGGUNA
================

Profil + saldo orang yang sedang chat. Dipakai untuk mengisi prompt sistem dan
untuk menentukan basis pengetahuan mana yang dipakai (kreator atau brand).

DUA PERBAIKAN PENTING DARI VERSI LAMA
-------------------------------------
1. URUTAN PENCARIAN PERAN. Versi lama mencari di `users` DULU dan menandai
   siapa pun yang ketemu di sana sebagai admin. Di produksi `users` cuma berisi
   belasan dokumen admin, sementara `creators` berisi ratusan ribu. Jadi urutan
   lama membuang satu baca sia-sia untuk hampir setiap pengguna. Sekarang
   `creators` -> `brands` -> `users`.

2. CACHE. Percakapan CS itu balas-balasan cepat. Tanpa cache, satu keluhan
   6 pesan = 6x baca profil + 6x baca dompet untuk data yang sama persis, dan
   Firestore ditagih per baca (lihat catatan billing proyek). TTL pendek
   (default 45 detik) supaya saldo yang berubah tetap terasa segar.
"""

import threading
import time

from . import config, firestore_db
from .logging_setup import get

log = get(__name__)

UNKNOWN_CONTEXT = {
    "uid": None,
    "name": "Kamu",
    "role": "unknown",
    "email": None,
    "isVerified": False,
    "isPro": False,
    "balance": 0,
    "profileFound": False,
}

_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        expires_at, value = hit
        if expires_at < time.monotonic():
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key, value, ttl):
    with _cache_lock:
        if len(_cache) >= config.CONTEXT_CACHE_MAX:
            # Buang yang paling dekat kedaluwarsa. Bukan LRU sungguhan, tapi
            # untuk ratusan entri berumur puluhan detik ini sudah cukup dan
            # tidak menambah dependensi.
            oldest = min(_cache.items(), key=lambda kv: kv[1][0])[0]
            _cache.pop(oldest, None)
        _cache[key] = (time.monotonic() + ttl, value)


def invalidate(uid):
    with _cache_lock:
        _cache.pop(("ctx", uid), None)


def to_millis(value):
    """Timestamp Firestore / datetime / angka / string ISO -> milidetik. 0 kalau gelap."""
    if not value:
        return 0
    try:
        if hasattr(value, "timestamp"):
            return int(value.timestamp() * 1000)
        if isinstance(value, dict) and "seconds" in value:
            return int(value["seconds"]) * 1000
        if isinstance(value, (int, float)):
            # Firestore kadang menyimpan detik, kadang milidetik.
            return int(value if value > 10_000_000_000 else value * 1000)
        if isinstance(value, str):
            from datetime import datetime

            return int(
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
            )
    except (ValueError, TypeError, OverflowError, OSError):
        return 0
    return 0


def days_ago(value):
    ms = to_millis(value)
    if not ms:
        return None
    return max(0, int((time.time() * 1000 - ms) / 86_400_000))


_BULAN = [
    "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
    "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
]


def human_date(value):
    """Tanggal siap dibacakan AI, dalam bahasa dan zona waktu penggunanya.

    Bulannya ditulis manual, bukan lewat strftime("%b"), karena strftime
    mengikuti locale kontainer - dan kontainer python:3.11-slim itu locale C,
    jadi hasilnya "Aug" bukan "Agu". Menyetel locale di dalam kontainer bisa,
    tapi itu keadaan global yang menular ke semua thread, sementara yang
    dibutuhkan cuma dua belas kata.
    """
    ms = to_millis(value)
    if not ms:
        return None
    from datetime import datetime, timedelta, timezone

    # WIB. Service ini melayani pengguna Indonesia, jadi tanggal yang dikutip AI
    # harus tanggal yang sama dengan yang dilihat pengguna di layarnya.
    wib = timezone(timedelta(hours=7))
    d = datetime.fromtimestamp(ms / 1000, wib)
    return "%d %s %d" % (d.day, _BULAN[d.month - 1], d.year)


def _first_existing(db, uid, collections):
    for name, role in collections:
        try:
            snap = db.collection(name).document(uid).get()
        except Exception:  # noqa: BLE001
            log.warning("context.read_failed", extra={"collection": name}, exc_info=True)
            continue
        if snap.exists:
            return snap.to_dict() or {}, role
    return None, "unknown"


def load(uid):
    """Konteks pengguna. Selalu mengembalikan dict, tidak pernah melempar."""
    cached = _cache_get(("ctx", uid))
    if cached is not None:
        return cached

    ctx = dict(UNKNOWN_CONTEXT, uid=uid)
    db = firestore_db.get_db()
    if db is None:
        return ctx

    data, role = _first_existing(
        db,
        uid,
        [("creators", "creator"), ("brands", "brand"), ("users", "admin")],
    )

    if data is None:
        # Tetap di-cache. Kalau UID-nya memang tidak ada, mengulang tiga baca
        # untuk tiap pesan cuma menagih tanpa hasil.
        _cache_put(("ctx", uid), ctx, config.CONTEXT_CACHE_TTL)
        return ctx

    balance = 0
    try:
        wallet = db.collection("wallets").document(uid).get()
        if wallet.exists:
            balance = (wallet.to_dict() or {}).get("balance", 0) or 0
    except Exception:  # noqa: BLE001
        log.warning("context.wallet_failed", exc_info=True)

    stored_role = (data.get("role") or "").strip().lower()
    if stored_role in ("creator", "brand", "admin"):
        role = stored_role

    ctx.update(
        {
            "name": data.get("name") or data.get("company") or data.get("brandName") or "Kamu",
            "role": role,
            "email": data.get("email"),
            "isVerified": bool(data.get("isVerified")),
            "isPro": (data.get("subscriptionStatus") == "active")
            or bool(data.get("isProVerified")),
            "balance": balance,
            "profileFound": True,
            "accountAgeDays": days_ago(data.get("createdAt")),
            "username": data.get("username"),
            "feeSpendLifetime": data.get("feeSpendLifetime") if role == "brand" else None,
            "customFeeRate": data.get("customFeeRate") if role == "brand" else None,
        }
    )

    _cache_put(("ctx", uid), ctx, config.CONTEXT_CACHE_TTL)
    return ctx
