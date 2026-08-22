# Relay grid Instagram di mesin rumah

Meminjam koneksi residensial untuk satu jenis request saja: HTML halaman
`instagram.com/{username}/reels/`, satu-satunya permukaan publik yang memuat
`play_count`.

Kenapa bukan yang lain:

| Jalan keluar | Hasil uji 22 Agu 2026 |
|---|---|
| Langsung dari VPS Hostinger | 429, 15 dari 15 |
| Cloudflare Worker | 429, 3 dari 3, di dua akun berbeda |
| IP residensial | berisi angka, 20 dari 20 |
| Proxy residensial sewa | belum diuji, berbayar |

Kontraknya sama persis dengan versi Worker, jadi sisi scraper tidak berubah
sama sekali. Yang berpindah cuma isi `IG_GRID_RELAY`.

## Jalankan relay

Dari folder ini, di mesin rumah:

```bash
RELAY_KEY=kunci_rahasia_kamu python relay.py
```

Uji dari mesin yang sama sebelum menyentuh tunnel:

```bash
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" -H "x-relay-key: kunci_rahasia_kamu" "http://127.0.0.1:8787/?username=ruang_ggelap"
```

`200` dengan **±690 KB** berarti benar. `200` dengan ~615 KB berarti halamannya
kosong. Tanpa kunci harus `403`, dan itu memang yang diharapkan.

## Buka ke VPS lewat Cloudflare Tunnel

Relay mendengarkan di `127.0.0.1` saja, jadi ia tidak terekspos ke jaringan
lokal maupun internet sampai tunnel dijalankan. Itu disengaja: yang boleh
menjangkaunya cuma tunnel.

### Cepat, untuk mencoba

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

Keluar alamat acak `https://sesuatu.trycloudflare.com`. Alamat ini **berubah
setiap cloudflared dijalankan ulang**, jadi hanya cocok untuk uji coba, bukan
untuk dipakai terus.

### Tetap, untuk dipakai sehari-hari

```bash
cloudflared tunnel login
cloudflared tunnel create ig-relay
cloudflared tunnel route dns ig-relay ig-relay.wefluence.app
cloudflared tunnel run --url http://127.0.0.1:8787 ig-relay
```

Alamatnya jadi `https://ig-relay.wefluence.app` dan tidak berubah lagi.

## Sambungkan ke scraper

Di `.env` VPS:

```
IG_GRID_RELAY=https://ig-relay.wefluence.app
IG_GRID_RELAY_KEY=kunci_rahasia_kamu
```

Lalu `docker compose up -d matrix-scrapper` dan verifikasi:

```bash
docker compose exec matrix-scrapper python api/test_ig_public.py
```

Bagian `[A] grid reels` harus keluar 12 reel dengan play_count.

## Yang perlu diterima apa adanya

Mesin ini mati atau tidur berarti relay mati. Kalau `IG_PUBLIC_MODE` masih
`fallback`, itu tidak menjatuhkan apa-apa: sessionid tetap jalur utama dan
relay cuma cadangan. Baru berarti kalau mode dinaikkan ke `first` atau `only` —
jangan lakukan itu sebelum relaynya jalan di mesin yang memang menyala terus.

Di Windows, laptop yang ditutup akan tidur meski dicolok listrik. Kalau relay
ini mau diandalkan, matikan sleep-nya:

```bash
powercfg /change standby-timeout-ac 0
```

Kode sengaja tidak mati kalau relay tidak terjangkau: `_grid_request` mencatat
kegagalannya lalu tetap mencoba jalur langsung. Jadi relay yang mati tidak ikut
mematikan yang sudah jalan.
