# ig-grid-relay

Worker kecil untuk mengambil HTML `instagram.com/{username}/reels/` dari IP tepi
Cloudflare, karena IP VPS ditolak 429 untuk rute profil.

Hipotesis yang diuji: penolakan itu berdasarkan reputasi rentang IP datacenter,
dan IP Cloudflare diperlakukan berbeda. Kalau ternyata sama-sama ditolak,
hasilnya tetap berguna: berarti sisa pilihannya cuma proxy residensial, dan itu
diketahui sebelum keluar biaya.

## Pasang

Tidak perlu membuat Worker lewat dashboard. `wrangler deploy` yang membuatnya,
namanya diambil dari `wrangler.toml`.

```bash
cd matrix-scrapper/cloudflare/ig-grid-relay
npx wrangler login
npx wrangler deploy
npx wrangler secret put RELAY_KEY
```

Urutannya sengaja deploy dulu baru secret. Kalau `secret put` dijalankan
sebelum Worker-nya ada, wrangler berhenti dan bertanya apakah mau membuat
Worker baru. Deploy pertama memang belum punya kunci, jadi semua request
dijawab 403 sampai `secret put` selesai -- itu perilaku yang benar, bukan
kegagalan. Mengisi secret langsung membuat versi baru, tidak perlu deploy ulang.

Hasil deploy memberi alamat seperti
`https://ig-grid-relay.<subdomain>.workers.dev`.

## Uji langsung, sebelum menyentuh scraper

```bash
curl -s -o /dev/null -w "status=%{http_code} ukuran=%{size_download}\n" \
  -H "x-relay-key: KUNCI_YANG_TADI" \
  "https://ig-grid-relay.<subdomain>.workers.dev/?username=ruang_ggelap"
```

Cara membaca:

- `status=200` dengan ukuran **±690 KB**: berhasil, halaman berisi data.
- `status=200` dengan ukuran **±615 KB**: itu shell kosong tanpa angka. IP
  Cloudflare dilayani tapi tidak diberi data. Relay tidak menolong.
- `status=429`: IP Cloudflare ikut ditolak. Relay tidak menolong.

Bedanya halus tapi menentukan, jadi perhatikan ukurannya, bukan cuma statusnya.

## Sambungkan ke scraper

Isi dua env di `.env` VPS, lalu `docker compose up -d matrix-scrapper`:

```
IG_GRID_RELAY=https://ig-grid-relay.<subdomain>.workers.dev
IG_GRID_RELAY_KEY=KUNCI_YANG_TADI
```

Verifikasi:

```bash
docker compose exec matrix-scrapper python api/test_ig_public.py
```

Bagian `[A] grid reels` harus keluar 12 reel dengan play_count.

## Biaya

Free tier Workers 100.000 request per hari. Grid dipanggil sekali per creator
dengan cache 2 menit di sisi Python, bukan sekali per URL klaim, jadi pemakaian
nyatanya jauh di bawah batas itu.
