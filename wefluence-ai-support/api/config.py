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
# KENAPA INI DAFTAR, BUKAN SATU NAMA
# ----------------------------------
# Nama model di Groq PUNYA MASA HIDUP. `llama-3.3-70b-versatile` yang dulu
# ditulis di sini akhirnya dihapus Groq, dan sejak itu setiap permintaan dijawab
# HTTP 404. Versi lama service ini nggak punya cadangan, jadi tiap pesan buntu
# dengan "sistem AI sedang sibuk" - selama berbulan-bulan, dan nggak ada yang
# tahu karena gejalanya kelihatan seperti gangguan sementara.
#
# Jadi yang dipatok di sini bukan satu nama, tapi URUTAN PILIHAN. Waktu jalan,
# daftar ini disaring dulu terhadap model yang BENAR-BENAR ada di akun Groq
# (lihat `available_groq_models` di llm.py), jadi nama yang sudah dipensiunkan
# dilewati sendiri, bukan dipakai lalu gagal.
#
# Urutannya dari yang paling diinginkan. Tambahkan nama baru di DEPAN kalau mau
# mencoba model lain; yang mati di belakangnya nggak perlu dihapus buru-buru.
GROQ_API_KEY = _s("GROQ_API_KEY")
GROQ_BASE_URL = _s("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODELS = [
    m.strip() for m in _s(
        "GROQ_MODELS",
        # DAFTAR INI DICOCOKKAN KE AKUN GROQ SUNGGUHAN, 21 Agu 2026.
        #
        # Groq sudah MENGHAPUS seluruh keluarga llama (llama-3.3-70b-versatile,
        # llama-3.1-8b-instant) dan moonshotai/kimi-k2-instruct dari akun ini.
        # Yang tersisa dan layak dipakai buat chat cuma keluarga gpt-oss dan
        # qwen. Diperiksa lewat `GET /api/health?probe=1` -> `groq_terdaftar`,
        # bukan dari ingatan.
        #
        # KONSEKUENSI YANG HARUS DIINGAT: semuanya model REASONING. Mereka
        # menghabiskan token buat berpikir sebelum menjawab, jadi dua pengaman
        # di bawah ini WAJIB tetap ada, bukan opsional:
        #   - LLM_MAX_TOKENS longgar (900), supaya jawabannya nggak terpotong
        #     jadi potongan kata seperti "Ini t t t... ? ... ... ..."
        #   - `reasoning_format: hidden` di llm.py buat model gpt-oss
        #   - `_looks_degenerate()` sebagai jaring terakhir
        #
        # `groq/compound*` sengaja TIDAK dipakai: itu sistem agentik dengan
        # perkakasnya sendiri, dan layar ini butuh tool calling versi kita.
        "openai/gpt-oss-120b,"
        "qwen/qwen3.6-27b,"
        "openai/gpt-oss-20b",
    ).split(",") if m.strip()
]

# Dipertahankan supaya .env lama yang menyetel dua variabel ini tetap dihormati.
# Kalau diisi, dia naik ke DEPAN daftar di atas.
GROQ_MODEL = _s("GROQ_MODEL")
GROQ_MODEL_FALLBACK = _s("GROQ_MODEL_FALLBACK")


def groq_preference():
    """Urutan model yang mau dicoba, tanpa duplikat, tanpa yang kosong."""
    out = []
    for m in [GROQ_MODEL, GROQ_MODEL_FALLBACK] + GROQ_MODELS:
        if m and m not in out:
            out.append(m)
    return out


# Berapa lama daftar model hidup di-cache (detik). Model nggak muncul dan hilang
# tiap menit, jadi sejam sudah lebih dari cukup dan biayanya satu panggilan.
GROQ_MODELS_TTL = _i("GROQ_MODELS_TTL", 3600)

GEMINI_API_KEY = _s("GEMINI_API_KEY")
GEMINI_BASE_URL = _s(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)
GEMINI_MODEL = _s("GEMINI_MODEL", "gemini-2.0-flash")

LLM_TIMEOUT = _f("LLM_TIMEOUT", 25.0)
LLM_CONNECT_TIMEOUT = _f("LLM_CONNECT_TIMEOUT", 5.0)
LLM_MAX_RETRIES = _i("LLM_MAX_RETRIES", 2)
LLM_TEMPERATURE = _f("LLM_TEMPERATURE", 0.2)
# Dinaikkan dari 500. Jawaban CS memang pendek, TAPI batas ini juga menampung
# tahap berpikir model reasoning. Terlalu ketat = jawabannya terpotong di tengah
# dan keluar sebagai potongan kata, bukan sebagai error yang kelihatan.
LLM_MAX_TOKENS = _i("LLM_MAX_TOKENS", 900)

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
