# Caption Scraper API

Microservice untuk verifikasi kode unik di caption TikTok/YouTube Shorts.

## Deploy ke Vercel

1. Login ke [Vercel](https://vercel.com)
2. Import repo GitHub
3. Set **Root Directory** ke `caption-scraper`
4. Deploy!

## Endpoint

### POST `/api/verify`

**Request:**
```json
{
  "url": "https://tiktok.com/@user/video/123456",
  "expectedCode": "WF-ABC123"
}
```

**Response (sukses):**
```json
{
  "valid": true,
  "platform": "tiktok",
  "message": "Kode verifikasi ditemukan!"
}
```

**Response (gagal):**
```json
{
  "valid": false,
  "platform": "tiktok",
  "reason": "code_not_found",
  "message": "Kode verifikasi tidak ditemukan di caption."
}
```

## Platform Support

| Platform | Verification |
|----------|--------------|
| TikTok | ✅ Scrape caption |
| YouTube Shorts | ✅ Scrape title + description |
| Instagram | ⏭️ Skip (langsung valid) |

## Local Development

```bash
cd caption-scraper
pip install -r requirements.txt
vercel dev
```
