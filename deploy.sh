#!/bin/bash
# =========================================================
# Wefluence Scraper - Deploy / Update Script
# Run di VPS setelah git pull terbaru.
#
# Pemakaian:
#   ./deploy.sh                        deploy SEMUA service (rebuild + restart penuh)
#   ./deploy.sh caption-scraper        deploy 1 service saja — service lain (+ cache
#                                      in-memory-nya, mis. health-tracker IG di matrix)
#                                      TIDAK disentuh
#   ./deploy.sh caption-scraper nginx  beberapa service sekaligus
# =========================================================

set -euo pipefail

cd "$(dirname "$0")"

# Nama service dari argumen. Kosong = semua service (perilaku lama).
SERVICES=("$@")

echo "[1/4] Pulling latest code..."
git pull --ff-only origin main || echo "(skipped - not a git repo or no remote)"

if [ ${#SERVICES[@]} -eq 0 ]; then
    echo "[2/4] Rebuilding ALL containers..."
    docker compose build --pull

    echo "[3/4] Restarting ALL services..."
    docker compose up -d --remove-orphans

    HEALTH=(caption-scraper matrix-scrapper ai-support)
else
    echo "[2/4] Rebuilding (targeted): ${SERVICES[*]} ..."
    docker compose build "${SERVICES[@]}"

    echo "[3/4] Recreating (targeted — service lain tidak disentuh): ${SERVICES[*]} ..."
    docker compose up -d "${SERVICES[@]}"

    HEALTH=("${SERVICES[@]}")
fi

# ---------------------------------------------------------------------------
# NGINX WAJIB DI-RELOAD MANUAL
# ---------------------------------------------------------------------------
# Config nginx masuk lewat bind mount (./nginx/conf.d -> /etc/nginx/conf.d).
# Artinya isinya memang langsung berubah di dalam container, TAPI spesifikasi
# containernya nggak berubah sedikit pun, jadi `docker compose up -d` menganggap
# nggak ada yang perlu dikerjakan dan meninggalkannya "Running" (bukan
# "Started"). Nginx sendiri baca config CUMA waktu start atau waktu disuruh
# reload. Tanpa langkah ini, perubahan conf kelihatan sudah ter-deploy padahal
# yang jalan masih yang lama, dan itu jenis kegagalan yang paling lama nggak
# ketahuan karena semua indikatornya hijau.
NEEDS_NGINX=0
if [ ${#SERVICES[@]} -eq 0 ]; then
    NEEDS_NGINX=1
else
    for svc in "${SERVICES[@]}"; do
        [ "$svc" = "nginx" ] && NEEDS_NGINX=1
    done
fi

if [ "$NEEDS_NGINX" -eq 1 ]; then
    echo ""
    echo "=== Reload config nginx ==="
    if docker compose exec -T nginx nginx -t >/dev/null 2>&1; then
        docker compose exec -T nginx nginx -s reload
        echo "nginx: config dimuat ulang"
    else
        echo "nginx: CONFIG TIDAK SAH, TIDAK di-reload (yang lama tetap jalan)"
        echo "  lihat detailnya: docker compose exec nginx nginx -t"
    fi
fi

echo ""
echo "[4/4] Pruning old images..."
docker image prune -f

echo ""
echo "=== Status ==="
docker compose ps

echo ""
echo "=== Health check ==="
sleep 3
for svc in "${HEALTH[@]}"; do
    # Nginx TIDAK bisa dicek dengan cara yang sama. Dua sebabnya: dia dengar di
    # 80/443, bukan 8000; dan image nginx:alpine sama sekali nggak punya curl.
    # Jadi cek lama SELALU melaporkan FAIL walau nginxnya sehat sempurna, dan
    # peringatan yang selalu menyala itu berhenti dibaca orang. Bukti yang benar
    # buat nginx: confignya sah dan prosesnya menerima perintah.
    if [ "$svc" = "nginx" ]; then
        if docker compose exec -T nginx nginx -t >/dev/null 2>&1; then
            echo "nginx: OK (config sah)"
        else
            echo "nginx: FAIL (config tidak sah — docker compose exec nginx nginx -t)"
        fi
        continue
    fi
    if docker compose exec -T "$svc" curl -sf http://127.0.0.1:8000/ >/dev/null 2>&1; then
        echo "$svc: OK"
    else
        echo "$svc: FAIL (cek: docker compose logs --tail 50 $svc)"
    fi
done

echo ""
echo "Done. Tail logs with: docker compose logs -f ${SERVICES[*]:-}"
