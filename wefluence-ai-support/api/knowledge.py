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
# Nominal tempat 5% baru menyamai lantai Rp 6.500. Di bawah ini yang berlaku
# SELALU lantainya, dan itu mayoritas penarikan di produksi.
WITHDRAWAL_FEE_BREAKEVEN = int(WITHDRAWAL_FEE_FLOOR / WITHDRAWAL_FEE_RATE)

MIN_CAMPAIGN_BUDGET = 1500000
MIN_PAYOUT_PER_1000 = 1000
MIN_PAYOUT_PER_1000_UGC = 5000
CAMPAIGN_TOPUP_FEE_RATE = 0.10

MIN_FIRST_CLAIM_VIEWS = 500
# Bayaran dihitung per kelipatan ini, dibulatkan KE BAWAH.
# Sumber: src/services/payment.js (Math.floor(views / 1000) * ratePer1000).
VIEWS_PER_PAYOUT_UNIT = 1000
# Kampanye berhenti menerima konten baru waktu sisa jatah kreator tinggal
# segini. Sumber: AMBANG_SISA_BUDGET di src/utils/campaignEligibility.js.
CAMPAIGN_CLOSED_BUDGET_SHARE = 0.05
# Batas unggah video bukti analitik dan banding, dalam MB.
# Sumber: MAX_VIDEO_SIZE_BYTES di src/screens/ClaimBandingScreen.js.
PROOF_VIDEO_MAX_MB = 200
# Panjang username profil publik. Sumber: functions/src/handles.js.
HANDLE_MIN_LEN = 3
HANDLE_MAX_LEN = 30
# Panjang password minimum waktu daftar. Sumber: src/screens/RegisterScreen.js.
PASSWORD_MIN_LEN = 6
# Batas "konten nggak ditinjau brand". Lewat ini, admin boleh menolak massal
# supaya budget kampanye nggak nyangkut. Sumber: STALE_DAYS di
# src/screens/StaleContentReviewScreen.js.
STALE_REVIEW_DAYS = 2
# Sanksi moderasi: kelipatan penolakan yang memicu suspend, dan lamanya.
# Sumber: functions/src/moderationAutomation.js (totalRejections % 8).
MODERATION_STRIKE_LIMIT = 8
MODERATION_SUSPEND_DAYS = 3
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
SUPPORTED_EWALLETS = ["GoPay", "DANA", "OVO", "ShopeePay"]
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


def hitung_biaya_penarikan(nominal, penarikan_pertama=False):
    """Biaya dan uang yang benar-benar masuk rekening. Cermin persis
    functions/src/payment.js.

    Ini ada karena modelnya BISA berhitung, dan justru itu masalahnya. Di
    produksi 22 Agu 2026 dia menjawab "tarik Rp 50.000, biaya 5% = Rp 2.500,
    masuk Rp 47.500" padahal lantainya Rp 6.500 (yang benar: biaya Rp 6.500,
    masuk Rp 43.500). Kalimat KB-nya sendiri sudah benar; yang salah cuma
    aritmatikanya, dan aritmatika salah begitu tetap terdengar meyakinkan.
    Jadi hitungannya dipindah ke kode, bukan dititipkan ke model.
    """
    import math

    try:
        n = int(round(float(nominal)))
    except (TypeError, ValueError):
        n = 0
    n = max(0, n)
    kotor = max(math.ceil(n * WITHDRAWAL_FEE_RATE), WITHDRAWAL_FEE_FLOOR)
    dibebaskan = min(kotor, FIRST_WITHDRAWAL_WAIVER_CAP) if penarikan_pertama else 0
    biaya = kotor - dibebaskan
    return {
        "nominal": n,
        "biaya": biaya,
        "biaya_sebelum_pembebasan": kotor,
        "dibebaskan": dibebaskan,
        "diterima": n - biaya,
        "kena_lantai": n < WITHDRAWAL_FEE_BREAKEVEN,
    }


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
    dict(
        id="creator.review_slow",
        roles="creator",
        topic="konten",
        source="src/screens/StaleContentReviewScreen.js",
        text=(
            "Kalau brand nggak juga meninjau kontenmu lewat "
            + str(STALE_REVIEW_DAYS) + " hari, tim Wefluence boleh menolaknya "
            "massal supaya budget kampanyenya nggak nyangkut kelamaan. Kalau itu "
            "terjadi, alasannya ditulis 'tidak direview dalam "
            + str(STALE_REVIEW_DAYS) + " hari' dan itu BUKAN karena kontenmu "
            "jelek. Kamu masih bisa kirim ulang atau ikut kampanye lain. Nggak "
            "ada cara mempercepat brand, dan Wefluence nggak bisa memutuskan "
            "menggantikan brand."
        ),
    ),
    dict(
        id="creator.multi_platform",
        roles="creator",
        topic="konten",
        source="functions/src/contentDedup.js",
        text=(
            "Satu video BOLEH diunggah ke beberapa platform sekaligus, misalnya "
            "TikTok dan YouTube. Yang dihitung sistem itu LINK-nya, bukan "
            "videonya, jadi dua platform berarti dua link dan dua-duanya bisa "
            "dikirim. Yang nggak boleh: satu link yang sama dipakai di dua "
            "kampanye berbeda."
        ),
    ),
    dict(
        id="creator.dont_delete",
        roles="creator",
        topic="konten",
        source="src/screens/ClaimViewsApprovalScreen.js",
        text=(
            "JANGAN hapus atau privat kontennya di sosmed selama masih kamu "
            "klaim, termasuk sesudah budget kampanyenya habis. Klaim views "
            "diverifikasi dengan membuka linknya; kalau videonya sudah nggak "
            "ada, viewsnya nggak bisa dibuktikan dan klaimnya ditolak. Kalau "
            "cuma mau merapikan tampilan, kontennya bisa diarsipkan di menu "
            "Portofolio, dan itu nggak menghapus videonya di sosmed."
        ),
    ),
    dict(
        id="creator.analytics_proof",
        roles="creator",
        topic="klaim",
        source="src/components/AnalyticsProofSheet.js",
        text=(
            "Kadang tim Wefluence minta bukti analitik sebelum klaim disetujui: "
            "rekaman layar halaman analitik kontenmu (TikTok Analytics, "
            "Instagram Insights, atau YouTube Studio) yang memperlihatkan judul "
            "video dan angka viewsnya. Kirimnya lewat menu Riwayat klaim, di "
            "baris yang statusnya 'Butuh bukti analitik', ada tombolnya di situ. "
            "Ini bukan tuduhan curang, cuma pengecekan angka. Rekam layar biasa "
            "sudah cukup, nggak usah diedit."
        ),
    ),
    dict(
        id="all.account_access",
        roles="all",
        topic="akun",
        # Tiap kalimat di bawah dicek langsung ke LoginScreen.js:
        #   :130 gerbang reCAPTCHA  ·  :138 gerbang emailVerified
        #   :181 kirim ulang tautan ·  :244 tautan lupa password
        source="src/screens/LoginScreen.js",
        text=(
            "Nggak bisa masuk, urutan yang paling sering jadi sebabnya: "
            "(1) emailnya belum diverifikasi. Login memang ditahan sampai tautan "
            "verifikasi yang dikirim waktu daftar diklik, jadi cek inbox dan "
            "folder spam. Tautannya bisa diminta ulang dari layar masuk. "
            "(2) di web, kotak reCAPTCHA di atas tombol masuk harus diselesaikan "
            "dulu. (3) lupa password bisa direset sendiri lewat tautan Lupa "
            "password di layar masuk. Kalau daftarnya pakai Google, masuknya "
            "juga harus pakai akun Google yang sama."
        ),
    ),
    dict(
        id="all.profile_rules",
        roles="all",
        topic="akun",
        source="functions/src/handles.js",
        text=(
            "Aturan isian akun: password minimal " + str(PASSWORD_MIN_LEN) + " "
            "karakter. Username profil publik " + str(HANDLE_MIN_LEN) + " sampai "
            + str(HANDLE_MAX_LEN) + " karakter dan cuma boleh huruf kecil, angka, "
            "titik, sama garis bawah. Profil kreator wajib mengisi minimal satu "
            "akun media sosial yang aktif, cukup usernamenya saja tanpa link."
        ),
    ),
    dict(
        id="all.contact_admin",
        roles="all",
        topic="dasar",
        source="-",
        text=(
            "Chat ini SUDAH jalur resmi ke tim Wefluence. Kalau ada yang nggak "
            "bisa aku selesaikan, aku teruskan ke admin dan balasannya masuk di "
            "chat yang sama, jadi nggak perlu kirim ulang atau nyari kontak lain. "
            "Wefluence nggak punya nomor WhatsApp pribadi buat dukungan."
        ),
    ),
    dict(
        id="creator.getting_started",
        roles="creator",
        topic="alur",
        source="-",
        text=(
            "Buat yang baru: yang bisa dikerjakan di sini itu ambil kampanye "
            "yang cocok, bikin videonya di akun sosmedmu sendiri, lalu dibayar "
            "dari budget kampanye itu. Langkah pertamanya buka menu Kampanye, "
            "baca briefnya, lalu daftar. Nggak ada biaya pendaftaran dan nggak "
            "ada target minimal yang bikin kena denda."
        ),
    ),
    dict(
        id="creator.claim_one_at_a_time",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimViewsScreen.js",
        text=(
            "Satu konten cuma boleh punya SATU klaim yang lagi berjalan. Selama "
            "klaim sebelumnya belum selesai direview, kamu nggak bisa kirim "
            "klaim baru buat konten yang sama. Views yang bertambah selama nunggu "
            "TIDAK hangus, semuanya tetap kehitung di klaim berikutnya."
        ),
    ),
    dict(
        id="creator.claim_bot",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimHistoryScreen.js",
        text=(
            "Kalau klaim berstatus 'Terdeteksi bot', artinya sistem menemukan "
            "tanda aktivitas bot di konten itu dan klaimnya nggak bisa diproses. "
            "Yang paling sering memicunya: views naik sangat cepat dalam waktu "
            "pendek, atau engagement yang polanya nggak wajar. Kalau kamu yakin "
            "viewsnya organik, ajukan banding dan siapkan rekaman analitiknya."
        ),
    ),
    dict(
        id="creator.banding",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimHistoryScreen.js",
        text=(
            "Klaim yang ditolak bisa dibanding lewat menu Riwayat klaim. "
            "Bandingnya diperiksa tim Wefluence, jadi tunggu hasilnya dan jangan "
            "kirim klaim baru buat konten itu selama banding masih jalan. Kalau "
            "banding ditolak, kamu masih bisa klaim ulang setelah 7 hari dengan "
            "melampirkan bukti analitik."
        ),
    ),
    dict(
        id="creator.views_manual",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimViewsScreen.js",
        text=(
            "Kadang sistem nggak bisa membaca views kamu otomatis, biasanya waktu "
            "TikTok atau Instagram lagi ramai. Kalau itu terjadi, angkanya diisi "
            "manual saja sesuai yang kelihatan di sosmed, dan klaimnya tetap "
            "diproses seperti biasa. Instagram memang belum didukung untuk "
            "pindai otomatis, jadi konten IG selalu diisi manual."
        ),
    ),
    dict(
        id="creator.payout_rounding",
        roles="creator",
        topic="klaim",
        source="src/services/payment.js",
        text=(
            "Bayaran dihitung per kelipatan " + rupiah(VIEWS_PER_PAYOUT_UNIT)[3:]
            + " views dan DIBULATKAN KE BAWAH. Jadi 1.900 views dibayar seperti "
            "1.000 views, sisa 900-nya nggak hilang tapi baru ikut dibayar kalau "
            "nanti tembus 2.000. Tiap klaim juga cuma membayar selisih ribuan yang "
            "belum pernah dibayar, bukan menghitung ulang dari nol. Ini alasan "
            "paling sering kenapa uang yang masuk terasa lebih kecil dari hitungan "
            "sendiri."
        ),
    ),
    dict(
        id="creator.proof_upload",
        roles="creator",
        topic="klaim",
        source="src/screens/ClaimBandingScreen.js",
        text=(
            "Video bukti analitik dan video banding maksimal "
            + str(PROOF_VIDEO_MAX_MB) + " MB, formatnya MP4 atau WebM. Kalau "
            "uploadnya ditolak, biasanya ukurannya lewat, jadi potong durasinya "
            "atau rekam ulang lebih pendek."
        ),
    ),
    dict(
        id="creator.budget_out",
        roles="creator",
        topic="klaim",
        source="src/screens/CampaignDetailScreen.js",
        text=(
            "Kalau budget kampanye habis, kontenmu berhenti menghasilkan di "
            "kampanye itu walaupun viewsnya masih naik. Ini bukan penalti, "
            "memang dananya sudah terpakai semua. Klaim yang sudah disetujui "
            "sebelumnya tetap dibayar. Kalau mau lanjut, cari kampanye lain yang "
            "budgetnya masih ada."
        ),
    ),
    dict(
        id="creator.keep_public",
        roles="creator",
        topic="konten",
        source="src/screens/CampaignDetailScreen.js",
        text=(
            "Kontennya wajib tetap PUBLIK sampai kampanyenya selesai. Diprivat "
            "atau di-archive di sosmed bikin viewsnya nggak bisa diverifikasi, "
            "dan klaimnya jadi gagal."
        ),
    ),
    dict(
        id="creator.payout_cap",
        roles="creator",
        topic="klaim",
        source="src/screens/CampaignDetailScreen.js",
        text=(
            "Sebagian kampanye memasang batas maksimal bayaran per kreator. "
            "Kalau ada, angkanya ditulis di halaman kampanyenya. Setelah batas "
            "itu tercapai, views yang bertambah nggak nambah bayaran lagi di "
            "kampanye tersebut."
        ),
    ),
    dict(
        id="creator.moderation_strike",
        roles="creator",
        topic="aturan",
        source="functions/src/moderationAutomation.js",
        text=(
            "Kalau kontenmu ditolak moderasi sampai "
            + str(MODERATION_STRIKE_LIMIT) + " kali, akunnya disuspend "
            + str(MODERATION_SUSPEND_DAYS) + " hari dan selama itu kamu nggak "
            "bisa mengirim konten. Ini beda dari blokir permanen: yang permanen "
            "itu buat kecurangan views. Cara menghindarinya cuma satu, baca "
            "alasan penolakan sebelum kirim lagi."
        ),
    ),
    dict(
        id="creator.campaign_closed",
        roles="creator",
        topic="alur",
        source="src/utils/campaignEligibility.js",
        text=(
            "Kampanye berhenti menerima konten baru kalau salah satu ini kejadian: "
            "lewat tenggat, statusnya sudah ditutup atau dibatalkan, atau sisa "
            "budgetnya tinggal 5 persen ke bawah. Tenggat dihitung sampai tengah "
            "malam hari itu, jadi kampanye yang tenggatnya hari ini masih boleh "
            "dikirimi konten sampai jam 23.59. Yang penting: berhenti menerima "
            "konten BUKAN berarti berhenti membayar. Konten yang sudah disetujui "
            "tetap bisa diklaim sampai budgetnya benar-benar nol."
        ),
    ),
    dict(
        id="creator.sample_eligibility",
        roles="creator",
        topic="alur",
        source="src/screens/CampaignDetailScreen.js",
        text=(
            "Tombol minta sampel cuma muncul kalau semuanya terpenuhi: profil kamu "
            "lengkap (nama, foto, bio, lokasi, dan minimal satu akun sosmed), kamu "
            "sudah pernah menyelesaikan minimal satu kampanye, alamat pengiriman "
            "sudah diisi, kamu belum pernah mengajukan sampel di kampanye itu, dan "
            "kalau kampanyenya pakai seleksi manual, lamaranmu harus sudah "
            "diterima dulu. Jadi kalau tombolnya nggak ada, cek profil dan alamat "
            "dulu, bukan kampanyenya yang rusak."
        ),
    ),
    dict(
        id="creator.dm",
        roles="creator",
        topic="alur",
        source="src/screens/DirectChatScreen.js",
        text=(
            "Obrolan langsung dengan brand dimulai dari sisi brand. Kalau brand "
            "belum pernah mengirim pesan, ruang chatnya memang belum ada dan kamu "
            "nggak bisa membukanya duluan. Pesan yang sudah masuk ada di menu "
            "Pesan. Kalau butuh bantuan yang nggak bisa nunggu brand, lewat menu "
            "Bantuan saja."
        ),
    ),
    dict(
        id="creator.reimburse",
        roles="creator",
        topic="alur",
        source="src/screens/CampaignDetailScreen.js",
        text=(
            "Sebagian kampanye punya Program Reimburse Belanja Kreator: kamu "
            "beli produknya sendiri dulu, lalu uangnya diganti sampai batas yang "
            "ditentukan brand. Kalau kampanyenya pakai program ini, halamannya "
            "menyebut toko mana saja yang diizinkan dan berapa maksimal "
            "penggantiannya. Struk belanjanya diunggah untuk diverifikasi, dan "
            "penggantiannya terpisah dari bayaran views."
        ),
    ),
    dict(
        id="creator.withdraw_blocked",
        roles="creator",
        topic="uang",
        source="src/screens/WithdrawScreen.js",
        text=(
            "Kalau muncul 'Akun kamu lagi dibatasi, jadi penarikan belum bisa "
            "diproses', itu artinya akunnya sedang ditahan tim Wefluence, bukan "
            "gangguan teknis. Saldonya nggak hilang. Ini cuma bisa dibuka admin, "
            "jadi tanyakan langsung alasannya lewat chat ini."
        ),
    ),
    dict(
        id="brand.create_rules",
        roles="brand",
        topic="alur",
        source="src/screens/CreateCampaignScreen.js",
        text=(
            "Waktu bikin kampanye, yang wajib diisi: tarif atau harga per video, "
            "gambar kampanye, minimal satu platform target, dan tanggal berakhir "
            "yang valid. Untuk clipping, minimal satu link video sumber juga "
            "wajib. Ada jeda 5 menit antara membuat satu kampanye dan kampanye "
            "berikutnya."
        ),
    ),
    dict(
        id="brand.topup_flow",
        roles="brand",
        topic="budget",
        source="src/screens/TopUpScreen.js",
        text=(
            "Isi saldo sekarang lewat transfer manual: layar top up menampilkan "
            "nomor rekening dan jumlah persis yang harus ditransfer, lalu admin "
            "mengonfirmasi. Transfer angka yang PERSIS sama supaya gampang "
            "dicocokkan. Gerbang pembayaran otomatis belum aktif."
        ),
    ),
    dict(
        id="brand.reimburse",
        roles="brand",
        topic="budget",
        source="src/screens/CreateCampaignScreen.js",
        text=(
            "Program Reimburse Belanja Kreator: brand mengganti uang belanja "
            "produk kreator sampai batas per kreator yang ditentukan. Total "
            "budget reimburse harus lebih besar dari harga produk maksimal, dan "
            "toko yang diizinkan wajib dipilih. Budget reimburse terpisah dari "
            "budget views."
        ),
    ),
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
        # Gembok dilepas 21 Agu 2026: kalimat UI-nya sudah ada di layar
        # yang dikirim ke pengguna (CreateCampaignScreen: "Harga per video wajib diisi"), jadi fiturnya memang tayang.
        
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
        id="creator.auto_approve_trust",
        roles="creator",
        topic="konten",
        source="functions/src/moderationAutomation.js",
        text=(
            "Sebagian kreator kontennya lolos tahap admin otomatis dan langsung "
            "masuk ke brand: yang langganan PRO aktif, dan yang diberi status "
            "terpercaya oleh admin. Notifikasinya bilang kontenmu diteruskan tanpa "
            "antre. Yang dilewati cuma antrean admin, brand tetap yang memutuskan."
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
        id="creator.perpost_paid_lock",
        roles="creator",
        topic="konten",
        source="src/screens/PublicProfileScreen.js",
        text=(
            "Konten di kampanye bayar per video yang sudah dibayar nggak bisa "
            "dihapus dari portofolio. Itu bukti pekerjaan yang sudah dibayar, jadi "
            "kalau ada masalah dengan post itu, hubungi admin."
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
            "dari " + rupiah(MIN_WITHDRAWAL_NEW_CREATOR) + ". Biayanya diambil yang "
            "LEBIH BESAR antara 5% nominal dan " + rupiah(WITHDRAWAL_FEE_FLOOR) + ", "
            "bukan 5% saja. Di bawah " + rupiah(WITHDRAWAL_FEE_BREAKEVEN) + " yang "
            "berlaku selalu " + rupiah(WITHDRAWAL_FEE_FLOOR) + ". Contoh: tarik "
            + rupiah(50000) + " biayanya " + rupiah(WITHDRAWAL_FEE_FLOOR) + " dan yang "
            "masuk " + rupiah(50000 - WITHDRAWAL_FEE_FLOOR) + "; tarik "
            + rupiah(200000) + " biayanya " + rupiah(10000) + " dan yang masuk "
            + rupiah(190000) + ". Penarikan "
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
        id="creator.withdraw_rejected",
        roles="creator",
        topic="uang",
        source="src/services/payment.js",
        text=(
            "Kalau penarikan ditolak atau gagal diproses, uangnya DIKEMBALIKAN "
            "penuh ke saldo, bukan hilang, dan biayanya juga nggak jadi dipotong. "
            "Notifikasinya berjudul Penarikan Belum Bisa Diproses, alasannya "
            "dibuka di detail notifikasi itu. Penyebab paling sering: nama pemilik "
            "rekening beda dengan nama akun, atau nomor rekeningnya salah. "
            "Perbaiki rekeningnya dulu, baru ajukan lagi."
        ),
    ),
    dict(
        id="creator.no_topup",
        roles="creator",
        topic="uang",
        source="src/screens/WalletScreen.js",
        text=(
            "Kreator nggak punya menu isi saldo. Top up itu cuma buat akun brand. "
            "Saldo kreator datang dari klaim views yang disetujui, jadi nggak ada "
            "yang perlu kamu setor duluan."
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
        id="creator.paid_promo",
        roles="creator",
        topic="aturan",
        # Belum ada di kode mana pun. Ini keputusan produk 22 Agu 2026, diambil
        # setelah AI menjawab "aman" ke kreator yang nanya boleh nggak pakai
        # promosi TikTok. Sebelum ini KB-nya sengaja diam karena aturannya
        # memang belum pernah ditulis, dan diamnya KB dijawab model dengan
        # tebakan yang terdengar meyakinkan. Kalau aturan ini nanti ditulis di
        # layar aplikasi atau syarat kampanye, ganti source ke berkas itu.
        source="keputusan produk 22 Agu 2026 (belum tertulis di aplikasi)",
        text=(
            "Views yang kamu klaim harus organik. Iklan berbayar bawaan platform, "
            "misalnya TikTok Promote atau Boost, Instagram dan Facebook Ads, atau "
            "jasa promosi berbayar apa pun, NGGAK BOLEH dipakai di konten yang kamu "
            "klaim di Wefluence, karena yang dibayar brand itu jangkauan asli bukan "
            "jangkauan yang dibeli. Kalau konten yang diklaim ternyata dipromosikan "
            "berbayar, klaimnya ditolak dan viewsnya nggak dibayar. Ini beda dari "
            "beli views atau bot yang hukumannya blokir permanen, tapi tetap nggak "
            "boleh. Boleh promosi gratis sebanyak-banyaknya: bagikan sendiri ke "
            "grup, story, atau akun lain milikmu."
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
        id="creator.tools",
        roles="creator",
        topic="alat",
        source="src/screens/CreatorToolsScreen.js",
        text=(
            "Menu Alat kreasi isinya empat: Wepost buat menjadwalkan posting, Bank "
            "hook buat contoh kalimat pembuka, Inspirasi konten buat melihat konten "
            "yang performanya bagus, dan Kalkulator cuan buat memperkirakan "
            "penghasilan dari jumlah views. Weclip yang muncul di banner masih "
            "segera hadir, belum bisa dipakai."
        ),
    ),
    dict(
        id="creator.wepost_rules",
        roles="creator",
        topic="alat",
        source="src/screens/SchedulePostScreen.js",
        text=(
            "Aturan Wepost: TikTok baru mendukung unggahan video, belum foto. "
            "Konten bertanda branded content nggak boleh disetel privat. Kalau "
            "muncul sesi kedaluwarsa, akun TikTok-nya tinggal dihubungkan ulang di "
            "Wepost. TikTok sendiri juga bisa menolak unggahannya, biasanya karena "
            "videonya dianggap tidak original, akunnya masih privat, atau kena "
            "batas unggah harian."
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
        # Gembok dilepas 21 Agu 2026: kalimat UI-nya sudah ada di layar
        # yang dikirim ke pengguna (ClaimViewsScreen: "sudah ditolak 3 kali, jadi kontennya dikunci"), jadi fiturnya memang tayang.
        
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
        # Gembok dilepas 21 Agu 2026: kalimat UI-nya sudah ada di layar
        # yang dikirim ke pengguna (ClaimViewsScreen: "IG belum bisa Auto-Klaim"), jadi fiturnya memang tayang.
        
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
        id="brand.campaign_states",
        roles="brand",
        topic="alur",
        source="src/screens/BrandCampaignsScreen.js",
        text=(
            "Kampanye yang belum dibayar belum tayang dan belum kelihatan oleh "
            "kreator. Sesudah dibayar, kampanyenya diperiksa tim Wefluence dulu "
            "sebelum boleh tayang. Kampanye yang dijeda tetap ada tapi kreator "
            "nggak bisa mengirim konten baru selama dijeda. Kampanye yang "
            "dibatalkan nggak bisa dijalankan lagi dan sisa dananya dikembalikan."
        ),
    ),
    dict(
        id="brand.perpost_quota",
        roles="brand",
        topic="budget",
        source="src/screens/CampaignTopUpScreen.js",
        text=(
            "Kampanye bayar per video budgetnya terkunci sesuai kuota, yaitu harga "
            "per video dikali jumlah video. Jadi kampanye jenis ini nggak bisa "
            "ditambah dana lewat tombol tambah dana biasa. Kalau mau menambah "
            "kuota videonya, hubungi tim Wefluence."
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
