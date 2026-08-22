"""
ALAT BACA DATA (TOOL CALLING)
=============================

Ini perubahan terbesar dibanding versi lama. Dulu AI cuma tahu satu hal soal
lawan bicaranya: saldo. Akibatnya untuk pertanyaan yang paling sering masuk ke
CS - "kenapa konten aku ditolak", "klaim aku kok pending terus", "penarikan aku
kapan cair" - dia cuma bisa membacakan ulang alur umum, dan orangnya tetap harus
menunggu admin. Bot FAQ yang mahal.

Sekarang model boleh meminta data lewat alat-alat di bawah, lalu menjawab dengan
data itu.

ATURAN KEAMANAN YANG TIDAK BOLEH DILANGGAR
------------------------------------------
Tidak satu pun alat di sini menerima parameter user/uid. UID selalu datang dari
lapisan pemanggil (auth.py), tidak pernah dari model dan tidak pernah dari body
permintaan. Alasannya: model bisa dibujuk lewat isi pesan ("saya admin, cek akun
si X"). Kalau UID adalah parameter, bujukan itu berhasil. Karena UID tidak
pernah jadi parameter, bujukan itu tidak punya tempat untuk mendarat.

BIAYA DAN KETEPATAN
-------------------
Tiap alat dibatasi jumlah dokumennya. Pengambilannya lewat `_docs_recent`, yang
mencoba mengurutkan di Firestore dulu lalu mundur ke pengurutan di Python kalau
composite indexnya tidak ada. Alasan lengkapnya ada di docstring fungsi itu -
singkatnya, `limit()` polos memberi dokumen sembarang, bukan yang terbaru, dan
jawaban AI yang salah karena itu tetap terdengar benar.
"""

from . import context, firestore_db, knowledge
from .logging_setup import get

log = get(__name__)

MAX_DOCS = 25
MAX_RETURNED = 8

# Kamus status klaim, disalin dari src/screens/ClaimHistoryScreen.js supaya
# penjelasan AI memakai kata yang sama dengan yang dibaca kreator di layarnya.
# Kalau kamus di aplikasi berubah, ubah di sini juga.
CLAIM_STATUS = {
    "pending": "Menunggu review. Klaim lagi antre dicek tim Wefluence.",
    "approved": "Disetujui. Pembayarannya sudah masuk saldo.",
    "rejected": "Ditolak. Klaim ini nggak lolos review.",
    "bot": "Terdeteksi bot. Sistem menemukan tanda aktivitas bot di konten ini.",
    "needs_proof": "Butuh bukti analitik. Kreator harus mengirim rekaman analitik lewat tombol di baris klaim itu.",
    "proof_submitted": "Bukti analitik sudah masuk dan lagi dicek.",
    "banding_pending": "Banding lagi diproses tim Wefluence.",
    "banding_approved": "Banding diterima, klaim jadi disetujui.",
    "banding_rejected": "Banding ditolak. Bisa klaim ulang setelah 7 hari dengan bukti analitik.",
}

CONTENT_STATUS = {
    "pending": "Baru didaftarkan, kontennya belum dikirim.",
    "pending_admin_review": "Lagi diperiksa admin Wefluence.",
    "content_submitted": "Sudah lolos admin, sekarang giliran brand yang menilai.",
    "content_approved": "Disetujui brand. Viewsnya sudah bisa diklaim.",
    "content_rejected": "Ditolak.",
    "approved": "Disetujui.",
    "accepted": "Diterima di kampanye ini.",
    "rejected": "Ditolak.",
    "completed": "Selesai.",
    "budget_exhausted": "Budget kampanyenya habis.",
    "dana_habis": "Budget kampanyenya habis.",
    "expired": "Kampanyenya sudah lewat tanggal berakhir.",
}

WITHDRAWAL_STATUS = {
    "pending": "Lagi antre diproses tim keuangan.",
    "approved": "Sudah disetujui, dananya lagi dikirim ke rekening.",
    "completed": "Selesai, dana sudah dikirim.",
    "rejected": "Ditolak.",
}


class ToolError(Exception):
    pass


def _db():
    db = firestore_db.get_db()
    if db is None:
        raise ToolError("Database lagi nggak bisa diakses.")
    return db


def _docs(query_ref, limit=MAX_DOCS):
    return [
        dict((d.to_dict() or {}), _id=d.id) for d in query_ref.limit(limit).stream()
    ]


def _docs_recent(query_ref, order_field, limit=MAX_DOCS):
    """Ambil N dokumen TERBARU, bukan N dokumen sembarang.

    KENAPA INI TIDAK SESEDERHANA limit()
    ------------------------------------
    `where(...).limit(25)` tanpa orderBy mengembalikan 25 dokumen menurut urutan
    ID, bukan menurut waktu. Untuk kreator yang punya lebih dari 25 kiriman,
    "konten terakhir kamu" yang dijawab AI bisa jadi kiriman dari tiga bulan lalu
    dan tidak ada yang tahu bahwa jawabannya salah, karena bentuknya benar.

    Sebaliknya, `where(...).orderBy(...)` menuntut composite index. Sebagian
    sudah ada karena aplikasinya memakai pola yang sama (MyContent memakai
    creatorId + createdAt desc), sebagian belum (ClaimHistory sengaja tidak
    memakai orderBy). Yang belum ada gagal saat dijalankan, bukan saat ditulis.

    Jadi: coba yang benar dulu, dan kalau indexnya tidak ada, mundur ke cara
    lama sambil mencatatnya. Yang dicatat itu penting - dari log inilah ketahuan
    index mana yang perlu ditambahkan.
    """
    try:
        return [
            dict((d.to_dict() or {}), _id=d.id)
            for d in query_ref.order_by(
                order_field, direction=firestore_db.descending()
            )
            .limit(limit)
            .stream()
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tools.order_by_unavailable",
            extra={"field": order_field, "reason": type(exc).__name__},
        )
        return _docs(query_ref, limit)


def _newest_first(rows, *fields):
    def key(row):
        for field in fields:
            ms = context.to_millis(row.get(field))
            if ms:
                return ms
        return 0

    return sorted(rows, key=key, reverse=True)


def _rp(v):
    return knowledge.rupiah(v or 0)


# ---------------------------------------------------------------------------
# Implementasi alat
# ---------------------------------------------------------------------------

def cek_saldo(uid, ctx, args):
    db = _db()
    rows = _newest_first(
        _docs_recent(db.collection("transactions").where("userId", "==", uid), "createdAt", 15),
        "createdAt",
    )
    return {
        "saldo": _rp(ctx.get("balance")),
        "transaksi_terakhir": [
            {
                "tanggal": context.human_date(t.get("createdAt")),
                "jenis": t.get("type"),
                "jumlah": _rp(t.get("amount")),
                "status": t.get("status"),
                "keterangan": t.get("description"),
            }
            for t in rows[:MAX_RETURNED]
        ],
    }


def cek_penarikan(uid, ctx, args):
    db = _db()
    rows = _newest_first(
        _docs_recent(db.collection("withdrawals").where("userId", "==", uid), "createdAt"),
        "requestedAt",
        "createdAt",
    )
    if not rows:
        return {
            "jumlah": 0,
            "catatan": (
                "Belum pernah menarik sama sekali. Penarikan pertama seumur hidup "
                "bebas biaya."
            ),
        }
    return {
        "jumlah": len(rows),
        "penarikan": [
            {
                "tanggal_diminta": context.human_date(w.get("requestedAt") or w.get("createdAt")),
                "hari_berjalan": context.days_ago(w.get("requestedAt") or w.get("createdAt")),
                "nominal": _rp(w.get("amount")),
                "biaya": _rp(w.get("fee")),
                "diterima_bersih": _rp(w.get("netAmount")),
                "status": w.get("status"),
                "arti_status": WITHDRAWAL_STATUS.get(w.get("status"), "Status nggak dikenal."),
                "tanggal_diproses": context.human_date(w.get("processedAt")),
                "bebas_biaya": bool(w.get("feeWaived")),
                "tujuan": (w.get("bankDetails") or {}).get("bankCode"),
                "alasan_ditolak": w.get("rejectionReason") or w.get("adminNote"),
            }
            for w in rows[:MAX_RETURNED]
        ],
    }


def hitung_penarikan(uid, ctx, args):
    """Hitung biaya penarikan dan uang yang benar-benar masuk rekening.

    Kenapa ini alat, bukan dibiarkan dihitung model: 22 Agu 2026 di produksi
    Kailouis menjawab "tarik Rp 50.000, biaya 5% (Rp 2.500), masuk Rp 47.500".
    Kalimat KB-nya sudah menyebut lantai Rp 6.500, modelnya saja yang cuma
    mengalikan 5%. Yang benar biaya Rp 6.500 dan masuk Rp 43.500, selisih
    Rp 4.000 di satu penarikan terkecil. Salah hitung soal uang itu langsung
    jadi komplain ke admin, dan bunyinya tetap meyakinkan jadi nggak ada yang
    curiga sebelum dananya masuk.

    Status penarikan pertama TIDAK ditanyakan ke model, dibaca sendiri dari
    Firestore, karena itu yang menentukan pembebasan biaya.
    """
    nominal = (args or {}).get("nominal")
    if isinstance(nominal, str):
        nominal = nominal.replace(".", "").replace(",", "").replace("Rp", "").strip()
    try:
        nominal = int(float(nominal))
    except (TypeError, ValueError):
        return {"error": "Nominal penarikannya belum jelas. Tanya dulu mau tarik berapa."}
    if nominal <= 0:
        return {"error": "Nominal penarikan harus lebih dari nol."}

    db = _db()
    pertama = not _docs(
        db.collection("withdrawals").where("userId", "==", uid), limit=1
    )

    hitung = knowledge.hitung_biaya_penarikan(nominal, penarikan_pertama=pertama)

    umur = ctx.get("accountAgeDays")
    baru = isinstance(umur, int) and umur <= knowledge.NEW_CREATOR_GRACE_DAYS
    minimal = (
        knowledge.MIN_WITHDRAWAL_NEW_CREATOR if baru else knowledge.MIN_WITHDRAWAL
    )

    hasil = {
        "nominal": _rp(hitung["nominal"]),
        "biaya": _rp(hitung["biaya"]),
        "diterima_di_rekening": _rp(hitung["diterima"]),
        "penarikan_pertama": pertama,
        "minimal_penarikan": _rp(minimal),
        "lama_proses": "1 sampai 3 hari kerja",
    }
    if hitung["dibebaskan"]:
        hasil["biaya_dibebaskan"] = _rp(hitung["dibebaskan"])
        hasil["catatan_pembebasan"] = "Penarikan pertama seumur hidup, biayanya dibebaskan."
    elif hitung["kena_lantai"]:
        hasil["catatan_biaya"] = (
            "Yang dipakai biaya terkecil " + _rp(knowledge.WITHDRAWAL_FEE_FLOOR)
            + ", bukan 5%, karena 5% dari nominal ini lebih kecil dari itu."
        )
    if nominal < minimal:
        hasil["peringatan"] = (
            "Nominal ini di bawah minimal penarikan " + _rp(minimal)
            + ", jadi permintaannya bakal ditolak sistem."
        )
    saldo = ctx.get("balance")
    if isinstance(saldo, (int, float)) and nominal > saldo:
        hasil["peringatan_saldo"] = (
            "Nominal ini lebih besar dari saldo sekarang (" + _rp(saldo) + ")."
        )
    return hasil


def _flatten_contents(app):
    """Satu dokumen `applications` bisa memuat banyak konten di `contents[]`.

    Layar MyContent memperlakukan tiap elemen `contents` sebagai satu kartu, dan
    kalau `contents` kosong dia jatuh ke field datar `contentLink`/`status` di
    dokumen induknya. Perilaku itu ditiru di sini, karena kalau tidak, kreator
    yang mengirim tiga video ke satu kampanye akan terbaca sebagai satu konten
    saja dan AI akan menjawab salah soal video yang mana yang ditolak.
    """
    campaign_name = app.get("campaignTitle") or app.get("campaignName") or "(tanpa nama)"
    contents = app.get("contents")
    if isinstance(contents, list) and contents:
        for idx, c in enumerate(contents):
            c = c or {}
            yield {
                "kampanye": campaign_name,
                "nomor_konten": idx + 1,
                "link": c.get("link") or c.get("contentLink"),
                "status": c.get("status") or app.get("contentStatus") or app.get("status"),
                "alasan_ditolak": c.get("rejectionReason") or app.get("rejectionReason"),
                "kode_verifikasi": c.get("verificationCode") or app.get("verificationCode"),
                "dikirim": context.human_date(c.get("submittedAt") or app.get("createdAt")),
                "boleh_kirim_ulang": (
                    app.get("allowResubmit") is not False
                    and int(app.get("resubmitCount") or 0) < knowledge.MAX_RESUBMIT
                ),
            }
        return
    yield {
        "kampanye": campaign_name,
        "nomor_konten": 1,
        "link": app.get("contentLink"),
        "status": app.get("contentStatus") or app.get("status"),
        "alasan_ditolak": app.get("rejectionReason"),
        "kode_verifikasi": app.get("verificationCode"),
        "dikirim": context.human_date(app.get("createdAt")),
        "boleh_kirim_ulang": (
            app.get("allowResubmit") is not False
            and int(app.get("resubmitCount") or 0) < knowledge.MAX_RESUBMIT
        ),
    }


def cek_konten(uid, ctx, args):
    db = _db()
    apps = _newest_first(
        _docs_recent(db.collection("applications").where("creatorId", "==", uid), "createdAt"),
        "createdAt",
    )
    if not apps:
        return {"jumlah": 0, "catatan": "Belum ada konten yang pernah dikirim."}

    items = []
    for app in apps:
        for item in _flatten_contents(app):
            item["arti_status"] = CONTENT_STATUS.get(
                item.get("status"), "Status nggak dikenal."
            )
            items.append(item)

    wanted = (args or {}).get("hanya_status")
    if wanted:
        items = [i for i in items if i.get("status") == wanted]

    return {"jumlah": len(items), "konten": items[:MAX_RETURNED]}


def cek_klaim(uid, ctx, args):
    db = _db()
    rows = _newest_first(
        _docs_recent(db.collection("viewClaims").where("creatorId", "==", uid), "createdAt"),
        "claimedAt",
        "createdAt",
    )
    if not rows:
        return {
            "jumlah": 0,
            "catatan": (
                "Belum pernah klaim views sama sekali. Klaim pertama minimal "
                + str(knowledge.MIN_FIRST_CLAIM_VIEWS)
                + " views dan cuma bisa setelah kontennya disetujui brand."
            ),
        }
    return {
        "jumlah": len(rows),
        "klaim": [
            {
                "tanggal": context.human_date(c.get("claimedAt") or c.get("createdAt")),
                "views_diklaim": c.get("claimedViews"),
                "dibayar": _rp(c.get("paymentAmount")),
                "status": c.get("status"),
                "arti_status": CLAIM_STATUS.get(c.get("status"), "Status nggak dikenal."),
                "alasan_ditolak": c.get("rejectionReason") or c.get("adminNote"),
            }
            for c in rows[:MAX_RETURNED]
        ],
    }


def _campaign_names(db, ids):
    names = {}
    for cid in list(dict.fromkeys([i for i in ids if i]))[:MAX_RETURNED]:
        try:
            snap = db.collection("campaigns").document(cid).get()
            if snap.exists:
                d = snap.to_dict() or {}
                names[cid] = d.get("title") or d.get("name") or cid
        except Exception:  # noqa: BLE001
            continue
    return names


def cek_kampanye_saya(uid, ctx, args):
    db = _db()
    if ctx.get("role") == "brand":
        rows = _newest_first(
            _docs_recent(db.collection("campaigns").where("userId", "==", uid), "createdAt"),
            "createdAt",
        )
        out = []
        for c in rows[:MAX_RETURNED]:
            total = c.get("creatorBudget")
            if not isinstance(total, (int, float)):
                total = c.get("budget") or 0
            spent = c.get("totalEstimatedPayout") or 0
            out.append(
                {
                    "nama": c.get("title") or c.get("name"),
                    "status": c.get("status"),
                    "budget_kreator": _rp(total),
                    "kira_kira_sisa": _rp(max(0, float(total or 0) - float(spent or 0))),
                    "tarif_per_1000_views": _rp(c.get("payoutPerUnit")),
                    "berakhir": context.human_date(c.get("endDate") or c.get("deadline")),
                    "dibayar": c.get("paymentStatus"),
                }
            )
        return {"peran": "brand", "jumlah": len(rows), "kampanye": out}

    apps = _newest_first(
        _docs_recent(db.collection("applications").where("creatorId", "==", uid), "createdAt"),
        "createdAt",
    )
    names = _campaign_names(db, [a.get("campaignId") for a in apps])
    seen = {}
    for a in apps:
        cid = a.get("campaignId")
        if cid in seen:
            continue
        seen[cid] = {
            "nama": a.get("campaignTitle") or a.get("campaignName") or names.get(cid, cid),
            "status_lamaran": a.get("status"),
            "arti_status": CONTENT_STATUS.get(a.get("status"), "Status nggak dikenal."),
            "didaftarkan": context.human_date(a.get("createdAt")),
        }
    return {
        "peran": "creator",
        "jumlah": len(seen),
        "kampanye": list(seen.values())[:MAX_RETURNED],
    }


def cek_pengajuan_masuk(uid, ctx, args):
    """Brand: berapa konten yang menunggu keputusan dia."""
    if ctx.get("role") != "brand":
        raise ToolError("Alat ini cuma untuk akun brand.")
    db = _db()
    campaigns = _docs(db.collection("campaigns").where("userId", "==", uid))
    waiting = []
    for c in campaigns[:MAX_RETURNED]:
        cid = c.get("_id")
        try:
            apps = _docs(
                db.collection("applications")
                .where("campaignId", "==", cid)
                .where("status", "==", "content_submitted"),
                MAX_DOCS,
            )
        except Exception:  # noqa: BLE001
            log.warning("tools.brand_submissions_failed", exc_info=True)
            continue
        if apps:
            waiting.append(
                {
                    "kampanye": c.get("title") or c.get("name"),
                    "menunggu_keputusan": len(apps),
                }
            )
    total = sum(w["menunggu_keputusan"] for w in waiting)
    return {"total_menunggu": total, "per_kampanye": waiting}


def eskalasi_ke_admin(uid, ctx, args):
    """Model memutuskan sendiri kalau ini di luar kemampuannya.

    Tidak menulis apa pun ke Firestore dari sini. Yang menulis adalah app.py
    setelah balasannya jadi, supaya eskalasi dan pesannya tercatat sekali dalam
    satu urutan yang benar. Alat ini cuma mengibarkan bendera.
    """
    reason = (args or {}).get("alasan") or "Diminta pengguna"
    return {
        "diteruskan": True,
        "alasan": reason,
        "sampaikan_ke_pengguna": (
            "Ini aku teruskan ke admin Wefluence ya. Balasannya masuk di chat ini "
            "juga, jadi nggak usah kirim ulang."
        ),
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY = {
    "cek_saldo": dict(
        fn=cek_saldo,
        roles=("creator", "brand", "admin", "unknown"),
        description=(
            "Baca saldo dompet dan transaksi terakhir milik pengguna yang sedang "
            "chat. Pakai kalau dia menanyakan saldo, uang masuk, atau uang keluar."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    "cek_penarikan": dict(
        fn=cek_penarikan,
        roles=("creator", "brand", "admin"),
        description=(
            "Baca riwayat penarikan dana pengguna beserta statusnya. Pakai kalau dia "
            "bertanya penarikannya sudah cair belum, kenapa lama, atau kenapa ditolak."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    "hitung_penarikan": dict(
        fn=hitung_penarikan,
        roles=("creator",),
        description=(
            "Hitung biaya penarikan dan uang yang benar-benar masuk rekening untuk "
            "satu nominal. WAJIB dipakai setiap kali pengguna menanyakan berapa yang "
            "dia terima kalau menarik sekian, atau berapa biayanya. Jangan pernah "
            "menghitungnya sendiri: biayanya bukan 5% polos, tapi mana yang lebih "
            "besar antara 5% dan biaya terkecil, dan alat ini juga tahu apakah ini "
            "penarikan pertamanya."
        ),
        parameters={
            "type": "object",
            "properties": {
                "nominal": {
                    "type": "integer",
                    "description": "Nominal penarikan dalam rupiah, angka polos tanpa titik.",
                }
            },
            "required": ["nominal"],
        },
    ),
    "cek_konten": dict(
        fn=cek_konten,
        roles=("creator",),
        description=(
            "Baca daftar konten yang pernah dikirim kreator, lengkap dengan status "
            "tiap konten dan alasan penolakannya. Pakai untuk pertanyaan seperti "
            "kenapa konten saya ditolak, konten saya sudah disetujui belum, atau "
            "saya boleh kirim ulang nggak."
        ),
        parameters={
            "type": "object",
            "properties": {
                "hanya_status": {
                    "type": "string",
                    "description": (
                        "Opsional. Saring ke satu status saja, misalnya "
                        "content_rejected atau content_approved."
                    ),
                }
            },
        },
    ),
    "cek_klaim": dict(
        fn=cek_klaim,
        roles=("creator",),
        description=(
            "Baca riwayat klaim views kreator beserta status dan alasan penolakan. "
            "Pakai kalau dia bertanya klaimnya kenapa pending, kenapa ditolak, atau "
            "kenapa uangnya belum masuk."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    "cek_kampanye_saya": dict(
        fn=cek_kampanye_saya,
        roles=("creator", "brand"),
        description=(
            "Baca kampanye yang berkaitan dengan pengguna. Untuk kreator: kampanye "
            "yang dia ikuti dan status lamarannya. Untuk brand: kampanye miliknya, "
            "sisa budget, dan tarifnya."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    "cek_pengajuan_masuk": dict(
        fn=cek_pengajuan_masuk,
        roles=("brand",),
        description=(
            "Khusus brand. Hitung konten kreator yang sedang menunggu keputusan brand "
            "di tiap kampanye miliknya."
        ),
        parameters={"type": "object", "properties": {}},
    ),
    "eskalasi_ke_admin": dict(
        fn=eskalasi_ke_admin,
        roles=("creator", "brand", "admin", "unknown"),
        description=(
            "Teruskan percakapan ini ke admin manusia. Pakai kalau pengguna memang "
            "minta bicara dengan orang, kalau dia melaporkan uang hilang atau dugaan "
            "penipuan, kalau dia sudah jelas marah, atau kalau kamu sudah mencoba dan "
            "datanya tetap tidak menjelaskan masalahnya."
        ),
        parameters={
            "type": "object",
            "properties": {
                "alasan": {
                    "type": "string",
                    "description": "Satu kalimat singkat untuk admin, bahasa Indonesia.",
                }
            },
            "required": ["alasan"],
        },
    ),
}


def specs_for(role):
    """Definisi alat dalam format OpenAI/Groq, disaring menurut peran."""
    role = (role or "unknown").lower()
    out = []
    for name, meta in _REGISTRY.items():
        if role not in meta["roles"]:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": meta["description"],
                    "parameters": meta["parameters"],
                },
            }
        )
    return out


def run(name, args, uid, ctx):
    """Jalankan satu alat. Selalu mengembalikan dict, tidak pernah melempar ke atas."""
    meta = _REGISTRY.get(name)
    if not meta:
        return {"error": "Alat tidak dikenal."}
    if (ctx.get("role") or "unknown").lower() not in meta["roles"]:
        return {"error": "Alat ini tidak berlaku untuk peran akun ini."}
    try:
        return meta["fn"](uid, ctx, args or {})
    except ToolError as exc:
        return {"error": str(exc)}
    except Exception:  # noqa: BLE001
        log.error("tools.failed", extra={"tool": name}, exc_info=True)
        return {"error": "Data ini lagi nggak bisa dibaca. Sarankan hubungi admin."}


# ---------------------------------------------------------------------------
# Ringkasan untuk penyedia tanpa alat (Gemini)
# ---------------------------------------------------------------------------

def snapshot(uid, ctx):
    """Ringkasan data hidup, dipakai kalau modelnya tidak bisa memanggil alat.

    Sengaja dibatasi tiga alat termurah. Ini jalur cadangan yang cuma jalan waktu
    Groq mati, jadi tujuannya "tetap bisa menjawab pertanyaan data yang paling
    umum", bukan menyamai jalur utama.
    """
    role = (ctx.get("role") or "unknown").lower()
    parts = []
    try:
        if role in ("creator", "brand"):
            wd = cek_penarikan(uid, ctx, {})
            latest = (wd.get("penarikan") or [None])[0]
            if latest:
                parts.append(
                    "Penarikan terakhir "
                    + str(latest.get("nominal"))
                    + " status "
                    + str(latest.get("status"))
                    + " (" + str(latest.get("arti_status")) + ")"
                )
        if role == "creator":
            konten = cek_konten(uid, ctx, {})
            for item in (konten.get("konten") or [])[:3]:
                parts.append(
                    "Konten di kampanye " + str(item.get("kampanye"))
                    + " status " + str(item.get("status"))
                    + (
                        ", alasan: " + str(item.get("alasan_ditolak"))
                        if item.get("alasan_ditolak")
                        else ""
                    )
                )
            klaim = cek_klaim(uid, ctx, {})
            latest = (klaim.get("klaim") or [None])[0]
            if latest:
                parts.append(
                    "Klaim terakhir " + str(latest.get("views_diklaim"))
                    + " views, status " + str(latest.get("status"))
                )
    except Exception:  # noqa: BLE001
        log.warning("tools.snapshot_failed", exc_info=True)
    return parts
