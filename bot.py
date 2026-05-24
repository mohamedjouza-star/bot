"""
bot.py — Zero RTS Telegram Bot (Render Background Worker)
يعمل مستقلاً بدون Streamlit
"""

import os
import json
import random
import re
import time
import hashlib
import hmac
import unicodedata
import logging
from datetime import datetime

import telebot
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ============================================================
# 1. إعدادات البيئة (Render Environment Variables)
# ============================================================
def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        log.critical(f"❌ متغير البيئة مفقود: {key}  — أضفه في Render → Environment")
        raise SystemExit(1)
    return val

TELEGRAM_TOKEN = _require_env("TELEGRAM_TOKEN")
BOT_USERNAME   = os.environ.get("BOT_USERNAME", "")
HASH_PEPPER    = os.environ.get("HASH_PEPPER", "zero-rts-default-pepper-2025")

# GCP_SERVICE_ACCOUNT: نص JSON كامل مخزّن كـ env var
_gcp_raw = _require_env("GCP_SERVICE_ACCOUNT")
try:
    GCP_CREDS_DICT = json.loads(_gcp_raw)
except json.JSONDecodeError as e:
    log.critical(f"❌ GCP_SERVICE_ACCOUNT ليس JSON صحيحاً: {e}")
    log.critical("تأكد أن القيمة تبدأ بـ { وتنتهي بـ } بدون علامات اقتباس زائدة")
    raise SystemExit(1)

# ============================================================
# 2. الحماية والتشفير
# ============================================================
def hash_code(code: str) -> str:
    return hmac.new(
        HASH_PEPPER.encode(),
        str(code).encode(),
        hashlib.sha256
    ).hexdigest()


# ============================================================
# 3. الوظائف المساعدة
# ============================================================
def remove_accents(text: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', str(text))
        if unicodedata.category(c) != 'Mn'
    ).lower().strip()

def normalize_phone(phone) -> str:
    try:
        if isinstance(phone, float):
            phone = int(phone)
    except Exception:
        pass
    p = str(phone).strip().replace(' ', '').replace('-', '').split('.')[0]
    if p.startswith('+213'): p = '0' + p[4:]
    elif p.startswith('213'): p = '0' + p[3:]
    if len(p) == 9 and p[0] in ['5', '6', '7']:
        p = '0' + p
    return p

def is_valid_algerian_phone(phone: str) -> bool:
    return bool(re.match(r'^0[567]\d{8}$', phone))

def sanitize_store_name(name: str) -> str:
    name = name.strip()
    if name and name[0] in ('=', '+', '-', '@', '|', '%'):
        name = "'" + name
    return name[:80]

def calculate_weight(date_str: str):
    try:
        r_date = datetime.strptime(date_str, '%Y-%m-%d')
        days = (datetime.now() - r_date).days
        if days <= 90:  return 1.0, r_date
        if days <= 365: return 0.5, r_date
        return 0.1, r_date
    except Exception:
        return 0.1, None

def signed_weight(date_str: str, record_type: str):
    w, d = calculate_weight(date_str)
    return (+w, d) if record_type == 'livree' else (-w, d)

# ============================================================
# 4. الاتصال بـ Google Sheets
# ============================================================
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def connect_to_gsheet():
    creds = Credentials.from_service_account_info(GCP_CREDS_DICT, scopes=SCOPE)
    client = gspread.authorize(creds)
    return client.open("Zero_RTS_Database")

try:
    spreadsheet = connect_to_gsheet()
    log.info("✅ Connected to Google Sheets")
except Exception as e:
    log.critical(f"❌ فشل الاتصال بـ Google Sheets: {e}")
    raise SystemExit(1)

# ============================================================
# 5. جلب البيانات (بدون Streamlit cache — cache بسيط في الذاكرة)
# ============================================================
_data_cache = {"bl": [], "rp": [], "ts": 0}
CACHE_TTL   = 300  # 5 دقائق

def fetch_data():
    global _data_cache
    if time.time() - _data_cache["ts"] < CACHE_TTL:
        return _data_cache["bl"], _data_cache["rp"]
    try:
        bl = spreadsheet.get_worksheet(0).get_all_values()
        rp = spreadsheet.worksheet("Reports").get_all_values()
        _data_cache = {"bl": bl, "rp": rp, "ts": time.time()}
        return bl, rp
    except Exception as e:
        log.error(f"fetch_data error: {e}")
        return _data_cache["bl"], _data_cache["rp"]

def invalidate_cache():
    _data_cache["ts"] = 0

# ============================================================
# 6. منطق البحث والتنبؤ
# ============================================================
def search_logic(nq: str, bl: list, rp: list):
    score = 0.0
    stores = set()
    dates = []
    retour_count = 0
    livree_count = 0

    for r in bl:
        if len(r) >= 3 and str(r[0]) == nq:
            rec_type = (r[3] or '').strip() if len(r) >= 4 and (r[3] or '').strip() in ('livree','retour') else 'retour'
            w, d = signed_weight(r[2], rec_type)
            score += w
            if rec_type == 'retour':
                stores.add(r[1]); retour_count += 1
            else:
                livree_count += 1
            if d: dates.append((d, rec_type))

    for r in rp:
        if len(r) >= 4 and str(r[1]) == nq:
            w, d = signed_weight(r[3], 'retour')
            score += w
            stores.add(r[0]); retour_count += 1
            if d: dates.append((d, 'retour'))

    return score, stores, dates, retour_count, livree_count

def predict_behavior(score, dates, stores, retour_count, livree_count):
    now = datetime.now()
    recent_retour = sum(1 for d, t in dates if t == 'retour' and (now - d).days <= 30)
    recent_livree = sum(1 for d, t in dates if t == 'livree' and (now - d).days <= 30)

    if recent_retour >= 2:
        trend = "📈 رجوعات متصاعدة بشكل خطير"
    elif recent_livree >= 2 and score > 0:
        trend = "✅ نشاط إيجابي حديث"
    else:
        trend = "📉 نشاط مستقر أو قديم"

    total_records = retour_count + livree_count
    danger_pct = min(int((retour_count / total_records) * 100), 99) if total_records > 0 else 0

    if score <= -2:   return trend, "🚫 لا ترسل — سجل رجوع سيء جداً",  danger_pct
    if score <= -0.5: return trend, "📞 حذر — يوجد رجوعات سابقة",       danger_pct
    if score >= 1:    return trend, "✅ زبون موثوق — سجل استلام جيد",   danger_pct
    return            trend, "✅ آمن — يمكن الإرسال",                    danger_pct

# ============================================================
# 7. البوت
# ============================================================
bot = telebot.TeleBot(TELEGRAM_TOKEN)
bot.remove_webhook()
time.sleep(0.5)
log.info("✅ Bot initialized")

# ── /start ──────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
def welcome(m):
    args = m.text.split()
    if len(args) > 1 and args[1].startswith('ACT-'):
        activate_store(m, args[1])
    else:
        bot.reply_to(m,
            "🛡️ مرحباً في Zero RTS Algeria\n\n"
            "🔍 للفحص: أرسل رقم الهاتف مباشرة\n"
            "🏪 للتفعيل: أرسل رمز ACT-XXXXX"
        )

# ── رمز التفعيل ──────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text and m.text.strip().upper().startswith('ACT-'))
def handle_activation(m):
    activate_store(m, m.text.strip())

def activate_store(m, user_token):
    try:
        pending_ws = spreadsheet.worksheet("Pending")
        all_rows   = pending_ws.get_all_values()

        for i, row in enumerate(all_rows):
            if len(row) >= 4 and row[0].strip().upper() == user_token.upper():

                try:
                    token_time = float(row[2])
                except (ValueError, TypeError):
                    pending_ws.delete_rows(i + 1)
                    bot.reply_to(m, "❌ رمز تالف، ارفع الملف من جديد.")
                    return

                if time.time() - token_time > 600:
                    pending_ws.delete_rows(i + 1)
                    bot.reply_to(m, "⏰ انتهت صلاحية الرمز (10 دقائق)\nارفع الملف من جديد.")
                    return

                store_name = row[1]

                try:
                    phones_data = json.loads(row[3])
                    if not isinstance(phones_data, list):
                        raise ValueError("format error")
                    if len(phones_data) > 50000:
                        raise ValueError("too large")
                except (json.JSONDecodeError, ValueError):
                    pending_ws.delete_rows(i + 1)
                    bot.reply_to(m, "❌ بيانات تالفة، ارفع الملف من جديد.")
                    return

                raw_code = str(random.randint(111111, 999999))
                hashed   = hash_code(raw_code)

                log_ws     = spreadsheet.worksheet("Log")
                safe_store = sanitize_store_name(store_name)
                log_ws.append_row([
                    safe_store,
                    datetime.now().strftime('%Y-%m-%d'),
                    hashed,
                    str(m.chat.id)
                ])

                data_ws  = spreadsheet.get_worksheet(0)
                existing = set(data_ws.col_values(1))
                today    = datetime.now().strftime('%Y-%m-%d')

                if phones_data and isinstance(phones_data[0], list):
                    to_add = [
                        [p, safe_store, today, t]
                        for p, t in phones_data
                        if isinstance(p, str) and is_valid_algerian_phone(p) and p not in existing
                    ]
                else:
                    to_add = [
                        [p, safe_store, today, 'retour']
                        for p in phones_data
                        if isinstance(p, str) and is_valid_algerian_phone(p) and p not in existing
                    ]

                if to_add:
                    data_ws.append_rows(to_add)

                pending_ws.delete_rows(i + 1)
                invalidate_cache()  # بدل fetch_data.clear()

                livree_added = sum(1 for r in to_add if len(r) >= 4 and r[3] == 'livree')
                retour_added = len(to_add) - livree_added

                safe_md = safe_store.replace('*','').replace('_','').replace('`','')
                bot.send_message(
                    m.chat.id,
                    f"🎉 تم تفعيل متجرك بنجاح!\n\n"
                    f"🏪 المتجر: *{safe_md}*\n"
                    f"✅ مُسلَّمة: {livree_added} رقم\n"
                    f"🔴 مرجوعة: {retour_added} رقم\n\n"
                    f"🔑 كود الدخول:\n`{raw_code}`\n\n"
                    f"⚠️ احتفظ بهذا الكود، لن يُرسل مجدداً.",
                    parse_mode="Markdown"
                )
                log.info(f"✅ Activated store: {safe_store}")
                return

        bot.reply_to(m, "❌ رمز غير صحيح أو تم استخدامه مسبقاً.")

    except Exception as e:
        bot.reply_to(m, "⚠️ حدث خطأ، حاول مجدداً.")
        log.error(f"Activation error: {e}")

# ── فحص رقم هاتف ─────────────────────────────────────────────
@bot.message_handler(func=lambda m: True)
def handle_check(m):
    q = normalize_phone(m.text)
    if is_valid_algerian_phone(q):
        bl, rp = fetch_data()
        s, sts, dts, rc, lc = search_logic(q, bl, rp)
        if not sts and s >= 0:
            bot.reply_to(m, "🟢 الزبون سليم ✅")
        else:
            trend, adv, danger_pct = predict_behavior(s, dts, sts, rc, lc)
            bot.reply_to(m,
                f"⚠️ *تحذير!*\n"
                f"درجة الخطورة: {danger_pct}%\n"
                f"السلوك: {trend}\n"
                f"التوصية: {adv}",
                parse_mode="Markdown"
            )
    else:
        bot.reply_to(m, "❌ أرسل رقم هاتف جزائري صحيح (مثال: 0550123456)")

# ============================================================
# 8. تشغيل البوت
# ============================================================
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Zero RTS Bot OK")
    def log_message(self, *a): pass

def _run_health():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=_run_health, daemon=True).start()
    log.info("🌐 Health server started")
    log.info("🚀 Starting bot polling...")
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                restart_on_change=False,
                allowed_updates=['message', 'callback_query']
            )
        except Exception as e:
            log.error(f"Polling crashed: {e} — restarting in 5s")
            time.sleep(5)
