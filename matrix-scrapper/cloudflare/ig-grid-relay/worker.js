/**
 * Relay satu tujuan: ambil HTML halaman reels sebuah profil Instagram dan
 * teruskan apa adanya.
 *
 * Kenapa ada: dari IP datacenter VPS, rute profil Instagram balas 429 secara
 * konsisten (terukur 3 ronde berjeda 60 detik, plus 60 kombinasi UA/path yang
 * semuanya ditolak), sementara rute post tetap dilayani normal. Halaman profil
 * itu satu-satunya permukaan publik yang memuat play_count, dan play_count
 * adalah angka yang benar-benar dilihat kreator. Worker ini menembakkan request
 * itu dari IP tepi Cloudflare, bukan dari IP VPS.
 *
 * Yang SENGAJA tidak dilakukan:
 * - Tidak menerima URL bebas dari pemanggil. Hanya username, dan formatnya
 *   divalidasi. Relay yang mau meneruskan sembarang URL adalah open proxy, dan
 *   begitu alamatnya bocor ia dipakai orang lain atas nama kita.
 * - Tidak mem-parse HTML di sini. Pengurainya sudah ada di ig_public.py; menaruh
 *   salinan kedua dalam bahasa lain berarti dua tempat yang harus ikut berubah
 *   setiap kali Instagram menggeser bentuk payload-nya.
 */

const UA_CHROME =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const USERNAME_RE = /^[A-Za-z0-9._]{1,30}$/;

export default {
  async fetch(request, env) {
    if (request.method !== 'GET') {
      return json({ error: 'hanya GET' }, 405);
    }

    // Kunci wajib. Tanpa ini alamat worker cukup diketahui untuk dipakai siapa pun.
    if (!env.RELAY_KEY || request.headers.get('x-relay-key') !== env.RELAY_KEY) {
      return json({ error: 'kunci relay salah' }, 403);
    }

    const url = new URL(request.url);
    const username = url.searchParams.get('username') || '';
    if (!USERNAME_RE.test(username)) {
      return json({ error: 'username tidak valid' }, 400);
    }

    const target = `https://www.instagram.com/${username}/reels/`;
    let upstream;
    try {
      upstream = await fetch(target, {
        headers: {
          'User-Agent': UA_CHROME,
          Accept:
            'text/html,application/xhtml+xml,application/xml;q=0.9,' +
            'image/avif,image/webp,*/*;q=0.8',
          'Accept-Language': 'en-US,en;q=0.9',
          'sec-ch-ua': '"Chromium";v="131", "Not_A Brand";v="24"',
          'sec-ch-ua-mobile': '?0',
          'sec-ch-ua-platform': '"Windows"',
          'sec-fetch-dest': 'document',
          'sec-fetch-mode': 'navigate',
          'sec-fetch-site': 'none',
          'sec-fetch-user': '?1',
          'upgrade-insecure-requests': '1',
        },
        redirect: 'follow',
        // Jangan pakai cache tepi: angka tayangan berubah terus, dan sisi Python
        // sudah punya cache sendiri per-username.
        cf: { cacheEverything: false },
      });
    } catch (err) {
      return json({ error: 'gagal menghubungi instagram', detail: String(err) }, 502);
    }

    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: {
        'content-type': 'text/html; charset=utf-8',
        // Status asli ikut dikirim terpisah supaya sisi Python bisa membedakan
        // "relay-nya yang bermasalah" dari "Instagram yang menolak".
        'x-ig-status': String(upstream.status),
        'cache-control': 'no-store',
      },
    });
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });
}
