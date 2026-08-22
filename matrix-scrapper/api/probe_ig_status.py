# -*- coding: utf-8 -*-
"""Pisahkan tiga kemungkinan yang gejalanya mirip:

  1. rute profil digembok untuk IP datacenter  -> profil 429, post 200
  2. seluruh instagram.com kena rate limit IP   -> semuanya 429
  3. limitnya sementara                         -> pulih di ronde berikutnya

Ini perlu dibedakan karena keputusannya beda jauh: kasus 1 cukup proxy untuk
panggilan grid, kasus 2 berarti semua jalur publik butuh jalan keluar lain, dan
kasus 3 cuma butuh sabar plus jeda.

Sengaja hemat: 3 request per ronde, 3 ronde, jeda 60 detik. Menghajar IP yang
sedang 429 justru memperpanjang hukumannya.

Jalankan:
    docker compose exec matrix-scrapper python api/probe_ig_status.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
import ig_public as ig  # noqa: E402

USER = sys.argv[1] if len(sys.argv) > 1 else 'ruang_ggelap'
CODE = sys.argv[2] if len(sys.argv) > 2 else 'DcUha0rTr43'
ROUNDS = int(os.environ.get('ROUNDS', '3'))
GAP = int(os.environ.get('GAP', '60'))

TARGETS = [
    ('A profil/reels', f'https://www.instagram.com/{USER}/reels/', ig.NAV_HEADERS),
    ('B halaman post', f'https://www.instagram.com/reel/{CODE}/', ig.NAV_HEADERS),
    ('C meta slackbot', f'https://www.instagram.com/reel/{CODE}/?hl=en',
     {'User-Agent': ig.UA_META_POOL[0], 'Accept': 'text/html,*/*'}),
]

print('IP keluar :', requests.get('https://api.ipify.org', timeout=10).text)
print('proxy     :', ig.IG_PUBLIC_PROXY or '(tidak ada)')
print()

for rnd in range(1, ROUNDS + 1):
    print(f'--- ronde {rnd} ({time.strftime("%H:%M:%S")}) ---')
    for label, url, headers in TARGETS:
        try:
            r = requests.get(url, headers=headers, proxies=ig._PROXIES, timeout=20)
            extra = ''
            if r.status_code == 429:
                ra = r.headers.get('Retry-After')
                extra = f' Retry-After={ra}' if ra else ' (tanpa Retry-After)'
            elif label.startswith('A'):
                n = [x for x in ig._parse_media_nodes(r.text) if x['play_count'] is not None]
                extra = f' play_nodes={len(n)}'
            elif label.startswith('B'):
                p = ig.fetch_post_ssr(CODE) if r.status_code == 200 else {}
                extra = f' likes={p.get("likes")} comments={p.get("comments")}'
            elif label.startswith('C'):
                v, rounded = ig.fetch_meta_views(CODE)
                extra = f' views={v} dibulatkan={rounded}'
            print(f'  {label:16s} {r.status_code} len={len(r.text):7d}{extra}')
        except Exception as e:
            print(f'  {label:16s} ERR {str(e)[:70]}')
        time.sleep(3)
    if rnd < ROUNDS:
        print(f'  (tunggu {GAP} detik)')
        time.sleep(GAP)

print()
print('Baca hasilnya begini:')
print('  A 429 tapi B/C 200  -> gembok khusus rute profil. Cukup IG_GRID_PROXY.')
print('  semua 429           -> IP kena limit menyeluruh. Butuh proxy untuk semua,')
print('                         atau turunkan laju scraping IG dari VPS.')
print('  ronde awal 429 lalu 200 -> limitnya sementara, tambah jeda saja.')
