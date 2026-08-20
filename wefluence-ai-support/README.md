# Wefluence AI Support

Asisten bantuan di dalam aplikasi (layar Bantuan → chat). Berjalan sebagai
kontainer `ai-support` di belakang nginx, dijangkau lewat
`https://api.wefluence.app/ai/api/chat`.

Versi 2 adalah tulis ulang. Bagian "Apa yang berubah" di bawah menjelaskan
kenapa, karena beberapa perubahan menyangkut data pengguna dan tidak boleh
dianggap sekadar rapi-rapi.

---

## Apa yang berubah dari versi 1

| Dulu | Sekarang |
|---|---|
| Pengetahuannya basi: biaya platform 12%, budget minimum Rp 5,5 juta, nol fitur 2026 | Pengetahuan ditarik dari angka yang benar, plus penjaga sinkron otomatis |
| Eskalasi menulis `status`, dasbor admin membaca `needsHumanSupport` → tidak pernah ada yang muncul | Dua-duanya ditulis. Ada uji regresinya |
| AI cuma tahu saldo | AI bisa membaca konten, klaim, penarikan, dan kampanye milik penanya |
| `userId` mentah dari body, kunci API publik → bisa membaca akun orang lain | Firebase ID token; mode peralihan tidak mengembalikan balasan lewat HTTP |
| `requests.post` tanpa timeout, tanpa retry, tanpa cadangan | Timeout, retry berjenjang, dan rantai Groq → Groq cadangan → Gemini |
| AI menyela waktu admin sedang menangani chat | AI diam selama admin memegang percakapan |
| Groq mati = "coba lagi nanti" | Groq mati = chat diteruskan ke admin |
| Tiga `except: pass` | Setiap kegagalan tercatat dan mengubah jawaban |
| Tidak ada uji | 37 uji, tanpa jaringan |
| Satu berkas 292 baris | Modul terpisah, tiap berkas punya alasan tertulis |

---

## Peta berkas

```
api/
  app.py            titik masuk HTTP, urutan satu permintaan
  auth.py           identitas pemanggil (token / mode peralihan)
  config.py         SEMUA pembacaan environment ada di sini
  context.py        profil + saldo penanya, dengan cache pendek
  escalation.py     kapan percakapan diteruskan ke manusia
  firestore_db.py   klien Firestore dan Firebase Admin
  knowledge.py      SEMUA angka bisnis dan fakta produk
  llm.py            rantai penyedia model, tool calling, retry
  logging_setup.py  log satu baris JSON
  prompts.py        penyusun prompt sistem
  ratelimit.py      batas per akun
  store.py          baca/tulis koleksi support_chats
  tools.py          alat baca data milik penanya
  chat.py           penerus lama (`api.chat:app`), boleh dihapus nanti
scripts/
  check_kb_sync.py  membandingkan angka di knowledge.py dengan repo aplikasi
tests/
  test_ai_support.py
```

---

## Menyiapkan `.env`

```bash
cd /path/ke/vps/wefluence-ai-support
cp .env.example .env
nano .env
```

Yang wajib: `GROQ_API_KEY` dan `FIREBASE_SERVICE_ACCOUNT`.
Yang sangat disarankan: `GEMINI_API_KEY` (jalur cadangan).

Tiga aturan format yang paling sering bikin celaka, semuanya diam-diam:

- jangan pakai tanda kutip mengelilingi nilai, Docker menganggapnya karakter asli
- `FIREBASE_SERVICE_ACCOUNT` harus satu baris
- `\n` di `private_key` dibiarkan sebagai dua karakter `\` dan `n`

---

## Deploy

```bash
cd /path/ke/vps
git pull
./deploy.sh ai-support nginx
```

`nginx` ikut karena konfigurasinya berubah: header `Authorization` sekarang
diizinkan di preflight CORS. Tanpa itu, browser menolak permintaan chat
sebelum sempat dikirim, dan tidak ada satu pun baris log di aplikasi.

Periksa sesudahnya:

```bash
docker compose exec ai-support curl -s http://127.0.0.1:8000/api/health
```

---

## Mengganti model

Default `GROQ_MODEL` sengaja disamakan dengan yang sudah berjalan. Uji dulu
sebelum mengganti, jangan langsung ke trafik asli:

```bash
docker compose exec ai-support curl -s "http://127.0.0.1:8000/api/health?probe=1"
```

`probe=1` benar-benar memanggil tiap penyedia dan melaporkan berhasil atau
tidak berikut waktunya. Nama model yang salah ketahuan di sini.

Kandidat yang lebih kuat untuk bahasa Indonesia sekaligus tool calling:
`moonshotai/kimi-k2-instruct`, `openai/gpt-oss-120b`.

---

## Menjaga pengetahuannya tetap benar

Ini penyebab utama versi 1 jadi tidak berguna, jadi ada alatnya:

```bash
python scripts/check_kb_sync.py --repo "D:/0000 claude code/wefluence"
```

Membandingkan tiga belas angka di `api/knowledge.py` dengan sumbernya di repo
aplikasi: budget minimum, tarif minimum, tangga biaya platform, biaya dan
minimum penarikan, harga PRO, dan lainnya. Kode keluar 1 kalau ada yang beda.

Jalankan setiap kali harga atau batas di aplikasi berubah, dan sebelum deploy.

### Fakta yang sengaja disembunyikan

Fakta bertanda `released=False` di `knowledge.py` TIDAK dikirim ke model.
Isinya fitur yang kodenya sudah ada di repo tapi belum tayang: kampanye per
video, blokir klaim tiga kali tolak, pindai semua di layar klaim, kampanye
privat, tanya jawab kampanye.

Menjelaskan tombol yang belum ada di layar orang lebih merugikan daripada diam.
Setelah fiturnya benar-benar tayang, setel `KB_INCLUDE_UNRELEASED=1` lalu
deploy ulang.

---

## Keamanan: rencana dua langkah

Sekarang `REQUIRE_AUTH=0`. Artinya klien lama yang cuma mengirim
`{userId, text}` masih dilayani, tapi balasannya **tidak** dikembalikan lewat
HTTP, cuma ditulis ke Firestore. Klien memang membaca pesan lewat listener
Firestore, jadi tidak ada yang rusak, sementara jalur bocornya tertutup.

Setelah patch klien tayang di web **dan** mobile:

```bash
# di .env
REQUIRE_AUTH=1
```

lalu `./deploy.sh ai-support`. Setelah itu permintaan tanpa Firebase ID token
ditolak sepenuhnya.

Jangan menyalakannya sebelum kedua platform tayang. Pengguna aplikasi versi
lama akan kehilangan asistennya.

---

## Uji

```bash
python -m unittest discover -s tests -v
```

Tiga puluh tujuh uji, tanpa Groq, tanpa Gemini, tanpa Firestore. Yang dijaga:
kontrak field dasbor admin, deteksi eskalasi (termasuk yang dulu salah
tangkap), riwayat tidak bisa menyuntikkan peran `system`, alat tidak pernah
menerima uid dari luar, dan klien tanpa token tidak menerima balasan di body.

---

## Membaca log

Semua log satu baris JSON, jadi bisa disaring:

```bash
docker compose logs -f ai-support

# cuma yang gagal
docker compose logs ai-support | grep '"level":"ERROR"'

# penyedia model yang jatuh ke cadangan
docker compose logs ai-support | grep 'llm.provider_failed'

# percakapan yang diteruskan ke admin
docker compose logs ai-support | grep '"escalated": true'
```

UID di log sengaja dipotong (`abc123~`) supaya log bukan daftar identitas.

---

## Gejala dan penyebabnya

| Gejala | Kemungkinan besar |
|---|---|
| Chat jalan di curl, mati di browser | nginx belum di-deploy, `Authorization` belum diizinkan di preflight |
| `/api/health` menyebut `firestore.ok: false` | `FIREBASE_SERVICE_ACCOUNT` rusak. Alasan lengkapnya ada di field `error` |
| AI tidak pernah menjawab, tidak ada error | Cek `store.chat_state`: chat itu mungkin bertanda sedang ditangani admin |
| AI menyebut angka yang salah | Jalankan `check_kb_sync.py` |
| AI bilang tidak tahu untuk pertanyaan data | Peran akun tidak terbaca. Cek `chat.answered` di log, field `role` |
| Semua balasan datang dari Gemini | Groq bermasalah. Cek `llm.provider_failed` |
