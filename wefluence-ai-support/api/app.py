"""
TITIK MASUK HTTP
================

Alur satu permintaan chat, berurutan:

  kunci internal -> identitas -> batas laju -> validasi teks -> konteks pengguna
  -> apakah admin sedang menangani -> riwayat -> nilai eskalasi -> model
  -> tulis ke Firestore -> balas

Yang berubah paling terasa dari versi lama:

- Kalau admin sedang memegang chat itu, AI DIAM. Dulu dia tetap menjawab dan
  menyela percakapan manusia.
- Kalau semua penyedia model mati, percakapannya DITERUSKAN ke admin, bukan
  ditutup dengan "sistem AI sedang sibuk, coba lagi nanti" yang membuat orang
  menunggu sesuatu yang tidak akan datang.
- Tidak ada lagi `except: pass`. Setiap kegagalan tercatat dan mengubah jawaban.
"""

import time

from flask import Flask, jsonify, request

from . import (
    auth,
    config,
    context,
    escalation,
    firestore_db,
    knowledge,
    llm,
    prompts,
    ratelimit,
    store,
    tools,
)
from .logging_setup import get, setup, short_uid

setup()
log = get(__name__)

app = Flask(__name__)

FALLBACK_REPLY = (
    "Maaf, aku lagi nggak bisa memproses ini. Chat kamu sudah aku teruskan ke "
    "admin Wefluence, jadi nggak usah kirim ulang ya."
)

ADMIN_HANDLING_REPLY = (
    "Chat ini lagi ditangani admin Wefluence, jadi aku nggak ikut menjawab dulu. "
    "Pesan kamu sudah masuk dan bakal dibalas orangnya."
)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Di produksi nginx yang mengatur CORS dan MEMBUANG header dari upstream
# (proxy_hide_header). Header di sini gunanya untuk pemanggilan langsung ke
# kontainer waktu menguji di VPS.
@app.after_request
def _cors(response):
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    response.headers.setdefault(
        "Access-Control-Allow-Headers",
        "Content-Type, Authorization, X-API-Key, X-Internal-Key, X-Requested-With",
    )
    return response


# ---------------------------------------------------------------------------
# Kesehatan
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def root():
    """deploy.sh memeriksa service dengan `curl -sf http://127.0.0.1:8000/`.
    Versi lama mengembalikan 404 di sini, jadi `./deploy.sh ai-support` selalu
    melaporkan FAIL walaupun servicenya sehat."""
    return jsonify({"service": config.SERVICE_NAME, "status": "ok"})


@app.route("/api/chat", methods=["GET"])
def chat_health():
    return jsonify(
        {
            "status": "ok",
            "service": config.SERVICE_NAME,
            "version": config.SERVICE_VERSION,
        }
    )


@app.route("/api/health", methods=["GET"])
def health():
    db = firestore_db.get_db()
    payload = {
        "service": config.SERVICE_NAME,
        "version": config.SERVICE_VERSION,
        "firestore": {
            "ok": db is not None,
            "database": config.FIRESTORE_DATABASE,
            "error": firestore_db.db_error(),
        },
        "providers": {
            "groq": {
                "configured": bool(config.GROQ_API_KEY),
                "model": config.GROQ_MODEL,
                "fallback": config.GROQ_MODEL_FALLBACK,
            },
            "gemini": {
                "configured": bool(config.GEMINI_API_KEY),
                "model": config.GEMINI_MODEL,
            },
        },
        "knowledge": {
            "fingerprint": knowledge.fingerprint(),
            "facts_total": len(knowledge.FACTS),
            "facts_active_creator": len(knowledge.facts_for("creator")),
            "facts_active_brand": len(knowledge.facts_for("brand")),
            "include_unreleased": knowledge.include_unreleased(),
        },
        "auth": {"require_auth": config.REQUIRE_AUTH},
        "ratelimit": ratelimit.stats(),
    }
    # ?probe=1 benar-benar memanggil tiap penyedia. Dipakai SEBELUM mengganti
    # model di .env, supaya nama model yang salah ketahuan di sini, bukan di
    # percakapan pengguna.
    if request.args.get("probe") == "1":
        payload["probe"] = llm.probe()
    return jsonify(payload)


@app.route("/api/chat", methods=["OPTIONS"])
def chat_options():
    return ("", 204)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    started = time.monotonic()

    try:
        auth.check_internal_key(request)
    except auth.AuthError as exc:
        return jsonify({"error": exc.message}), exc.status

    body = request.get_json(silent=True) or {}

    try:
        uid, trust = auth.resolve_identity(request, body)
    except auth.AuthError as exc:
        return jsonify({"error": exc.message}), exc.status

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text wajib diisi."}), 400
    text = text.strip()[: config.MAX_MESSAGE_CHARS]

    allowed, wait, limit_kind = ratelimit.check(uid)
    if not allowed:
        log.info("chat.rate_limited", extra={"uid": short_uid(uid), "kind": limit_kind})
        return (
            jsonify(
                {
                    "error": "Kebanyakan pesan dalam waktu singkat. Tunggu sebentar ya.",
                    "retryAfter": wait,
                }
            ),
            429,
        )

    ctx = context.load(uid)

    # Admin sedang memegang percakapan ini. Diam adalah jawaban yang benar.
    state = store.chat_state(uid)
    if state.get("adminHandling"):
        log.info("chat.admin_handling", extra={"uid": short_uid(uid)})
        return _respond(trust, ADMIN_HANDLING_REPLY, escalated=True, skipped="admin")

    history = prompts.build_history(body.get("history"), config.HISTORY_TURNS)
    if not history:
        history = prompts.build_history(
            store.load_history(uid, config.HISTORY_TURNS * 2), config.HISTORY_TURNS
        )

    pre_escalate, pre_reason = escalation.assess(text, history)

    tool_specs = tools.specs_for(ctx.get("role"))
    system_prompt = prompts.build_system_prompt(ctx, has_tools=bool(tool_specs))

    try:
        result = llm.complete(system_prompt, history, text, tool_specs, uid, ctx)
    except llm.LLMUnavailable as exc:
        # Semua penyedia gagal. Meneruskan ke admin jauh lebih baik daripada
        # menyuruh orang mencoba lagi nanti: dari sisi dia, permintaannya
        # menguap begitu saja.
        log.error("chat.llm_unavailable", extra={"uid": short_uid(uid), "detail": str(exc)})
        store.append_message(uid, FALLBACK_REPLY, "ai", {"aiFailed": True})
        store.update_summary(uid, FALLBACK_REPLY, True, "AI tidak tersedia")
        return _respond(trust, FALLBACK_REPLY, escalated=True, skipped="llm_down")

    escalate = pre_escalate or bool(result.escalation_reason)
    reason = result.escalation_reason or pre_reason

    wrote = store.append_message(
        uid,
        result.text,
        "ai",
        {"provider": result.provider, "model": result.model},
    )
    store.update_summary(uid, result.text, escalate, reason)

    log.info(
        "chat.answered",
        extra=dict(
            uid=short_uid(uid),
            role=ctx.get("role"),
            trust=trust,
            escalated=escalate,
            reason=reason,
            persisted=wrote,
            ms=int((time.monotonic() - started) * 1000),
            **result.as_log(),
        ),
    )

    return _respond(trust, result.text, escalated=escalate)


def _respond(trust, reply, escalated=False, skipped=None):
    """Balasan hanya ikut di body kalau pemanggilnya terbukti pemilik akun.

    Klien lama membaca pesan lewat listener Firestore dan mengabaikan body
    respons sepenuhnya, jadi menahan teks di sini tidak merusak apa pun sambil
    menutup jalur bocor yang dijelaskan di auth.py.
    """
    payload = {"status": "success", "escalated": escalated}
    if skipped:
        payload["skipped"] = skipped
    if trust == auth.TRUST_VERIFIED:
        payload["reply"] = reply
    return jsonify(payload)


@app.errorhandler(404)
def _not_found(_):
    return jsonify({"error": "Endpoint tidak ada."}), 404


@app.errorhandler(500)
def _server_error(_):
    log.error("http.unhandled", exc_info=True)
    return jsonify({"error": "Terjadi kesalahan di server."}), 500
