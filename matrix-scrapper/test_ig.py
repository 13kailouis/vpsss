"""
Run this on VPS: python3 test_ig.py
Ganti TEST_URL dengan Instagram reel URL yang mau ditest.
"""
import requests, json, re, sys

TEST_URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.instagram.com/reel/GANTI_INI/"

def sc(url):
    m = re.search(r'/(reel|p|tv)/([A-Za-z0-9_-]+)', url)
    return m.group(2) if m else None

shortcode = sc(TEST_URL)
print(f"\nURL       : {TEST_URL}")
print(f"Shortcode : {shortcode}\n")

BASE_UA    = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
MOBILE_UA  = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'

session = requests.Session()

# ── Method 1: GraphQL ────────────────────────────────────────────────────────
print("── Method 1: GraphQL API (xdt_shortcode_media.video_url) ──────")
try:
    r0 = session.get(f'https://www.instagram.com/p/{shortcode}/embed/captioned/',
                     headers={'User-Agent': BASE_UA}, timeout=10)
    print(f"  embed prefetch : {r0.status_code}  cookies={dict(session.cookies)}")
    r = session.get(
        'https://www.instagram.com/graphql/query/',
        params={'doc_id': '8845758582119845', 'variables': json.dumps({'shortcode': shortcode})},
        headers={
            'User-Agent': BASE_UA, 'X-IG-App-ID': '936619743392459',
            'X-Requested-With': 'XMLHttpRequest', 'Accept': '*/*',
            'Referer': f'https://www.instagram.com/p/{shortcode}/embed/',
        }, timeout=15)
    print(f"  GraphQL status : {r.status_code}")
    try:
        data = r.json()
        media = data.get('data', {}).get('xdt_shortcode_media') or {}
        print(f"  is_video       : {media.get('is_video')}")
        print(f"  media keys     : {list(media.keys())[:12]}")
        vurl = media.get('video_url')
        if vurl:
            print(f"  ✅ video_url   : {vurl[:80]}...")
        else:
            print(f"  ❌ video_url not in response")
    except Exception as e:
        print(f"  parse error    : {e}  raw={r.text[:200]}")
except Exception as e:
    print(f"  ❌ {e}")

# ── Method 2: Embed page HTML ────────────────────────────────────────────────
print("\n── Method 2: Embed page HTML scraping ─────────────────────────")
try:
    r2 = session.get(
        f'https://www.instagram.com/p/{shortcode}/embed/captioned/',
        headers={'User-Agent': MOBILE_UA, 'Accept-Language': 'en-US,en;q=0.9'},
        timeout=15)
    print(f"  status : {r2.status_code}  len={len(r2.text)}")
    m2 = re.search(r'"video_url"\s*:\s*"([^"]+)"', r2.text)
    if m2:
        vurl2 = m2.group(1).replace('\\u0026', '&').replace('\\/', '/')
        print(f"  ✅ video_url : {vurl2[:80]}...")
    else:
        m2b = re.search(r'(https://scontent[^\s"\'\\]+\.mp4[^\s"\'\\]*)', r2.text)
        if m2b:
            print(f"  ✅ scontent mp4 : {m2b.group(1)[:80]}...")
        else:
            print(f"  ❌ no video_url. has 'video': {'video' in r2.text.lower()}")
except Exception as e:
    print(f"  ❌ {e}")

# ── Method 3: yt-dlp ─────────────────────────────────────────────────────────
print("\n── Method 3: yt-dlp ────────────────────────────────────────────")
try:
    import yt_dlp
    ydl_opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'format': 'best[ext=mp4]/best',
        'http_headers': {'User-Agent': MOBILE_UA},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(TEST_URL, download=False)
    if info and info.get('url'):
        print(f"  ✅ url : {info['url'][:80]}...")
    elif info and info.get('formats'):
        f0 = info['formats'][-1]
        print(f"  ✅ format url : {f0.get('url','')[:80]}...")
    else:
        print(f"  ❌ no url in info")
except Exception as e:
    print(f"  ❌ {e}")

# ── Method 4: instaloader ─────────────────────────────────────────────────────
print("\n── Method 4: instaloader ───────────────────────────────────────")
try:
    import instaloader
    L = instaloader.Instaloader(max_connection_attempts=1, quiet=True)
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    if post.is_video:
        print(f"  ✅ video_url : {post.video_url[:80]}...")
    else:
        print(f"  ❌ post is not a video")
except Exception as e:
    print(f"  ❌ {e}")

print("\n=== Done ===\n")
