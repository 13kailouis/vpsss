"""
PENYEDIA MODEL
==============

Rantai: GROQ_MODEL -> GROQ_MODEL_FALLBACK -> Gemini.

Versi lama satu baris:

    resp = requests.post(GROQ_API_URL, json=payload, headers=headers)

Tiga hal yang salah di baris itu, semuanya sudah pernah menggigit sistem lain:
1. Tanpa `timeout`. requests tanpa timeout menunggu SELAMANYA. Kalau Groq
   menggantung koneksinya, worker gunicorn ikut menggantung sampai batas 60
   detiknya, dan selama itu worker tersebut tidak bisa melayani siapa pun.
2. Tanpa retry. Groq mengembalikan 429 waktu ramai. Sekali 429, pengguna dapat
   "sistem AI sedang sibuk" padahal mencoba lagi 800 milidetik kemudian hampir
   selalu berhasil.
3. Tanpa cadangan. Satu vendor bermasalah = fitur mati total.

KENAPA GEMINI DIPAKAI TANPA ALAT
--------------------------------
Gemini punya function calling, tapi protokolnya beda dari format OpenAI yang
dipakai Groq, jadi mendukungnya berarti memelihara dua alur alat sekaligus, dan
alur keduanya nyaris tidak pernah dijalani sehingga bugnya baru ketahuan tepat
di saat jalur utama sedang mati. Gantinya, jalur Gemini menerima RINGKASAN data
pengguna yang sudah diambil lebih dulu (tools.snapshot). Dia tetap bisa menjawab
pertanyaan data yang paling umum, dengan satu alur yang harus dijaga.
"""

import json
import time

import requests

from . import config, tools
from .logging_setup import get

log = get(__name__)

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
_session.mount("https://", _adapter)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMUnavailable(Exception):
    pass


class Result:
    def __init__(self, text, provider, model, tool_names=None, escalation_reason=None):
        self.text = text
        self.provider = provider
        self.model = model
        self.tool_names = tool_names or []
        self.escalation_reason = escalation_reason

    def as_log(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "tools": self.tool_names,
            "escalated_by_model": bool(self.escalation_reason),
        }


def _timeout():
    return (config.LLM_CONNECT_TIMEOUT, config.LLM_TIMEOUT)


def _post_with_retry(url, headers, payload, label):
    """POST dengan backoff. Melempar LLMUnavailable kalau semua percobaan gagal."""
    last = "tidak diketahui"
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            resp = _session.post(url, headers=headers, json=payload, timeout=_timeout())
        except requests.RequestException as exc:
            last = type(exc).__name__
            log.warning(
                "llm.request_error",
                extra={"provider": label, "attempt": attempt, "reason": last},
            )
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    last = "respons bukan JSON"
                    log.warning("llm.bad_json", extra={"provider": label})
            else:
                last = "HTTP " + str(resp.status_code)
                log.warning(
                    "llm.http_error",
                    extra={
                        "provider": label,
                        "attempt": attempt,
                        "status": resp.status_code,
                        # Dipotong: badan error Groq bisa panjang dan kadang
                        # memuat kembali seluruh prompt yang dikirim.
                        "body": resp.text[:300],
                    },
                )
                if resp.status_code not in RETRYABLE_STATUS:
                    break
        if attempt < config.LLM_MAX_RETRIES:
            time.sleep(0.4 * (2 ** attempt))
    raise LLMUnavailable(label + ": " + last)


# ---------------------------------------------------------------------------
# Groq (format OpenAI, dengan alat)
# ---------------------------------------------------------------------------

def _groq_once(model, messages, tool_specs):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
    }
    if tool_specs:
        payload["tools"] = tool_specs
        payload["tool_choice"] = "auto"

    data = _post_with_retry(
        config.GROQ_BASE_URL.rstrip("/") + "/chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.GROQ_API_KEY,
        },
        payload,
        "groq/" + model,
    )
    choices = data.get("choices") or []
    if not choices:
        raise LLMUnavailable("groq/" + model + ": respons tanpa choices")
    return choices[0].get("message") or {}


def _run_groq(model, system_prompt, history, user_text, tool_specs, uid, ctx):
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    used_tools = []
    escalation_reason = None

    for round_index in range(config.MAX_TOOL_ROUNDS):
        # Di putaran terakhir alatnya dicabut, supaya model terpaksa menjawab
        # dengan apa yang sudah ada dan tidak berputar memanggil alat sampai
        # timeout habis.
        allow_tools = tool_specs if round_index < config.MAX_TOOL_ROUNDS - 1 else None
        message = _groq_once(model, messages, allow_tools)
        calls = message.get("tool_calls") or []

        if not calls:
            text = (message.get("content") or "").strip()
            if not text:
                raise LLMUnavailable("groq/" + model + ": balasan kosong")
            return Result(text, "groq", model, used_tools, escalation_reason)

        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            }
        )

        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except (ValueError, TypeError):
                args = {}

            result = tools.run(name, args, uid, ctx)
            used_tools.append(name)
            if name == "eskalasi_ke_admin" and result.get("diteruskan"):
                escalation_reason = result.get("alasan")

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:6000],
                }
            )

    raise LLMUnavailable("groq/" + model + ": tidak selesai dalam batas putaran alat")


# ---------------------------------------------------------------------------
# Gemini (tanpa alat, dengan ringkasan data)
# ---------------------------------------------------------------------------

def _run_gemini(system_prompt, history, user_text, uid, ctx):
    snapshot_lines = tools.snapshot(uid, ctx)
    system = system_prompt
    if snapshot_lines:
        system += (
            "\n\n<data_terbaru_pengguna>\n"
            + "\n".join("- " + line for line in snapshot_lines)
            + "\n</data_terbaru_pengguna>"
        )

    contents = []
    for item in history:
        contents.append(
            {
                "role": "model" if item["role"] == "assistant" else "user",
                "parts": [{"text": item["content"]}],
            }
        )
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    url = (
        config.GEMINI_BASE_URL.rstrip("/")
        + "/models/"
        + config.GEMINI_MODEL
        + ":generateContent"
    )
    data = _post_with_retry(
        url,
        {
            "Content-Type": "application/json",
            "x-goog-api-key": config.GEMINI_API_KEY,
        },
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "temperature": config.LLM_TEMPERATURE,
                "maxOutputTokens": config.LLM_MAX_TOKENS,
            },
        },
        "gemini/" + config.GEMINI_MODEL,
    )

    candidates = data.get("candidates") or []
    if not candidates:
        # Paling sering karena promptFeedback.blockReason. Dicatat supaya kalau
        # ini sering muncul, penyebabnya kelihatan tanpa menebak.
        raise LLMUnavailable(
            "gemini: tanpa kandidat (" + str(data.get("promptFeedback"))[:120] + ")"
        )
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise LLMUnavailable("gemini: balasan kosong")
    return Result(text, "gemini", config.GEMINI_MODEL)


# ---------------------------------------------------------------------------
# Muka depan
# ---------------------------------------------------------------------------

def complete(system_prompt, history, user_text, tool_specs, uid, ctx):
    """Coba rantai penyedia sampai ada yang menjawab. Melempar LLMUnavailable
    kalau semuanya gagal - pemanggil yang memutuskan apa yang dikatakan ke
    pengguna, bukan berkas ini."""
    attempts = []

    if config.GROQ_API_KEY:
        models = [config.GROQ_MODEL]
        if config.GROQ_MODEL_FALLBACK and config.GROQ_MODEL_FALLBACK != config.GROQ_MODEL:
            models.append(config.GROQ_MODEL_FALLBACK)
        for model in models:
            try:
                return _run_groq(
                    model, system_prompt, history, user_text, tool_specs, uid, ctx
                )
            except LLMUnavailable as exc:
                attempts.append(str(exc))
                log.warning("llm.provider_failed", extra={"detail": str(exc)})

    if config.GEMINI_API_KEY:
        try:
            return _run_gemini(system_prompt, history, user_text, uid, ctx)
        except LLMUnavailable as exc:
            attempts.append(str(exc))
            log.warning("llm.provider_failed", extra={"detail": str(exc)})

    if not attempts:
        attempts.append("tidak ada penyedia yang dikonfigurasi (cek GROQ_API_KEY)")
    raise LLMUnavailable(" | ".join(attempts))


def probe():
    """Uji cepat tiap penyedia. Dipakai /api/health?probe=1 supaya pergantian
    model bisa diverifikasi SEBELUM trafik asli menabraknya."""
    out = {}
    ping = [{"role": "user", "content": "balas satu kata: ok"}]

    for label, model in (
        ("groq_primary", config.GROQ_MODEL),
        ("groq_fallback", config.GROQ_MODEL_FALLBACK),
    ):
        if not config.GROQ_API_KEY or not model:
            out[label] = {"ok": False, "detail": "tidak dikonfigurasi"}
            continue
        started = time.monotonic()
        try:
            message = _groq_once(model, ping, None)
            out[label] = {
                "ok": bool((message.get("content") or "").strip()),
                "model": model,
                "ms": int((time.monotonic() - started) * 1000),
            }
        except LLMUnavailable as exc:
            out[label] = {"ok": False, "model": model, "detail": str(exc)[:200]}

    if config.GEMINI_API_KEY:
        started = time.monotonic()
        try:
            result = _run_gemini("Balas satu kata.", [], "ok", None, {"role": "unknown"})
            out["gemini"] = {
                "ok": bool(result.text),
                "model": config.GEMINI_MODEL,
                "ms": int((time.monotonic() - started) * 1000),
            }
        except LLMUnavailable as exc:
            out["gemini"] = {"ok": False, "model": config.GEMINI_MODEL, "detail": str(exc)[:200]}
    else:
        out["gemini"] = {"ok": False, "detail": "tidak dikonfigurasi"}

    return out
