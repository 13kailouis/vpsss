# -*- coding: utf-8 -*-
"""Uji jalur Instagram tanpa cookie DARI IP tempat skrip ini dijalankan.

Ini pertanyaan yang tidak bisa dijawab dari laptop: jalur lama mati di VPS karena
IP datacenter diblokir, jadi jalur baru pun wajib dibuktikan dari sana.

Skripnya sengaja ditaruh di dalam api/ karena Dockerfile hanya menyalin folder
itu ke image; berkas di root matrix-scrapper/ tidak pernah ikut terbawa.

Jalankan di VPS, dari folder yang ada docker-compose.yml-nya:
    docker compose exec matrix-scrapper python api/test_ig_public.py
atau langsung di host:
    python3 api/test_ig_public.py [username] [shortcode ...]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
import ig_public as ig  # noqa: E402

USER = sys.argv[1] if len(sys.argv) > 1 else 'ruang_ggelap'
CODES = sys.argv[2:] or ['DcUha0rTr43', 'Db_E9-wT9DR', 'DcDnY62zz6K']


def ip():
    try:
        return requests.get('https://api.ipify.org', timeout=10).text
    except Exception as e:
        return f'(gagal: {e})'


print('IP keluar :', ip())
print('proxy     :', ig.IG_PUBLIC_PROXY or '(tidak ada)')
print()

print(f'[A] grid reels /{USER}/reels/')
t0 = time.time()
grid = ig.fetch_reels_grid(USER, use_cache=False)
print(f'    {len(grid)} reel dengan play_count eksak, {round(time.time()-t0, 2)}s')
for code, node in list(grid.items())[:5]:
    print(f'      {code:12s} play={node["play_count"]:>9} like={node["like_count"]:>7} '
          f'cmt={node["comment_count"]:>5}')
if not grid:
    print('    KOSONG -> IP ini kemungkinan tidak dilayani SSR. Coba lewat IG_PUBLIC_PROXY.')
print()

for code in CODES:
    print(f'[B/C] {code}')
    t0 = time.time()
    post = ig.fetch_post_ssr(code)
    print(f'    SSR post  : user={post["username"]} likes={post["likes"]} '
          f'comments={post["comments"]} ({round(time.time()-t0, 2)}s)')
    t0 = time.time()
    views, rounded = ig.fetch_meta_views(code)
    print(f'    meta views: {views} (dibulatkan={rounded}) ({round(time.time()-t0, 2)}s)')
    t0 = time.time()
    full = ig.get_instagram_public(f'https://www.instagram.com/reel/{code}/')
    print(f'    gabungan  : {full} ({round(time.time()-t0, 2)}s)')
    print()

print('=== burst 15x tanpa jeda (uji rate limit) ===')
ok = 0
for i in range(15):
    g = ig.fetch_reels_grid(USER, use_cache=False)
    ok += 1 if g else 0
    print(f'  #{i+1:02d} {"ok" if g else "KOSONG"} ({len(g)} node)')
print(f'sukses {ok}/15')
