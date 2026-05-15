import json
import requests
import re
from urllib.parse import urlparse, parse_qs, quote
import random
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

def get_tiktok_embed_html(video_id):
    """Fetch TikTok Embed V2 HTML using rotating headers"""
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
    try:
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code == 200:
            return resp.text
        return None
    except:
        return None

def get_tiktok_tikwm(url):
    """Fetch TikTok caption via TikWM API (Bypass for restricted/hidden content)"""
    try:
        api_url = f"https://www.tikwm.com/api/?url={quote(url)}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        }
        resp = requests.get(api_url, headers=headers, timeout=6)
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

def get_tiktok_oembed(url):
    """Fetch TikTok caption via oEmbed API (Very reliable for basic metadata)"""
    try:
        # Strip query params for oEmbed to stay clean
        clean_url = url.split('?')[0]
        oembed_url = f"https://www.tiktok.com/oembed?url={quote(clean_url)}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        }
        resp = requests.get(oembed_url, headers=headers, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            # oEmbed 'title' often contains the caption
            return data.get('title', ''), "TikTok:oEmbed", "oEmbed:OK"
        return None, None, f"oEmbed:{resp.status_code}"
    except Exception as e:
        return None, None, f"oEmbedErr:{str(e)[:15]}"

def get_tiktok_caption(url, expected_code=None):
    debug_log = []
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
                response = session.head(url, headers=headers, allow_redirects=True, timeout=5)
                url = response.url
            except:
                debug_log.append("ResolveFail")

        # 2. Main Page Scraping (Primary - Full JSON)
        try:
            response = session.get(url, headers=headers, timeout=8)
            html = response.text

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
        
        # 4. Embed V2 Scraping (Fallback) - Good for getting full caption with hashtags
        video_id = None
        match = re.search(r'/video/(\d+)', url)
        if not match: match = re.search(r'/photo/(\d+)', url)
        
        if match:
            video_id = match.group(1)
            embed_html = get_tiktok_embed_html(video_id)
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
                
        # 4. oEmbed Scraping (Fallback - Often truncated)
        caption, src, oembed_err = get_tiktok_oembed(url)
        if caption:
            cap_lower = caption.lower()
            if not any(p.lower() in cap_lower for p in garbage_phrases):
                return caption, src
            else:
                debug_log.append("oEmbed:Garbage")
        else:
            debug_log.append(oembed_err)
        
        # 5. TikWM API (Aggressive Fallback for Restricted Content)
        # This is a high-success bypass for age-restricted or hidden captions
        caption, src = get_tiktok_tikwm(url)
        if caption:
            cap_lower = caption.lower()
            if not any(p.lower() in cap_lower for p in garbage_phrases):
                return caption, src
            else:
                debug_log.append("TikWM:Garbage")

        return None, f"TikTok:None|Log:{','.join(debug_log)}"
    except Exception as e: 
        print(f"Scraper Error: {str(e)}")
        return None, f"TikTok:Error:{str(e)[:20]}"

def get_youtube_caption(url, expected_code=None):
    # Spoof Googlebot - YouTube usually treats this as a VIP
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    
    debug = {"log": []}
    
    try:
        video_id = None
        if '/shorts/' in url:
            match = re.search(r'/shorts/([a-zA-Z0-9_-]+)', url)
            if match: video_id = match.group(1)
        elif 'youtu.be' in url:
            video_id = urlparse(url).path.strip('/')
        else:
            qs = parse_qs(urlparse(url).query)
            if 'v' in qs: video_id = qs['v'][0]
            
        if not video_id: 
            return None, "No Video ID extracted"
        
        debug['log'].append(f"VI:{video_id}")
        
        # Direct Scraping with Googlebot UA
        try:
            response = requests.get(f"https://www.youtube.com/watch?v={video_id}", headers=headers, timeout=7)
            html = response.text
            debug['len'] = len(html)
            
            # Check for Sign In / Consent indicators
            if "sign in" in html.lower(): debug['log'].append("Sign-in")
            if "consent" in html.lower(): debug['log'].append("Consent")
            
            # 1. Try application/ld+json (Only if it has a real description — YouTube often omits it)
            ld_json_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
            for ld_match in ld_json_matches:
                try:
                    ld_data = json.loads(ld_match)
                    if isinstance(ld_data, list): ld_data = ld_data[0]
                    if isinstance(ld_data, dict):
                        description = ld_data.get('description', '') or ''
                        title = ld_data.get('name', '') or ld_data.get('title', '') or ''
                        # Only trust LD-JSON if description is actually populated.
                        # YouTube frequently returns empty description here, which would
                        # short-circuit the more reliable PlayerJSON path below and miss
                        # unique codes placed in the real description body.
                        if description and not description.endswith('...'):
                            full = f"{title} {description}".strip()
                            if full:
                                return full, "Src:LD-JSON"
                except: continue
            debug['log'].append("LD-JSON-Fail")

            # 2. Extract JSON using a more robust splitting method (Regex can hang on large HTML)
            def extract_json(html, marker):
                try:
                    start_ptr = html.find(marker)
                    if start_ptr == -1: return None
                    start_ptr = html.find('{', start_ptr)
                    if start_ptr == -1: return None
                    
                    # Search for the end of the JSON object
                    # We look for the closing brace followed by a delimiter
                    delimiters = [';', '<', '/script>', '\n', '\r']
                    end_ptr = -1
                    for delim in delimiters:
                        ptr = html.find(delim, start_ptr)
                        if ptr != -1:
                            if end_ptr == -1 or ptr < end_ptr:
                                end_ptr = ptr
                    
                    if end_ptr == -1: end_ptr = len(html)
                    json_str = html[start_ptr:end_ptr].strip()
                    # Final cleanup of trailing non-JSON chars
                    if json_str.endswith(';'): json_str = json_str[:-1]
                    return json.loads(json_str)
                except: return None

            # --- Try ytInitialPlayerResponse ---
            player_data = extract_json(html, 'ytInitialPlayerResponse')
            if player_data:
                try:
                    # Method A: microformat (standard)
                    micro = player_data.get('microformat', {}).get('playerMicroformatRenderer', {})
                    m_desc = micro.get('description', {}).get('simpleText', '')
                    m_title = micro.get('title', {}).get('simpleText', '')
                    
                    # Method B: videoDetails (Shorts & Standard)
                    d = player_data.get('videoDetails', {})
                    title = d.get('title', '')
                    desc = d.get('shortDescription', '')
                    
                    final_title = title or m_title
                    final_desc = desc or m_desc
                    full = f"{final_title} {final_desc}".strip()
                    if full: return full, "Src:PlayerJSON"
                except: pass
            debug['log'].append("No-PlayerJSON")

            # --- Try ytInitialData ---
            initial_data = extract_json(html, 'ytInitialData')
            if initial_data:
                try:
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
                                    if text: return text, "Src:InitialData-Panel"
                except: pass
            debug['log'].append("No-InitialData")

            # 4. Meta Tags (Fallback)
            meta_desc = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html, re.IGNORECASE)
            meta_title = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html, re.IGNORECASE)
            
            desc = meta_desc.group(1) if meta_desc else ""
            title = meta_title.group(1) if meta_title else ""
            
            if not desc:
                meta_desc_name = re.search(r'<meta[^>]*name="description"[^>]*content="([^"]*)"', html, re.IGNORECASE)
                if meta_desc_name: desc = meta_desc_name.group(1)

            full_meta = f"{title} {desc}".strip()
            if full_meta:
                 return full_meta, "Src:MetaTags"
            else:
                 debug['log'].append("Meta-Empty")
                 
            # 5. Exact code fallback directly in HTML
            if expected_code and expected_code.lower() in html.lower():
                code_idx = html.lower().find(expected_code.lower())
                start = max(0, code_idx - 200)
                end = min(len(html), code_idx + len(expected_code) + 200)
                snip = html[start:end]
                snip = re.sub(r'<[^>]+>', ' ', snip)
                return f"...{snip}...", "Src:HTML-Grepped-ExactCode"
        except Exception as e:
            debug['log'].append(f"ReqFailed:{str(e)[:30]}")
            
        return None, f"Fail:{';'.join(debug['log'])}"
    except Exception as e: return None, f"Err:{str(e)}"

def extract_instagram_shortcode(url):
    """Extract shortcode from Instagram URL"""
    match = re.search(r'/(p|reel|reels)/([A-Za-z0-9_-]+)', url)
    if match:
        return match.group(2)
    return None

def get_instagram_caption(url, expected_code=None):
    """Scrape caption from Instagram Reels/Post URL using multiple methods"""
    caption = None
    debug_log = []
    
    try:
        shortcode = extract_instagram_shortcode(url)
        if not shortcode:
            return None, "Invalid URL"
        
        # Method 1: Instagram public oEmbed used to work without auth; as of 2024+
        # api.instagram.com/oembed returns an HTML login wall, not JSON. Skipped.

        # Method 2: Mobile User Agent scraping
        try:
            mobile_headers = {
                'User-Agent': 'Instagram 219.0.0.12.117 Android (26/8.0.0; 480dpi; 1080x1920; samsung; SM-G950F; dreamlte; samsungexynos8895; en_US)',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }
            clean_url = f"https://www.instagram.com/p/{shortcode}/"
            response = requests.get(clean_url, headers=mobile_headers, timeout=6)
            html = response.text
            
            # 2a. Look for description meta tag - Improved Extraction
            desc_match = re.search(r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', html)
            if not desc_match:
                desc_match = re.search(r'<meta[^>]*content="([^"]*)"[^>]*property="og:description"', html)
                
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
                        return res, "Src:MobileMeta-Greedy"

                # Fallback: any quoted segment — take the longest (usually the caption)
                caption_parts = re.findall(r'[:\s]["“](.*?)["”]', raw_desc, re.DOTALL)
                if caption_parts:
                    longest = max(caption_parts, key=len).strip()
                    if longest:
                        return longest, "Src:MobileMeta-Refined"

                # Last resort: return the raw og:description so upstream can compare codes
                if raw_desc.strip():
                    return raw_desc, "Src:MobileMeta-Raw"

            # 2b. Search for exact expected code directly in HTML - VERY RELIABLE FALLBACK
            if expected_code and expected_code.lower() in html.lower():
                code_idx = html.lower().find(expected_code.lower())
                start = max(0, code_idx - 200)
                end = min(len(html), code_idx + len(expected_code) + 200)
                snip = html[start:end]
                snip = re.sub(r'<[^>]+>', ' ', snip)
                return f"...{snip}...", "Src:HTML-Grepped-ExactCode"

            # Legacy WF code fallback (for older WF- format)
            wf_match = re.search(r'WF-[A-Z0-9]{4}-[A-Z0-9]{4}', html)
            if wf_match:
                # Find the surrounding text to provide some context
                start = max(0, wf_match.start() - 200)
                end = min(len(html), wf_match.end() + 200)
                snip = html[start:end]
                # Clean up HTML tags from snippet
                snip = re.sub(r'<[^>]+>', ' ', snip)
                return f"...{snip}...", "Src:HTML-Grepped-Code"

            debug_log.append("Mobile:NoMeta")
        except Exception as e:
            debug_log.append(f"MobileErr:{str(e)[:20]}")

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


class VerifyRequest(BaseModel):
    url: str
    expectedCode: str


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
        }

    normalized_caption = caption.upper()
    normalized_code = code.upper().strip()
    is_valid = normalized_code in normalized_caption

    # If rejected BUT source was risky (truncated OG tags etc) -> ALLOW (Manual Check)
    risky_sources = [
        "TikTok:MetaOG", "TikTok:Embed-HTML", "Mobile:NoMeta",
        "TikTok:oEmbed", "TikTok:Embed-RawDesc",
        "Threads:MetaOG", "Facebook:OGTitle", "Facebook:WatchOG",
        "CapCut:MetaOG",
    ]
    if not is_valid and debug_src in risky_sources:
        return {
            'valid': True,
            'message': 'Verifikasi manual diperlukan (Possibly Truncated).',
            'debug_caption': f"TRUNCATED: {debug_src}",
            'manual_check': True,
        }

    return {
        'valid': is_valid,
        'message': 'Kode ditemukan!' if is_valid else f'Kode {code} tidak ditemukan di caption. Mohon edit caption di platform terkait lalu submit ulang.',
        'debug_caption': f"Src:{debug_src}" if not is_valid else 'HIDDEN',
    }
