"""
BATAS LAJU PER PENGGUNA
=======================

Nginx sudah membatasi per alamat IP (30r/s). Yang tidak dibatasi apa pun adalah
per AKUN, dan itu justru yang mahal: satu panggilan chat memicu satu sampai
empat panggilan model plus beberapa baca Firestore. Satu skrip yang menembak
seratus pesan dari satu akun tidak menyentuh batas nginx sama sekali, tapi
menghabiskan kuota Groq untuk semua orang.

CATATAN PENERAPAN
-----------------
Penghitungnya ada di memori proses. Itu disengaja: menambah Redis untuk satu
kontainer kecil tidak sebanding, dan Firestore terlalu mahal untuk dipakai
sebagai penghitung yang ditulis tiap pesan. Konsekuensinya batas ini berlaku
per worker gunicorn. Karena itu Dockerfile menjalankan SATU worker dengan
banyak thread, bukan banyak worker - lihat catatan di Dockerfile.
"""

import threading
import time

from . import config

_lock = threading.Lock()
_buckets = {}
_last_sweep = 0.0

MINUTE = 60.0
DAY = 86400.0


def _sweep(now):
    """Buang jejak akun yang sudah lama tidak muncul supaya dict tidak tumbuh
    selamanya di proses yang hidup berminggu-minggu."""
    global _last_sweep
    if now - _last_sweep < 600:
        return
    _last_sweep = now
    for uid in [u for u, b in _buckets.items() if not b["day"] or now - b["day"][-1] > DAY]:
        _buckets.pop(uid, None)


def check(uid):
    """Kembalikan (boleh, detik_tunggu, alasan)."""
    now = time.time()
    with _lock:
        _sweep(now)
        bucket = _buckets.setdefault(uid, {"minute": [], "day": []})

        bucket["minute"] = [t for t in bucket["minute"] if now - t < MINUTE]
        bucket["day"] = [t for t in bucket["day"] if now - t < DAY]

        if len(bucket["minute"]) >= config.RATE_LIMIT_PER_MINUTE:
            wait = int(MINUTE - (now - bucket["minute"][0])) + 1
            return False, wait, "per_minute"
        if len(bucket["day"]) >= config.RATE_LIMIT_PER_DAY:
            wait = int(DAY - (now - bucket["day"][0])) + 1
            return False, wait, "per_day"

        bucket["minute"].append(now)
        bucket["day"].append(now)
        return True, 0, None


def stats():
    with _lock:
        return {"tracked_users": len(_buckets)}
