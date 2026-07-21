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

    HEALTH=(caption-scraper matrix-scrapper)
else
    echo "[2/4] Rebuilding (targeted): ${SERVICES[*]} ..."
    docker compose build "${SERVICES[@]}"

    echo "[3/4] Recreating (targeted — service lain tidak disentuh): ${SERVICES[*]} ..."
    docker compose up -d "${SERVICES[@]}"

    HEALTH=("${SERVICES[@]}")
fi

echo "[4/4] Pruning old images..."
docker image prune -f

echo ""
echo "=== Status ==="
docker compose ps

echo ""
echo "=== Health check ==="
sleep 3
for svc in "${HEALTH[@]}"; do
    if docker compose exec -T "$svc" curl -sf http://127.0.0.1:8000/ >/dev/null 2>&1; then
        echo "$svc: OK"
    else
        echo "$svc: FAIL (cek: docker compose logs --tail 50 $svc)"
    fi
done

echo ""
echo "Done. Tail logs with: docker compose logs -f ${SERVICES[*]:-}"
