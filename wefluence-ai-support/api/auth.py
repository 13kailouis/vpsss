"""
IDENTITAS PEMANGGIL
===================

MASALAH YANG DITUTUP DI SINI
----------------------------
Versi lama menerima `userId` mentah dari body dan langsung memakainya untuk
membaca profil, email, dan saldo, lalu mengembalikan hasilnya di body HTTP.
Satu-satunya penjaga adalah `X-API-Key` di nginx - dan kunci itu ditulis apa
adanya di dalam bundel web publik (`SupportChatScreen.js`), jadi siapa pun yang
membuka DevTools bisa menyalinnya. Artinya: kirim userId siapa saja, tanya
"berapa saldo saya", dan saldo orang itu terbaca di respons.

TIGA TINGKAT KEPERCAYAAN
------------------------
1. `verified`  - ada `Authorization: Bearer <Firebase ID token>` yang sah. UID
                 diambil dari token, `userId` di body diabaikan sepenuhnya.
                 Balasan boleh dikembalikan lewat HTTP.
2. `legacy`    - tidak ada token, tapi REQUIRE_AUTH=0. Dilayani supaya klien
                 lama tidak mati, TAPI balasannya cuma ditulis ke Firestore.
                 Klien lama memang tidak pernah membaca body respons - dia
                 menampilkan pesan lewat onSnapshot - jadi tidak ada yang rusak.
3. ditolak     - REQUIRE_AUTH=1 dan tokennya tidak ada atau tidak sah.
"""

from firebase_admin import auth as fb_auth

from . import config, firestore_db
from .logging_setup import get

log = get(__name__)

TRUST_VERIFIED = "verified"
TRUST_LEGACY = "legacy"


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.message = message
        self.status = status


def _bearer(request):
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def check_internal_key(request):
    """Lapis kedua opsional. Kosong = mati (nginx sudah memeriksa X-API-Key)."""
    if not config.INTERNAL_API_KEY:
        return
    if request.headers.get("X-Internal-Key") != config.INTERNAL_API_KEY:
        raise AuthError("Kunci internal tidak valid.", status=403)


def resolve_identity(request, body):
    """Kembalikan (uid, trust). Melempar AuthError kalau tidak boleh dilayani."""
    token = _bearer(request)

    if token:
        try:
            firestore_db.init_admin_app()
            # check_revoked=False disengaja: verifikasinya jadi lokal (tanpa
            # panggilan jaringan ke Google per pesan). Kalau token dicabut,
            # paling lama satu jam sampai kedaluwarsa sendiri. Untuk chat CS
            # itu jauh lebih murah daripada menambah satu round-trip di jalur
            # yang sudah menunggu model.
            decoded = fb_auth.verify_id_token(token, check_revoked=False)
        except Exception as exc:  # noqa: BLE001 - pesan aslinya tidak untuk user
            log.warning("auth.token_invalid", extra={"reason": type(exc).__name__})
            raise AuthError("Sesi kamu sudah kedaluwarsa. Coba muat ulang halaman.")

        uid = decoded.get("uid") or decoded.get("user_id")
        if not uid:
            raise AuthError("Token tidak membawa UID.")

        claimed = (body or {}).get("userId")
        if claimed and claimed != uid:
            # Bukan sekadar dicatat lalu diteruskan: kalau ini pernah terjadi di
            # produksi, itu percobaan impersonasi atau bug klien yang serius.
            log.warning("auth.uid_mismatch", extra={"token_uid": uid[:6] + "~"})
            raise AuthError("userId tidak cocok dengan sesi login.", status=403)

        return uid, TRUST_VERIFIED

    if config.REQUIRE_AUTH:
        raise AuthError("Butuh login untuk memakai bantuan AI.")

    uid = (body or {}).get("userId")
    if not uid or not isinstance(uid, str):
        raise AuthError("userId wajib diisi.", status=400)
    if len(uid) > 128:
        raise AuthError("userId tidak valid.", status=400)

    return uid, TRUST_LEGACY
