"""
BASIS PENGETAHUAN
=================

SATU-SATUNYA tempat angka bisnis hidup di service ini. Kalau ada angka rupiah
atau persen muncul di berkas lain, itu bug.

KENAPA BERKAS INI BENTUKNYA BEGINI
----------------------------------
Versi lama menyimpan pengetahuan sebagai tiga string panjang. Hasilnya membusuk
tanpa suara: per Agustus 2026 dia masih mengajari pengguna bahwa biaya platform
12% (padahal sudah bertingkat mulai 15%), budget minimum Rp 5,5 juta (padahal
Rp 1,5 juta), dan tidak tahu satu pun fitur yang dibangun sepanjang 2026.
Tidak ada yang mengingatkan, karena tidak ada yang bisa membandingkannya dengan
apa pun.

Sekarang tiap fakta punya:
  - source   : berkas di repo aplikasi tempat angka itu ditegakkan. Ini yang
               dibaca scripts/check_kb_sync.py untuk memberi tahu kalau
               aplikasinya berubah dan berkas ini belum ikut berubah.
  - released : False = kodenya sudah ditulis di repo tapi BELUM di-deploy ke
               produksi. Fakta begini TIDAK dikirim ke model secara default,
               karena memberi tahu kreator soal tombol yang belum ada di
               layarnya lebih merugikan daripada diam. Nyalakan dengan
               KB_INCLUDE_UNRELEASED=1 setelah deploy.

PERINGATAN YANG SENGAJA TIDAK DISEMBUNYIKAN
-------------------------------------------
Tangga fee di bawah adalah tangga FINAL yang tertulis di
functions/src/platformFee.js. Kalau yang benar benar berjalan di produksi masih
tangga versi sebelumnya, AI akan menyebut angka yang tidak ditagihkan. Jalankan
scripts/check_kb_sync.py sebelum percaya berkas ini.
"""

import hashlib
import os

# ---------------------------------------------------------------------------
# Angka. Dipakai juga oleh tools.py untuk memformat jawaban.
# ---------------------------------------------------------------------------

MIN_WITHDRAWAL = 50000
MIN_WITHDRAWAL_NEW_CREATOR = 20000
NEW_CREATOR_GRACE_DAYS = 30
WITHDRAWAL_FEE_RATE = 0.05
WITHDRAWAL_FEE_FLOOR = 6500
FIRST_WITHDRAWAL_WAIVER_CAP = 50000

MIN_CAMPAIGN_BUDGET = 1500000
MIN_PAYOUT_PER_1000 = 1000
MIN_PAYOUT_PER_1000_UGC = 5000
CAMPAIGN_TOPUP_FEE_RATE = 0.10

MIN_FIRST_CLAIM_VIEWS = 500
MAX_RESUBMIT = 2
CLAIM_REJECT_BLOCK_LIMIT = 3

PRO_PRICE_MONTHLY = 49000
PRO_PRICE_YEARLY = 490000

# Cermin FEE_LADDER di functions/src/platformFee.js. (batas atas inklusif, rate)
FEE_LADDER = [
    (50000000, 0.15),
    (150000000, 0.14),
    (250000000, 0.13),
    (500000000, 0.12),
    (float("inf"), 0.10),
]

SUPPORTED_BANKS = ["BCA", "BNI", "BRI", "Mandiri", "Permata"]
SUPPORTED_EWALLETS = ["GoPay", "DANA", "OVO"]
SUPPORTED_PLATFORMS = ["TikTok", "Instagram Reels", "YouTube Shorts"]


def rupiah(amount):
    """Format rupiah gaya Indonesia. Titik ribuan, tanpa desimal.

    Versi lama punya bug tampilan di sini: pemanggilnya menulis
    f"Rp {format_currency(x)}" padahal format_currency sudah menambahkan "Rp",
    jadi konteks yang dikirim ke model berbunyi "Rp Rp 50.000".
    """
    try:
        n = int(round(float(amount)))
    except (TypeError, ValueError):
        return "Rp 0"
    return "Rp " + f"{n:,}".replace(",", ".")


def ladder_rate_for(lifetime_spend):
    """Rate fee menurut total belanja seumur hidup brand (inklusif transaksi berjalan)."""
    try:
        total = max(0.0, float(lifetime_spend))
    except (TypeError, ValueError):
        total = 0.0
    for upper, rate in FEE_LADDER:
        if total <= upper:
            return rate
    return FEE_LADDER[-1][1]


def _ladder_text():
    rows = []
    low = 0
    for upper, rate in FEE_LADDER:
        pct = f"{rate * 100:.0f}%"
        if upper == float("inf"):
            rows.append("di atas " + rupiah(low) + " = " + pct)
        else:
            rows.append(rupiah(low) + " sampai " + rupiah(upper) + " = " + pct)
            low = upper
    return "; ".join(rows)


# ---------------------------------------------------------------------------
# Fakta
# ---------------------------------------------------------------------------
# roles: "creator" / "brand" / "all". Fakta "all" masuk ke semua peran.

FACTS = [
    # --- Dasar platform ----------------------------------------------------
    dict(
        id="platform.what",
        roles="all",
        topic="dasar",
        source="-",
        text=(
            "Wefluence itu marketplace konten bayar per hasil. Brand bikin kampanye "
            "dan menaruh budget di depan, kreator bikin konten di akun sosmed-nya "
            "sendiri, lalu dibayar dari budget itu sesuai hasilnya."
        ),
    ),
    dict(
        id="platform.campaign_kinds",
        roles="all",
        topic="dasar",
        source="src/screens/CampaignTypeSelectorScreen.js",
        text=(
            "Dua kategori kampanye. UGC = kreator bikin konten asli soal produk brand "
            "(produk digital, produk fisik yang dikirim ke kreator, atau restoran). "
            "Clipping = kreator memotong materi yang sudah disediakan brand (podcast, "
            "edukasi, film, musik, game, atau materi brand)."
        ),
    ),
    dict(
        id="platform.channels",
        roles="all",
        topic="dasar",
        source="src/utils/validation.js",
        text=(
            "Kontennya di-upload ke akun kreator sendiri di "
            + ", ".join(SUPPORTED_PLATFORMS)
            + ". Akun kreator wajib publik, kalau digembok viewsnya nggak bisa dicek."
        ),
    ),
    dict(
        id="platform.handle",
        roles="all",
        topic="dasar",
        source="src/utils/handles.js",
        text=(
            "Tiap kreator punya alamat profil sendiri di wefluence.app/u/username. "
            "Itu link yang dipakai buat kirim portofolio ke brand."
        ),
    ),
    dict(
        id="platform.pricing_models",
        roles="all",
        topic="dasar",
        source="src/screens/CampaignTypeSelectorScreen.js",
        released=False,
        text=(
            "Ada dua model bayar. Per views: kreator dibayar per 1.000 views, makin "
            "banyak views makin besar bayarannya. Per video: brand bayar harga tetap "
            "per video yang lolos, berapa pun viewsnya."
        ),
    ),

    # --- Alur kreator ------------------------------------------------------
    dict(
        id="creator.flow",
        roles="creator",
        topic="alur",
        source="src/screens/SubmitContentScreen.js",
        text=(
            "Alur lengkap kreator: pilih kampanye lalu daftar, upload konten ke "
            "TikTok / IG Reels / YouTube Shorts dari akun sendiri, salin link "
            "kontennya dan kirim lewat menu Kirim konten, admin cek dulu lalu brand "
            "yang memutuskan, setelah disetujui baru bisa klaim views, hasilnya masuk "
            "saldo, lalu tarik ke bank atau e-wallet."
        ),
    ),
    dict(
        id="creator.verification_code",
        roles="creator",
        topic="konten",
        source="src/utils/verificationCode.js",
        text=(
            "Tiap kiriman konten punya kode verifikasi unik, contohnya a7x2k9m4. Kode "
            "itu WAJIB ada di caption postingannya, kalau nggak ada kontennya ditolak. "
            "Gunanya membuktikan video itu memang punya kamu. Kodenya ganti tiap kali "
            "kirim ulang, jadi selalu salin yang terbaru dari layar kirim konten dan "
            "jangan diketik ulang manual, huruf l kecil dan angka 1 gampang ketuker."
        ),
    ),
    dict(
        id="creator.review_time",
        roles="creator",
        topic="konten",
        source="functions/src/moderationAutomation.js",
        text=(
            "Setelah link dikirim: admin memeriksa keasliannya dulu, baru brand yang "
            "menyetujui atau menolak. Biasanya 1 sampai 3 hari kerja sampai dua-duanya "
            "selesai. Selama masih ditinjau, viewsnya belum bisa diklaim."
        ),
    ),
    dict(
        id="creator.rejection_reasons",
        roles="creator",
        topic="konten",
        source="functions/src/moderationAutomation.js",
        text=(
            "Alasan konten ditolak yang paling sering: kode verifikasi nggak ada di "
            "caption atau salah ketik, isi kontennya nggak sesuai brief kampanye, "
            "linknya salah atau nggak bisa dibuka, akunnya diprivat, atau linknya sudah "
            "pernah dipakai buat klaim di tempat lain."
        ),
    ),
    dict(
        id="creator.duplicate_link",
        roles="creator",
        topic="konten",
        source="functions/src/contentDedup.js",
        text=(
            "Satu video cuma boleh dipakai di satu kampanye. Kalau link yang sama "
            "dikirim lagi ke kampanye lain, sistem menolaknya otomatis. Ini bukan "
            "hukuman, cuma penjaga supaya satu video nggak dibayar dua kali."
        ),
    ),
    dict(
        id="creator.resubmit",
        roles="creator",
        topic="konten",
        source="src/components/ApprovedContentCard.js",
        text=(
            "Konten yang ditolak biasanya masih bisa diperbaiki lalu dikirim ulang, "
            "maksimal " + str(MAX_RESUBMIT) + " kali. Tombolnya muncul di kartu konten "
            "itu kalau memang masih boleh. Perbaiki dulu penyebabnya, kirim ulang link "
            "yang sama tanpa diperbaiki cuma bikin ditolak lagi."
        ),
    ),
    dict(
        id="creator.claim_views",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimViewsScreen.js",
        text=(
            "Views TIDAK naik sendiri, kamu yang klaim lewat menu Klaim views. Isi "
            "jumlah views TERBARU yang kelihatan di TikTok / IG / YouTube kamu, sistem "
            "membayar selisih dari yang sudah pernah dibayar. Klaim pertama minimal "
            + str(MIN_FIRST_CLAIM_VIEWS) + " views. Klaim boleh berkali-kali selama "
            "kampanyenya masih jalan dan budgetnya masih ada."
        ),
    ),
    dict(
        id="creator.claim_accumulate",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimViewsScreen.js",
        text=(
            "Kalau kamu punya beberapa video di kampanye yang sama, viewsnya "
            "dijumlahkan. Jadi 2 video yang masing-masing 300 views tetap bisa diklaim "
            "karena totalnya 600."
        ),
    ),
    dict(
        id="creator.claim_review",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimViewsApprovalScreen.js",
        text=(
            "Klaim views diperiksa admin dulu sebelum uangnya masuk. Yang dicek: "
            "viewsnya benar segitu, kontennya masih ada, dan naiknya wajar. Kalau "
            "kontennya sudah dihapus dari sosmed, klaimnya nggak bisa diverifikasi dan "
            "bakal ditolak."
        ),
    ),
    dict(
        id="creator.claim_partial",
        roles="creator",
        topic="klaim",
        source="src/services/payment.js",
        text=(
            "Kalau sisa budget kampanye lebih kecil dari nilai klaim kamu, yang dibayar "
            "cuma sebatas sisa budgetnya, sisanya hangus dan bukan ditahan. Makanya "
            "kampanye yang budgetnya menipis sebaiknya diklaim lebih cepat."
        ),
    ),
    dict(
        id="creator.withdraw",
        roles="creator",
        topic="uang",
        source="functions/src/payment.js",
        text=(
            "Tarik saldo minimal " + rupiah(MIN_WITHDRAWAL) + ". Kreator baru, yaitu "
            "dalam " + str(NEW_CREATOR_GRACE_DAYS) + " hari sejak daftar, boleh mulai "
            "dari " + rupiah(MIN_WITHDRAWAL_NEW_CREATOR) + ". Biayanya 5% dari nominal "
            "dengan biaya terkecil " + rupiah(WITHDRAWAL_FEE_FLOOR) + ". Penarikan "
            "PERTAMA seumur hidup bebas biaya. Prosesnya 1 sampai 3 hari kerja. "
            "Tujuannya bank (" + ", ".join(SUPPORTED_BANKS) + ") atau e-wallet ("
            + ", ".join(SUPPORTED_EWALLETS) + ")."
        ),
    ),
    dict(
        id="creator.withdraw_account",
        roles="creator",
        topic="uang",
        source="src/screens/BankAccountsScreen.js",
        text=(
            "Nama pemilik rekening harus sama dengan nama akun Wefluence kamu. Kalau "
            "beda, penarikannya ditahan buat diperiksa. Rekening diatur di menu "
            "Rekening bank."
        ),
    ),
    dict(
        id="creator.pro",
        roles="creator",
        topic="pro",
        source="src/screens/SubscriptionScreen.js",
        text=(
            "Langganan PRO kreator " + rupiah(PRO_PRICE_MONTHLY) + " per bulan atau "
            + rupiah(PRO_PRICE_YEARLY) + " per tahun. Isinya badge terverifikasi, "
            "konten nggak ikut antre review admin lagi sehingga langsung masuk ke brand "
            "(brand tetap yang memutuskan), dan pencairan didahulukan. Masa aktifnya "
            "dihitung ulang 30 atau 365 hari dari tanggal bayar, jadi jangan "
            "memperpanjang jauh sebelum habis karena sisa hari yang lama hangus."
        ),
    ),
    dict(
        id="creator.fraud",
        roles="creator",
        topic="aturan",
        source="functions/src/moderationAutomation.js",
        text=(
            "Yang bikin akun diblokir permanen: beli views, suntik views, bot, "
            "engagement palsu, atau mengaku-ngaku konten orang lain. Nggak ada "
            "peringatan dan saldonya ikut hangus."
        ),
    ),
    dict(
        id="creator.wepost",
        roles="creator",
        topic="alat",
        source="src/screens/CreatorToolsScreen.js",
        text=(
            "Di menu Alat kreasi ada penjadwal posting: sambungkan akun TikTok, lalu "
            "video bisa dijadwalkan tayang otomatis. Instagram belum aktif, masih "
            "menunggu izin dari Meta."
        ),
    ),
    dict(
        id="creator.store_closed",
        roles="creator",
        topic="alat",
        source="src/utils/storeAccess.js",
        text=(
            "Fitur tukar saldo jadi barang lagi ditutup sementara. Saldo tetap bisa "
            "ditarik ke bank atau e-wallet seperti biasa."
        ),
    ),
    dict(
        id="creator.sample",
        roles="creator",
        topic="alur",
        source="src/screens/SampleRequestsScreen.js",
        text=(
            "Untuk kampanye produk fisik dan restoran, kreator mengajukan permintaan "
            "sampel dulu. Brand yang menyetujui dan mengirim barangnya, nomor resi "
            "muncul di layar pengajuan sampel."
        ),
    ),
    dict(
        id="creator.claim_block",
        roles="creator",
        topic="klaim",
        source="src/utils/claimBlock.js",
        released=False,
        text=(
            "Kalau klaim untuk satu konten ditolak " + str(CLAIM_REJECT_BLOCK_LIMIT)
            + " kali beruntun tanpa ada yang disetujui di antaranya, konten itu dikunci "
            "dari klaim. Ada tombol Buka ulang buat minta ditinjau lagi. Sekali ada "
            "klaim yang disetujui, hitungannya balik nol."
        ),
    ),
    dict(
        id="creator.auto_claim",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimViewsScreen.js",
        released=False,
        text=(
            "Ada tombol pindai semua di layar klaim: sistem mengecek semua konten kamu "
            "yang aktif sekaligus, lalu yang punya views baru bisa diklaim dalam sekali "
            "tekan. Cuma untuk kampanye yang masih aktif."
        ),
    ),

    # --- Alur brand --------------------------------------------------------
    dict(
        id="brand.flow",
        roles="brand",
        topic="alur",
        source="src/screens/CreateCampaignScreen.js",
        text=(
            "Alur brand: isi saldo, bikin kampanye dan tentukan tarif per 1.000 views, "
            "bayar budget di depan sehingga masuk escrow dan bukan jadi milik "
            "Wefluence, kreator daftar dan mengirim konten, brand menyetujui atau "
            "menolak di menu Pengajuan, kreator klaim views dan dibayar dari budget "
            "itu, sisa budget kembali kalau kampanyenya berakhir."
        ),
    ),
    dict(
        id="brand.min_budget",
        roles="brand",
        topic="budget",
        source="src/screens/CreateCampaignScreen.js",
        text=(
            "Budget kampanye minimal " + rupiah(MIN_CAMPAIGN_BUDGET) + ". Tarif minimal "
            + rupiah(MIN_PAYOUT_PER_1000) + " per 1.000 views untuk clipping, dan "
            + rupiah(MIN_PAYOUT_PER_1000_UGC) + " per 1.000 views untuk UGC karena "
            "kreator bikin kontennya dari nol."
        ),
    ),
    dict(
        id="brand.fee",
        roles="brand",
        topic="budget",
        source="functions/src/platformFee.js",
        text=(
            "Biaya platform dihitung DI ATAS budget, jadi nggak memotong jatah kreator. "
            "Tarifnya bertingkat menurut total belanja brand seumur hidup di Wefluence: "
            + _ladder_text() + ". Brand baru mulai di 15%. Isi ulang budget kampanye "
            "yang sudah jalan kena " + f"{CAMPAIGN_TOPUP_FEE_RATE * 100:.0f}%"
            + ", lebih murah daripada bikin kampanye baru."
        ),
    ),
    dict(
        id="brand.topup",
        roles="brand",
        topic="budget",
        source="src/screens/TopUpScreen.js",
        text=(
            "Isi saldo brand lewat menu Dompet, tanpa biaya tambahan. Untuk sekarang "
            "pengisiannya dikonfirmasi admin dulu, jadi belum instan."
        ),
    ),
    dict(
        id="brand.review",
        roles="brand",
        topic="konten",
        source="src/screens/SubmissionHubScreen.js",
        text=(
            "Konten kreator ditinjau di menu Pengajuan. Yang masuk ke sana sudah lolos "
            "cek admin, jadi tinggal dinilai cocok atau nggak sama brief kampanye. "
            "Kalau ditolak, tulis alasannya, karena kreator membacanya dan bisa "
            "memperbaiki lalu kirim ulang."
        ),
    ),
    dict(
        id="brand.budget_out",
        roles="brand",
        topic="budget",
        source="functions/src/campaignExpiry.js",
        text=(
            "Kampanye berhenti menerima konten baru kalau budgetnya habis. Kalau "
            "kampanyenya lewat tanggal berakhir dan budgetnya masih sisa, sisanya bisa "
            "dikembalikan ke saldo brand. Pengembaliannya nggak otomatis, brand yang "
            "menekan tombolnya di halaman kampanye."
        ),
    ),
    dict(
        id="brand.escrow",
        roles="brand",
        topic="budget",
        source="src/services/payment.js",
        text=(
            "Budget kampanye ditahan di escrow. Uangnya baru pindah ke kreator waktu "
            "klaim viewsnya disetujui."
        ),
    ),
    dict(
        id="brand.broadcast",
        roles="brand",
        topic="alur",
        source="src/screens/CampaignBroadcastScreen.js",
        text=(
            "Brand bisa mengirim pengumuman ke semua kreator yang sudah disetujui di "
            "kampanyenya, lewat halaman kampanye. Ini untuk info brief, bukan buat "
            "menawarkan kerja sama di luar platform."
        ),
    ),
    dict(
        id="brand.private_campaign",
        roles="brand",
        topic="alur",
        source="src/utils/privateCampaign.js",
        released=False,
        text=(
            "Kampanye privat cuma kelihatan oleh kreator peringkat atas dan kreator "
            "yang lagi berlangganan PRO."
        ),
    ),
    dict(
        id="brand.qna",
        roles="brand",
        topic="alur",
        source="src/screens/InboxScreen.js",
        released=False,
        text=(
            "Kreator yang sudah disetujui di kampanye bisa mengajukan pertanyaan soal "
            "brief, dan jawabannya kelihatan oleh semua kreator di kampanye itu."
        ),
    ),

    # --- Aturan bersama ----------------------------------------------------
    dict(
        id="all.no_offplatform",
        roles="all",
        topic="aturan",
        source="functions/src/messageGuard.js",
        text=(
            "Nomor WhatsApp, link grup, dan ajakan pindah ke luar platform otomatis "
            "disensor di kolom pesan. Alasannya bukan supaya Wefluence dapat komisi: "
            "kalau transaksinya di luar, nggak ada escrow, dan kalau salah satu pihak "
            "nggak bayar nggak ada yang bisa dilakukan."
        ),
    ),
]


def _include_unreleased():
    return os.environ.get("KB_INCLUDE_UNRELEASED", "").lower() in ("1", "true", "yes")


def include_unreleased():
    """Dilaporkan di /api/health supaya jelas fakta mana yang sedang aktif."""
    return _include_unreleased()


def facts_for(role):
    role = (role or "unknown").lower()
    if role not in ("creator", "brand"):
        role = "all"
    allow_unreleased = _include_unreleased()
    out = []
    for fact in FACTS:
        if not fact.get("released", True) and not allow_unreleased:
            continue
        scope = fact.get("roles", "all")
        if scope == "all" or role == "all" or scope == role:
            out.append(fact)
    return out


def build(role):
    """Basis pengetahuan siap tempel ke prompt sistem."""
    lines = []
    current_topic = None
    for fact in facts_for(role):
        topic = fact.get("topic", "lain")
        if topic != current_topic:
            lines.append("")
            lines.append("## " + topic.upper())
            current_topic = topic
        lines.append("- [" + fact["id"] + "] " + fact["text"])
    return "\n".join(lines).strip()


def fingerprint():
    """Sidik jari isi KB. Dilaporkan di /api/health supaya bisa dipastikan versi
    yang jalan di kontainer itu yang mana, tanpa harus masuk ke dalamnya."""
    blob = "|".join(f["id"] + ":" + f["text"] for f in FACTS)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
