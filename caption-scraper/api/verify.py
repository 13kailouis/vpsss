import json
import requests
import re
from urllib.parse import urlparse, parse_qs, quote
import random
import os
import time
import threading
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── TikTok: sumber caption & urutannya ───────────────────────────────────
# embed/v2 kena rate-limit ketat dari IP datacenter: tanpa cookie cuma lolos
# 1-2 request lalu dibalas "overload-protect triggered" (503) atau 400. Diuji
# 7 Agu 2026, 5 request beruntun -> 200 200 503 503 503. Jadi embed/v2 TIDAK
# layak lagi jadi sumber awal. Urutan baru: yang murah & tahan banting duluan.
#
#   1. oEmbed          JSON ~1.6 KB, caption UTUH. Diuji atas 19 video (caption
#                      39-1767 karakter): title oEmbed SELALU byte-identik dgn
#                      desc asli, termasuk hashtag di ujung -> aman buat kode
#                      yang ditaruh di akhir caption.
#   2. halaman kanonik UA Bytespider (crawler resmi ByteDance) tembus penuh
#                      ~380 KB & selalu memuat "desc" (8/8), sedangkan UA Chrome
#                      cuma dapat bot-wall 1462 byte (8/8).
#   3. embed/v2        tetap dipakai, tapi paling akhir + retry/backoff.
#   4. TikWM           last resort pihak ketiga.
#
# CATATAN player/v1: SENGAJA tidak dipakai untuk caption. Endpoint itu memang
# stabil 200 (lolos rate-limit) tapi isinya cuma shell player — tidak ada desc,
# tidak ada og:*, dan JSON __MODERN_ROUTER_DATA__-nya cuma config pemutar.
# Diuji: nol caption. Dia cocok buat <iframe> di app (TikTokEmbed.js sudah
# pindah ke sana), BUKAN buat mengambil teks caption.

_TIKTOK_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Anggaran waktu untuk SATU panggilan get_tiktok_caption. Rantainya ada 4 sumber
# & dua di antaranya berat (halaman kanonik ~380 KB, embed/v2 ~300 KB), jadi
# tanpa batas ini kasus terburuk terukur tembus ~35 detik saat jaringan lemot —
# kelamaan untuk endpoint yang dipanggil interaktif. Sumber yang sisa waktunya
# tidak cukup akan dilewati & dicatat di debug_log (mis. "Main:Budget"), bukan
# bikin request menggantung. Hasil sementara (mis. caption oEmbed) tetap dipakai.
#
# Ini anggaran LUNAK, bukan stopwatch keras: batasnya dicek di antara request,
# dan requests menghitung timeout connect & read terpisah, jadi satu request yang
# terlanjur jalan masih bisa lewat sedikit. Terukur: target 15 dtk -> realisasi
# terburuk ~17 dtk (URL tak valid, semua sumber gagal). Kalau butuh lebih ketat/
# longgar, ganti lewat env TIKTOK_BUDGET_SEC tanpa rebuild image.
_TIKTOK_BUDGET_SEC = float(os.environ.get('TIKTOK_BUDGET_SEC', '15'))
_TIKTOK_MIN_SLICE_SEC = 2.0  # di bawah ini percuma mulai request baru
_TIKTOK_CONNECT_CAP_SEC = 4.0  # host tak nyambung: jangan buang jatah di connect


def _tt_left(deadline):
    """Sisa anggaran waktu (detik)."""
    return deadline - time.time()


def _tt_timeout(deadline, want):
    """Timeout (connect, read) untuk satu request, dibatasi sisa anggaran.

    Dipisah connect vs read supaya host yang mati/diblokir tidak menghabiskan
    seluruh jatah cuma buat menunggu handshake.
    """
    left = max(1.0, min(want, _tt_left(deadline)))
    return (min(_TIKTOK_CONNECT_CAP_SEC, left), left)


_TIKTOK_UA_BYTESPIDER = (
    'Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)'
)


def _tiktok_get(url, headers=None, timeout=6, retries=1, session=None, deadline=None):
    """GET dengan backoff kecil khusus jawaban rate-limit/overload TikTok.

    Balikin response terakhir (boleh non-200) atau None kalau koneksinya yang
    gagal. Backoff pendek + jitter: verify dipanggil interaktif, jadi lebih baik
    cepat nyerah lalu pindah ke sumber berikutnya daripada nahan request lama.

    PENTING — retry penuh HANYA untuk status rate-limit/overload (503/429/5xx).
    Jawaban itu datang cepat, jadi mengulanginya murah dan memang di situ
    gunanya backoff. Sebaliknya timeout/error koneksi sudah menghabiskan jatah
    waktu penuh; mengulanginya cuma melipatgandakan latensi dan jarang menolong,
    jadi error koneksi dibatasi 1 percobaan ulang saja. Tanpa batas ini rantai
    4 sumber bisa tembus ~17 detik saat semua sumber lemot (terukur).
    """
    getter = (session or requests).get
    last = None
    conn_errors = 0
    for attempt in range(retries + 1):
        try:
            resp = getter(url, headers=headers, timeout=timeout)
            if resp.status_code not in _TIKTOK_RETRY_STATUS:
                return resp
            last = resp
        except Exception:
            last = None
            conn_errors += 1
            if conn_errors > 1:
                break
        if attempt < retries:
            wait = 0.5 * (2 ** attempt) + random.uniform(0, 0.25)
            # Jangan tidur (lalu retry) kalau anggaran waktunya sudah tidak cukup.
            if deadline is not None and _tt_left(deadline) - wait < _TIKTOK_MIN_SLICE_SEC:
                break
            time.sleep(wait)
    return last


def get_tiktok_embed_html(video_id, deadline=None):
    """Fetch TikTok Embed V2 HTML using rotating headers.

    Dipakai PALING AKHIR sekarang (lihat catatan rate-limit di atas), dengan
    retry/backoff supaya 503 sesaat tidak langsung mematikan jalur ini.
    """
    if deadline is None:
        deadline = time.time() + _TIKTOK_BUDGET_SEC
    url = f"https://www.tiktok.com/embed/v2/{video_id}"
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
    ]
    headers = {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    resp = _tiktok_get(url, headers=headers, timeout=_tt_timeout(deadline, 6),
                       retries=2, deadline=deadline)
    if resp is not None and resp.status_code == 200:
        return resp.text
    return None

def get_tiktok_tikwm(url, deadline=None):
    """Fetch TikTok caption via TikWM API (Bypass for restricted/hidden content)"""
    if deadline is None:
        deadline = time.time() + _TIKTOK_BUDGET_SEC
    try:
        api_url = f"https://www.tikwm.com/api/?url={quote(url)}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        }
        resp = requests.get(api_url, headers=headers, timeout=_tt_timeout(deadline, 6))
        if resp.status_code == 200:
            data = resp.json()
            if data.get('code') == 0:
                # 'title' contains the description/caption
                caption = data.get('data', {}).get('title', '')
                if caption:
                    return caption, "TikTok:TikWM"
        return None, None
    except:
        return None, None

def get_tiktok_oembed(url, video_id=None, deadline=None):
    """Caption via oEmbed resmi TikTok — sekarang sumber UTAMA.

    BUKAN sumber terpotong: diuji 7 Agu 2026 atas 19 video dgn caption 39-1767
    karakter, field 'title' SELALU byte-identik dgn caption penuh (hashtag di
    ujung ikut terbawa). Endpoint-nya juga jauh lebih ringan dari embed/v2
    (~1.6 KB vs ~300 KB) dan tidak ikut kena overload-protect embed/v2.

    Dua sifat rewelnya yang sudah diakali di sini:
      - username di URL DIABAIKAN (cuma id yg dipakai), jadi URL tanpa @user
        atau dgn @user salah tetap dilayani;
      - path-nya rewel: '/photo/{id}' dibalas 400 sedangkan '/video/{id}' 200.
    Makanya kalau URL asli gagal & id-nya kita punya, dicoba ulang dalam bentuk
    '/video/{id}' — ini yang menyelamatkan post foto.
    """
    if deadline is None:
        deadline = time.time() + _TIKTOK_BUDGET_SEC
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    }
    # Strip query params for oEmbed to stay clean
    candidates = [url.split('?')[0]]
    if video_id:
        normalized = f"https://www.tiktok.com/@i/video/{video_id}"
        if normalized not in candidates:
            candidates.append(normalized)

    last_err = "oEmbed:NoTry"
    for candidate in candidates:
        if _tt_left(deadline) < _TIKTOK_MIN_SLICE_SEC:
            return None, None, "oEmbed:Budget"
        oembed_url = f"https://www.tiktok.com/oembed?url={quote(candidate)}"
        resp = _tiktok_get(oembed_url, headers=headers,
                           timeout=_tt_timeout(deadline, 6), retries=2, deadline=deadline)
        if resp is None:
            last_err = "oEmbedErr:conn"
            continue
        if resp.status_code != 200:
            last_err = f"oEmbed:{resp.status_code}"
            continue
        try:
            title = (resp.json().get('title') or '').strip()
        except Exception as e:
            last_err = f"oEmbedErr:{str(e)[:15]}"
            continue
        if title:
            return title, "TikTok:oEmbed", "oEmbed:OK"
        # Video dihapus/privat dibalas 200 tapi title kosong — bukan error,
        # tapi juga bukan caption. Lanjut ke kandidat/sumber berikutnya.
        last_err = "oEmbed:Empty"
    return None, None, last_err

def get_tiktok_caption(url, expected_code=None):
    debug_log = []
    deadline = time.time() + _TIKTOK_BUDGET_SEC
    # Modern Rotating User-Agents (Post Chrome 130)
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0'
    ]
    
    headers = {
        'User-Agent': random.choice(user_agents),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        # Referer + Sec-Ch-Ua bikin request tampak seperti navigasi browser asli.
        # matrix-scrapper pakai set header ini & TERBUKTI dapat data TikTok dari IP
        # datacenter VPS yang sama (mis. video ZSXXtwUdU → 711 views), sedangkan
        # tanpa Referer sering balik bot-wall "Main:NoData". Murah & aman.
        'Referer': 'https://www.tiktok.com/',
        'Sec-Ch-Ua': '"Chromium";v="131", "Google Chrome";v="131", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
    }
    
    session = requests.Session()
    session.headers.update(headers)
    garbage_phrases = [
        "Watch more exciting videos on TikTok", 
        "Watch now", 
        "See more posts", 
        "Log in to TikTok", 
        "TikTok: Make Your Day",
        "TikTokでおもしろい動画をもっと見る",
        "今すぐ見る",
        "imur sekarang",
        "Video pendek",
        "short video with",
        "on TikTok",
        "| TikTok",
        "dengan ♬",
        "con ♬",
        "Something went wrong",
        "Please try again later"
    ]

    try:
        # 1. Resolve short URLs. If HEAD fails it usually means VPS can't reach the
        # short-URL host at all (vt.tiktok.com is frequently flaky from datacenter
        # IPs), so a follow-up GET would just burn another timeout. Skip it.
        if 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
            try:
                response = session.head(url, headers=headers, allow_redirects=True,
                                        timeout=_tt_timeout(deadline, 5))
                url = response.url
            except:
                debug_log.append("ResolveFail")

        # Id video dipakai beberapa sumber (oEmbed & embed/v2), jadi diambil
        # sekali di sini. Post foto pakai /photo/{id} tapi id-nya sama bentuknya.
        video_id = None
        id_match = re.search(r'/video/(\d+)', url) or re.search(r'/photo/(\d+)', url)
        if id_match:
            video_id = id_match.group(1)

        # 1. oEmbed (SUMBER UTAMA — murah, caption utuh, tak kena rate-limit
        # embed/v2). Kalau captionnya sudah memuat kode yang dicari, itu bukti
        # definitif: langsung balik, hemat fetch halaman ~380 KB di bawah.
        #
        # Kalau captionnya ADA tapi TANPA kode, JANGAN langsung balik — simpan
        # dulu sebagai cadangan lalu tetap coba sumber lain. Alasannya oEmbed
        # bisa dilayani dari cache CDN & masih basi beberapa saat setelah
        # creator baru mengedit caption; kalau kita kunci hasil basi itu,
        # creator yang sudah benar bisa kena tolak.
        oembed_fallback = None
        oembed_caption, oembed_src, oembed_err = get_tiktok_oembed(url, video_id, deadline)
        if oembed_caption:
            cap_lower = oembed_caption.lower()
            if any(p.lower() in cap_lower for p in garbage_phrases):
                debug_log.append("oEmbed:Garbage")
            elif expected_code and expected_code.lower() in cap_lower:
                return oembed_caption, oembed_src
            else:
                oembed_fallback = (oembed_caption, oembed_src)
        else:
            debug_log.append(oembed_err)

        # 2. Main Page Scraping (Full JSON)
        try:
            # UA Bytespider = crawler resmi ByteDance, dan TikTok melayaninya
            # penuh dari IP datacenter: diuji 7 Agu 2026 atas 8 video, Bytespider
            # 8/8 dapat halaman ~380 KB yang memuat "desc", sedangkan UA Chrome
            # 8/8 cuma dapat bot-wall 1462 byte (inilah biang "Main:NoData" yang
            # sering muncul di log VPS). UA browser tetap dicoba sebagai cadangan
            # kalau suatu saat Bytespider yang diblokir.
            html = ''
            for _ua in (_TIKTOK_UA_BYTESPIDER, headers['User-Agent']):
                if _tt_left(deadline) < _TIKTOK_MIN_SLICE_SEC:
                    debug_log.append("Main:Budget")
                    break
                _headers = dict(headers)
                _headers['User-Agent'] = _ua
                _resp = _tiktok_get(url, headers=_headers, timeout=_tt_timeout(deadline, 8),
                                    retries=1, session=session, deadline=deadline)
                if _resp is not None and _resp.status_code == 200 and len(_resp.text) > len(html):
                    html = _resp.text
                if len(html) > 50000:  # halaman penuh — tak perlu coba UA lain
                    break
            if not html:
                raise ValueError("empty")

            # Pattern 0: EARLY EXACT CODE CHECK on main HTML (Highest Reliability)
            # If the unique verification code is anywhere in the main page HTML,
            # the creator definitely included it. Skip fragile JSON pattern matching.
            if expected_code and expected_code.lower() in html.lower():
                code_idx = html.lower().find(expected_code.lower())
                start = max(0, code_idx - 300)
                end = min(len(html), code_idx + len(expected_code) + 300)
                snip = html[start:end]
                snip = re.sub(r'<[^>]+>', ' ', snip)
                snip = re.sub(r'\s+', ' ', snip).strip()
                return f"...{snip}...", "TikTok:Main-EarlyExact"

            # Try finding hydration data
            patterns = [
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
                r'<script id="SIGI_STATE"[^>]*>(.+?)</script>',
                r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>'
            ]
            
            for pattern in patterns:
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        # Schema 1: __DEFAULT_SCOPE__
                        if '__DEFAULT_SCOPE__' in data:
                            scope = data.get('__DEFAULT_SCOPE__', {})
                            # Check multiple paths for caption in webapp layout
                            paths = [
                                ['webapp.video-detail', 'itemInfo', 'itemStruct', 'desc'],
                                ['webapp.video-detail', 'shareMeta', 'desc'],
                                ['webapp.video-detail', 'shareMeta', 'title']
                            ]
                            for path in paths:
                                current = scope
                                for key in path:
                                    current = current.get(key, {}) if isinstance(current, dict) else None
                                if isinstance(current, str) and current:
                                    cap_lower = current.lower()
                                    if not any(gp.lower() in cap_lower for gp in garbage_phrases):
                                        return current, "TikTok:JSON-Scope"
                            
                        # Schema 2: ItemModule
                        if 'ItemModule' in data:
                            for key, item in data['ItemModule'].items():
                                desc = item.get('desc')
                                if desc:
                                    cap_lower = desc.lower()
                                    if not any(gp.lower() in cap_lower for gp in garbage_phrases):
                                        return desc, "TikTok:JSON-Item"
                                
                        # Schema 3: videoDetail
                        video_detail = data.get('props', {}).get('pageProps', {}).get('itemInfo', {}).get('itemStruct', {})
                        desc = video_detail.get('desc')
                        if desc:
                            cap_lower = desc.lower()
                            if not any(gp.lower() in cap_lower for gp in garbage_phrases):
                                return desc, "TikTok:JSON-Props"
                    except: continue
            
            # Fallback to OG tags (High risk of truncation but better than nothing)
            meta_patterns = [
                r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"',
                r'<meta[^>]*name="description"[^>]*content="([^"]*)"',
                r'<meta[^>]*property="twitter:description"[^>]*content="([^"]*)"'
            ]
            for p in meta_patterns:
                match = re.search(p, html)
                if match:
                    res = match.group(1).replace('&amp;', '&').replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>')
                    if res and not any(p.lower() in res.lower() for p in garbage_phrases):
                        return res, "TikTok:MetaOG"
            debug_log.append("Main:NoData")
        except Exception as e: 
            debug_log.append(f"MainErr:{str(e)[:15]}")
        
        # 3. Embed V2 Scraping — sekarang PALING AKHIR sebelum TikWM, karena
        # endpoint inilah yang kena overload-protect. Sudah ber-retry/backoff di
        # get_tiktok_embed_html(); tetap dipertahankan karena DOM-nya kadang
        # memuat caption penuh saat sumber lain cuma kasih potongan.
        if video_id and _tt_left(deadline) >= _TIKTOK_MIN_SLICE_SEC:
            embed_html = get_tiktok_embed_html(video_id, deadline)
            if embed_html:
                # Pattern 0: EARLY EXACT CODE CHECK (Most Reliable - Bypasses pattern fragility)
                # If the unique code is present anywhere in the embed HTML, the user
                # definitely included it. This bypasses TikTok's IP-based JSON shape
                # variations that make pattern matching unreliable on Vercel/datacenter IPs.
                if expected_code and expected_code.lower() in embed_html.lower():
                    code_idx = embed_html.lower().find(expected_code.lower())
                    start = max(0, code_idx - 300)
                    end = min(len(embed_html), code_idx + len(expected_code) + 300)
                    snip = embed_html[start:end]
                    snip = re.sub(r'<[^>]+>', ' ', snip)
                    snip = re.sub(r'\s+', ' ', snip).strip()
                    return f"...{snip}...", "TikTok:Embed-EarlyExact"

                # Pattern 1a: Frontity State JSON (Most Reliable for Full Captions)
                frontity_match = re.search(r'<script id="__FRONTITY_CONNECT_STATE__" type="application/json">(.+?)</script>', embed_html, re.DOTALL)
                if frontity_match:
                    try:
                        frontity_data = json.loads(frontity_match.group(1))
                        # The caption is often buried deep in the state. We search for it.
                        def find_deep_text(obj):
                            if isinstance(obj, dict):
                                if 'videoData' in obj and 'itemInfos' in obj['videoData']:
                                    text = obj['videoData']['itemInfos'].get('text')
                                    if text: return text
                                for v in obj.values():
                                    res = find_deep_text(v)
                                    if res: return res
                            elif isinstance(obj, list):
                                for item in obj:
                                    res = find_deep_text(item)
                                    if res: return res
                            return None
                        
                        cap = find_deep_text(frontity_data)
                        if cap and not any(p.lower() in cap.lower() for p in garbage_phrases):
                             if "#" in cap or len(cap) > 20:
                                 return cap, "TikTok:Embed-Frontity-JSON"
                    except: pass

                # Pattern 1b: Look for "desc" in JSON data (Specific to itemStruct)
                desc_match = re.search(r'"itemStruct"\s*:\s*\{[^}]*?"desc"\s*:\s*"([^"]+)"', embed_html)
                if not desc_match:
                    desc_match = re.search(r'"desc"\s*:\s*"([^"]+)"', embed_html)
                
                if desc_match:
                    cap = desc_match.group(1).encode().decode('unicode_escape', errors='ignore')
                    if cap and not any(p.lower() in cap.lower() for p in garbage_phrases):
                        if "#" in cap or len(cap) > 20: # High Confidence
                            return cap, "TikTok:Embed-JSON-Desc"

                # Pattern 1c: Look for "text" in JSON data (Last Resort for JSON)
                text_match = re.search(r'"text"\s*:\s*"([^"]+)"', embed_html)
                if text_match:
                    cap = text_match.group(1).encode().decode('unicode_escape', errors='ignore')
                    if cap and not any(p.lower() in cap.lower() for p in garbage_phrases):
                        if "#" in cap or len(cap) > 20: # High Confidence
                            return cap, "TikTok:Embed-JSON-Text"

                # Pattern 2: Look for direct Text in DOM
                # Target common data-e2e attributes and classes
                dom_patterns = [
                    r'data-e2e="(?:video-v2-ClampedText-CardTag|video-v2-ClampedText-Text|browse-video-desc)"[^>]*>(.*?)</div>',
                    r'class="[^"]*video-description[^"]*"[^>]*>(.*?)</div>',
                    r'<a[^>]*data-e2e="src-SmartWrapperExtension-a"[^>]*>(.*?)</a>'
                ]
                
                full_caption_parts = []
                for p in dom_patterns:
                    matches = re.findall(p, embed_html, re.DOTALL)
                    for raw_text in matches:
                        clean = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', raw_text, flags=re.DOTALL | re.IGNORECASE)
                        clean = re.sub(r'<[^>]+>', '', clean)
                        clean = clean.replace('&amp;', '&').replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>')
                        cleaned_str = clean.strip()
                        if cleaned_str and cleaned_str not in full_caption_parts:
                            full_caption_parts.append(cleaned_str)
                
                if full_caption_parts:
                    final_caption = " ".join(full_caption_parts).strip()
                    if final_caption:
                        cap_lower = final_caption.lower()
                        if any(p.lower() in cap_lower for p in garbage_phrases):
                             print(f"Garbage Detection (DOM): Found placeholder. Skipping DOM.")
                        else:
                             return final_caption, "TikTok:Embed-DOM"

                if expected_code and expected_code.lower() in embed_html.lower():
                    code_idx = embed_html.lower().find(expected_code.lower())
                    start = max(0, code_idx - 200)
                    end = min(len(embed_html), code_idx + len(expected_code) + 200)
                    snip = embed_html[start:end]
                    snip = re.sub(r'<[^>]+>', ' ', snip)
                    return f"...{snip}...", "TikTok:Embed-HTML-Exact"
                    
                if "#wefluence" in embed_html.lower():
                    return embed_html, "TikTok:Embed-HTML"
                
                # Pattern 4: Broad check for desc key in raw string
                desc_fallback = re.search(r'"desc":"(.*?)"', embed_html)
                if desc_fallback:
                    return desc_fallback.group(1), "TikTok:Embed-RawDesc"
                
        # 4. TikWM API (Aggressive Fallback for Restricted Content)
        # This is a high-success bypass for age-restricted or hidden captions
        if _tt_left(deadline) < _TIKTOK_MIN_SLICE_SEC:
            debug_log.append("TikWM:Budget")
            caption, src = None, None
        else:
            caption, src = get_tiktok_tikwm(url, deadline)
        if caption:
            cap_lower = caption.lower()
            if any(p.lower() in cap_lower for p in garbage_phrases):
                debug_log.append("TikWM:Garbage")
            elif expected_code and expected_code.lower() in cap_lower:
                return caption, src
            elif oembed_fallback is None:
                # Sama-sama tanpa kode: oEmbed (resmi) lebih dipercaya daripada
                # mirror pihak ketiga, jadi TikWM cuma dipakai kalau oEmbed kosong.
                oembed_fallback = (caption, src)

        # Tidak ada sumber yang memuat kode. Balikin caption cadangan (kalau ada)
        # supaya pemanggil tetap lihat caption asli — penting buat includeCaption
        # (moderasi AI) & buat aturan risky_sources/fail-open di handler.
        if oembed_fallback:
            return oembed_fallback

        return None, f"TikTok:None|Log:{','.join(debug_log)}"
    except Exception as e: 
        print(f"Scraper Error: {str(e)}")
        return None, f"TikTok:Error:{str(e)[:20]}"

# ── YouTube config & helpers ─────────────────────────────────────────────
# Data API v3 key resmi (OPSIONAL). Kalau di-set, dipakai sebagai sumber UTAMA:
# tanpa bot-check, deskripsi selalu lengkap → kode pasti kebaca. Key SAMA dengan
# yang dipakai matrix-scrapper (share kuota; verify volumenya kecil, 1 unit/call).
YT_API_KEY = os.environ.get('YT_API_KEY', '').strip()

# Deskripsi generik bawaan YouTube ("Nikmati video dan musik yang Anda suka,
# upload konten asli, ..."/versi EN). Muncul saat deskripsi ASLI tak tersedia
# (bot-check / HTML dipangkas di IP datacenter). DULU string ini diterima sebagai
# "caption" → kode creator tak ketemu → creator DITOLAK padahal kodenya benar
# ada. Sekarang dianggap sampah dan TIDAK PERNAH dikembalikan sebagai caption.
_YT_BOILERPLATE = (
    'upload konten asli',          # ID boilerplate
    'upload original content',     # EN boilerplate
    'nikmati video dan musik',
    'enjoy the videos and music',
)


def _yt_is_boilerplate(text):
    t = (text or '').lower()
    return any(m in t for m in _YT_BOILERPLATE)


def _yt_extract_id(url):
    """videoId dari semua bentuk URL YT (shorts/watch/youtu.be/live/embed)."""
    try:
        for marker in ('/shorts/', '/live/', '/embed/', 'youtu.be/'):
            if marker in url:
                cand = url.split(marker)[1]
                cand = re.split(r'[?&#/]', cand)[0]
                if cand:
                    return cand
        qs = parse_qs(urlparse(url).query)
        if qs.get('v') and qs['v'][0]:
            return qs['v'][0]
    except Exception:
        pass
    return None


def _yt_find_player_response(html, video_id):
    """Parse ytInitialPlayerResponse jadi JSON beneran pakai raw_decode (berhenti
    TEPAT di akhir 1 objek → mustahil kepotong/bocor seperti `find(';')` lama yang
    motong di ';'/'<'/'\\n' PERTAMA — karakter itu sering muncul di dalam string
    JSON YouTube, jadi hasilnya untung-untungan per request/IP). HANYA terima kalau
    videoDetails.videoId == video yang diminta."""
    decoder = json.JSONDecoder()
    search_from = 0
    while True:
        idx = html.find('ytInitialPlayerResponse', search_from)
        if idx == -1:
            return None
        search_from = idx + len('ytInitialPlayerResponse')
        brace = html.find('{', idx)
        # Marker bisa muncul di string lain (bukan assignment) — kalau '{' jauh, skip
        if brace == -1 or brace - idx > 200:
            continue
        try:
            obj, _ = decoder.raw_decode(html, brace)
        except ValueError:
            continue
        vd = obj.get('videoDetails') or {}
        if vd.get('videoId') == video_id:
            return obj


def _yt_caption_from_player(player):
    vd = (player or {}).get('videoDetails', {}) or {}
    title = vd.get('title', '') or ''
    desc = vd.get('shortDescription', '') or ''
    if not desc:
        micro = (player or {}).get('microformat', {}).get('playerMicroformatRenderer', {})
        desc = (micro.get('description', {}) or {}).get('simpleText', '') or ''
    full = f"{title} {desc}".strip()
    return full or None


def _yt_innertube_caption(video_id):
    """Fallback resmi: InnerTube player API (dipakai player YouTube sendiri). Tetap
    balikin videoDetails (title+shortDescription) walau HTML watch page kena
    bot-check di IP datacenter. videoId dicocokkan agar tak kebawa video lain."""
    try:
        body = {
            'context': {'client': {'clientName': 'WEB', 'clientVersion': '2.20240509.00.00', 'hl': 'en', 'gl': 'US'}},
            'videoId': video_id, 'contentCheckOk': True, 'racyCheckOk': True,
        }
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Origin': 'https://www.youtube.com',
            'Referer': f'https://www.youtube.com/watch?v={video_id}',
        }
        resp = requests.post('https://www.youtube.com/youtubei/v1/player', json=body, headers=headers, timeout=7)
        vd = resp.json().get('videoDetails') or {}
        if vd.get('videoId') == video_id:
            title = vd.get('title', '') or ''
            desc = vd.get('shortDescription', '') or ''
            full = f"{title} {desc}".strip()
            return full or None
    except Exception:
        pass
    return None


def _yt_data_api_caption(video_id):
    """Sumber paling andal: Data API v3 resmi (part=snippet). Tanpa bot-check.
    Hanya jalan kalau YT_API_KEY di-set. snippet.description memuat kode unik."""
    if not YT_API_KEY:
        return None
    try:
        resp = requests.get(
            'https://www.googleapis.com/youtube/v3/videos',
            params={'part': 'snippet', 'id': video_id, 'key': YT_API_KEY},
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get('items', [])
        if items:
            snip = items[0].get('snippet', {}) or {}
            title = snip.get('title', '') or ''
            desc = snip.get('description', '') or ''
            full = f"{title} {desc}".strip()
            return full or None
    except Exception:
        pass
    return None


def get_youtube_caption(url, expected_code=None):
    """Baca judul+deskripsi video YT (tempat kode unik ditaruh creator).

    Strategi berlapis, dari paling andal ke fallback:
      1. Data API v3 resmi (kalau YT_API_KEY di-set) — tanpa bot-check.
      2. HTML watch page → ytInitialPlayerResponse (raw_decode, videoId cocok).
      3. InnerTube WEB player API — tetap jalan walau HTML kena bot-check.
      4. ytInitialData panel deskripsi terstruktur.
      5. Grep kode persis di HTML (sinyal positif kuat).

    Deskripsi generik bawaan YT TIDAK PERNAH diterima. Kalau caption asli tak
    terbaca → balikin None supaya endpoint fail-open (cek manual), BUKAN nolak
    creator dengan 'Kode tidak ditemukan' palsu (bug lama: boilerplate diterima
    sebagai caption lalu kode dinyatakan tak ada)."""
    video_id = _yt_extract_id(url)
    if not video_id:
        return None, "YT:NoVideoID"

    # 1) Data API v3 resmi (paling andal, tanpa bot-check)
    cap = _yt_data_api_caption(video_id)
    if cap and not _yt_is_boilerplate(cap):
        return cap, "Src:DataAPI"

    # 2) Watch page HTML → player response (raw_decode + videoId match)
    html = ""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.youtube.com/',
        }
        html = requests.get(f"https://www.youtube.com/watch?v={video_id}", headers=headers, timeout=8).text
    except Exception:
        html = ""

    if html:
        player = _yt_find_player_response(html, video_id)
        if player:
            cap = _yt_caption_from_player(player)
            if cap and not _yt_is_boilerplate(cap):
                return cap, "Src:PlayerJSON"

    # 3) InnerTube WEB player fallback (lolos bot-check)
    cap = _yt_innertube_caption(video_id)
    if cap and not _yt_is_boilerplate(cap):
        return cap, "Src:InnerTube"

    # 4) ytInitialData: panel deskripsi terstruktur (kalau ada)
    if html:
        try:
            decoder = json.JSONDecoder()
            search_from = 0
            while True:
                idx = html.find('ytInitialData', search_from)
                if idx == -1:
                    break
                search_from = idx + len('ytInitialData')
                brace = html.find('{', idx)
                if brace == -1 or brace - idx > 200:
                    continue
                try:
                    initial_data, _ = decoder.raw_decode(html, brace)
                except ValueError:
                    continue
                panels = initial_data.get('engagementPanels', [])
                for panel in panels:
                    renderer = panel.get('engagementPanelSectionListRenderer', {})
                    if renderer.get('targetId') == 'engagement-panel-structured-description':
                        items = renderer.get('content', {}).get('structuredDescriptionContentRenderer', {}).get('items', [])
                        for item in items:
                            body = item.get('expandableVideoDescriptionBodyRenderer', {})
                            if body:
                                runs = body.get('descriptionBodyText', {}).get('runs', [])
                                text = "".join([r.get('text', '') for r in runs])
                                if text and not _yt_is_boilerplate(text):
                                    return text, "Src:InitialData-Panel"
                if panels:
                    break  # objek player-response benar sudah ketemu; stop nyari
        except Exception:
            pass

    # 5) Grep kode persis langsung di HTML (sinyal positif sangat kuat)
    if html and expected_code and expected_code.lower() in html.lower():
        code_idx = html.lower().find(expected_code.lower())
        start = max(0, code_idx - 200)
        end = min(len(html), code_idx + len(expected_code) + 200)
        snip = re.sub(r'<[^>]+>', ' ', html[start:end])
        snip = re.sub(r'\s+', ' ', snip).strip()
        return f"...{snip}...", "Src:HTML-Grepped-ExactCode"

    # Gagal baca caption asli → None → endpoint fail-open (cek manual). TIDAK
    # pernah nolak palsu berdasarkan boilerplate generik YouTube.
    return None, f"YT:Unreadable(vid={video_id})"

def extract_instagram_shortcode(url):
    """Extract shortcode from Instagram URL"""
    match = re.search(r'/(p|reel|reels)/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(2)
    return None


# ── Instagram config & helpers ───────────────────────────────────────────
# Cookie 'sessionid' akun burner IG untuk bypass blok IP datacenter. Dari VPS,
# SEMUA UA (facebot/googlebot/iphone/...) kena HTTP 429 tanpa login → caption
# tak pernah kebaca. Dengan sessionid, GraphQL API resmi jalan normal. Bisa
# banyak sessionid (pisah koma/spasi/baris) → dirotasi acak. SAMA seperti
# matrix-scrapper (yang sudah terbukti jalan di produksi untuk views).
def _parse_ig_pool(raw):
    if not raw:
        return []
    return [s.strip() for s in re.split(r'[,\s]+', raw) if s.strip()]


IG_SESSION_POOL = _parse_ig_pool(os.environ.get('IG_SESSIONID', '') or os.environ.get('IG_SESSIONIDS', ''))
IG_PROXY = os.environ.get('IG_PROXY', '').strip()
IG_BASE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'


def _ig_graphql_caption(shortcode):
    """Ambil caption penuh via GraphQL API resmi IG + cookie sessionid — satu-
    satunya cara andal dari IP datacenter (embed/og mentah kena 429). Return teks
    caption atau None. doc_id & alur sama dengan matrix-scrapper. Retry pakai
    sessionid berbeda (gagal biasanya = akun itu kena flag)."""
    if not IG_SESSION_POOL:
        return None  # tak ada cookie → skip; metode lama di bawah tetap dicoba
    gql_url = 'https://www.instagram.com/graphql/query/'
    params = {'doc_id': '8845758582119845', 'variables': json.dumps({'shortcode': shortcode})}
    tried = set()
    for attempt in range(2):
        pool = [s for s in IG_SESSION_POOL if s not in tried] or IG_SESSION_POOL
        sid = random.choice(pool)
        tried.add(sid)
        s = requests.Session()
        s.cookies.set('sessionid', sid, domain='.instagram.com')
        if IG_PROXY:
            s.proxies.update({'http': IG_PROXY, 'https': IG_PROXY})
        # Warmup embed dulu supaya dapat cookie csrftoken/mid
        try:
            s.get(f'https://www.instagram.com/p/{shortcode}/embed/captioned/',
                  headers={'User-Agent': IG_BASE_UA}, timeout=4)
        except Exception:
            pass
        headers = {
            'User-Agent': IG_BASE_UA,
            'X-IG-App-ID': '936619743392459',
            'X-ASBD-ID': '129477',
            'X-IG-WWW-Claim': '0',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': f'https://www.instagram.com/p/{shortcode}/embed/',
        }
        csrf = s.cookies.get('csrftoken')
        if csrf:
            headers['X-CSRFToken'] = csrf
        try:
            r = s.get(gql_url, headers=headers, params=params, timeout=8)
            if r.status_code == 200:
                media = (r.json().get('data', {}) or {}).get('xdt_shortcode_media')
                if media:
                    edges = (media.get('edge_media_to_caption', {}) or {}).get('edges', [])
                    if edges:
                        text = (edges[0].get('node', {}) or {}).get('text', '')
                        if text:
                            return text
                    return None  # media ketemu tapi caption memang kosong
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.6)
    return None


def get_instagram_caption(url, expected_code=None):
    """Scrape caption from Instagram Reels/Post URL using multiple methods"""
    caption = None
    debug_log = []

    try:
        shortcode = extract_instagram_shortcode(url)
        if not shortcode:
            return None, "Invalid URL"

        # Method 0: GraphQL API resmi + cookie sessionid. Dari IP datacenter ini
        # praktis SATU-SATUNYA yang lolos (metode di bawah kena 429). Kalau tak ada
        # cookie / gagal → return None → lanjut metode lama tanpa efek samping.
        gql_cap = _ig_graphql_caption(shortcode)
        if gql_cap:
            return gql_cap, "Src:GraphQL-Auth"

        # Method 1: Instagram public oEmbed used to work without auth; as of 2024+
        # api.instagram.com/oembed returns an HTML login wall, not JSON. Skipped.

        # Method 1b: /embed/captioned/ — designed for iframe embedding, much more
        # permissive than the main page (still returns 200 from datacenter IPs that
        # get 429 on the main URL). Caption sits inside <div class="Caption">.
        try:
            embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
            for ua in [
                'facebookexternalhit/1.1',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
            ]:
                try:
                    r = requests.get(embed_url, headers={'User-Agent': ua}, timeout=8)
                    if r.status_code != 200:
                        debug_log.append(f"emb-{ua[:3]}:{r.status_code}")
                        continue
                    page = r.text
                    # Look for the Caption div — class is literally "Caption"
                    cap_match = re.search(
                        r'<div[^>]*class="[^"]*\bCaption\b[^"]*"[^>]*>(.*?)</div>\s*</div>',
                        page, re.DOTALL,
                    )
                    if not cap_match:
                        cap_match = re.search(
                            r'<div[^>]*class="[^"]*\bCaption\b[^"]*"[^>]*>(.*?)</div>',
                            page, re.DOTALL,
                        )
                    if cap_match:
                        raw = cap_match.group(1)
                        # Strip nested tags, keep text/whitespace, unescape entities
                        text = re.sub(r'<[^>]+>', ' ', raw)
                        try:
                            import html as _html
                            text = _html.unescape(text)
                        except Exception:
                            pass
                        text = re.sub(r'[ \t]+', ' ', text).strip()
                        if text and len(text) > 5:
                            return text, "Src:Embed-Captioned"
                    # Fallback: grep for expected code anywhere in embed HTML
                    if expected_code and expected_code.lower() in page.lower():
                        idx = page.lower().find(expected_code.lower())
                        snip = re.sub(r'<[^>]+>', ' ',
                                       page[max(0, idx-200):idx+len(expected_code)+200])
                        snip = re.sub(r'\s+', ' ', snip).strip()
                        return f"...{snip}...", "Src:Embed-Grepped"
                    debug_log.append(f"emb-{ua[:3]}:NoCap")
                except Exception as e:
                    debug_log.append(f"embErr:{str(e)[:15]}")
        except Exception as e:
            debug_log.append(f"EmbErr:{str(e)[:15]}")

        # Method 2: Crawler/Mobile UA scraping — try a list of UAs. Datacenter IPs
        # (Hostinger/Vercel/etc) often get a login wall on the default Instagram UA,
        # but social/search crawler UAs still receive a clean og:description for
        # public posts. We try them in order of reliability.
        ua_list = [
            ('facebot', 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'),
            ('whatsapp', 'WhatsApp/2.23.20.0'),
            ('bingbot', 'Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)'),
            ('googlebot', 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'),
            ('iphone', 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'),
            ('igapp', 'Instagram 219.0.0.12.117 Android (26/8.0.0; 480dpi; 1080x1920; samsung; SM-G950F; dreamlte; samsungexynos8895; en_US)'),
        ]
        clean_url = f"https://www.instagram.com/p/{shortcode}/"
        html = None
        used_ua = None
        desc_match = None
        for tag, ua in ua_list:
            try:
                hdrs = {
                    'User-Agent': ua,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                }
                response = requests.get(clean_url, headers=hdrs, timeout=6, allow_redirects=True)
                if response.status_code != 200:
                    debug_log.append(f"{tag}:{response.status_code}")
                    continue
                page = response.text
                m = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', page)
                if not m:
                    m = re.search(r'<meta[^>]*content="([^"]*)"[^>]*property="og:description"', page)
                if m and m.group(1).strip():
                    html = page
                    used_ua = tag
                    desc_match = m
                    break
                else:
                    debug_log.append(f"{tag}:NoMeta")
            except Exception as e:
                debug_log.append(f"{tag}Err:{str(e)[:15]}")

        try:
            if desc_match:
                raw_desc = desc_match.group(1)
                raw_desc = (raw_desc.replace('&amp;', '&').replace('&quot;', '"')
                                    .replace('&#39;', "'").replace('&#x27;', "'")
                                    .replace('&lt;', '<').replace('&gt;', '>'))
                # Decode numeric HTML entities (&#x1f496; etc) so emoji/whitespace survive
                try:
                    import html as _html
                    raw_desc = _html.unescape(raw_desc)
                except Exception:
                    pass

                # Instagram og:description format:
                #   "0 likes, 0 comments - user on May 15, 2026: \"CAPTION\". "
                # Extract the quoted caption — greedy so multi-line captions survive.
                greedy_match = re.search(r'(?:Instagram|:)\s*["“](.*)["”]', raw_desc, re.DOTALL)
                if greedy_match:
                    res = greedy_match.group(1).strip()
                    if res:
                        return res, f"Src:Meta-Greedy-{used_ua}"

                # Fallback: any quoted segment — take the longest (usually the caption)
                caption_parts = re.findall(r'[:\s]["“](.*?)["”]', raw_desc, re.DOTALL)
                if caption_parts:
                    longest = max(caption_parts, key=len).strip()
                    if longest:
                        return longest, f"Src:Meta-Refined-{used_ua}"

                # Last resort: return the raw og:description so upstream can compare codes
                if raw_desc.strip():
                    return raw_desc, f"Src:Meta-Raw-{used_ua}"

            # 2b. Search for exact expected code directly in HTML - VERY RELIABLE FALLBACK
            if html and expected_code and expected_code.lower() in html.lower():
                code_idx = html.lower().find(expected_code.lower())
                start = max(0, code_idx - 200)
                end = min(len(html), code_idx + len(expected_code) + 200)
                snip = html[start:end]
                snip = re.sub(r'<[^>]+>', ' ', snip)
                return f"...{snip}...", "Src:HTML-Grepped-ExactCode"

            # Legacy WF code fallback (for older WF- format)
            if html:
                wf_match = re.search(r'WF-[A-Z0-9]{4}-[A-Z0-9]{4}', html)
                if wf_match:
                    start = max(0, wf_match.start() - 200)
                    end = min(len(html), wf_match.end() + 200)
                    snip = html[start:end]
                    snip = re.sub(r'<[^>]+>', ' ', snip)
                    return f"...{snip}...", "Src:HTML-Grepped-Code"
        except Exception as e:
            debug_log.append(f"ParseErr:{str(e)[:20]}")

        # Method 3: Desktop User Agent & JSON hunting
        try:
            desktop_headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            }
            response = requests.get(f"https://www.instagram.com/p/{shortcode}/", headers=desktop_headers, timeout=6)
            html = response.text
            
            # 3a. Look for sharedData JSON
            json_match = re.search(r'window\._sharedData\s*=\s*({.+?});</script>', html)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    media = data.get('entry_data', {}).get('PostPage', [{}])[0].get('graphql', {}).get('shortcode_media', {})
                    edges = media.get('edge_media_to_caption', {}).get('edges', [])
                    if edges:
                        return edges[0].get('node', {}).get('text', ''), "Src:DesktopJSON"
                except: pass
            
            # 3b. Look for "caption" or "text" in ANY script tag
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
            for script in scripts:
                if expected_code and expected_code.lower() in script.lower():
                    # Deeply buried inside raw JSON - return exact code with some context
                    return f"...{expected_code}...", "Src:Script-JSON-Grepped-Exact"
                if 'WF-' in script:
                    cap_match = re.search(r'"text":\s*"([^"]*WF-[^"]*)"', script)
                    if cap_match:
                        try:
                            return cap_match.group(1).encode().decode('unicode_escape'), "Src:Script-JSON-Grepped"
                        except: pass
            
            # 3c. Final Raw extraction attempt
            extra_match = re.search(r'"caption":\s*\{\s*"text":\s*"([^"]*)"', html)
            if extra_match:
                try:
                    cap = extra_match.group(1).encode().decode('unicode_escape')
                    return cap, "Src:DesktopExtra"
                except: pass
            
            debug_log.append("Desktop:NoData")
        except Exception as e:
            debug_log.append(f"DesktopErr:{str(e)[:20]}")

        return None, f"Fail:{';'.join(debug_log)}"
    except Exception as e:
        return None, f"Err:{str(e)}"

def get_threads_caption(url, expected_code=None):
    """Scrape caption from Threads post URL"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=8)
        html = response.text

        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)

        def find_thread_items(obj):
            if isinstance(obj, dict):
                if 'thread_items' in obj:
                    return obj['thread_items']
                for v in obj.values():
                    result = find_thread_items(v)
                    if result is not None:
                        return result
            elif isinstance(obj, list):
                for item in obj:
                    result = find_thread_items(item)
                    if result is not None:
                        return result
            return None

        for script in scripts:
            if 'BarcelonaPostPageDirectQuery' in script and 'thread_items' in script:
                json_match = re.search(r'\{.*\}', script, re.DOTALL)
                if json_match:
                    try:
                        data = json.loads(json_match.group(0))
                        thread_items = find_thread_items(data)
                        if thread_items and len(thread_items) > 0:
                            post = thread_items[0].get('post', {})
                            caption_obj = post.get('caption')
                            if caption_obj and isinstance(caption_obj, dict):
                                text = caption_obj.get('text', '')
                                if text:
                                    return text, "Threads:JSON"
                    except:
                        pass

        # Fallback: og:description
        og_match = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
        if og_match:
            text = og_match.group(1).replace('&amp;', '&').replace('&quot;', '"').replace('&#39;', "'")
            if text:
                return text, "Threads:MetaOG"

        return None, "Threads:None"
    except Exception as e:
        return None, f"Threads:Err:{str(e)[:20]}"


def get_x_caption(url, expected_code=None):
    """Scrape caption from X (Twitter) post URL"""
    tweet_id_match = re.search(r'/status/(\d+)', url)
    if not tweet_id_match:
        return None, "X:NoTweetID"
    tweet_id = tweet_id_match.group(1)

    try:
        # Method 1: vxtwitter API (no auth needed)
        try:
            username_match = re.search(r'(?:x|twitter)\.com/([^/]+)/status', url)
            username = username_match.group(1) if username_match else 'i'
            r_vx = requests.get(
                f'https://api.vxtwitter.com/{username}/status/{tweet_id}',
                headers={'User-Agent': 'Mozilla/5.0'}, timeout=6
            )
            if r_vx.status_code == 200:
                vx_data = r_vx.json()
                text = vx_data.get('text', '')
                if text:
                    return text, "X:vxtwitter"
        except:
            pass

        # Method 2: X GraphQL Guest API
        try:
            ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            r_home = requests.get('https://x.com', headers={'User-Agent': ua}, timeout=6)
            js_urls = re.findall(r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js', r_home.text)
            if js_urls:
                r_js = requests.get(js_urls[0], headers={'User-Agent': ua}, timeout=8)
                js_text = r_js.text

                bearer_match = re.search(r'"(AAAAAAAAAAAAAAAAAAAAANR[A-Za-z0-9+/=_%]{50,150})"', js_text)
                qid_match = re.search(r'queryId:"([^"]+)",operationName:"TweetResultByRestId"', js_text)

                if bearer_match and qid_match:
                    bearer = bearer_match.group(1)
                    qid = qid_match.group(1)

                    r_guest = requests.post(
                        'https://api.x.com/1.1/guest/activate.json',
                        headers={'Authorization': f'Bearer {bearer}', 'User-Agent': ua},
                        timeout=6
                    )
                    if r_guest.status_code == 200:
                        guest_token = r_guest.json().get('guest_token', '')
                        if guest_token:
                            variables = json.dumps({
                                "tweetId": tweet_id,
                                "withCommunity": False,
                                "includePromotedContent": False,
                                "withVoice": False
                            })
                            features = json.dumps({
                                "creator_subscriptions_tweet_preview_api_enabled": True,
                                "communities_web_enable_tweet_community_results_fetch": True,
                                "c9s_tweet_anatomy_moderator_badge_enabled": True,
                                "articles_preview_enabled": True,
                                "responsive_web_edit_tweet_api_enabled": True,
                                "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                                "view_counts_everywhere_api_enabled": True,
                                "longform_notetweets_consumption_enabled": True,
                                "responsive_web_twitter_article_tweet_consumption_enabled": True,
                                "tweet_awards_web_tipping_enabled": False,
                                "creator_subscriptions_quote_tweet_preview_enabled": False,
                                "freedom_of_speech_not_reach_fetch_enabled": True,
                                "standardized_nudges_misinfo": True,
                                "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                                "rweb_video_timestamps_enabled": True,
                                "longform_notetweets_rich_text_read_enabled": True,
                                "longform_notetweets_inline_media_enabled": True,
                                "responsive_web_graphql_exclude_directive_enabled": True,
                                "verified_phone_label_enabled": False,
                                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                                "responsive_web_graphql_timeline_navigation_enabled": True,
                                "responsive_web_enhance_cards_enabled": False
                            })
                            r_gql = requests.get(
                                f'https://x.com/i/api/graphql/{qid}/TweetResultByRestId',
                                params={'variables': variables, 'features': features},
                                headers={
                                    'User-Agent': ua,
                                    'Authorization': f'Bearer {bearer}',
                                    'X-Guest-Token': guest_token,
                                    'x-csrf-token': '0' * 32,
                                    'x-twitter-active-user': 'yes',
                                    'x-twitter-client-language': 'en',
                                },
                                timeout=8
                            )
                            if r_gql.status_code == 200:
                                gql_data = r_gql.json()

                                def find_tweet_node(obj):
                                    if isinstance(obj, dict):
                                        if 'legacy' in obj and isinstance(obj['legacy'], dict):
                                            if 'full_text' in obj['legacy']:
                                                return obj
                                        for v in obj.values():
                                            result = find_tweet_node(v)
                                            if result:
                                                return result
                                    elif isinstance(obj, list):
                                        for item in obj:
                                            result = find_tweet_node(item)
                                            if result:
                                                return result
                                    return None

                                node = find_tweet_node(gql_data)
                                if node:
                                    text = node['legacy'].get('full_text', '')
                                    if text:
                                        return text, "X:GraphQL"
        except:
            pass

        return None, "X:None"
    except Exception as e:
        return None, f"X:Err:{str(e)[:20]}"


def get_capcut_caption(url, expected_code=None):
    """Scrape caption from CapCut template URL"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=8)
        html = response.text

        # Extract __MODERN_ROUTER_DATA__ (embedded as <script id="__MODERN_ROUTER_DATA__">JSON</script>)
        match = re.search(r'<script[^>]*id="__MODERN_ROUTER_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                loader_data = data.get('loaderData', {})
                for key, value in loader_data.items():
                    if isinstance(value, dict):
                        td = value.get('templateDetail', {})
                        if td:
                            title = td.get('title', '') or td.get('tagTitle', '')
                            desc = td.get('desc', '')
                            caption = f"{title} {desc}".strip()
                            if caption:
                                return caption, "CapCut:JSON"
            except:
                pass

        # Fallback: og tags
        og_title = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html)
        og_desc = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
        title = og_title.group(1) if og_title else ''
        desc = og_desc.group(1) if og_desc else ''
        caption = f"{title} {desc}".strip()
        if caption:
            return caption, "CapCut:MetaOG"

        return None, "CapCut:None"
    except Exception as e:
        return None, f"CapCut:Err:{str(e)[:20]}"


def get_facebook_caption(url, expected_code=None):
    """Scrape caption from Facebook video/reel/post URL.
    Tight time budget: max ~20s wall-clock so nginx (60s) never times out even when
    other handlers (TikTok etc) have already eaten part of the budget. Tries up to
    3 endpoints and stops the moment a caption (or the expected code) is found."""
    import time as _time
    deadline = _time.time() + 20.0

    bot_headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    iphone_headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    def remaining():
        return max(0.5, deadline - _time.time())

    def clean_meta(text):
        return (text.replace('&amp;', '&').replace('&quot;', '"')
                    .replace('&#39;', "'").replace('&#x27;', "'")
                    .replace('&lt;', '<').replace('&gt;', '>'))

    def grep_code(html, source):
        """If expected_code is anywhere in raw HTML, return high-confidence snippet.
        Tags are NOT stripped: code is often inside <meta content="..."> and stripping
        the surrounding tag would erase the code along with it."""
        if not expected_code:
            return None, None
        if expected_code.lower() in html.lower():
            idx = html.lower().find(expected_code.lower())
            start = max(0, idx - 200)
            end = min(len(html), idx + len(expected_code) + 200)
            snip = re.sub(r'\s+', ' ', html[start:end]).strip()
            return f"...{snip}...", source
        return None, None

    def try_extract(html, src_prefix="Facebook"):
        cap, src = grep_code(html, f"{src_prefix}:HTML-Grepped")
        if cap:
            return cap, src
        m = re.search(r'"story_title"\s*:\s*\{"text"\s*:\s*"([^"]+)"', html)
        if m:
            return m.group(1), f"{src_prefix}:StoryTitle"
        # Collect all message candidates and prefer the one containing the code.
        # The original first-match regex often grabbed a comment or a related-post
        # snippet instead of the real reel caption.
        msgs = re.findall(r'"message"\s*:\s*\{"text"\s*:\s*"([^"]{10,})"', html)
        if msgs:
            if expected_code:
                for msg in msgs:
                    if expected_code.lower() in msg.lower():
                        return msg, f"{src_prefix}:Message-CodeMatch"
            generic = ['log in', 'see posts', 'see more', 'lihat postingan']
            non_generic = [m for m in msgs if not any(g in m.lower() for g in generic)]
            if non_generic:
                return max(non_generic, key=len), f"{src_prefix}:Message"
        m = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
        if m:
            desc = clean_meta(m.group(1))
            generic = ['log in', 'see posts', 'see more', 'lihat postingan', 'facebook']
            if desc and len(desc) > 15 and not any(g in desc.lower() for g in generic):
                return desc, f"{src_prefix}:OGDesc"
        return None, None

    debug_log = []

    # Attempt 1: Googlebot on original URL — handles /share/r/ redirects automatically
    # and is what Facebook serves the most caption-rich HTML to.
    video_id = None
    try:
        r = requests.get(url, headers=bot_headers, allow_redirects=True,
                         timeout=min(10, remaining()))
        if r.status_code == 200:
            cap, src = try_extract(r.text, "Facebook")
            if cap:
                return cap, src
            vid_m = re.search(r'(?:reel/|watch/?[?&]v=|videos?/|/video/)(\d{10,})',
                              r.url + r.text[:8000])
            if vid_m:
                video_id = vid_m.group(1)
        debug_log.append(f"bot:{r.status_code}")
    except Exception as e:
        debug_log.append(f"botErr:{str(e)[:15]}")

    # Attempt 2: lightweight m.facebook.com (~13KB) — frequently bypasses login wall
    # when full desktop site does not. Skip if we have no video ID or no time left.
    if video_id and remaining() > 4:
        try:
            r = requests.get(f'https://m.facebook.com/reel/{video_id}/',
                             headers=iphone_headers, allow_redirects=True,
                             timeout=min(7, remaining()))
            if r.status_code == 200:
                cap, src = try_extract(r.text, "Facebook:M")
                if cap:
                    return cap, src
            debug_log.append(f"m.reel:{r.status_code}")
        except Exception as e:
            debug_log.append(f"m.reelErr:{str(e)[:15]}")

    # Attempt 3: plugins/post.php embed — last resort, only if budget allows
    if video_id and remaining() > 4:
        try:
            from urllib.parse import quote as _q
            plugin_url = ('https://www.facebook.com/plugins/post.php?href='
                          + _q(f'https://www.facebook.com/reel/{video_id}/', safe='')
                          + '&show_text=true')
            r = requests.get(plugin_url, headers=bot_headers, allow_redirects=True,
                             timeout=min(7, remaining()))
            if r.status_code == 200:
                cap, src = try_extract(r.text, "Facebook:Plugin")
                if cap:
                    return cap, src
            debug_log.append(f"plg:{r.status_code}")
        except Exception as e:
            debug_log.append(f"plgErr:{str(e)[:15]}")

    return None, f"Facebook:LoginWall|{','.join(debug_log)}"


# --- FastAPI App ---

app = FastAPI(title="Wefluence Caption Scraper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Cache hasil verify (in-memory, TTL). HANYA simpan hasil VALID (kode ketemu /
# manual). 'Kode tidak ditemukan' & scraper-skip TIDAK di-cache → creator yang
# baru edit caption langsung dapat cek fresh, & item invalid tetap di-cek ulang.
# Mengurangi scrape-ulang berat saat admin refresh layar moderasi berkali-kali.
VERIFY_CACHE_TTL = int(os.environ.get('VERIFY_CACHE_TTL', '600'))  # 10 menit
_verify_cache = {}
_verify_cache_lock = threading.Lock()


def _vcache_key(url, code, inc):
    return f"{url}\x00{code}\x00{int(inc)}"


def _vcache_get(key):
    with _verify_cache_lock:
        e = _verify_cache.get(key)
        if not e:
            return None
        ts, data = e
        if time.time() - ts > VERIFY_CACHE_TTL:
            _verify_cache.pop(key, None)
            return None
        return data


def _vcache_set(key, data):
    with _verify_cache_lock:
        _verify_cache[key] = (time.time(), data)
        if len(_verify_cache) > 5000:  # cleanup oportunistik
            now = time.time()
            for k in [k for k, (t, _) in _verify_cache.items() if now - t > VERIFY_CACHE_TTL]:
                _verify_cache.pop(k, None)


def _canon_confusable(s):
    """Lipat karakter yang MIRIP SECARA VISUAL supaya kode tetap kecocokan walau
    creator salah ketik pakai karakter kembar. Kode di-generate dari alfabet
    'abcdefghjklmnpqrstuvwxyz23456789' (huruf 'i','o' & angka '1','0' SUDAH
    dibuang) TAPI huruf 'l' (L kecil) masih ikut — dan 'l' itu di layar tak bisa
    dibedakan dari '1'/'I'. Creator sering menyalin ulang kode & menukar 'l' jadi
    'I' atau '1' (persis kasus id 'lk2aewlp' -> ditulis 'Ik2aewlp' di caption).

    Cuma melipat kelas yang TIDAK MUNGKIN bentrok dgn alfabet generator, jadi
    nggak bikin kode beda jadi ketuker:
      I / L / 1 / |  -> I   (generator cuma pernah keluarin 'l')
      O / 0          -> O   (generator nggak pernah keluarin dua-duanya)
    Input diasumsikan sudah .upper(). Sengaja konservatif: hanya MENAMBAH
    kecocokan, tidak pernah menghilangkan.
    """
    return (s.replace('L', 'I').replace('1', 'I').replace('|', 'I')
             .replace('0', 'O'))


class VerifyRequest(BaseModel):
    url: str
    expectedCode: str
    # Opt-in: kalau True, sertakan teks caption di respons (dipakai AI moderasi
    # biar bisa cek kesesuaian brief/larangan). Default False -> perilaku LAMA
    # tidak berubah (caption tetap disembunyikan dari pemanggil biasa/creator).
    includeCaption: bool = False


@app.get("/")
def health():
    return {"status": "ok", "service": "caption-scraper"}


@app.post("/api/verify")
def verify(req: VerifyRequest):
    url = (req.url or "").strip()
    code = (req.expectedCode or "").strip()

    if not url or not code:
        raise HTTPException(
            status_code=400,
            detail={"error": "Missing url or expectedCode", "valid": False},
        )

    # Cache hit → balikin instan (hemat scrape ulang saat admin refresh berkali2)
    _ck = _vcache_key(url, code, bool(req.includeCaption))
    _hit = _vcache_get(_ck)
    if _hit is not None:
        return _hit

    url_lower = url.lower()
    caption = None
    debug_src = "Unknown"

    if 'tiktok.com' in url_lower:
        caption, debug_src = get_tiktok_caption(url, code)
    elif 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        caption, debug_src = get_youtube_caption(url, code)
    elif 'instagram.com' in url_lower:
        caption, debug_src = get_instagram_caption(url, code)
    elif 'threads.com' in url_lower or 'threads.net' in url_lower:
        caption, debug_src = get_threads_caption(url, code)
    elif 'x.com' in url_lower or 'twitter.com' in url_lower:
        caption, debug_src = get_x_caption(url, code)
    elif 'capcut.com' in url_lower:
        caption, debug_src = get_capcut_caption(url, code)
    elif 'facebook.com' in url_lower or 'fb.watch' in url_lower:
        caption, debug_src = get_facebook_caption(url, code)

    # Fail-Open: scraper totally failed -> ALLOW (Manual Check)
    if not caption:
        return {
            'valid': True,
            'message': 'Verifikasi manual diperlukan (Scraper Skipped).',
            'debug_caption': f"SKIP: {debug_src}",
            'manual_check': True,
            **({'caption': caption} if req.includeCaption else {}),
        }

    normalized_caption = caption.upper()
    normalized_code = code.upper().strip()
    is_valid = normalized_code in normalized_caption

    # Fallback toleransi karakter kembar (l/I/1, O/0). Creator yang menyalin ulang
    # kode dgn tangan sering menukar 'l' -> 'I'/'1' dsb, bikin kode SEBENARNYA ada
    # di caption tapi gagal cocok persis. Lipat kedua sisi ke bentuk kanonik lalu
    # cek ulang. Hanya MENAMBAH kecocokan, tidak pernah menolak yg sudah valid.
    if not is_valid:
        is_valid = _canon_confusable(normalized_code) in _canon_confusable(normalized_caption)

    # If rejected BUT source was risky (truncated OG tags etc) -> ALLOW (Manual Check)
    risky_sources = [
        "TikTok:MetaOG", "TikTok:Embed-HTML", "Mobile:NoMeta",
        "TikTok:oEmbed", "TikTok:Embed-RawDesc",
        "Threads:MetaOG", "Facebook:OGTitle", "Facebook:WatchOG",
        "CapCut:MetaOG",
    ]
    if not is_valid and debug_src in risky_sources:
        result = {
            'valid': True,
            'message': 'Verifikasi manual diperlukan (Possibly Truncated).',
            'debug_caption': f"TRUNCATED: {debug_src}",
            'manual_check': True,
            **({'caption': caption} if req.includeCaption else {}),
        }
        _vcache_set(_ck, result)
        return result

    result = {
        'valid': is_valid,
        'message': 'Kode ditemukan!' if is_valid else f'Kode {code} tidak ditemukan di caption. Mohon edit caption di platform terkait lalu submit ulang.',
        'debug_caption': f"Src:{debug_src}" if not is_valid else 'HIDDEN',
        **({'caption': caption} if req.includeCaption else {}),
    }
    # Cache HANYA yang valid (kode ketemu). 'Tidak ketemu' jangan di-cache biar
    # creator yang baru perbaiki caption langsung dapat hasil fresh.
    if is_valid:
        _vcache_set(_ck, result)
    return result
