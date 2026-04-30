#!/bin/bash
# =========================================================
# Wefluence Scraper - Deploy / Update Script
# Run di VPS setelah git pull terbaru.
# =========================================================

set -euo pipefail

cd "$(dirname "$0")"

echo "[1/4] Pulling latest code..."
git pull --ff-only origin main || echo "(skipped - not a git repo or no remote)"

echo "[2/4] Rebuilding containers..."
docker compose build --pull

echo "[3/4] Restarting services..."
docker compose up -d --remove-orphans

echo "[4/4] Pruning old images..."
docker image prune -f

echo ""
echo "=== Status ==="
docker compose ps

echo ""
echo "=== Health check ==="
sleep 3
docker compose exec -T caption-scraper curl -sf http://127.0.0.1:8000/ || echo "caption-scraper: FAIL"
docker compose exec -T matrix-scrapper curl -sf http://127.0.0.1:8000/ || echo "matrix-scrapper: FAIL"

echo ""
echo "Done. Tail logs with: docker compose logs -f"
