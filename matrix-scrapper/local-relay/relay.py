# -*- coding: utf-8 -*-
"""Relay grid Instagram yang dijalankan di mesin rumah.

Kontraknya sama persis dengan Cloudflare Worker di ../cloudflare/ig-grid-relay/,
jadi sisi scraper tidak perlu diubah sama sekali: cukup arahkan IG_GRID_RELAY ke
alamat relay ini.

Kenapa versi rumahan ini ada: Worker Cloudflare sudah dicoba dan ikut ditolak
429, sama seperti VPS. Yang terbukti dilayani Instagram cuma IP residensial
(terukur 20 dari 20 percobaan berisi angka). Relay ini meminjam koneksi rumah
tanpa harus menyewa proxy residensial.

Batasnya jujur saja: kalau mesin ini mati atau tidur, relay ikut mati dan
panggilan grid gagal. Selama IG_PUBLIC_MODE masih 'fallback', itu tidak
menjatuhkan apa-apa karena sessionid tetap jalur utama. Baru berarti kalau mode
dinaikkan ke 'first' atau 'only'.

Jalankan:
    RELAY_KEY=... python relay.py           # port 8787
    RELAY_KEY=... PORT=9000 python relay.py
"""
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api'))

import requests  # noqa: E402

# Header diambil dari modul yang sama dengan yang dipakai scraper, bukan disalin.
# Kalau Instagram menuntut header baru nanti, cukup satu tempat yang diubah.
try:
    from ig_public import NAV_HEADERS
except ImportError:
    print('Tidak menemukan api/ig_public.py. Jalankan dari dalam repo vps.')
    raise

def _read_key():
    """Kunci dari env, atau dari berkas relay.key di sebelah skrip ini.

    Berkasnya ada supaya relay bisa dinyalakan otomatis saat boot: penjadwal
    tugas Windows tidak membawa variabel lingkungan sesi, dan menuliskan kunci
    di dalam perintah penjadwal berarti kunci itu ikut terbaca di daftar tugas.
    """
    env_key = os.environ.get('RELAY_KEY', '').strip()
    if env_key:
        return env_key
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'relay.key')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    return ''


RELAY_KEY = _read_key()
PORT = int(os.environ.get('PORT', '8787'))
TIMEOUT = float(os.environ.get('TIMEOUT', '20'))

USERNAME_RE = re.compile(r'^[A-Za-z0-9._]{1,30}$')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Bawaannya mencetak tiap request dengan format apache yang berisik.
        pass

    def _send(self, status, body, content_type='application/json; charset=utf-8',
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            self._send(200, '{"ok":true}')
            return

        # Kunci wajib. Relay ini akan terbuka ke internet lewat tunnel, jadi
        # tanpa penjaga ini alamatnya cukup diketahui untuk dipakai siapa pun.
        if not RELAY_KEY or self.headers.get('x-relay-key') != RELAY_KEY:
            self._send(403, '{"error":"kunci relay salah"}')
            return

        username = (parse_qs(parsed.query).get('username') or [''])[0]
        if not USERNAME_RE.match(username):
            self._send(400, '{"error":"username tidak valid"}')
            return

        try:
            r = requests.get(f'https://www.instagram.com/{username}/reels/',
                             headers=NAV_HEADERS, timeout=TIMEOUT)
        except Exception as e:
            self._send(502, '{"error":"gagal menghubungi instagram"}')
            print(f'[relay] {username}: gagal - {str(e)[:100]}')
            return

        body = r.content
        # Status asli dikirim terpisah supaya sisi Python bisa membedakan
        # "relaynya yang bermasalah" dari "Instagram yang menolak".
        self._send(r.status_code, body, 'text/html; charset=utf-8',
                   {'x-ig-status': str(r.status_code)})
        print(f'[relay] {username}: {r.status_code}, {len(body):,} byte')


if __name__ == '__main__':
    if not RELAY_KEY:
        print('Kunci belum ada. Relay akan menolak semua request.')
        print('Isi lewat env RELAY_KEY, atau simpan di berkas relay.key')
        print('di folder ini (isinya kuncinya saja, satu baris).')
        sys.exit(1)
    print(f'Relay jalan di http://127.0.0.1:{PORT}')
    print('Uji cepat: curl -H "x-relay-key: ..." '
          f'"http://127.0.0.1:{PORT}/?username=ruang_ggelap"')
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
