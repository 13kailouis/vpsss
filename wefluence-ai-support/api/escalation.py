"""
ESKALASI KE ADMIN
=================

BUG PRODUKSI YANG DIPERBAIKI DI SINI
------------------------------------
Versi lama, waktu mendeteksi kata kunci, menulis:

    support_chats/{uid}.status = 'escalated'

Sementara layar admin (src/screens/AdminSupportChatsScreen.js baris 99 dan 153)
menyaring dengan:

    c.needsHumanSupport === true && c.status !== 'resolved'

Field `needsHumanSupport` TIDAK PERNAH ditulis oleh siapa pun kecuali tombol
Selesai di AdminChatInterface, yang menulisnya `false`. Artinya saringan
"butuh admin" di dasbor admin selamanya kosong, dan setiap keluhan yang
terdeteksi sistem hilang begitu saja. Sekarang kedua field ditulis bersamaan.

KENAPA TIDAK CUKUP DAFTAR KATA
------------------------------
Daftar lama memakai pencocokan substring polos, jadi "cape" ikut kena di
"capek", tapi juga di kata lain yang kebetulan memuatnya, dan "refund" membuat
brand yang cuma bertanya cara refund langsung dilempar ke admin. Sekarang:

- kata dicocokkan dengan batas kata, bukan substring
- pemicu dipisah jadi KERAS (langsung teruskan) dan LUNAK (menumpuk)
- pengulangan pertanyaan dihitung sebagai sinyal tersendiri, karena orang yang
  bertanya hal sama tiga kali memang tidak sedang tertolong
- model juga boleh meneruskan sendiri lewat alat eskalasi_ke_admin
"""

import re
import unicodedata

from .logging_setup import get

log = get(__name__)

# Pemicu keras: satu saja cukup untuk meneruskan.
HARD_PATTERNS = [
    # minta manusia
    (r"\b(chat|bicara|ngomong|hubungi|sambung(kan)?)\s+(sama\s+|ke\s+|dengan\s+)?(cs|admin|orang|manusia|customer service)\b", "minta bicara dengan admin"),
    (r"\b(cs|admin)\s*(dong|dong ah|nya mana|mana)\b", "minta bicara dengan admin"),
    (r"\bhuman support\b", "minta bicara dengan admin"),
    (r"\bbukan (bot|ai)\b", "minta bicara dengan admin"),
    # tuduhan penipuan
    (r"\b(penipu|penipuan|nipu|ditipu|ketipu|scam|bohong(i|in)?)\b", "menuduh penipuan"),
    # uang bermasalah
    (r"\b(uang|duit|saldo|dana)\s*(saya|aku|gue|gw)?\s*(hilang|ilang|raib|kepotong|dipotong|nggak masuk|gak masuk|ga masuk|belum masuk)\b", "melaporkan uang bermasalah"),
    (r"\b(balik(in|kan)?|kembali(kan|in)?)\s+(uang|duit|saldo|dana)\b", "minta uang dikembalikan"),
    # akun bermasalah
    (r"\b(akun|akun saya|akunku)\s*(saya|aku)?\s*(di)?(banned|ban|blokir|diblokir|suspend|ditangguhkan)\b", "akun diblokir"),
    # ancaman
    (r"\b(lapor|laporkan)\s+(polisi|ojk|kominfo|yls|yayasan)\b", "mengancam melapor"),
    (r"\b(tuntut|somasi|pengacara)\b", "mengancam jalur hukum"),
]

# Pemicu lunak: butuh dua atau lebih.
SOFT_PATTERNS = [
    (r"\b(kesel|kesal|marah|bete|bt|emosi|geram)\b", "nada marah"),
    (r"\b(bodoh|bego|goblok|tolol|anjir|anjing|bangsat|kampret)\b", "kata kasar"),
    (r"\b(nggak|gak|ga|tidak)\s+(guna|berguna|becus|beres|bener|jelas)\b", "menilai layanan buruk"),
    (r"\b(capek|cape|lelah)\s*(banget|bgt|deh)?\b", "kelelahan"),
    (r"\b(berkali-kali|berulang kali|udah berapa kali|sudah berapa kali|dari kemarin|dari minggu lalu)\b", "sudah berulang"),
    (r"\b(nggak|gak|ga|tidak)\s+(selesai|kelar|beres)(-selesai|-kelar|-beres)?\b", "masalah belum selesai"),
    (r"\b(komplain|keluhan|protes)\b", "menyampaikan komplain"),
    (r"\b(nyesel|menyesal|mau berhenti|hapus akun|keluar aja)\b", "mau berhenti"),
]

_HARD = [(re.compile(p, re.IGNORECASE), r) for p, r in HARD_PATTERNS]
_SOFT = [(re.compile(p, re.IGNORECASE), r) for p, r in SOFT_PATTERNS]

SOFT_THRESHOLD = 2
REPEAT_THRESHOLD = 3


def _normalize(text):
    """Rapikan sebelum dicocokkan.

    Yang dibereskan: huruf yang diregangkan ("cappeeek"), tanda baca yang
    dipakai memisah huruf, dan aksen. Tanpa ini pemicu gampang dilewati hanya
    dengan menulis lebih emosional, padahal justru pesan yang ditulis emosional
    itulah yang paling perlu diteruskan.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s-]", " ", text)
    text = re.sub(r"(.)\1{2,}", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _squeezed(text):
    """Varian kedua: SEMUA huruf kembar dipendekkan jadi satu.

    Perlu karena orang meregangkan huruf dengan pola yang tidak seragam.
    "cappeeek" tidak tertolong oleh aturan tiga-atau-lebih saja, karena "pp"
    cuma dua. Setelah semua kembaran dipendekkan dia jadi "capek".

    Varian ini dipakai SEBAGAI TAMBAHAN, bukan pengganti, karena memendekkan
    semua kembaran juga merusak kata yang memang berhuruf kembar. Aman di sini
    karena tidak ada satu pun pola di berkas ini yang memuat huruf kembar - dan
    itu syarat yang harus dijaga kalau nanti ada pola baru ditambahkan.
    """
    return re.sub(r"(.)\1+", r"\1", text)


def _similar(a, b):
    """Kemiripan kasar dua pesan, dihitung dari irisan kata."""
    wa = set(_normalize(a).split())
    wb = set(_normalize(b).split())
    if len(wa) < 3 or len(wb) < 3:
        return 0.0
    return len(wa & wb) / float(len(wa | wb))


def assess(message_text, history=None):
    """Kembalikan (perlu_eskalasi, alasan)."""
    normalized = _normalize(message_text)
    if not normalized:
        return False, None

    variants = (normalized, _squeezed(normalized))

    for pattern, reason in _HARD:
        if any(pattern.search(v) for v in variants):
            return True, reason

    hits = []
    for pattern, reason in _SOFT:
        if any(pattern.search(v) for v in variants):
            hits.append(reason)
    if len(hits) >= SOFT_THRESHOLD:
        return True, ", ".join(dict.fromkeys(hits))

    # Pengulangan: pertanyaan yang sama berkali-kali berarti jawabannya tidak
    # menolong, apa pun nada bicaranya.
    user_msgs = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    repeats = sum(1 for prev in user_msgs[-6:] if _similar(prev, message_text) >= 0.6)
    if repeats >= REPEAT_THRESHOLD - 1:
        return True, "menanyakan hal yang sama berulang kali"

    return False, None
