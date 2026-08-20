"""
TULIS DAN BACA support_chats
============================

Semua sentuhan ke koleksi support_chats lewat sini, supaya bentuk dokumennya
tetap satu dan cocok dengan yang dibaca aplikasi:

  support_chats/{uid}                       - ringkasan, dibaca dasbor admin
  support_chats/{uid}/messages/{autoId}     - pesan, dibaca layar chat pengguna

Field yang dibaca aplikasi (jangan diubah namanya tanpa mengubah aplikasinya):
  lastMessage, lastMessageAt, unreadCount, status, needsHumanSupport
  messages: text, sender ('user' | 'ai' | 'admin' | 'system'), createdAt, isRead
"""

import time

from . import config, context, firestore_db
from .logging_setup import get

log = get(__name__)


def load_history(uid, limit):
    """Riwayat dari Firestore, dipakai kalau klien tidak mengirimkannya.

    Klien web dan mobile sekarang sudah mengirim riwayatnya sendiri, jadi jalur
    ini jarang terpakai. Tetap dipertahankan supaya panggilan dari mana pun
    (mis. pengujian manual dengan curl) tetap punya ingatan percakapan.
    """
    db = firestore_db.get_db()
    if db is None:
        return []
    try:
        docs = list(
            db.collection("support_chats")
            .document(uid)
            .collection("messages")
            .order_by("createdAt", direction=firestore_db.descending())
            .limit(limit)
            .stream()
        )
    except Exception:  # noqa: BLE001
        log.warning("store.history_failed", exc_info=True)
        return []

    docs.reverse()
    out = []
    for doc in docs:
        data = doc.to_dict() or {}
        text = (data.get("text") or "").strip()
        if not text:
            continue
        sender = data.get("sender")
        if sender == "user":
            out.append({"role": "user", "content": text})
        elif sender in ("ai", "admin"):
            # Balasan admin sengaja dimasukkan sebagai 'assistant'. Kalau tidak,
            # AI bisa mengulang jawaban yang barusan dikoreksi manusia.
            out.append({"role": "assistant", "content": text})
    return out


def _admin_replied_recently(db, uid, window_hours):
    """Apakah ada balasan admin di beberapa pesan terakhir.

    Perlu karena chat lama tidak punya `lastAdminReplyAt` - field itu baru
    ditulis setelah AdminChatInterface ikut dipatch. Dibatasi 5 dokumen dan
    hanya dipanggil untuk chat yang memang bertanda butuh admin, supaya
    tambahan bacanya tidak menempel di setiap pesan.
    """
    try:
        docs = (
            db.collection("support_chats")
            .document(uid)
            .collection("messages")
            .order_by("createdAt", direction=firestore_db.descending())
            .limit(5)
            .stream()
        )
    except Exception:  # noqa: BLE001
        log.warning("store.admin_scan_failed", exc_info=True)
        return False

    cutoff = time.time() * 1000 - window_hours * 3_600_000
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get("sender") == "admin":
            return context.to_millis(data.get("createdAt")) >= cutoff
    return False


def chat_state(uid):
    """Keadaan chat: sedang dipegang manusia atau tidak.

    KENAPA BUKAN SEKADAR "kalau sudah dieskalasi, AI diam"
    ------------------------------------------------------
    Versi pertama aturan ini membungkam AI begitu `needsHumanSupport` menyala.
    Itu salah, dan salahnya menumpuk: eskalasi bisa dipicu kata kunci (satu
    kalimat "ini penipuan" sudah cukup), sementara yang mematikannya cuma tombol
    Selesai di dasbor admin. Chat yang tidak sempat ditutup admin akan kehilangan
    asistennya SELAMANYA, tanpa ada yang tahu.

    Aturan sekarang: yang membungkam AI adalah MANUSIA YANG BENAR-BENAR SUDAH
    MENJAWAB, bukan bendera eskalasi. Selama belum ada admin yang masuk, AI tetap
    membantu sambil antreannya jalan. Begitu admin menjawab, AI mundur selama
    ADMIN_HANDOVER_HOURS supaya tidak menyela atau mengulang jawaban yang baru
    saja diralat.
    """
    db = firestore_db.get_db()
    if db is None:
        return {"exists": False, "adminHandling": False}
    try:
        snap = db.collection("support_chats").document(uid).get()
    except Exception:  # noqa: BLE001
        log.warning("store.chat_state_failed", exc_info=True)
        return {"exists": False, "adminHandling": False}

    if not snap.exists:
        return {"exists": False, "adminHandling": False}

    data = snap.to_dict() or {}
    status = data.get("status")
    needs_human = bool(data.get("needsHumanSupport"))

    if status == "resolved":
        return {
            "exists": True,
            "status": status,
            "adminHandling": False,
            "needsHumanSupport": needs_human,
        }

    handling = False
    last_admin_ms = context.to_millis(data.get("lastAdminReplyAt"))
    if last_admin_ms:
        hours = (time.time() * 1000 - last_admin_ms) / 3_600_000
        handling = hours < config.ADMIN_HANDOVER_HOURS
    elif needs_human:
        handling = _admin_replied_recently(db, uid, config.ADMIN_HANDOVER_HOURS)

    return {
        "exists": True,
        "status": status,
        "adminHandling": handling,
        "needsHumanSupport": needs_human,
    }


def append_message(uid, text, sender, extra=None):
    db = firestore_db.get_db()
    if db is None:
        return False
    payload = {
        "text": text,
        "sender": sender,
        "createdAt": firestore_db.server_timestamp(),
        "isRead": False,
    }
    if extra:
        payload.update(extra)
    try:
        db.collection("support_chats").document(uid).collection("messages").add(payload)
        return True
    except Exception:  # noqa: BLE001
        log.error("store.append_failed", extra={"sender": sender}, exc_info=True)
        return False


def update_summary(uid, last_message, escalate, escalation_reason=None):
    """Perbarui dokumen ringkasan yang dibaca dasbor admin.

    `needsHumanSupport` WAJIB ikut ditulis. Lihat catatan di escalation.py -
    dasbor admin menyaring dengan field itu, bukan dengan `status`, jadi menulis
    `status` saja membuat eskalasi tidak pernah kelihatan oleh siapa pun.
    """
    db = firestore_db.get_db()
    if db is None:
        return False

    payload = {
        "lastMessage": last_message[:500],
        "lastMessageAt": firestore_db.server_timestamp(),
        "unreadCount": firestore_db.increment(1),
        # Dibaca lencana admin di aplikasi (components/AdminSupportDock.js) untuk
        # menghitung siapa yang masih menunggu jawaban. Klien menulis 'user'
        # waktu orangnya mengirim, admin menulis 'admin' waktu membalas. Selama
        # AI masih menjawab, chatnya TIDAK dihitung menunggu - yang muncul di
        # lencana cuma yang AI-nya memang diam (admin sedang memegang) atau yang
        # dieskalasi.
        "lastSender": "ai",
    }
    if escalate:
        payload["status"] = "escalated"
        payload["needsHumanSupport"] = True
        payload["escalatedAt"] = firestore_db.server_timestamp()
        if escalation_reason:
            payload["escalationReason"] = escalation_reason[:200]

    try:
        db.collection("support_chats").document(uid).set(payload, merge=True)
        return True
    except Exception:  # noqa: BLE001
        log.error("store.summary_failed", exc_info=True)
        return False
