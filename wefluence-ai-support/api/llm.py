"""
PENYEDIA MODEL
==============

Rantai: daftar model Groq (disaring ke yang benar-benar hidup) -> Gemini.

Nama model di Groq PUNYA MASA HIDUP. `llama-3.3-70b-versatile` yang dulu dipatok
di config akhirnya dihapus Groq, dan sejak itu tiap permintaan dijawab HTTP 404 -
berbulan-bulan, tanpa ada yang tahu, karena gejalanya kelihatan seperti gangguan
sementara. Karena itu model sekarang dipilih dari daftar yang ditanyakan langsung
ke Groq (`available_groq_models`), bukan dari nama yang ditulis tangan.

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
import threading
import time

import requests

from . import config, tools
from .logging_setup import get

log = get(__name__)

_models_cache = {"at": 0.0, "ids": None}
_models_lock = threading.Lock()

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
_session.mount("https://", _adapter)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMUnavailable(Exception):
    pass


def _looks_degenerate(text):
    """Apakah balasan ini potongan rusak, bukan jawaban.

    KENAPA PERLU: model yang kehabisan jatah token di tengah tahap berpikir
    tetap mengembalikan HTTP 200. Dari sisi kode semuanya "berhasil", jadi
    tanpa pemeriksaan ini sampah seperti "Ini t t t... ? ... ... ..." dikirim
    apa adanya ke pengguna dan disimpan permanen di riwayat chatnya.

    Yang dianggap rusak: isinya didominasi titik-titik, atau terlalu pendek
    padahal berisi elipsis (tanda kalimat yang nggak pernah selesai). Ambangnya
    sengaja longgar supaya jawaban pendek yang SAH ("Iya, bisa.") tetap lolos.
    """
    t = (text or "").strip()
    if not t:
        return True
    dots = t.count(".") + t.count("\u2026") * 3
    if len(t) < 40 and ("\u2026" in t or "..." in t):
        return True
    if dots and dots / max(len(t), 1) > 0.28:
        return True
    if t.count("\u2026") >= 4:
        return True
    return False


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
# Model mana yang benar-benar ada
# ---------------------------------------------------------------------------

def available_groq_models(force=False):
    """Set id model yang hidup di akun Groq, atau None kalau nggak bisa dicek.

    None dan set kosong ARTINYA BEDA, dan bedanya penting: None = "nggak tahu"
    (jaringan gagal, kunci salah), dan waktu nggak tahu kita TIDAK boleh
    menyaring apa pun, karena menyaring dengan pengetahuan kosong sama saja
    dengan membuang semua model. Set kosong = "sudah dicek, memang nggak ada".
    """
    now = time.time()
    with _models_lock:
        fresh = now - _models_cache["at"] < config.GROQ_MODELS_TTL
        if not force and fresh and _models_cache["ids"] is not None:
            return _models_cache["ids"]

    if not config.GROQ_API_KEY:
        return None
    try:
        resp = _session.get(
            config.GROQ_BASE_URL.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + config.GROQ_API_KEY},
            timeout=_timeout(),
        )
        if resp.status_code != 200:
            log.warning("llm.models_list_failed", extra={"status": resp.status_code})
            return None
        ids = {m.get("id") for m in (resp.json().get("data") or []) if m.get("id")}
    except (requests.RequestException, ValueError):
        log.warning("llm.models_list_error", exc_info=True)
        return None

    with _models_lock:
        _models_cache["at"] = now
        _models_cache["ids"] = ids
    return ids


def groq_chain():
    """Urutan model yang dipakai hari ini: pilihan kita, disaring yang hidup."""
    prefer = config.groq_preference()
    live = available_groq_models()
    if not live:
        # Nggak tahu mana yang hidup: jalan apa adanya. Lebih baik mencoba dan
        # gagal di satu model daripada nggak mencoba sama sekali.
        return prefer
    usable = [m for m in prefer if m in live]
    dropped = [m for m in prefer if m not in live]
    if dropped:
        # Ini peringatan dininya. Kalau baris ini muncul, ada nama model di
        # konfigurasi yang sudah dipensiunkan Groq - dan dulu justru inilah yang
        # nggak pernah kelihatan sampai semuanya buntu.
        log.warning("llm.models_retired", extra={"dropped": dropped, "usable": usable})
    return usable or prefer


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

    # Model gpt-oss mengembalikan tahap berpikirnya kalau nggak diminta diam.
    # `hidden` bikin Groq membuangnya dan cuma mengirim jawaban akhirnya, jadi
    # jatah token nggak habis di teks yang memang nggak akan ditampilkan.
    # Parameter ini khusus keluarga gpt-oss; dikirim ke model lain bisa ditolak
    # 400, makanya dipagari nama model.
    if "gpt-oss" in model:
        payload["reasoning_format"] = "hidden"

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + config.GROQ_API_KEY,
    }
    url = config.GROQ_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        data = _post_with_retry(url, headers, payload, "groq/" + model)
    except LLMUnavailable:
        # Groq bisa saja menolak `reasoning_format` (nama parameternya berubah,
        # atau modelnya nggak mendukung). Kalau itu penyebabnya, mencoba sekali
        # lagi tanpa parameter itu jauh lebih baik daripada mematikan modelnya.
        if "reasoning_format" not in payload:
            raise
        payload.pop("reasoning_format", None)
        log.warning("llm.retry_without_reasoning_format", extra={"model": model})
        data = _post_with_retry(url, headers, payload, "groq/" + model)
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
            if _looks_degenerate(text):
                # Dilempar sebagai kegagalan, BUKAN dikirim. Dengan begitu
                # rantai penyedia lanjut ke model berikutnya, dan kalau semua
                # gagal, chatnya diteruskan ke admin - dua-duanya jauh lebih
                # baik daripada mengirim potongan kata ke orang yang lagi nanya.
                log.warning(
                    "llm.degenerate",
                    extra={"model": model, "sample": text[:80]},
                )
                raise LLMUnavailable("groq/" + model + ": balasan rusak")
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
    if _looks_degenerate(text):
        log.warning("llm.degenerate", extra={"model": config.GEMINI_MODEL, "sample": text[:80]})
        raise LLMUnavailable("gemini: balasan rusak")
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
        for model in groq_chain():
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

    live = available_groq_models(force=True)
    out["groq_terdaftar"] = sorted(live)[:40] if live else "tidak bisa dicek"
    chain = groq_chain()
    out["groq_urutan_dipakai"] = chain

    for i, model in enumerate(chain[:3]):
        label = "groq_%d_%s" % (i + 1, model.split("/")[-1])
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
