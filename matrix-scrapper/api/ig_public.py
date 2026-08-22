# -*- coding: utf-8 -*-
"""Instagram TANPA cookie/sessionid.

Kenapa ada berkas ini: jalur lama (graphql/query/ + sessionid) sekarang balas 401
untuk anonim, jadi VPS wajib punya kolam akun burner yang gampang mati/kena flag.
Berkas ini mengambil angka dari permukaan PUBLIK Instagram yang masih dirender di
server (SSR), tanpa login sama sekali.

Tiga permukaan yang dipakai (hasil uji 22 Agu 2026):

  A. https://www.instagram.com/{username}/reels/
     HTML-nya membawa 12 reel terbaru lengkap dengan play_count, like_count,
     comment_count -- ANGKA EKSAK. Ini sumber views terbaik.
     Catatan: grid-nya tidak dirender di browser saat logged-out, tapi datanya
     tetap ikut terkirim di HTML. Jadi cukup GET biasa, tidak perlu jalankan JS.

  B. https://www.instagram.com/reel/{shortcode}/  (UA browser biasa)
     SSR halaman post: like_count + comment_count EKSAK, plus username pemilik.
     Tidak ada angka views di sini.

  C. https://www.instagram.com/reel/{shortcode}/  (UA Slack/Skype/Embedly/Iframely)
     Instagram mengirim varian meta yang berbeda:
        og:description = "31K views: ..."  /  "5,013 views: ..."
     Views ini DIBULATKAN di atas 10 ribu (31K, 1.2M). Dipakai hanya sebagai
     cadangan kalau reel-nya sudah tergeser keluar dari 12 terbaru di (A).

PENTING soal dua angka views:
    play_count (A)  != angka "views" di meta (C).  Contoh nyata:
        DcUha0rTr43  play_count 9.438   vs meta "5,019 views"   (rasio ~1,88x)
        DcLFS3ivCcn  play_count 14.449  vs meta "7,474 views"   (rasio ~1,93x)
    play_count = jumlah pemutaran (termasuk replay), sama dengan field
    video_play_count yang dipakai jalur GraphQL lama. Jadi (A) adalah pengganti
    setara untuk perhitungan payout yang sudah jalan; (C) angkanya beda kelas dan
    JANGAN dicampur diam-diam.
"""

import html as _html
import json
import os
import re
import threading
import time

import requests

UA_CHROME = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

# UA yang memicu varian meta ber-views. Kalau satu diblokir, sisanya masih jalan.
UA_META_POOL = [
    'Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)',
    'SkypeUriPreview Preview/0.5',
    'Mozilla/5.0 (compatible; Embedly/0.2; +http://support.embed.ly/)',
    'Iframely/1.3.1 (+https://iframely.com/docs/about)',
    'Mozilla/5.0 (compatible; Viber-Url-Downloader)',
]

NAV_HEADERS = {
    'User-Agent': UA_CHROME,
    'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
               'image/avif,image/webp,*/*;q=0.8'),
    'Accept-Language': 'en-US,en;q=0.9',
    'sec-ch-ua': '"Chromium";v="131", "Not_A Brand";v="24"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
}

IG_PUBLIC_TIMEOUT = float(os.environ.get('IG_PUBLIC_TIMEOUT', '15'))
IG_GRID_TTL = int(os.environ.get('IG_GRID_TTL', '120'))  # detik
IG_PUBLIC_PROXY = (os.environ.get('IG_PUBLIC_PROXY', '')
                   or os.environ.get('IG_PROXY', '')
                   or os.environ.get('SCRAPER_PROXY', '')).strip()

# Terukur 22 Agu 2026: dari IP datacenter, halaman post (B) dan meta views (C)
# tetap dilayani, tapi rute PROFIL balik shell kosong 15 dari 15 percobaan.
# Jadi hanya panggilan grid yang perlu keluar lewat proxy residensial. Memisahkan
# ini penting karena kuota proxy residensial dijual per-GB, dan grid cuma 1
# request per creator (bukan per URL) sementara B dan C jalan per URL.
IG_GRID_PROXY = (os.environ.get('IG_GRID_PROXY', '') or IG_PUBLIC_PROXY).strip()

# Grid kadang balas shell kosong walau dari IP sehat, biasanya sehabis burst.
# Terukur: dengan jeda 3 detik, 20 dari 20 berisi; tanpa jeda sesudah burst,
# sempat nol beruntun lalu pulih sendiri. Jadi kosong != pasti tidak ada.
IG_GRID_RETRY = max(1, int(os.environ.get('IG_GRID_RETRY', '3')))
IG_GRID_RETRY_DELAY = float(os.environ.get('IG_GRID_RETRY_DELAY', '2.0'))

_PROXIES = {'http': IG_PUBLIC_PROXY, 'https': IG_PUBLIC_PROXY} if IG_PUBLIC_PROXY else None
_GRID_PROXIES = {'http': IG_GRID_PROXY, 'https': IG_GRID_PROXY} if IG_GRID_PROXY else None

# Cache grid per-username. Satu creator yang punya 12 klaim = 1 request, bukan 12.
_GRID_CACHE = {}
_GRID_LOCK = threading.Lock()

_SHORTCODE_RE = re.compile(r'/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)')
_MEDIA_NODE_RE = re.compile(r'\{"__typename":"XIGPolaris\w*Media"')
_OG_URL_RE = re.compile(
    r'<meta property="og:url" content="https://www\.instagram\.com/([^/"]+)/(?:reel|p|tv)/')
_OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')

# "31K views" / "5,013 views" / "1.2M views" — angka + satuan, apa pun bahasanya
# tidak dipakai: kita selalu minta halaman versi Inggris.
_META_VIEWS_RE = re.compile(r'([0-9][0-9.,]*)\s*([KMB])?\s*views', re.IGNORECASE)

# Varian meta untuk UA crawler mesin pencari: "271 likes, 17 comments - user on ..."
_META_LIKES_RE = re.compile(
    r'([0-9][0-9.,]*)\s*([KMB])?\s*likes?,\s*([0-9][0-9.,]*)\s*([KMB])?\s*comments?',
    re.IGNORECASE)
UA_CRAWLER = 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'


def extract_shortcode(url):
    m = _SHORTCODE_RE.search(url or '')
    return m.group(1) if m else None


def parse_compact_number(num_text, suffix=None):
    """'31' + 'K' -> 31000. '5,013' -> 5013. '1.2' + 'M' -> 1200000."""
    if not num_text:
        return 0
    clean = num_text.strip()
    mult = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}.get((suffix or '').upper(), 1)
    if mult == 1:
        # angka penuh: pemisah ribuan bisa koma atau titik
        return int(re.sub(r'[^0-9]', '', clean) or 0)
    # angka ringkas: titik/koma = desimal
    clean = clean.replace(',', '.')
    try:
        return int(round(float(clean) * mult))
    except ValueError:
        return 0


def _get(url, headers, timeout=None):
    return requests.get(url, headers=headers, proxies=_PROXIES,
                        timeout=timeout or IG_PUBLIC_TIMEOUT)


# --------------------------------------------------------------------------
# A. Grid reels: satu-satunya permukaan publik dengan play_count EKSAK
# --------------------------------------------------------------------------

def _parse_media_nodes(html):
    """Tarik node media dari HTML SSR.

    Sengaja regex, bukan JSON parser: payload-nya tertanam di tengah bundel JS
    dan bentuk pembungkusnya sering berubah, sementara bentuk node-nya stabil.
    """
    out, seen = [], set()
    for m in _MEDIA_NODE_RE.finditer(html):
        seg = html[m.start():m.start() + 2000]
        code = re.search(r'"code":"([A-Za-z0-9_-]+)"', seg)
        if not code or code.group(1) in seen:
            continue

        def num(key):
            mm = re.search(r'"%s":(\d+)' % key, seg)
            return int(mm.group(1)) if mm else None

        seen.add(code.group(1))
        out.append({
            'code': code.group(1),
            'pk': (re.search(r'"pk":"(\d+)"', seg) or (None, None))[1],
            'play_count': num('play_count'),
            'like_count': num('like_count'),
            'comment_count': num('comment_count'),
            'counts_disabled': '"like_and_view_counts_disabled":true' in seg,
        })
    return out


def fetch_reels_grid(username, use_cache=True):
    """12 reel terbaru milik `username`, dengan angka eksak. {} kalau tidak ada."""
    if not username:
        return {}
    now = time.time()
    if use_cache:
        with _GRID_LOCK:
            hit = _GRID_CACHE.get(username)
            if hit and hit[0] > now:
                return hit[1]

    result = {}
    for attempt in range(IG_GRID_RETRY):
        try:
            r = requests.get(f'https://www.instagram.com/{username}/reels/',
                             headers=NAV_HEADERS, proxies=_GRID_PROXIES,
                             timeout=IG_PUBLIC_TIMEOUT)
            if r.status_code == 200:
                for node in _parse_media_nodes(r.text):
                    if node['play_count'] is not None:
                        result[node['code']] = node
        except Exception:
            result = {}
        if result:
            break
        if attempt < IG_GRID_RETRY - 1:
            time.sleep(IG_GRID_RETRY_DELAY)

    # Hasil kosong tetap di-cache (TTL pendek) supaya akun yang memang tergembok
    # tidak dihajar berulang kali dalam satu batch.
    with _GRID_LOCK:
        _GRID_CACHE[username] = (now + IG_GRID_TTL, result)
    return result


# --------------------------------------------------------------------------
# B. Halaman post: likes/comments eksak + username pemilik
# --------------------------------------------------------------------------

def fetch_post_ssr(shortcode):
    """like/comment eksak + pemilik + caption dari SSR anonim halaman post."""
    data = {'username': None, 'likes': None, 'comments': None,
            'caption': None, 'counts_disabled': False}
    try:
        r = _get(f'https://www.instagram.com/reel/{shortcode}/', NAV_HEADERS)
    except Exception:
        return data
    if r.status_code != 200:
        return data
    html = r.text

    m = _OG_URL_RE.search(html)
    if m:
        data['username'] = m.group(1)

    # Blok media utama: dikenali dari pasangan like_count + like_and_view_counts_disabled
    m = re.search(r'"like_count":(\d+),"like_and_view_counts_disabled":(\w+),'
                  r'"comment_count":(\d+)', html)
    if m:
        data['likes'] = int(m.group(1))
        data['counts_disabled'] = m.group(2) == 'true'
        data['comments'] = int(m.group(3))
        # Caption HARUS diambil dari blok media utama, bukan dari kemunculan
        # "caption" pertama di halaman -- halaman post juga memuat daftar
        # "postingan lainnya" yang punya caption sendiri dan akan menang kalau
        # dicari dari awal berkas.
        cm = re.search(r'"caption":\{.*?"text":"(.*?)"[,}]', html[m.end():m.end() + 8000], re.S)
        if cm:
            data['caption'] = _unescape_json_text(cm.group(1))

    if not data['username']:
        m = re.search(r'"owner":\{[^}]*"username":"([A-Za-z0-9_.]+)"', html)
        if m:
            data['username'] = m.group(1)

    # Post yang dibatasi usia tidak mengirim blok media sama sekali ke UA browser
    # biasa, tapi UA crawler mesin pencari tetap dilayani lewat meta description.
    if data['likes'] is None:
        data.update(_fetch_post_meta_counts(shortcode, data))
    return data


def _unescape_json_text(raw):
    try:
        return json.loads('"%s"' % raw)[:300]
    except Exception:
        return raw[:300]


def _fetch_post_meta_counts(shortcode, base):
    """Cadangan untuk post yang SSR-nya kosong: baca meta versi crawler."""
    out = {}
    try:
        r = _get(f'https://www.instagram.com/reel/{shortcode}/?hl=en',
                 {'User-Agent': UA_CRAWLER, 'Accept': 'text/html,*/*',
                  'Accept-Language': 'en-US,en;q=0.9'})
    except Exception:
        return out
    if r.status_code != 200:
        return out

    m = re.search(r'"like_count":(\d+),"like_and_view_counts_disabled":(\w+),'
                  r'"comment_count":(\d+)', r.text)
    if m:
        out['likes'] = int(m.group(1))
        out['counts_disabled'] = m.group(2) == 'true'
        out['comments'] = int(m.group(3))
    else:
        d = _OG_DESC_RE.search(r.text)
        if d:
            lm = _META_LIKES_RE.search(d.group(1))
            if lm:
                out['likes'] = parse_compact_number(lm.group(1), lm.group(2))
                out['comments'] = parse_compact_number(lm.group(3), lm.group(4))

    if not base.get('username'):
        um = _OG_URL_RE.search(r.text)
        if um:
            out['username'] = um.group(1)
    if not base.get('caption'):
        d = _OG_DESC_RE.search(r.text)
        if d:
            # buang awalan "N likes, M comments - user on <tanggal>: " sebelum caption
            txt = re.sub(r'^.*?:\s*&quot;', '', d.group(1))
            if txt != d.group(1):
                out['caption'] = _html.unescape(txt)[:300]
    return out


# --------------------------------------------------------------------------
# C. Meta ala Slack: views (dibulatkan di atas 10 ribu)
# --------------------------------------------------------------------------

def fetch_meta_views(shortcode):
    """(views, rounded_bool). views=None kalau IG tidak mengirim angkanya."""
    for ua in UA_META_POOL:
        try:
            r = _get(f'https://www.instagram.com/reel/{shortcode}/?hl=en',
                     {'User-Agent': ua, 'Accept': 'text/html,*/*',
                      'Accept-Language': 'en-US,en;q=0.9'})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        m = _OG_DESC_RE.search(r.text)
        if not m:
            continue
        vm = _META_VIEWS_RE.search(m.group(1))
        if vm:
            return parse_compact_number(vm.group(1), vm.group(2)), bool(vm.group(2))
    return None, False


# --------------------------------------------------------------------------
# Gabungan
# --------------------------------------------------------------------------

def get_instagram_public(url, known_username=None, want_meta=False):
    """Ambil statistik satu URL Instagram tanpa cookie.

    Balikan mengikuti bentuk yang sudah dipakai get_instagram_custom, ditambah
    metadata asal-usul angkanya:
        views_metric -- 'play_count' | 'ig_views'. WAJIB dilihat sebelum angka
                        dipakai: dua-duanya bernama "views" tapi play_count
                        kira-kira 1,9x lebih besar, jadi mencampurnya dalam satu
                        kolom laporan bikin selisih bayaran hampir dua kali.
        views_exact  -- True kalau angkanya tidak dibulatkan. INI SOAL PEMBULATAN,
                        BUKAN soal metrik: meta views di bawah 10 ribu juga eksak.
        views_source -- 'grid' | 'meta' | None
        meta_views   -- angka "views" versi tampilan IG
    """
    shortcode = extract_shortcode(url)
    if not shortcode:
        return None

    data = {
        'platform': 'Instagram', 'uploader': 'Unknown', 'title': 'Instagram Video',
        'views': 0, 'likes': 0, 'comments': 0, 'shares': 0,
        'views_exact': False, 'views_source': None, 'views_metric': None,
        'meta_views': None,
    }

    username = known_username
    post = {}
    if not username:
        post = fetch_post_ssr(shortcode)
        username = post.get('username')
    if username:
        data['uploader'] = username

    node = fetch_reels_grid(username).get(shortcode) if username else None
    if node:
        data['views'] = node['play_count'] or 0
        data['views_exact'] = True
        data['views_source'] = 'grid'
        data['views_metric'] = 'play_count'
        if node['like_count'] is not None:
            data['likes'] = node['like_count']
        if node['comment_count'] is not None:
            data['comments'] = node['comment_count']

    # Likes/comments dari halaman post selalu lebih segar daripada grid; ambil
    # kalau grid tidak memberi (mis. reel sudah tergeser keluar 12 terbaru).
    need_post = data['likes'] == 0 and data['comments'] == 0
    if not post and need_post:
        post = fetch_post_ssr(shortcode)
    if post.get('likes') is not None and data['likes'] == 0:
        data['likes'] = post['likes']
    if post.get('comments') is not None and data['comments'] == 0:
        data['comments'] = post['comments']
    if post.get('caption'):
        data['title'] = post['caption'][:100]

    # Meta hanya ditembak kalau memang perlu: kalau grid sudah memberi angka
    # eksak, request tambahan ini murni beban -- dan tiap request ke IG adalah
    # jatah yang lebih baik disimpan untuk URL yang belum punya angka.
    if not data['views_exact'] or want_meta:
        mv, rounded = fetch_meta_views(shortcode)
        data['meta_views'] = mv
        if not data['views_exact'] and mv:
            data['views'] = mv
            data['views_source'] = 'meta'
            data['views_metric'] = 'ig_views'
            data['views_exact'] = not rounded

    if data['views'] or data['likes'] or data['comments']:
        return data
    return None


if __name__ == '__main__':
    import sys
    for arg in sys.argv[1:]:
        print(arg)
        print(json.dumps(get_instagram_public(arg), ensure_ascii=False, indent=2))
        print()
