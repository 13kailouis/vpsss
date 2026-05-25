import json
import re
import requests
import traceback
import os
import time
import threading
from urllib.parse import parse_qs, urlparse
import html as html_lib
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================================
# In-memory cache untuk hasil scrape — TTL 5 menit
# Mengurangi beban ke TikTok/IG/YT saat banyak admin scrape URL yang sama
# ============================================================
CACHE_TTL_SECONDS = int(os.environ.get('SCRAPE_CACHE_TTL', '300'))  # 5 menit
_scrape_cache: dict = {}  # { url: (timestamp, result_dict) }
_cache_lock = threading.Lock()
_last_cleanup_ts = 0.0

def _cache_get(url: str):
    """Return cached result kalau masih fresh, else None."""
    now = time.time()
    with _cache_lock:
        entry = _scrape_cache.get(url)
        if not entry:
            return None
        ts, data = entry
        if now - ts > CACHE_TTL_SECONDS:
            # Expired — hapus
            _scrape_cache.pop(url, None)
            return None
        return data

def _cache_set(url: str, data: dict):
    """Simpan hasil ke cache — hanya kalau bukan error."""
    if not data or 'error' in data:
        return
    global _last_cleanup_ts
    now = time.time()
    with _cache_lock:
        _scrape_cache[url] = (now, data)
        # Cleanup expired entries setiap 60 detik supaya dict tidak grow tanpa batas
        if now - _last_cleanup_ts > 60:
            _last_cleanup_ts = now
            expired_keys = [k for k, (t, _) in _scrape_cache.items() if now - t > CACHE_TTL_SECONDS]
            for k in expired_keys:
                _scrape_cache.pop(k, None)

def _cache_stats():
    with _cache_lock:
        return {"size": len(_scrape_cache), "ttl_seconds": CACHE_TTL_SECONDS}

# --- SCRAPER LOGIC START ---

def get_tiktok_custom(url):
    import random
    
    # List of modern User-Agents to rotate
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0'
    ]
    
    def fetch_and_parse(target_url, use_mobile=False):
        if use_mobile:
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        else:
            headers = {
                'User-Agent': random.choice(user_agents),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.tiktok.com/',
                'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Upgrade-Insecure-Requests': '1',
            }

        try:
            session = requests.Session()
            response = session.get(target_url, headers=headers, allow_redirects=True, timeout=10)
            
            if response.status_code != 200:
                return None, response.url

            html = response.text
            data = {
                'platform': 'TikTok', 
                'uploader': 'Unknown', 
                'title': 'TikTok Video', 
                'views': 0, 
                'likes': 0,
                'comments': 0,
                'shares': 0
            }

            # Regex Parsing
            patterns = {
                'views': [r'"playCount":\s*(\d+)', r'"play_count":\s*(\d+)', r'"viewCount":\s*(\d+)'],
                'likes': [r'"diggCount":\s*(\d+)', r'"digg_count":\s*(\d+)'],
                'comments': [r'"commentCount":\s*(\d+)', r'"comment_count":\s*(\d+)'],
                'shares': [r'"shareCount":\s*(\d+)', r'"share_count":\s*(\d+)'],
            }

            for key, pats in patterns.items():
                for p in pats:
                    match = re.search(p, html)
                    if match:
                        data[key] = int(match.group(1))
                        break

            # SIGI_STATE Fallback
            if data['views'] == 0:
                sigi_match = re.search(r'SIGI_STATE\s*=\s*({.+?});', html)
                if sigi_match:
                    try:
                        jdata = json.loads(sigi_match.group(1))
                        if 'ItemModule' in jdata:
                            for vid in jdata['ItemModule']:
                                stats = jdata['ItemModule'][vid].get('stats', {})
                                data['views'] = int(stats.get('playCount', 0))
                                data['likes'] = int(stats.get('diggCount', 0))
                                data['comments'] = int(stats.get('commentCount', 0))
                                data['shares'] = int(stats.get('shareCount', 0))
                                
                                author_id = jdata['ItemModule'][vid].get('author')
                                if author_id and 'UserModule' in jdata and 'users' in jdata['UserModule']:
                                    data['uploader'] = jdata['UserModule']['users'].get(author_id, {}).get('uniqueId', 'Unknown')
                                break
                    except: pass

            return data, response.url

        except Exception:
            return None, target_url

    # Main logic
    result, final_url = fetch_and_parse(url, use_mobile=False)
    if not result or result['views'] == 0:
        result_mobile, _ = fetch_and_parse(final_url, use_mobile=True)
        if result_mobile and result_mobile['views'] > 0:
            return result_mobile
            
    if result and result['views'] > 0:
        return result
        
    return None

# Helper to load cookies from cookies.txt (Netscape format)
def load_cookies():
    cookies = {}
    try:
        # Try finding cookies.txt in current or parent directories
        paths = ['cookies.txt', '../cookies.txt', '../../cookies.txt']
        cookie_file = None
        for p in paths:
            if os.path.exists(p):
                cookie_file = p
                break
        
        if cookie_file:
            with open(cookie_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.startswith('#') and line.strip():
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            # format: domain, flag, path, secure, expiration, name, value
                            cookies[parts[5]] = parts[6].strip()
    except Exception:
        pass
    return cookies

def _extract_shortcode(url):
    """Extract Instagram shortcode from various URL formats."""
    # Handle /reel/CODE/, /p/CODE/, /tv/CODE/
    match = re.search(r'/(reel|p|tv)/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(2)
    return None

def _get_instagram_graphql(shortcode):
    """Fetch Instagram data via GraphQL API (primary method - gets views/play_count)."""
    session = requests.Session()

    # Visit embed page first to get session cookies (bypasses data center IP blocks on Vercel)
    try:
        session.get(f'https://www.instagram.com/p/{shortcode}/embed/captioned/', headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        }, timeout=8)
    except Exception:
        pass

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'X-IG-App-ID': '936619743392459',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': f'https://www.instagram.com/p/{shortcode}/embed/',
    }

    variables = json.dumps({'shortcode': shortcode})
    gql_url = 'https://www.instagram.com/graphql/query/'
    params = {
        'doc_id': '8845758582119845',
        'variables': variables,
    }

    r = session.get(gql_url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    resp_data = r.json()

    media = resp_data.get('data', {}).get('xdt_shortcode_media')
    if not media:
        return None

    data = {
        'platform': 'Instagram',
        'uploader': 'Unknown',
        'title': 'Instagram Video',
        'views': 0,
        'likes': 0,
        'comments': 0,
        'shares': 0
    }

    # Views: prefer video_play_count (actual plays shown on IG), fallback to video_view_count
    data['views'] = media.get('video_play_count') or media.get('video_view_count') or 0

    # Likes
    edge_like = media.get('edge_media_preview_like', {})
    data['likes'] = edge_like.get('count', 0) if isinstance(edge_like, dict) else 0

    # Comments
    edge_comment = media.get('edge_media_to_parent_comment') or media.get('edge_media_preview_comment', {})
    data['comments'] = edge_comment.get('count', 0) if isinstance(edge_comment, dict) else 0

    # Uploader
    owner = media.get('owner', {})
    if isinstance(owner, dict):
        data['uploader'] = owner.get('username', 'Unknown')

    # Title from caption
    edge_caption = media.get('edge_media_to_caption', {})
    if isinstance(edge_caption, dict):
        edges = edge_caption.get('edges', [])
        if edges and isinstance(edges, list):
            caption_text = edges[0].get('node', {}).get('text', '')
            if caption_text:
                data['title'] = caption_text[:100]

    return data

def get_instagram_custom(url):
    data = {
        'platform': 'Instagram',
        'uploader': 'Unknown',
        'title': 'Instagram Video',
        'views': 0,
        'likes': 0,
        'comments': 0,
        'shares': 0
    }

    # Method 1: GraphQL API (best for views)
    shortcode = _extract_shortcode(url)
    if shortcode:
        try:
            gql_data = _get_instagram_graphql(shortcode)
            if gql_data and (gql_data.get('views', 0) > 0 or gql_data.get('likes', 0) > 0):
                return gql_data
        except Exception:
            pass

    # Method 2: Direct HTML scraping fallback (last resort)
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }

        session = requests.Session()
        response = session.get(url, headers=headers, allow_redirects=True, timeout=10)

        html = response.text

        # Try finding JSON data in HTML
        view_patterns = [r'"video_view_count":\s*(\d+)', r'"video_play_count":\s*(\d+)', r'"play_count":\s*(\d+)', r'"view_count":\s*(\d+)']
        for p in view_patterns:
            matches = re.findall(p, html)
            if matches:
                data['views'] = max([int(v) for v in matches])
                break

        # Meta Tags fallback for Likes/Comments
        meta_desc = re.search(r'<meta\s+(?:property="og:description"|name="description")\s+content="([^"]+)"', html)

        if meta_desc:
            desc_text = meta_desc.group(1)

            l_match = re.search(r'([0-9,.]+[KMB]?)\s+likes', desc_text, re.IGNORECASE)
            if l_match:
                data['likes'] = parse_count(l_match.group(1))

            c_match = re.search(r'([0-9,.]+[KMB]?)\s+comments', desc_text, re.IGNORECASE)
            if c_match:
                data['comments'] = parse_count(c_match.group(1))

        if data['likes'] == 0:
            like_patterns = [r'"like_count":\s*(\d+)', r'"edge_media_preview_like":\s*{\s*"count":\s*(\d+)']
            for p in like_patterns:
                matches = re.findall(p, html)
                if matches:
                    data['likes'] = max([int(v) for v in matches])
                    break

        if data['comments'] == 0:
            comment_patterns = [r'"edge_media_to_comment":\s*{\s*"count":\s*(\d+)', r'"comment_count":\s*(\d+)']
            for p in comment_patterns:
                matches = re.findall(p, html)
                if matches:
                    data['comments'] = max([int(v) for v in matches])
                    break

        if data['views'] > 0 or data['likes'] > 0 or data['comments'] > 0:
            return data

    except Exception:
        pass

    return None

# Helper to parse counts with K/M/B suffixes
def parse_count(text_val):
    if not text_val: return 0
    try:
        text = str(text_val).strip().split(' ')[0] # Take first word if "10 likes"
        text = text.replace(',', '')
        multiplier = 1
        if 'K' in text.upper():
            multiplier = 1000
            text = text.upper().replace('K', '')
        elif 'M' in text.upper():
            multiplier = 1000000
            text = text.upper().replace('M', '')
        elif 'B' in text.upper():
            multiplier = 1000000000
            text = text.upper().replace('B', '')
        
        return int(float(text) * multiplier)
    except:
        return 0

def get_youtube_custom(url):
    try:
        # Use headers that look like a real browser
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.youtube.com/',
            'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
        }
        
        # Force desktop watch URL for consistency
        if "/shorts/" in url:
            try:
                vid_id = url.split("/shorts/")[1].split("?")[0]
                url = f"https://www.youtube.com/watch?v={vid_id}"
            except: pass
        
        # Load real cookies from file (Fallback to empty or hardcoded if needed, but here we prioritize file)
        cookies = load_cookies()
        
        session = requests.Session()
        response = session.get(url, headers=headers, cookies=cookies, timeout=5) # Fast timeout
        html = response.text
        
        data = {
            'platform': 'YouTube', 
            'uploader': 'Unknown', 
            'title': 'YouTube Video', 
            'views': 0, 
            'likes': 0,
            'comments': 0,
            'shares': 0
        }

        # 1. Try extracting ytInitialData (most reliable for standard pages)
        try:
             # Use DOTALL (. matches newline) in case of pretty printing
            json_str = None
            patterns = [
                r'var ytInitialData\s*=\s*({.+?});',
                r'window\["ytInitialData"\]\s*=\s*({.+?});',
                r'ytInitialData\s*=\s*({.+?});'
            ]
            
            for p in patterns:
                match = re.search(p, html, re.DOTALL)
                if match:
                    json_str = match.group(1)
                    break
            
            if json_str:
                
                # Views
                v_match = re.search(r'"viewCount":"(\d+)"', json_str)
                if not v_match:
                    v_match = re.search(r'"simpleText":"([0-9,.]+[KMB]?)\s*views"', json_str) # Catch "10K views"
                if v_match:
                    data['views'] = parse_count(v_match.group(1))
                
                # Title
                t_match = re.search(r'"title":\s*{\s*"runs":\s*\[\s*{\s*"text":"(.*?)"', json_str)
                if t_match: data['title'] = t_match.group(1)
                
                # Comments
                c_match = re.search(r'"commentCount":\s*{\s*"simpleText":"([0-9,.]+[KMB]?)"', json_str)
                if c_match:
                    data['comments'] = parse_count(c_match.group(1))
                
                # New UI Comments: Look for "Comments" title followed by the count in "contextualInfo"
                if data['comments'] == 0:
                     c_match_new = re.search(r'"text":"Comments"\s*}\s*]\s*},\s*"contextualInfo":\s*{\s*"runs":\s*\[\s*{\s*"text":"([0-9,.]+[KMB]?)"', json_str)
                     if c_match_new:
                        data['comments'] = parse_count(c_match_new.group(1))
                
                # Likes - Check multiple patterns
                # 1. New UI: "expandedLikeCountIfLiked" (Most reliable from debugging)
                l_match = re.search(r'"expandedLikeCountIfLiked":\s*{\s*"content":"([0-9,.]+[KMB]?)"', json_str)
                if l_match:
                   data['likes'] = parse_count(l_match.group(1))
                
                # 2. New UI: accessibilityText "like this video along with ..."
                if data['likes'] == 0:
                    l_match = re.search(r'"accessibilityText":"like this video along with ([0-9,.]+) other people"', json_str, re.IGNORECASE)
                    if l_match:
                        data['likes'] = parse_count(l_match.group(1))

                # 3. Old UI / Fallback
                if data['likes'] == 0:
                    l_match = re.search(r'"accessibilityData":\s*{\s*"label":"([0-9,.]+[KMB]?)\s*(?:likes|like|suka)"', json_str, re.IGNORECASE)
                    if l_match:
                        data['likes'] = parse_count(l_match.group(1))
                
                # Structure 2: New UI "segmentedLikeDislikeButton"
                if data['likes'] == 0:
                     # Try finding "X likes" text generally in JSON
                     l2_matches = re.findall(r'"label":"([0-9,.]+[KMB]?)\s+likes"', json_str, re.IGNORECASE)
                     if l2_matches:
                         vals = [parse_count(x) for x in l2_matches]
                         if vals: data['likes'] = max(vals)

        except: pass

        # 2. Global HTML Search (Fallback)
        # Search for "X likes" directly in the raw HTML matching aria-labels
        if data['likes'] == 0:
            l_global = re.findall(r'label":"([0-9,.]+[KMB]?)\s+likes"', html, re.IGNORECASE)
            if l_global:
                 vals = [parse_count(x) for x in l_global]
                 if vals: data['likes'] = max(vals)

        if data['comments'] == 0:
            c_global = re.search(r'"commentCount":\s*{\s*"simpleText":"([0-9,.]+[KMB]?)"', html)
            if c_global:
                data['comments'] = parse_count(c_global.group(1))

        # 3. videoDetails Fallback
        if data['views'] == 0:
            match_details = re.search(r'"videoDetails":\s*(\{(?:[^{}]|{|})*\})', html, re.DOTALL)
            if not match_details:
                 match_details = re.search(r'"videoDetails":\s*({.+?})', html, re.DOTALL)
    
            if match_details:
                details_json = match_details.group(1)
                v_match = re.search(r'"viewCount":"(\d+)"', details_json)
                if v_match: data['views'] = int(v_match.group(1))
                
                t_match = re.search(r'"title":"(.*?)"', details_json)
                if t_match: data['title'] = t_match.group(1)
                
                a_match = re.search(r'"author":"(.*?)"', details_json)
                if a_match: data['uploader'] = a_match.group(1)

        # 3. Meta Fallback
        if data['views'] == 0:
            meta_match = re.search(r'itemprop="interactionCount" content="(\d+)"', html)
            if meta_match: data['views'] = int(meta_match.group(1))

        if data['views'] > 0:
            return data
            
    except Exception:
        pass
        
    return None # Do NOT return error, just None to trigger fail-fast

def get_universal_stats(url):
    import yt_dlp  #  Lazy import to reduce cold start time for other platforms
    # Only for fallback on non-YT/IG links if needed, or if specifically requested.
    # But for optimization, we want to AVOID this for YT/IG.
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True, # Prevent crashing on block
        'skip_download': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'nocheckcertificate': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                'platform': info.get('extractor_key', 'Unknown'),
                'title': info.get('title', 'No Title'),
                'views': info.get('view_count', 0),
                'likes': info.get('like_count', 0),
                'comments': info.get('comment_count', 0),
                'shares': info.get('repost_count', 0), 
                'uploader': info.get('uploader', 'Unknown')
            }
    except Exception as e:
        return {'error': str(e)}

# --- SCRAPER LOGIC END ---

from concurrent.futures import ThreadPoolExecutor

# ... (keep existing scraper functions: get_tiktok_custom, get_instagram_custom, etc.) ...

def get_facebook_custom(url):
    """Fetch Facebook reel/video data via /watch/ page + share link resolution."""
    data = {
        'platform': 'Facebook',
        'uploader': 'Unknown',
        'title': 'Facebook Video',
        'views': 0, 'likes': 0, 'comments': 0, 'shares': 0
    }

    headers_mobile = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    headers_desktop = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    # Step 1: Resolve share link (facebook.com/share/v/HASH/) → dapatkan real video ID
    video_id = None
    resolved_url = url

    # Coba ambil video ID langsung dari URL (minimal 10 digit untuk menghindari false match)
    m = re.search(r'(?:reel/|watch/?[?&]v=|videos?/|/video/)(\d{10,})', url)
    if m:
        video_id = m.group(1)
    else:
        # Ini share link — resolve dulu dengan mobile UA untuk dapat og:url
        try:
            session = requests.Session()
            r_share = session.get(url.replace('web.facebook.com', 'www.facebook.com'),
                                   headers=headers_mobile, timeout=12, allow_redirects=True)
            if r_share.status_code == 200:
                # Cari og:url atau reel link di halaman
                og_url = re.search(r'<meta[^>]+property="og:url"[^>]+content="([^"]+)"', r_share.text)
                if not og_url:
                    og_url = re.search(r'content="(https://www\.facebook\.com/reel/\d+[^"]*)"', r_share.text)
                if og_url:
                    resolved_url = og_url.group(1)
                else:
                    # Fallback: cari reel ID langsung
                    reel_m = re.search(r'facebook\.com/reel/(\d+)', r_share.text)
                    if reel_m:
                        video_id = reel_m.group(1)

                vm2 = re.search(r'(?:reel/|watch/?[?&]v=|videos?/|/video/)(\d{10,})', resolved_url)
                if vm2:
                    video_id = vm2.group(1)
        except Exception:
            pass

    if not video_id:
        return None

    # Step 2: /watch/?v=ID → scrape semua data dari JSON embedded + og:title
    try:
        r = requests.get(f'https://www.facebook.com/watch/?v={video_id}',
                         headers=headers_desktop, timeout=12)
        if r.status_code == 200:
            html_text = r.text

            # === Views: ambil play_count di dekat is_play_count_supported ===
            m_play = re.search(r'"play_count":(\d+),"is_play_count_supported"', html_text)
            if m_play:
                data['views'] = int(m_play.group(1))

            # === Comments: total_comment_count ===
            m_cmt = re.search(r'"total_comment_count":(\d+)', html_text)
            if m_cmt:
                data['comments'] = int(m_cmt.group(1))

            # === Likes: total reactions count (ambil dari konteks feedback) ===
            # "count":N di dalam blok feedback reactions
            reaction_ctx = re.search(
                r'"reaction_count":\d+[^}]{0,300}"reaction_count":\d+',
                html_text
            )
            if reaction_ctx:
                # Ambil semua reaction_count lalu sum
                all_rc = re.findall(r'"reaction_count":(\d+)', html_text)
                if all_rc:
                    # Total reaksi = sum dari semua tipe reaksi (Like, Love, Care, Haha, Wow, Sad, Angry)
                    # Ambil max sebagai fallback jika sumnya tidak masuk akal
                    data['likes'] = sum(int(x) for x in all_rc[:7])

            # Fallback likes: dari og:title reactions
            if data['likes'] == 0:
                og = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
                if og:
                    rm = re.search(r'([\d,.]+[KMB]?)\s*reactions?',
                                   html_lib.unescape(og.group(1)), re.IGNORECASE)
                    if rm:
                        data['likes'] = parse_count(rm.group(1))

            # Fallback views: dari og:title
            if data['views'] == 0:
                og = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
                if og:
                    vm = re.search(r'([\d,.]+[KMB]?)\s*views?',
                                   html_lib.unescape(og.group(1)), re.IGNORECASE)
                    if vm:
                        data['views'] = parse_count(vm.group(1))

            # === Uploader & Title dari og:title ===
            og = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
            if og:
                og_title = html_lib.unescape(og.group(1))
                parts = og_title.split('|')
                if len(parts) >= 3:
                    data['uploader'] = parts[-1].strip()
                    data['title'] = parts[-2].strip()
                elif len(parts) >= 2:
                    data['uploader'] = parts[-1].strip()
                    data['title'] = parts[0].strip()

            od = re.search(r'<meta property="og:description" content="([^"]+)"', html_text)
            if od and data['title'] == 'Facebook Video':
                data['title'] = html_lib.unescape(od.group(1))[:150]
    except Exception:
        pass

    # Clean uploader
    if data.get('uploader') and 'Facebook' in data['uploader']:
        data['uploader'] = data['uploader'].replace('Facebook', '').strip().strip('|').strip() or 'Unknown'

    if data['views'] > 0 or data['likes'] > 0 or data['comments'] > 0:
        return data
    return None

def get_threads_custom(url):
    """Fetch Threads post data by scraping embedded JSON + raw HTML from the page."""
    data = {
        'platform': 'Threads',
        'uploader': 'Unknown',
        'title': 'Threads Post',
        'views': 0,
        'likes': 0,
        'comments': 0,
        'shares': 0,
        'saves': 0,
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        session = requests.Session()
        response = session.get(url, headers=headers, allow_redirects=True, timeout=15)
        if response.status_code != 200:
            return None
        html = response.text

        # === METHOD 1: view_counts dari raw HTML ===
        # Threads menyimpan view count di pattern: "view_counts":ANGKA
        # Berlaku untuk semua tipe post (teks, foto, video)
        m_views = re.search(r'"view_counts"\s*:\s*(\d+)', html)
        if m_views:
            data['views'] = int(m_views.group(1))

        # === METHOD 2: Engagement stats dari embedded JSON script ===
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
        target_script = None
        for s in scripts:
            if 'BarcelonaPostPageDirectQuery' in s and 'thread_items' in s:
                target_script = s
                break

        if target_script:
            script_data = json.loads(target_script)

            # Rekursif cari objek yang punya 'thread_items'
            def find_thread_items(obj):
                if isinstance(obj, dict):
                    if 'thread_items' in obj:
                        return obj
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

            node = find_thread_items(script_data)
            if node:
                thread_items = node.get('thread_items', [])
                if thread_items:
                    post = thread_items[0].get('post', {})
                    tpai = post.get('text_post_app_info', {}) or {}
                    user = post.get('user', {}) or {}
                    caption = post.get('caption', {}) or {}

                    data['uploader'] = (
                        user.get('username') or
                        user.get('full_name') or
                        'Unknown'
                    )
                    caption_text = caption.get('text', '')
                    if caption_text:
                        data['title'] = caption_text[:150]

                    data['likes'] = int(post.get('like_count') or 0)
                    data['comments'] = int(tpai.get('direct_reply_count') or 0)
                    # shares = repost (quote) + reshare (repost tanpa komentar)
                    data['shares'] = (
                        int(tpai.get('repost_count') or 0) +
                        int(tpai.get('reshare_count') or 0)
                    )
                    # Fallback views dari play_count jika view_counts tidak ketemu
                    if data['views'] == 0:
                        raw_views = post.get('play_count') or post.get('view_count') or 0
                        data['views'] = int(raw_views)

        if data['likes'] > 0 or data['comments'] > 0 or data['views'] > 0:
            return data

    except Exception:
        pass

    return None


def _get_x_bearer_and_guest():
    """Ambil bearer token dari JS X.com dan aktivasi guest token."""
    try:
        # Ambil bearer token dari main.js X.com
        r_main = requests.get('https://x.com/', headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        }, timeout=10)
        # Cari URL main.js
        js_url_m = re.search(r'src="(https://abs\.twimg\.com/[^"]+main\.[a-f0-9]+\.js)"', r_main.text)
        if not js_url_m:
            return None, None
        r_js = requests.get(js_url_m.group(1), headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        bearer_m = re.search(r'"(AAAAAAAAAAAAAAAAAAAAANR[A-Za-z0-9+/=_%]{50,150})"', r_js.text)
        if not bearer_m:
            return None, None
        from urllib.parse import unquote
        bearer = unquote(bearer_m.group(1))

        # Aktivasi guest token
        g = requests.post('https://api.twitter.com/1.1/guest/activate.json',
            headers={'Authorization': f'Bearer {bearer}', 'User-Agent': 'Mozilla/5.0'}, timeout=10)
        guest_token = g.json().get('guest_token')
        return bearer, guest_token
    except Exception:
        return None, None


def _get_x_graphql_qid():
    """Ambil TweetResultByRestId query ID dari main.js X.com."""
    try:
        r_main = requests.get('https://x.com/', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        js_url_m = re.search(r'src="(https://abs\.twimg\.com/[^"]+main\.[a-f0-9]+\.js)"', r_main.text)
        if not js_url_m:
            return None
        r_js = requests.get(js_url_m.group(1), headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        qid_m = re.search(r'queryId:"([^"]+)",operationName:"TweetResultByRestId"', r_js.text)
        return qid_m.group(1) if qid_m else None
    except Exception:
        return None


def get_x_custom(url):
    """Fetch X (Twitter) post via GraphQL guest API → views, likes, comments, shares, bookmarks."""
    data = {
        'platform': 'X',
        'uploader': 'Unknown',
        'title': 'X Post',
        'views': 0,
        'likes': 0,
        'comments': 0,
        'shares': 0,
        'saves': 0,
    }

    m = re.search(r'/status/(\d+)', url)
    if not m:
        return None
    tweet_id = m.group(1)

    user_m = re.search(r'(?:x|twitter)\.com/([^/?#]+)/status', url)
    username = user_m.group(1) if user_m else 'twitter'

    # === Method 1: X GraphQL Guest API (views + semua metrics) ===
    try:
        bearer, guest_token = _get_x_bearer_and_guest()

        if bearer and guest_token:
            # Cari query ID TweetResultByRestId dari JS
            r_main = requests.get('https://x.com/', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            js_url_m = re.search(r'src="(https://abs\.twimg\.com/[^"]+main\.[a-f0-9]+\.js)"', r_main.text)
            qid = None
            if js_url_m:
                r_js = requests.get(js_url_m.group(1), headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
                qid_m = re.search(r'queryId:"([^"]+)",operationName:"TweetResultByRestId"', r_js.text)
                if qid_m:
                    qid = qid_m.group(1)

            if qid:
                variables = json.dumps({
                    'tweetId': tweet_id,
                    'includePromotedContent': False,
                    'withCommunity': False,
                    'withVoice': False,
                })
                features = json.dumps({
                    'view_counts_everywhere_api_enabled': True,
                    'creator_subscriptions_tweet_preview_api_enabled': True,
                    'longform_notetweets_consumption_enabled': True,
                    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
                    'responsive_web_graphql_exclude_directive_enabled': True,
                    'freedom_of_speech_not_reach_fetch_enabled': True,
                    'standardized_nudges_misinfo': True,
                    'responsive_web_edit_tweet_api_enabled': True,
                    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
                    'rweb_video_timestamps_enabled': True,
                    'longform_notetweets_rich_text_read_enabled': True,
                    'longform_notetweets_inline_media_enabled': True,
                })
                gql_headers = {
                    'Authorization': f'Bearer {bearer}',
                    'X-Guest-Token': guest_token,
                    'x-csrf-token': '0' * 32,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'x-twitter-active-user': 'yes',
                    'x-twitter-client-language': 'en',
                    'Referer': 'https://x.com/',
                    'Origin': 'https://x.com',
                }
                r_gql = requests.get(
                    f'https://x.com/i/api/graphql/{qid}/TweetResultByRestId',
                    params={'variables': variables, 'features': features},
                    headers=gql_headers, timeout=15
                )
                if r_gql.status_code == 200:
                    gql_data = r_gql.json()

                    # Rekursif cari node yang punya 'legacy' dengan 'favorite_count'
                    def find_tweet_node(obj):
                        if isinstance(obj, dict):
                            lg = obj.get('legacy', {})
                            if isinstance(lg, dict) and 'favorite_count' in lg:
                                return obj
                            for v in obj.values():
                                r2 = find_tweet_node(v)
                                if r2: return r2
                        elif isinstance(obj, list):
                            for item in obj:
                                r2 = find_tweet_node(item)
                                if r2: return r2
                        return None

                    node = find_tweet_node(gql_data)
                    if node:
                        legacy = node.get('legacy', {})
                        views = node.get('views', {})

                        data['views']    = int(views.get('count') or 0)
                        data['likes']    = int(legacy.get('favorite_count') or 0)
                        data['comments'] = int(legacy.get('reply_count') or 0)
                        data['shares']   = int(legacy.get('retweet_count') or 0)
                        data['saves']    = int(legacy.get('bookmark_count') or 0)

                        # Full text
                        full_text = legacy.get('full_text', '')
                        clean_text = re.sub(r'https://t\.co/\S+', '', full_text).strip()
                        if clean_text:
                            data['title'] = clean_text[:150]

                        # User dari core.user_results atau author_id fallback
                        core = node.get('core', {})
                        user_res = core.get('user_results', {}).get('result', {})
                        ul = user_res.get('legacy', {})
                        if ul.get('name'):
                            data['uploader'] = ul['name']
                        elif ul.get('screen_name'):
                            data['uploader'] = ul['screen_name']
    except Exception:
        pass

    # === Method 2: vxtwitter (fallback jika GraphQL gagal) ===
    if data['likes'] == 0 and data['views'] == 0:
        try:
            r2 = requests.get(
                f'https://api.vxtwitter.com/{username}/status/{tweet_id}',
                headers={'User-Agent': 'Mozilla/5.0'}, timeout=10
            )
            if r2.status_code == 200:
                tw = r2.json().get('tweet', r2.json())
                data['likes']    = int(tw.get('likes') or 0)
                data['comments'] = int(tw.get('replies') or 0)
                data['shares']   = int(tw.get('retweets') or 0)
                data['uploader'] = tw.get('user_name') or tw.get('user_screen_name') or 'Unknown'
                txt = re.sub(r'https://t\.co/\S+', '', tw.get('text', '')).strip()
                if txt: data['title'] = txt[:150]
        except Exception:
            pass

    # === Method 3: oEmbed sebagai fallback uploader ===
    if data['uploader'] == 'Unknown':
        try:
            r3 = requests.get(
                f'https://publish.twitter.com/oembed?url=https://x.com/{username}/status/{tweet_id}&omit_script=true',
                headers={'User-Agent': 'Mozilla/5.0'}, timeout=8
            )
            if r3.status_code == 200:
                data['uploader'] = r3.json().get('author_name', 'Unknown')
        except Exception:
            pass

    if data['likes'] > 0 or data['views'] > 0 or data['comments'] > 0:
        return data
    return None


def get_capcut_custom(url):
    """Fetch CapCut template data via __MODERN_ROUTER_DATA__ JSON."""
    data = {
        'platform': 'CapCut',
        'uploader': 'Unknown',
        'title': 'CapCut Template',
        'views': 0,
        'likes': 0,
        'comments': 0,
        'shares': 0,
        'saves': 0,
        'exports': 0
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        session = requests.Session()
        response = session.get(url, headers=headers, allow_redirects=True, timeout=15)

        if response.status_code != 200:
            return None

        html = response.text

        # Method 1: Parse __MODERN_ROUTER_DATA__ JSON (most reliable)
        router_match = re.search(
            r'<script type="application/json" id="__MODERN_ROUTER_DATA__">(.*?)</script>',
            html, re.DOTALL
        )
        if router_match:
            try:
                router_data = json.loads(router_match.group(1))
                loader = router_data.get('loaderData', {})
                # Find the template detail loader (key may vary)
                td = None
                for key, val in loader.items():
                    if val and isinstance(val, dict) and 'templateDetail' in val:
                        td = val['templateDetail']
                        break

                if td and isinstance(td, dict):
                    data['views'] = int(td.get('playAmount', 0) or 0)
                    data['exports'] = int(td.get('usageAmount', 0) or 0)
                    data['likes'] = int(td.get('likeAmount', 0) or 0)
                    data['comments'] = int(td.get('commentAmount', 0) or 0)
                    data['title'] = td.get('title', '') or td.get('tagTitle', '') or 'CapCut Template'

                    author = td.get('author', {})
                    if isinstance(author, dict):
                        data['uploader'] = author.get('name', 'Unknown') or 'Unknown'

                    if data['views'] > 0 or data['likes'] > 0 or data['exports'] > 0:
                        return data
            except (json.JSONDecodeError, ValueError):
                pass

        # Method 2: Regex fallback from interactionStatistic / structured data
        like_match = re.search(r'"likeCount"\s*:\s*(\d+)', html)
        if like_match:
            data['likes'] = int(like_match.group(1))

        use_match = re.search(r'"useCount"\s*:\s*(\d+)', html)
        if use_match:
            data['exports'] = int(use_match.group(1))

        # Fallback: parse visible text "20.29K uses, 5.43K likes"
        actions_match = re.search(
            r'([\d,.]+[KMB]?)\s*uses?,\s*([\d,.]+[KMB]?)\s*likes?',
            html, re.IGNORECASE
        )
        if actions_match:
            if data['exports'] == 0:
                data['exports'] = parse_count(actions_match.group(1))
            if data['likes'] == 0:
                data['likes'] = parse_count(actions_match.group(2))

        if data['likes'] > 0 or data['exports'] > 0:
            return data

    except Exception:
        pass

    return None


def process_single_url(url):
    """Refactored helper to process a single URL"""
    url = url.strip()
    if not url: return None

    result = None
    if "capcut.com" in url:
        result = get_capcut_custom(url)
        if not result:
            result = get_universal_stats(url)

    elif "tiktok.com" in url:
        result = get_tiktok_custom(url)
        if not result:
            result = get_universal_stats(url)
    
    elif "instagram.com" in url:
        result = get_instagram_custom(url)
        # DISABLE yt-dlp fallback for Instagram
        if not result:
             return {'error': 'Instagram scan failed (Login required)'}

    elif "threads.com" in url or "threads.net" in url:
        result = get_threads_custom(url)
        if not result:
            return {'error': 'Threads scan failed'}

    elif "facebook.com" in url or "fb.watch" in url:
        result = get_facebook_custom(url)
        if not result:
            result = get_universal_stats(url)

    elif "x.com" in url or "twitter.com" in url:
        result = get_x_custom(url)
        if not result:
            return {'error': 'X scan failed'}

    elif "youtube.com" in url or "youtu.be" in url:
        result = get_youtube_custom(url)
        # DISABLE yt-dlp fallback for YouTube
        if not result:
             return {'error': 'YouTube scan failed (Bot detection)'}
    
    else:
        result = get_universal_stats(url)
        
    return result

# --- FastAPI App ---

app = FastAPI(title="Wefluence Matrix Scraper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class ScrapeRequest(BaseModel):
    url: Optional[str] = None
    urls: Optional[List[str]] = None


@app.get("/")
def health():
    return {"status": "ok", "service": "matrix-scrapper", "cache": _cache_stats()}


@app.post("/api/cache/clear")
def cache_clear():
    """Clear semua cache — dipakai admin saat debug atau force refresh."""
    with _cache_lock:
        before = len(_scrape_cache)
        _scrape_cache.clear()
    return {"cleared": before}


@app.post("/api/scrape")
def scrape(req: ScrapeRequest):
    urls: List[str] = list(req.urls or [])
    single_url = req.url
    if single_url:
        urls.append(single_url)

    if not urls:
        raise HTTPException(status_code=400, detail={"error": "No URLs provided"})

    # Limit batch size to prevent timeout
    if len(urls) > 50:
        urls = urls[:50]

    # Dedupe & normalize sambil pertahankan urutan asli untuk respons
    seen = set()
    deduped_urls = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            deduped_urls.append(u)

    # 1) Cek cache dulu — URL yang sudah ada di cache tidak perlu scrape ulang
    results = {}
    urls_to_scrape = []
    for u in deduped_urls:
        cached = _cache_get(u)
        if cached is not None:
            results[u] = cached
        else:
            urls_to_scrape.append(u)

    # 2) Scrape hanya yang belum di-cache
    if urls_to_scrape:
        try:
            with ThreadPoolExecutor(max_workers=30) as executor:
                future_to_url = {executor.submit(process_single_url, u): u for u in urls_to_scrape}
                for future in future_to_url:
                    u = future_to_url[future]
                    try:
                        data = future.result()
                        if data and 'error' not in data:
                            results[u] = data
                            _cache_set(u, data)  # simpan ke cache
                        else:
                            results[u] = {
                                'error': 'Failed to fetch',
                                'details': data.get('error') if data else 'Unknown',
                            }
                    except Exception as e:
                        results[u] = {'error': str(e)}
        except Exception:
            err = traceback.format_exc()
            raise HTTPException(
                status_code=500,
                detail={"error": "Internal Server Error", "details": str(err)},
            )

    # If single URL request, maintain backward compatibility format
    if single_url and not req.urls and len(urls) == 1:
        return results[single_url]
    return {'results': results}
