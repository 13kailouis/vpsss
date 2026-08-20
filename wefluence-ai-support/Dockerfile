FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY api/ ./api/
COPY scripts/ ./scripts/

# Jangan jalan sebagai root. Service ini memegang kredensial service account
# Firebase, jadi eksekusi kode di dalam kontainer harganya mahal.
RUN useradd --create-home --shell /usr/sbin/nologin wefluence \
 && chown -R wefluence:wefluence /app
USER wefluence

EXPOSE 8000

# /api/health membuktikan lebih banyak daripada /api/chat: dia ikut memaksa
# klien Firestore dibuat. Kalau kredensialnya salah, kontainer ditandai
# unhealthy alih-alih kelihatan hidup tapi menjawab dengan konteks kosong.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# SATU worker, banyak thread. Bukan gaya biasa, dan alasannya spesifik:
#
# 1. Batas laju per pengguna dan cache konteks disimpan di memori proses
#    (lihat api/ratelimit.py). Dengan 2 worker, keduanya terbelah dua dan
#    batasnya jadi dua kali lipat dari yang tertulis di .env.
# 2. Beban di sini hampir seluruhnya menunggu jaringan (Groq, Firestore).
#    Thread menangani itu dengan baik dan GIL bukan hambatannya.
#
# --timeout 120 karena satu permintaan bisa berisi beberapa putaran alat, dan
# tiap putaran adalah satu panggilan model.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--threads", "16", \
     "--worker-class", "gthread", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--forwarded-allow-ips", "*", \
     "--access-logfile", "-", \
     "--access-logformat", "%(h)s %(m)s %(U)s %(s)s %(M)sms", \
     "api.app:app"]
