# -*- coding: utf-8 -*-
"""Cari varian request yang bisa menembus grid reels dari IP datacenter.

Konteks: dari IP residensial, GET biasa ke /{user}/reels/ sudah mengembalikan 12
node dengan play_count. Dari VPS Hostinger, halaman post (B) dan meta views (C)
tetap dilayani, tapi halaman PROFIL balik kosong. Jadi yang digembok spesifik
rute profil, bukan seluruh instagram.com.

Skrip ini menembak kombinasi UA x URL x header, lalu melaporkan mana yang
mengembalikan node media. Tujuannya satu: menemukan satu kombinasi yang jalan,
atau membuktikan tidak ada -- sehingga keputusan berikutnya (proxy) diambil
berdasarkan bukti, bukan tebakan.

Jalankan:
    docker compose exec matrix-scrapper python api/diag_ig_grid.py [username]
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
import ig_public as ig  # noqa: E402

USER = sys.argv[1] if len(sys.argv) > 1 else 'ruang_ggelap'

UAS = {
    'chrome-desktop': ig.UA_CHROME,
    'googlebot': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
    'googlebot-smart': ('Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile '
                        'Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'),
    'bingbot': 'Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)',
    'fbbot': 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)',
    'applebot': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
                 '(KHTML, like Gecko) Version/17.0 Safari/605.1.15 '
                 '(Applebot/0.1; +http://www.apple.com/go/applebot)'),
    'yandex': 'Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)',
    'gptbot': ('Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; '
               'GPTBot/1.1; +https://openai.com/gptbot'),
    'iphone': ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
               'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'),
    'android': ('Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'),
    'slackbot': 'Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)',
    'embedly': 'Mozilla/5.0 (compatible; Embedly/0.2; +http://support.embed.ly/)',
}

PATHS = {
    'reels': '/{u}/reels/',
    'reels-hl': '/{u}/reels/?hl=en',
    'profile': '/{u}/',
    'profile-hl': '/{u}/?hl=en',
}

BASE_ACCEPT = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'


def probe(ua, path, extra=None, warm=None):
    url = 'https://www.instagram.com' + path.format(u=USER)
    headers = {'User-Agent': ua, 'Accept': BASE_ACCEPT,
               'Accept-Language': 'en-US,en;q=0.9'}
    if extra:
        headers.update(extra)
    try:
        r = (warm or requests).get(url, headers=headers,
                                   proxies=ig._PROXIES, timeout=20)
    except Exception as e:
        return {'err': str(e)[:60]}
    nodes = ig._parse_media_nodes(r.text)
    with_play = [n for n in nodes if n['play_count'] is not None]
    return {
        'status': r.status_code,
        'len': len(r.text),
        'nodes': len(nodes),
        'play': len(with_play),
        'sample': with_play[0]['play_count'] if with_play else None,
        'login_wall': 'accounts/login' in r.text[:200000],
        'has_bio': '"biography"' in r.text,
    }


def line(label, res):
    if 'err' in res:
        print(f'  {label:34s} ERR {res["err"]}')
        return False
    flag = 'JALAN' if res['play'] else '     '
    print(f'  {label:34s} {flag} {res["status"]} len={res["len"]:7d} '
          f'nodes={res["nodes"]:2d} play={res["play"]:2d} sample={res["sample"]} '
          f'bio={res["has_bio"]}')
    return bool(res['play'])


print('IP keluar :', requests.get('https://api.ipify.org', timeout=10).text)
print('proxy     :', ig.IG_PUBLIC_PROXY or '(tidak ada)')
print('target    :', USER)
print()

winners = []

print('=== 1. UA x path ===')
for uk, ua in UAS.items():
    for pk, path in PATHS.items():
        if line(f'{uk} {pk}', probe(ua, path)):
            winners.append(f'{uk} {pk}')
        time.sleep(0.4)

print()
print('=== 2. header tambahan (UA chrome, path reels) ===')
EXTRAS = {
    'sec-fetch-lengkap': {k: v for k, v in ig.NAV_HEADERS.items() if k != 'User-Agent'},
    'x-ig-app-id': {'X-IG-App-ID': '936619743392459'},
    'lang-id': {'Accept-Language': 'id-ID,id;q=0.9,en;q=0.8'},
    'referer-google': {'Referer': 'https://www.google.com/'},
    'referer-ig': {'Referer': f'https://www.instagram.com/{USER}/'},
    'no-accept-lang': {'Accept-Language': None},
}
for name, extra in EXTRAS.items():
    clean = {k: v for k, v in extra.items() if v is not None}
    if line(f'chrome+{name}', probe(ig.UA_CHROME, '/{u}/reels/', clean)):
        winners.append(f'chrome+{name}')
    time.sleep(0.4)

print()
print('=== 3. sesi hangat (ambil cookie csrftoken/mid dulu) ===')
s = requests.Session()
try:
    s.get('https://www.instagram.com/', headers={'User-Agent': ig.UA_CHROME,
                                                 'Accept': BASE_ACCEPT}, timeout=20)
    print('  cookie didapat:', dict(s.cookies))
    time.sleep(1)
    if line('sesi-hangat reels', probe(ig.UA_CHROME, '/{u}/reels/',
                                       ig.NAV_HEADERS, warm=s)):
        winners.append('sesi-hangat')
except Exception as e:
    print('  gagal:', str(e)[:80])

print()
print('=== 4. blokir keras atau intermiten? 8x jalur biasa, jeda 3 detik ===')
# Dari IP residensial, pola ini 20/20 berisi. Kalau di sini 0/8, artinya bukan
# soal timing melainkan rute profil memang tidak dilayani untuk IP ini.
hits = 0
for i in range(8):
    res = probe(ig.UA_CHROME, '/{u}/reels/', ig.NAV_HEADERS)
    hits += 1 if res.get('play') else 0
    print(f'  #{i+1} {"berisi" if res.get("play") else "kosong"} '
          f'(len={res.get("len")})')
    time.sleep(3)
print(f'  hasil: {hits}/8 berisi angka')

print()
if hits:
    winners.append(f'jalur biasa dengan jeda ({hits}/8)')

if winners:
    print('KOMBINASI YANG JALAN:')
    for w in winners:
        print('  -', w)
else:
    print('TIDAK ADA yang jalan dari IP ini. Grid reels butuh IP residensial '
          '(set IG_PUBLIC_PROXY), atau views > 10 ribu terpaksa pakai angka '
          'meta yang dibulatkan.')
