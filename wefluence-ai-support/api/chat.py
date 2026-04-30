from flask import Flask, request, jsonify
import json
import os
import firebase_admin
from firebase_admin import credentials, firestore
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ==========================================
# CORS Helper
# ==========================================
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    return response

@app.after_request
def after_request(response):
    return add_cors_headers(response)

# ==========================================
# Firebase Init
# ==========================================
db = None

def get_db():
    global db
    if db:
        return db

    try:
        service_account_info = json.loads(os.environ.get('FIREBASE_SERVICE_ACCOUNT', '{}'))

        if not firebase_admin._apps:
            if service_account_info:
                cred = credentials.Certificate(service_account_info)
                firebase_admin.initialize_app(cred)
            else:
                firebase_admin.initialize_app()

        from google.cloud import firestore as google_firestore
        if service_account_info:
            from google.oauth2 import service_account
            gcp_cred = service_account.Credentials.from_service_account_info(service_account_info)
            project_id = service_account_info.get('project_id')
            db = google_firestore.Client(credentials=gcp_cred, project=project_id, database='wefluence-jakarta')
        else:
            db = google_firestore.Client(database='wefluence-jakarta')

        return db
    except Exception as e:
        print(f"Firebase Init Error: {e}")
        return None

# ==========================================
# Constants
# ==========================================
GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

PAYMENT_CONFIG = {
    'minimumWithdrawal': 50000,
    'withdrawalProcessingFee': 6500,
    'supportedBanks': ['BCA', 'BNI', 'BRI', 'MANDIRI', 'PERMATA', 'GOPAY', 'DANA', 'OVO']
}

ESCALATION_KEYWORDS = [
    'chat cs', 'chat dengan cs', 'chat admin', 'bicara admin', 'hubungi cs', 'hubungi admin',
    'mau komplain', 'komplain', 'gak puas', 'tidak puas', 'mau lapor',
    'lapor admin', 'minta tolong admin', 'bantuan manusia', 'human support',
    'customer service', 'cs dong', 'mau ngomong sama admin',
    'kesel banget', 'kesal banget', 'marah banget', 'bete banget',
    'ini gimana sih', 'kok gini sih', 'gak bener nih', 'gak beres',
    'nipu', 'penipu', 'penipuan', 'scam', 'tipu',
    'gak berguna', 'gak guna', 'bodoh', 'bego', 'tolol',
    'refund', 'uang saya', 'balikin uang', 'kembalikan uang',
    'udah berapa kali', 'berkali-kali', 'masalah terus',
    'gak selesai-selesai', 'gak kelar-kelar', 'capek', 'cape',
    'mau berhenti', 'gak mau lagi', 'nyesel'
]

def format_currency(amount):
    return f"Rp {amount:,.0f}".replace(",", ".")

def check_escalation(message_text):
    if not message_text:
        return False
    lower_text = message_text.lower()
    return any(keyword in lower_text for keyword in ESCALATION_KEYWORDS)

def get_user_context(user_id):
    try:
        database = get_db()
        if not database:
            return {'name': 'User', 'role': 'unknown', 'email': 'Unknown', 'balance': 0, 'isVerified': False, 'pendingWithdrawals': []}

        user_data = None
        role = 'unknown'

        user_doc = database.collection('users').document(user_id).get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            role = 'admin'
        else:
            user_doc = database.collection('creators').document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict()
                role = 'creator'
            else:
                user_doc = database.collection('brands').document(user_id).get()
                if user_doc.exists:
                    user_data = user_doc.to_dict()
                    role = 'brand'

        if not user_data:
            return {'name': 'User', 'role': 'unknown', 'balance': 0, 'isVerified': False, 'pendingWithdrawals': []}

        wallet_doc = database.collection('wallets').document(user_id).get()
        balance = wallet_doc.to_dict().get('balance', 0) if wallet_doc.exists else 0

        withdrawals = database.collection('withdrawals').where('userId', '==', user_id).where('status', '==', 'pending').limit(1).stream()
        pending_withdrawals = []
        for w in withdrawals:
            data = w.to_dict()
            pending_withdrawals.append(f"Rp {format_currency(data.get('amount', 0))} ({data.get('bankName', '')})")

        return {
            'name': user_data.get('name') or user_data.get('company') or 'User',
            'role': user_data.get('role', role),
            'email': user_data.get('email', 'Email not found'),
            'isVerified': user_data.get('isVerified', False),
            'balance': balance,
            'pendingWithdrawals': pending_withdrawals
        }

    except Exception as e:
        print(f"Error fetching context: {e}")
        return {'name': 'User', 'role': 'unknown', 'email': 'Unknown', 'balance': 0, 'isVerified': False, 'pendingWithdrawals': []}

# ==========================================
# Prompts & Knowledge Base
# ==========================================

KNOWLEDGE_BASE_CREATOR = """
[FACT 1] SISTEM BAYAR: Pay per performance. Kreator dibayar per 1000 views. Rate ditentukan brand (contoh: Rp 3.000/1k views, Rp 17.000/1k views). Untuk campaign UGC, rate minimal Rp 5.000/1k views.
[FACT 2] CARA UPLOAD: Upload konten ke TikTok / Instagram Reels / YouTube Shorts seperti biasa. Setelah upload, copy LINK kontennya dan submit di halaman 'Submit Konten' di Wefluence.
[FACT 3] PROSES REVIEW: Setelah submit link, admin review dulu (memastikan konten valid), lalu brand review. Total 1-3 hari kerja. Baru bisa klaim views setelah disetujui.
[FACT 4] VIEWS 0: Views TIDAK update otomatis. Harus klaim manual lewat menu 'Konten Saya' -> 'Klaim Views'.
[FACT 5] CARA KLAIM VIEWS: Dashboard -> 'Aksi Cepat' -> 'Klaim Views'. Input jumlah views TERBARU dari sosmed (TikTok/IG/YT). Klaim pertama minimal 500 views. Views bisa DIAKUMULASI dari beberapa video dalam campaign yang sama (contoh: 2 video x 500 views = 1000 views = dibayar). Klaim bisa dilakukan BERULANG.
[FACT 6] WITHDRAW: Minimal Rp 50.000. Biaya tarik 5% (minimal Rp 6.500). Proses 1-3 hari kerja. Tujuan: Bank (BCA, BNI, BRI, Mandiri, Permata) atau E-Wallet (GoPay, DANA, OVO).
[FACT 7] KONTEN DITOLAK: Kemungkinan sebab: kode verifikasi tidak ada di caption, konten tidak sesuai brief, kualitas buruk, atau terdeteksi fake views/bot.
[FACT 8] RULES: DILARANG fake views / suntik views / fake engagement -> BAN PERMANENT. Kode verifikasi (#wefluence WF-XXXX) WAJIB ada di caption. Akun harus PUBLIC.
[FACT 9] FITUR PRO (Rp 49k/bulan): Verified Badge, Prioritas Review (lebih cepat), Prioritas WD (batch pertama).
"""

KNOWLEDGE_BASE_BRAND = """
[FACT 1] SISTEM BAYAR: Pay per performance. Brand tentukan sendiri rate per 1000 views. Untuk UGC, rate minimal Rp 5.000/1k views.
[FACT 2] MINIMAL BUDGET: Budget minimum campaign adalah Rp 5.500.000 (sudah termasuk biaya platform 12%).
[FACT 3] PROSES KONTEN: Kreator upload ke TikTok/IG Reels/YT Shorts, submit link. Admin review dulu 1-3 hari, baru masuk ke review brand.
[FACT 4] REVIEW KONTEN: Buka menu 'Pengajuan' (ikon tas). Swipe kanan = Approve, swipe kiri = Reject.
[FACT 5] BUDGET HABIS: Campaign otomatis tutup / berhenti terima konten baru saat budget habis.
[FACT 6] EXPIRED: Sisa budget otomatis di-refund ke wallet brand kalau campaign melewati deadline.
[FACT 7] TOP UP: Buka menu 'Dompet' -> 'Top Up'. Tersedia Virtual Account (BCA, BNI, BRI, Mandiri, Permata) dan E-Wallet (OVO, DANA, GoPay). Saldo masuk otomatis.
"""

KNOWLEDGE_BASE_DEFAULT = """
[FACT 1] Wefluence adalah platform distribusi konten marketing pay-per-performance.
[FACT 2] Brand buat campaign dan tentukan rate per 1000 views (min Rp 5.000/1k untuk UGC, min budget Rp 5.5jt).
[FACT 3] Kreator upload konten ke TikTok/IG Reels/YT Shorts, submit link, tunggu review admin (1-3 hari).
[FACT 4] Klaim views pertama minimal 500 views. Views bisa DIAKUMULASI lintas video dalam satu campaign. Klaim bisa BERULANG.
"""

def detect_persona(message, history):
    msg = (message or "").lower()
    if any(x in msg for x in ['rekomendasi', 'saran', 'kritik', 'bagusan mana', 'nilai konten']): return 'coach'
    if any(x in msg for x in ['ide', 'bikinin', 'script', 'judul', 'caption', 'trend', 'konten apa']): return 'creative'
    if any(x in msg for x in ['data', 'statistik', 'analisa', 'performa', 'turun', 'naik']): return 'analyst'
    return 'support'  # Default ke support untuk semua hal teknis

# ==========================================
# Routes
# ==========================================
@app.route('/api/chat', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'message': 'Wefluence AI Support API is running'})

@app.route('/api/chat', methods=['OPTIONS'])
def options():
    return jsonify({}), 200

@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json()

        user_id = data.get('userId')
        message_text = data.get('text')

        if not user_id or not message_text:
            return jsonify({'error': 'userId and text are required'}), 400

        context = get_user_context(user_id)
        history = data.get('history') or []
        database = get_db()

        if not history and database:
            try:
                history_ref = database.collection(f'support_chats/{user_id}/messages').order_by('createdAt', direction=firestore.Query.DESCENDING).limit(10)
                docs = list(history_ref.stream())
                docs.reverse()
                for doc in docs:
                    d = doc.to_dict()
                    history.append({'role': 'user' if d.get('sender') == 'user' else 'assistant', 'content': d.get('text', '')})
            except:
                pass

        user_role = context.get('role', 'unknown').lower()

        if user_role == 'creator':
            knowledge_base = KNOWLEDGE_BASE_CREATOR
        elif user_role == 'brand':
            knowledge_base = KNOWLEDGE_BASE_BRAND
        else:
            knowledge_base = KNOWLEDGE_BASE_DEFAULT

        # Tone berdasarkan role
        tone_creator = 'Santai, singkat, pakai bahasa gaul Indonesia (lo/gue boleh). Langsung ke solusi, max 2-3 kalimat.'
        tone_brand   = 'Profesional, ringkas, langsung ke inti. Max 2-3 kalimat.'
        tone_default = 'Ramah, jelas, singkat. Max 2-3 kalimat.'
        tone = tone_creator if user_role == 'creator' else (tone_brand if user_role == 'brand' else tone_default)

        SYSTEM_PROMPT = f"""Kamu adalah Kailouis, AI Customer Support resmi Wefluence.

<user_info>
Nama: {context['name']}
Role: {context['role']}
Verified: {context['isVerified']}
Saldo: {format_currency(context['balance'])}
</user_info>

<knowledge_base>
{knowledge_base}
</knowledge_base>

<strict_rules>
1. JAWAB HANYA berdasarkan fakta di dalam <knowledge_base>. Jangan tambahkan informasi yang tidak ada di sana.
2. Jika pertanyaan di luar knowledge base, jawab: "Untuk itu aku belum punya info pastinya, coba hubungi admin Wefluence langsung ya."
3. JANGAN menyebut angka, fitur, atau alur yang tidak tertulis di knowledge base.
4. Jangan katakan "seperti biasa", "biasanya", atau generalisasi yang tidak berdasar fakta.
5. Jawab SINGKAT. Tone: {tone}
6. JANGAN sapa user dengan nama di setiap kalimat.
</strict_rules>

<pro_upsell_rule>
Hanya sebutkan fitur Pro JIKA user mempertanyakan: lama withdraw, lama review konten, atau kapan saldo cair.
Sebutkan 1 kalimat saja di akhir. Contoh: "Btw, kreator Pro dapat prioritas review & WD lebih cepat — cek menu Profil kalau tertarik."
Jangan sebut Pro di pertanyaan lain.
</pro_upsell_rule>"""

        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": message_text}],
            "temperature": 0.2,
            "max_tokens": 400
        }

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"}
        resp = requests.post(GROQ_API_URL, json=payload, headers=headers)
        ai_reply = resp.json()['choices'][0]['message']['content'] if resp.status_code == 200 else "Maaf, sistem AI sedang sibuk. Coba lagi nanti ya."

        if database:
            try:
                database.collection(f'support_chats/{user_id}/messages').add({
                    'text': ai_reply, 'sender': 'ai', 'createdAt': firestore.SERVER_TIMESTAMP, 'isRead': False
                })
                needs_escalation = check_escalation(message_text)
                database.collection('support_chats').document(user_id).set({
                    'lastMessage': ai_reply, 'lastMessageAt': firestore.SERVER_TIMESTAMP,
                    'unreadCount': firestore.Increment(1), 'status': 'escalated' if needs_escalation else 'active'
                }, merge=True)
            except:
                pass

        return jsonify({'reply': ai_reply, 'status': 'success'})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
