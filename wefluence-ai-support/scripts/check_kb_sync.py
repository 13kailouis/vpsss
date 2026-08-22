#!/usr/bin/env python3
"""
PENJAGA SINKRON BASIS PENGETAHUAN
=================================

Masalah yang bikin skrip ini ada: basis pengetahuan AI support membusuk tanpa
suara. Waktu diperiksa Agustus 2026, dia masih mengajari brand bahwa biaya
platform 12% (sudah bertingkat mulai 15% sejak Februari) dan budget minimum
Rp 5,5 juta (sudah Rp 1,5 juta). Tidak ada yang salah dan tidak ada yang error,
karena tidak ada satu pun mekanisme yang membandingkan angka di AI dengan angka
di aplikasi.

Skrip ini membandingkannya. Jalankan setiap kali ada perubahan harga, biaya,
atau batas minimum di aplikasi, dan sebelum deploy AI support.

PEMAKAIAN
    python scripts/check_kb_sync.py
    python scripts/check_kb_sync.py --repo "D:/0000 claude code/wefluence"

    Lokasi repo juga bisa diisi lewat environment WEFLUENCE_REPO.

KELUARAN
    kode 0 = semua cocok
    kode 1 = ada yang beda (dicetak dengan berkas asalnya)
    kode 2 = repo tidak ketemu, jadi tidak bisa diperiksa

CATATAN JUJUR SOAL BATASNYA
    Ini membaca kode di repo, BUKAN yang sedang berjalan di produksi. Kalau ada
    perubahan yang sudah di-commit tapi belum di-deploy, skrip ini akan bilang
    cocok padahal pengguna masih melihat angka lama. Untuk itulah ada penanda
    `released` di knowledge.py.
"""

import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import knowledge  # noqa: E402

DEFAULT_REPO = os.environ.get("WEFLUENCE_REPO", r"D:/0000 claude code/wefluence")


def read(repo, relative):
    path = os.path.join(repo, relative.replace("/", os.sep))
    if not os.path.isfile(path):
        return None
    return io.open(path, encoding="utf-8", errors="replace").read()


def grab(source, pattern, cast=float):
    if source is None:
        return None
    match = re.search(pattern, source)
    if not match:
        return None
    try:
        return cast(match.group(1))
    except (TypeError, ValueError):
        return None


CHECKS = []


def check(label, relative, pattern, expected, cast=float):
    CHECKS.append((label, relative, pattern, expected, cast))


check(
    "budget minimum kampanye",
    "src/screens/CreateCampaignScreen.js",
    r"MIN_CAMPAIGN_BUDGET\s*=\s*(\d+)",
    knowledge.MIN_CAMPAIGN_BUDGET,
)
check(
    "tarif minimum per 1000 views",
    "src/screens/CreateCampaignScreen.js",
    r"MIN_PAYOUT_PER_1000\s*=\s*(\d+)",
    knowledge.MIN_PAYOUT_PER_1000,
)
check(
    "tarif minimum UGC per 1000 views",
    "src/screens/CreateCampaignScreen.js",
    r"MIN_PAYOUT_UGC\s*=\s*(\d+)",
    knowledge.MIN_PAYOUT_PER_1000_UGC,
)
check(
    "minimum tarik dana",
    "functions/src/payment.js",
    r"minWithdrawal:\s*(\d+)",
    knowledge.MIN_WITHDRAWAL,
)
check(
    "biaya tarik dana terkecil",
    "functions/src/payment.js",
    r"withdrawalProcessingFee:\s*(\d+)",
    knowledge.WITHDRAWAL_FEE_FLOOR,
)
check(
    "persen biaya tarik dana",
    "functions/src/payment.js",
    r"withdrawalFeePercentage:\s*([\d.]+)",
    knowledge.WITHDRAWAL_FEE_RATE,
)
check(
    "masa kreator baru (hari)",
    "functions/src/payment.js",
    r"gracePeriodDays:\s*(\d+)",
    knowledge.NEW_CREATOR_GRACE_DAYS,
)
check(
    "plafon pembebasan biaya tarik pertama",
    "functions/src/payment.js",
    r"firstWithdrawalFeeWaiverCap:\s*(\d+)",
    knowledge.FIRST_WITHDRAWAL_WAIVER_CAP,
)
check(
    "harga PRO bulanan",
    "src/screens/SubscriptionScreen.js",
    r"price:\s*(49000)",
    knowledge.PRO_PRICE_MONTHLY,
)
check(
    "harga PRO tahunan",
    "src/screens/SubscriptionScreen.js",
    r"price:\s*(490000)",
    knowledge.PRO_PRICE_YEARLY,
)
check(
    "biaya isi ulang budget kampanye",
    "functions/src/brandPayment.js",
    r"TOPUP_FEE_RATE\s*=\s*([\d.]+)",
    knowledge.CAMPAIGN_TOPUP_FEE_RATE,
)
check(
    "batas konten tidak ditinjau brand (hari)",
    "src/screens/StaleContentReviewScreen.js",
    r"STALE_DAYS\s*=\s*(\d+)",
    knowledge.STALE_REVIEW_DAYS,
)
check(
    "kelipatan tolak moderasi -> suspend",
    "functions/src/moderationAutomation.js",
    r"totalRejections % (\d+) === 0",
    knowledge.MODERATION_STRIKE_LIMIT,
)
check(
    "batas tolak beruntun klaim",
    "src/utils/claimBlock.js",
    r"CLAIM_REJECT_BLOCK_LIMIT\s*=\s*(\d+)",
    knowledge.CLAIM_REJECT_BLOCK_LIMIT,
)


check(
    "kelipatan views yang dibayar",
    "src/services/payment.js",
    r"Math\.floor\(parsedViews / (\d+)\)",
    knowledge.VIEWS_PER_PAYOUT_UNIT,
)
check(
    "ambang sisa budget kampanye ditutup",
    "src/utils/campaignEligibility.js",
    r"AMBANG_SISA_BUDGET\s*=\s*([\d.]+)",
    knowledge.CAMPAIGN_CLOSED_BUDGET_SHARE,
)
check(
    "batas ukuran video bukti (MB)",
    "src/screens/ClaimBandingScreen.js",
    r"MAX_VIDEO_SIZE_BYTES\s*=\s*(\d+)\s*\*\s*1024",
    knowledge.PROOF_VIDEO_MAX_MB,
)
check(
    "panjang minimum username",
    "functions/src/handles.js",
    r"h\.length < (\d+)",
    knowledge.HANDLE_MIN_LEN,
)
check(
    "panjang maksimum username",
    "functions/src/handles.js",
    r"h\.length > (\d+)",
    knowledge.HANDLE_MAX_LEN,
)
check(
    "panjang minimum password",
    "src/screens/RegisterScreen.js",
    r"password\.length < (\d+)",
    knowledge.PASSWORD_MIN_LEN,
)


def check_ladder(repo, problems, skipped):
    source = read(repo, "src/utils/platformFee.js")
    if source is None:
        skipped.append("tangga biaya platform (src/utils/platformFee.js tidak ada)")
        return
    block = re.search(r"FEE_LADDER\s*=\s*\[(.*?)\]", source, re.DOTALL)
    if not block:
        skipped.append("tangga biaya platform (FEE_LADDER tidak terbaca)")
        return
    rows = re.findall(r"upTo:\s*([\w.]+)\s*,\s*rate:\s*([\d.]+)", block.group(1))
    if not rows:
        skipped.append("tangga biaya platform (isi FEE_LADDER tidak terbaca)")
        return

    found = []
    for upto, rate in rows:
        upper = float("inf") if upto == "Infinity" else float(upto)
        found.append((upper, float(rate)))

    if found != [(float(u), float(r)) for u, r in knowledge.FEE_LADDER]:
        problems.append(
            (
                "tangga biaya platform",
                "src/utils/platformFee.js",
                found,
                knowledge.FEE_LADDER,
            )
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=DEFAULT_REPO)
    args = parser.parse_args()

    repo = args.repo
    if not os.path.isdir(repo):
        print("Repo aplikasi tidak ketemu: " + repo)
        print("Isi lewat --repo atau environment WEFLUENCE_REPO.")
        return 2

    problems = []
    skipped = []

    for label, relative, pattern, expected, cast in CHECKS:
        source = read(repo, relative)
        if source is None:
            skipped.append(label + " (" + relative + " tidak ada)")
            continue
        actual = grab(source, pattern, cast)
        if actual is None:
            skipped.append(label + " (pola tidak ketemu di " + relative + ")")
            continue
        if abs(actual - float(expected)) > 1e-9:
            problems.append((label, relative, actual, expected))

    check_ladder(repo, problems, skipped)

    print("Repo   : " + repo)
    print("KB     : " + knowledge.fingerprint() + " (" + str(len(knowledge.FACTS)) + " fakta)")
    print("Dicek  : " + str(len(CHECKS) + 1))
    print("")

    if skipped:
        print("TIDAK BISA DIPERIKSA")
        for item in skipped:
            print("  - " + item)
        print("")

    if problems:
        print("BEDA - knowledge.py perlu diperbarui:")
        for label, relative, actual, expected in problems:
            print("  - " + label)
            print("      aplikasi   : " + str(actual) + "   (" + relative + ")")
            print("      AI support : " + str(expected) + "   (api/knowledge.py)")
        print("")
        print("Setelah memperbaiki: rebuild dan deploy ai-support.")
        return 1

    print("Semua angka cocok.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
