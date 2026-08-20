"""
KONFIGURASI INFRASTRUKTUR
=========================

Semua yang dibaca dari environment ada di SATU berkas ini. Aturannya: modul lain
tidak boleh memanggil os.environ langsung. Alasannya bukan kerapian - berkas lama
(api/chat.py 292 baris) membaca GROQ_API_KEY di tengah berkas, sehingga tidak ada
satu tempat pun untuk melihat "apa saja yang harus diisi di .env", dan salah ketik
nama variabel baru ketahuan waktu produksi mati.

Nilai bisnis (tarif, minimum, biaya) TIDAK ada di sini. Itu ada di knowledge.py
karena angkanya harus dicocokkan dengan repo aplikasi, bukan dengan .env.
"""

import os


def _s(name, default=""):
    v = os.environ.get(name)
    return v.strip() if isinstance(v, str) and v.strip() else default


def _i(name, default):
    try:
        return int(_s(name, str(default)))
    except ValueError:
        return default


def _f(name, default):
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return default


def _b(name, default=False):
    v = _s(name, "").lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Penyedia model
# ---------------------------------------------------------------------------
# Rantainya: GROQ_MODEL -> GROQ_MODEL_FALLBACK -> Gemini. Groq yang punya alat
# (tool calling); Gemini sengaja dipakai TANPA alat, dia cuma menerima ringkasan
# data user yang sudah diambil duluan. Lihat llm.py bagian "kenapa Gemini tanpa alat".
#
# Default GROQ_MODEL sengaja dibiarkan sama dengan yang sudah jalan di produksi.
# Model yang lebih pintar (kimi-k2 / gpt-oss-120b) tinggal diisi lewat .env dan
# diuji dulu dengan: curl "http://127.0.0.1:8000/api/health?probe=1"
GROQ_API_KEY = _s("GROQ_API_KEY")
GROQ_BASE_URL = _s("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = _s("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACK = _s("GROQ_MODEL_FALLBACK", "openai/gpt-oss-120b")

GEMINI_API_KEY = _s("GEMINI_API_KEY")
GEMINI_BASE_URL = _s(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)
GEMINI_MODEL = _s("GEMINI_MODEL", "gemini-2.0-flash")

LLM_TIMEOUT = _f("LLM_TIMEOUT", 25.0)
LLM_CONNECT_TIMEOUT = _f("LLM_CONNECT_TIMEOUT", 5.0)
LLM_MAX_RETRIES = _i("LLM_MAX_RETRIES", 2)
LLM_TEMPERATURE = _f("LLM_TEMPERATURE", 0.2)
LLM_MAX_TOKENS = _i("LLM_MAX_TOKENS", 500)

# Berapa kali model boleh minta alat sebelum dipaksa menjawab. Tiap putaran =
# 1 panggilan model + N baca Firestore, jadi ini pengendali biaya sekaligus
# pengaman dari model yang berputar-putar memanggil alat yang sama.
MAX_TOOL_ROUNDS = _i("MAX_TOOL_ROUNDS", 3)

# ---------------------------------------------------------------------------
# Firestore
# ---------------------------------------------------------------------------
FIREBASE_SERVICE_ACCOUNT = _s("FIREBASE_SERVICE_ACCOUNT")
FIRESTORE_DATABASE = _s("FIRESTORE_DATABASE", "wefluence-jakarta")

# ---------------------------------------------------------------------------
# Keamanan
# ---------------------------------------------------------------------------
# REQUIRE_AUTH=0 (default) = mode peralihan. Klien lama yang cuma mengirim
# {userId, text} tetap dilayani, TAPI balasannya tidak dikembalikan di body HTTP,
# cuma ditulis ke Firestore (yang cuma bisa dibaca pemilik chat-nya).
#
# Kenapa begitu: X-API-Key ada di dalam bundel web publik, jadi siapa pun bisa
# membacanya. Kalau body HTTP ikut membawa balasan, penyerang tinggal POST
# userId orang lain dengan teks "berapa saldo saya" dan saldo korban terbaca di
# respons. Menahan balasan menutup jalur bocornya tanpa memutus klien lama.
#
# Setelah patch klien (kirim Authorization: Bearer <Firebase ID token>) naik ke
# produksi web + mobile, setel REQUIRE_AUTH=1 supaya jalur lama benar-benar mati.
REQUIRE_AUTH = _b("REQUIRE_AUTH", False)

# Lapis kedua, opsional. Nginx sudah memeriksa X-API-Key, tapi kalau suatu saat
# kontainer ini terekspos tanpa nginx, isi INTERNAL_API_KEY supaya tetap tertutup.
INTERNAL_API_KEY = _s("INTERNAL_API_KEY")

# ---------------------------------------------------------------------------
# Batas laju (per pengguna, bukan per IP - nginx yang urus per IP)
# ---------------------------------------------------------------------------
RATE_LIMIT_PER_MINUTE = _i("RATE_LIMIT_PER_MINUTE", 8)
RATE_LIMIT_PER_DAY = _i("RATE_LIMIT_PER_DAY", 120)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# Konteks pengguna (profil + saldo) dibaca ulang paling cepat tiap 45 detik.
# Percakapan CS itu balas-balasan cepat; tanpa cache satu keluhan 6 pesan =
# 6x baca profil + 6x baca dompet untuk data yang sama.
CONTEXT_CACHE_TTL = _i("CONTEXT_CACHE_TTL", 45)
CONTEXT_CACHE_MAX = _i("CONTEXT_CACHE_MAX", 512)

# ---------------------------------------------------------------------------
# Percakapan
# ---------------------------------------------------------------------------
MAX_MESSAGE_CHARS = _i("MAX_MESSAGE_CHARS", 1200)
HISTORY_TURNS = _i("HISTORY_TURNS", 8)

# Selama admin masih menangani sebuah chat, AI diam. Ini jendelanya (jam).
# Tanpa ini AI menyela di tengah percakapan manusia dan mengulang jawaban
# yang barusan dikoreksi admin.
ADMIN_HANDOVER_HOURS = _i("ADMIN_HANDOVER_HOURS", 12)

LOG_LEVEL = _s("LOG_LEVEL", "INFO")
SERVICE_NAME = "wefluence-ai-support"
SERVICE_VERSION = "2.0.0"
