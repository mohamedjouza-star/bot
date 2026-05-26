"""
bot.py — Zero RTS Algeria | Telegram Bot
نشر مستقل على Render (Worker Service)
المتغيرات البيئية المطلوبة:
  TELEGRAM_TOKEN
  GCP_SERVICE_ACCOUNT  (JSON كامل كنص)
  HASH_PEPPER          (اختياري)
"""

import os
import json
import re
import io
import random
import hashlib
import hmac
import time
import threading
import unicodedata
from datetime import datetime

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============================================================
# 1. إعداد الاتصال بـ Google Sheets
# ============================================================
def _get_pepper() -> str:
    return os.environ.get("HASH_PEPPER", "zero-rts-default-pepper-2025")

def hash_code(code: str) -> str:
    return hmac.new(_get_pepper().encode(), str(code).encode(), hashlib.sha256).hexdigest()  # type: ignore

def connect_to_gsheet():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    try:
        raw = os.environ.get("GCP_SERVICE_ACCOUNT", "")
        creds_dict = json.loads(raw)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
        return gspread.authorize(creds).open("Zero_RTS_Database")
    except Exception as e:
        print(f"⚠️ خطأ في الاتصال بـ Sheets: {e}")
        return None

spreadsheet = connect_to_gsheet()

# ============================================================
# 2. Cache بسيط (بدون Streamlit)
# ============================================================
_bot_cache = {"data": None, "ts": 0}

def fetch_data(ttl=300):
    if _bot_cache["data"] and time.time() - _bot_cache["ts"] < ttl:
        return _bot_cache["data"]
    if not spreadsheet:
        print("❌ fetch_data: لا يوجد اتصال بـ Sheets")
        return [], []
    try:
        bl_raw = spreadsheet.get_worksheet(0).get_all_values()
        rp_raw = spreadsheet.worksheet("Reports").get_all_values()
        # تخطي صف العناوين (الصف الأول لا يبدأ برقم هاتف)
        bl = bl_raw[1:] if bl_raw and not str(bl_raw[0][0]).startswith('0') else bl_raw
        rp = rp_raw[1:] if rp_raw and not str(rp_raw[0][1]).startswith('0') else rp_raw
        print(f"✅ fetch_data: {len(bl)} سجل، {len(rp)} بلاغ")
        _bot_cache["data"] = (bl, rp)
        _bot_cache["ts"]   = time.time()
        return bl, rp
    except Exception as e:
        print(f"❌ fetch_data error: {e}")
        return [], []

def clear_cache():
    _bot_cache["data"] = None
    _bot_cache["ts"]   = 0

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
        if isinstance(phone, float): phone = int(phone)
    except: pass
    p = str(phone).strip().replace(' ', '').replace('-', '').split('.')[0]
    if p.startswith('+213'): p = '0' + p[4:]
    elif p.startswith('213'): p = '0' + p[3:]
    if len(p) == 9 and p[0] in ['5', '6', '7']: p = '0' + p
    return p

def is_valid_algerian_phone(phone: str) -> bool:
    return bool(re.match(r'^0[567]\d{8}$', phone))

def calculate_weight(date_str: str):
    try:
        r_date = datetime.strptime(date_str, '%Y-%m-%d')
        days = (datetime.now() - r_date).days
        if days <= 90:  return 1.0, r_date
        if days <= 365: return 0.5, r_date
        return 0.1, r_date
    except:
        return 0.1, None

def signed_weight(date_str: str, record_type: str):
    w, d = calculate_weight(date_str)
    return (+w, d) if record_type == 'livree' else (-w, d)

# ============================================================
# 4. كشف نوع ملف الشحن
# ============================================================
RETURN_KEYWORDS = ['retour', 'retourne', 'annul', 'refus', 'echec', 'echoue']
LIVREE_KEYWORDS = ['livre', 'livr', 'recouvr', 'recouvert', 'encaisse']

def get_situation_type(val: str) -> str:
    v = remove_accents(str(val))
    if any(kw in v for kw in RETURN_KEYWORDS): return 'retour'
    if any(kw in v for kw in LIVREE_KEYWORDS): return 'livree'
    return 'pending'

def verify_shipping_file(df):
    if df.empty or len(df) < 2:
        return False, None, None, None

    col_pairs = [(remove_accents(str(c)), str(c)) for c in df.columns]

    def contains_any(kws):
        return next((orig for clean, orig in col_pairs
                     if any(kw in clean for kw in kws)), None)

    def count_matches(kws):
        return sum(1 for kw in kws if any(kw in c for c, _ in col_pairs))

    def find_situation():
        for c, orig in col_pairs:
            if c.strip() == 'situation': return orig
        for c, orig in col_pairs:
            if any(kw in c for kw in ['situation', 'statut', 'etat', 'status']): return orig
        for col in df.columns:
            for val in df[col].dropna().head(30).astype(str):
                if any(kw in remove_accents(val) for kw in ['livree', 'retour', 'recouvert']):
                    return col
        return None

    y = contains_any(['telephone', 'tel', 'phone'])
    if y and count_matches(['tracking', 'wilaya', 'commune', 'produit', 'prix']) >= 2:
        return True, "Yalidine Express", y, find_situation()

    z = contains_any(['mobile1', 'mobile', 'tel1', 'phone1'])
    if z and count_matches(['traking', 'tracking', 'situation', 'wilaya', 'produit', 'frais']) >= 2:
        return True, "ZR Express", z, find_situation()

    return False, None, None, None

def extract_phones(df, phone_col: str, situation_col) -> tuple:
    total = len(df[phone_col].dropna())
    has_sit = situation_col and situation_col in df.columns
    if not has_sit:
        df = df.copy()
        df['_sit'] = 'Retour Client'
        situation_col = '_sit'

    rows = df[[phone_col, situation_col]].dropna(subset=[phone_col])
    phones_with_type = {}
    stats = {'livree': 0, 'retour': 0, 'pending': 0}

    for _, row in rows.iterrows():
        phone    = normalize_phone(row[phone_col])
        sit_type = get_situation_type(str(row[situation_col]))
        stats[sit_type] += 1
        if not is_valid_algerian_phone(phone) or sit_type == 'pending':
            continue
        if phone not in phones_with_type:
            phones_with_type[phone] = sit_type
        elif sit_type == 'retour':
            phones_with_type[phone] = 'retour'

    return list(phones_with_type.items()), total, stats

def read_excel_bytes(raw_bytes, filename: str):
    buf = io.BytesIO(raw_bytes)
    if filename.lower().endswith(".xls"):
        return pd.read_excel(buf, engine="xlrd")
    return pd.read_excel(buf, engine="openpyxl")

# ============================================================
# 5. منطق البحث والتنبؤ
# ============================================================
def search_logic(nq: str, bl: list, rp: list):
    score, stores, dates = 0.0, set(), []
    retour_count = livree_count = 0

    for r in bl:
        if len(r) >= 3 and str(r[0]) == nq:
            rec_type = (r[3] or '').strip() if len(r) >= 4 and (r[3] or '').strip() in ('livree', 'retour') else 'retour'
            w, d = signed_weight(r[2], rec_type)
            score += w
            if rec_type == 'retour': stores.add(r[1]); retour_count += 1
            else: livree_count += 1
            if d: dates.append((d, rec_type))

    for r in rp:
        if len(r) >= 4 and str(r[1]) == nq:
            w, d = signed_weight(r[3], 'retour')
            score += w; stores.add(r[0]); retour_count += 1
            if d: dates.append((d, 'retour'))

    return score, stores, dates, retour_count, livree_count

def predict_behavior(score, dates, stores, retour_count, livree_count):
    now = datetime.now()
    rc = sum(1 for d, t in dates if t == 'retour' and (now - d).days <= 30)
    lc = sum(1 for d, t in dates if t == 'livree' and (now - d).days <= 30)

    if rc >= 2:   trend = "📈 رجوعات متصاعدة بشكل خطير"
    elif lc >= 2: trend = "✅ نشاط إيجابي حديث"
    else:         trend = "📉 نشاط مستقر أو قديم"

    total = retour_count + livree_count
    danger_pct = min(int((retour_count / total) * 100), 99) if total > 0 else 0

    if score <= -2:   return trend, "🚫 لا ترسل — سجل رجوع سيء جداً",  "error",   danger_pct
    if score <= -0.5: return trend, "📞 حذر — يوجد رجوعات سابقة",       "warning", danger_pct
    if score >= 1:    return trend, "✅ زبون موثوق — سجل استلام جيد",    "success", danger_pct
    return            trend, "✅ آمن — يمكن الإرسال",                    "success", danger_pct

# ============================================================
# 6. تفعيل المتجر في Sheets
# ============================================================
_lock = threading.Lock()

def complete_activation(store_name: str, phones_data: list, chat_id: int):
    raw_code = str(random.randint(111111, 999999))
    today    = datetime.now().strftime('%Y-%m-%d')

    spreadsheet.worksheet("Log").append_row([
        store_name, today, hash_code(raw_code), str(chat_id)
    ])

    data_ws  = spreadsheet.get_worksheet(0)
    existing = set(data_ws.col_values(1))

    to_add = [
        [p, store_name, today, t]
        for p, t in phones_data
        if is_valid_algerian_phone(p) and p not in existing
    ]
    if to_add:
        data_ws.append_rows(to_add)

    clear_cache()
    return raw_code, len(to_add)

# ============================================================
# 7. حالة المستخدمين في الذاكرة
# ============================================================
_user_states = {}

def _get_state(chat_id):  return _user_states.get(str(chat_id), {})
def _set_state(chat_id, state, data=None):
    _user_states[str(chat_id)] = {"state": state, "data": data or {}}
def _clear_state(chat_id): _user_states.pop(str(chat_id), None)

# ============================================================
# 8. دالة الفحص المشتركة
# ============================================================
def _do_check(bot, m, phone_raw: str):
    q = normalize_phone(phone_raw)
    if not is_valid_algerian_phone(q):
        bot.reply_to(m, "❌ رقم غير صحيح — مثال: 0550123456")
        return

    bl, rp = fetch_data()
    if not bl and not rp:
        bot.reply_to(m, "⚠️ تعذّر الاتصال بقاعدة البيانات — حاول لاحقاً")
        return
    s, sts, dts, rc, lc = search_logic(q, bl, rp)

    total_records = rc + lc
    if total_records == 0 and not sts:
        bot.reply_to(m, "🟢 *الزبون سليم* — لا توجد سجلات في القاعدة", parse_mode="Markdown")
    elif s >= 0 and not sts:
        bot.reply_to(m,
            f"🟢 *الزبون سليم* ✅\n\n"
            f"✅ استلامات: {lc}\n"
            f"🔴 رجوعات: {rc}",
            parse_mode="Markdown"
        )
    else:
        tr, adv, _, pct = predict_behavior(s, dts, sts, rc, lc)
        bar = "🔴" * (pct // 20) + "⚪" * (5 - pct // 20)
        bot.reply_to(m,
            f"⚠️ *تحذير!*\n\n"
            f"الخطورة: {bar} *{pct}%*\n"
            f"الرجوعات: {rc} | الاستلامات: {lc}\n"
            f"المصادر: {len(sts)} متجر\n"
            f"السلوك: {tr}\n\n"
            f"التوصية: {adv}",
            parse_mode="Markdown"
        )

# ============================================================
# 9. تشغيل البوت
# ============================================================
def main():
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        print("❌ TELEGRAM_TOKEN غير موجود في المتغيرات البيئية")
        return

    bot = telebot.TeleBot(token)
    # إيقاف أي webhook أو polling قديم قبل البدء
    try:
        bot.remove_webhook()
        print("✅ Webhook removed")
    except Exception as e:
        print(f"⚠️ remove_webhook: {e}")
    time.sleep(3)  # انتظر حتى تنتهي أي جلسة قديمة

    # ── /start ────────────────────────────────────────────────
    @bot.message_handler(commands=['start'])
    def cmd_start(m):
        _clear_state(m.chat.id)
        bot.send_message(m.chat.id,
            "🛡️ *Zero RTS Algeria*\n\n"
            "حماية متجرك من رفض الاستلام\n\n"
            "🔍 /check — فحص رقم زبون\n"
            "📢 /report — تبليغ عن زبون\n"
            "🏪 /activate — تفعيل متجر جديد\n"
            "📂 /newfile — رفع ملف جديد\n"
            "📊 /stats — إحصائياتي\n"
            "❓ /help — مساعدة",
            parse_mode="Markdown"
        )

    # ── /help ─────────────────────────────────────────────────
    @bot.message_handler(commands=['help'])
    def cmd_help(m):
        bot.send_message(m.chat.id,
            "❓ *دليل الاستخدام*\n\n"
            "*فحص رقم:*\n"
            "أرسل الرقم مباشرة أو `/check 0550123456`\n\n"
            "*تبليغ:*\n"
            "`/report` ثم اتبع الخطوات\n\n"
            "*تفعيل متجر جديد:*\n"
            "`/activate` ثم أرسل ملف Excel\n\n"
            "*رفع ملف جديد:*\n"
            "`/newfile` ثم أرسل الملف\n\n"
            "*إحصائيات:*\n"
            "`/stats`",
            parse_mode="Markdown"
        )

    # ── /activate ─────────────────────────────────────────────
    @bot.message_handler(commands=['activate'])
    def cmd_activate(m):
        logs = spreadsheet.worksheet("Log").get_all_records()
        already = next((l for l in logs
                        if str(l.get('telegram_chat_id')) == str(m.chat.id)), None)
        if already:
            bot.reply_to(m,
                f"✅ متجرك *{already['store_name']}* مفعّل بالفعل!\n\n"
                "لرفع ملف جديد استخدم /newfile",
                parse_mode="Markdown"
            )
            return
        _set_state(m.chat.id, "await_store_name")
        bot.reply_to(m, "🏪 أدخل اسم متجرك:")

    # ── /newfile ──────────────────────────────────────────────
    @bot.message_handler(commands=['newfile'])
    def cmd_newfile(m):
        logs = spreadsheet.worksheet("Log").get_all_records()
        store = next((l for l in logs
                      if str(l.get('telegram_chat_id')) == str(m.chat.id)), None)
        if not store:
            bot.reply_to(m, "❌ متجرك غير مفعل\nاستخدم /activate أولاً")
            return
        _set_state(m.chat.id, "await_new_file", {"store_name": store["store_name"]})
        bot.reply_to(m,
            f"📂 أرسل ملف Excel الجديد من ZR أو Yalidine\n"
            f"المتجر: *{store['store_name']}*",
            parse_mode="Markdown"
        )

    # ── /check ────────────────────────────────────────────────
    @bot.message_handler(commands=['check'])
    def cmd_check(m):
        parts = m.text.split()
        if len(parts) >= 2:
            _do_check(bot, m, parts[1])
        else:
            bot.reply_to(m, "📱 أرسل الرقم مباشرة أو:\n`/check 0550123456`",
                         parse_mode="Markdown")

    # ── /report ───────────────────────────────────────────────
    @bot.message_handler(commands=['report'])
    def cmd_report(m):
        logs = spreadsheet.worksheet("Log").get_all_records()
        store = next((l for l in logs
                      if str(l.get('telegram_chat_id')) == str(m.chat.id)), None)
        if not store:
            bot.reply_to(m, "❌ متجرك غير مفعل\nاستخدم /activate أولاً")
            return
        _set_state(m.chat.id, "report_phone", {"store_name": store["store_name"]})
        bot.reply_to(m, "📱 أرسل رقم الهاتف للتبليغ عنه:")

    # ── /stats ────────────────────────────────────────────────
    @bot.message_handler(commands=['stats'])
    def cmd_stats(m):
        logs = spreadsheet.worksheet("Log").get_all_records()
        store = next((l for l in logs
                      if str(l.get('telegram_chat_id')) == str(m.chat.id)), None)
        if not store:
            bot.reply_to(m, "❌ متجرك غير مفعل")
            return

        store_name = store["store_name"]
        today      = datetime.now().strftime('%Y-%m-%d')
        bl, rp     = fetch_data()

        my_phones = sum(1 for r in bl if len(r) >= 2 and r[1] == store_name)
        today_rp  = sum(1 for r in rp if len(r) > 3 and r[0] == store_name and r[3] == today)

        bot.reply_to(m,
            f"📊 *إحصائيات {store_name}*\n\n"
            f"📱 أرقامك في القاعدة: *{my_phones}*\n"
            f"📢 بلاغاتك اليوم: *{today_rp}/10*",
            parse_mode="Markdown"
        )

    # ── رسائل نصية ───────────────────────────────────────────
    @bot.message_handler(content_types=['text'])
    def handle_text(m):
        st_data = _get_state(m.chat.id)
        state   = st_data.get("state")
        data    = st_data.get("data", {})

        if state == "await_store_name":
            name = m.text.strip()
            if len(name) < 2:
                bot.reply_to(m, "❌ الاسم قصير جداً — أعد المحاولة:")
                return
            _set_state(m.chat.id, "await_file", {"store_name": name})
            bot.reply_to(m,
                f"✅ الاسم: *{name}*\n\n"
                "📂 أرسل ملف Excel من ZR أو Yalidine (.xlsx أو .xls):",
                parse_mode="Markdown"
            )

        elif state == "report_phone":
            phone = normalize_phone(m.text)
            if not is_valid_algerian_phone(phone):
                bot.reply_to(m, "❌ رقم غير صحيح — أعد الإرسال:")
                return
            _set_state(m.chat.id, "report_reason",
                       {"store_name": data["store_name"], "phone": phone})
            markup = InlineKeyboardMarkup()
            markup.row(
                InlineKeyboardButton("🚫 رفض الاستلام", callback_data="reason_رفض الاستلام"),
                InlineKeyboardButton("📵 لا يرد",        callback_data="reason_لا يرد")
            )
            markup.row(
                InlineKeyboardButton("👻 رقم وهمي",      callback_data="reason_رقم وهمي"),
                InlineKeyboardButton("📍 عنوان وهمي",    callback_data="reason_عنوان وهمي")
            )
            bot.reply_to(m, f"📋 السبب للرقم `{phone}`:",
                         reply_markup=markup, parse_mode="Markdown")

        else:
            q = normalize_phone(m.text)
            if is_valid_algerian_phone(q):
                _do_check(bot, m, q)
            else:
                bot.reply_to(m,
                    "❓ لم أفهم طلبك\n"
                    "أرسل رقم هاتف مباشرة أو اكتب /help"
                )

    # ── ملف Excel ────────────────────────────────────────────
    @bot.message_handler(content_types=['document'])
    def handle_document(m):
        st_data = _get_state(m.chat.id)
        state   = st_data.get("state")
        data    = st_data.get("data", {})

        if state not in ("await_file", "await_new_file"):
            bot.reply_to(m, "❓ لم أكن أنتظر ملفاً\nاستخدم /activate أو /newfile أولاً")
            return

        fname = m.document.file_name or ""
        if not fname.lower().endswith((".xlsx", ".xls")):
            bot.reply_to(m, "❌ أرسل ملف Excel (.xlsx أو .xls) فقط")
            return

        if m.document.file_size > 10 * 1024 * 1024:
            bot.reply_to(m, "❌ حجم الملف كبير جداً (الحد 10MB)")
            return

        bot.reply_to(m, "⏳ جاري معالجة الملف...")

        try:
            raw = bot.download_file(bot.get_file(m.document.file_id).file_path)
            df  = read_excel_bytes(raw, fname)
        except Exception as e:
            bot.reply_to(m, f"❌ خطأ في قراءة الملف: {e}")
            return

        ok, company, phone_col, sit_col = verify_shipping_file(df)
        if not ok:
            bot.reply_to(m, "❌ الملف غير مدعوم\nتأكد أنه ملف أصلي من ZR أو Yalidine")
            return

        phones_with_type, total, stats = extract_phones(df, phone_col, sit_col)
        if not phones_with_type:
            bot.reply_to(m, "❌ لا توجد أرقام صحيحة في الملف")
            return

        store_name = data.get("store_name", "")

        with _lock:
            if state == "await_file":
                raw_code, count = complete_activation(store_name, phones_with_type, m.chat.id)
                _clear_state(m.chat.id)
                livree_c = sum(1 for _, t in phones_with_type if t == 'livree')
                retour_c = sum(1 for _, t in phones_with_type if t == 'retour')
                bot.send_message(m.chat.id,
                    f"🎉 *تم تفعيل متجرك بنجاح!*\n\n"
                    f"🏪 المتجر: *{store_name}*\n"
                    f"📦 الشركة: {company}\n"
                    f"✅ مُسلَّمة: {livree_c} رقم\n"
                    f"🔴 مرجوعة: {retour_c} رقم\n\n"
                    f"🔑 كود الدخول للموقع:\n`{raw_code}`\n\n"
                    f"⚠️ احتفظ به — لن يُرسل مجدداً",
                    parse_mode="Markdown"
                )

            elif state == "await_new_file":
                data_ws  = spreadsheet.get_worksheet(0)
                existing = set(data_ws.col_values(1))
                today    = datetime.now().strftime('%Y-%m-%d')
                to_add   = [
                    [p, store_name, today, t]
                    for p, t in phones_with_type
                    if is_valid_algerian_phone(p) and p not in existing
                ]
                if to_add:
                    data_ws.append_rows(to_add)
                clear_cache()
                _clear_state(m.chat.id)
                bot.reply_to(m,
                    f"✅ تم تحديث الأرقام!\n"
                    f"📦 {company} — أُضيف *{len(to_add)}* رقم جديد",
                    parse_mode="Markdown"
                )

    # ── أزرار Inline (سبب التبليغ) ───────────────────────────
    @bot.callback_query_handler(func=lambda c: c.data.startswith("reason_"))
    def handle_reason(c):
        st_data = _get_state(c.message.chat.id)
        if st_data.get("state") != "report_reason":
            bot.answer_callback_query(c.id, "انتهت الجلسة — ابدأ من /report")
            return

        data       = st_data.get("data", {})
        reason     = c.data.replace("reason_", "")
        store_name = data.get("store_name")
        phone      = data.get("phone")
        today      = datetime.now().strftime('%Y-%m-%d')

        all_rp = spreadsheet.worksheet("Reports").get_all_values()
        daily  = sum(1 for r in all_rp if len(r) > 3 and r[0] == store_name and r[3] == today)

        if daily >= 10:
            bot.answer_callback_query(c.id, "⚠️ استنفدت حصتك اليومية")
            bot.edit_message_text("⚠️ 10 بلاغات — استنفدت حصتك اليومية",
                                  c.message.chat.id, c.message.message_id)
            _clear_state(c.message.chat.id)
            return

        already = any(r[0] == store_name and r[1] == phone and r[3] == today
                      for r in all_rp if len(r) > 3)
        if already:
            bot.answer_callback_query(c.id, "بلّغت عن هذا الرقم اليوم مسبقاً")
            bot.edit_message_text("❌ بلّغت عن هذا الرقم اليوم",
                                  c.message.chat.id, c.message.message_id)
            _clear_state(c.message.chat.id)
            return

        spreadsheet.worksheet("Reports").append_row([store_name, phone, reason, today])
        clear_cache()
        _clear_state(c.message.chat.id)

        bot.answer_callback_query(c.id, "✅ تم التبليغ")
        bot.edit_message_text(
            f"✅ *تم التبليغ بنجاح*\n\n"
            f"📱 الرقم: `{phone}`\n"
            f"📋 السبب: {reason}\n"
            f"📊 بلاغاتك اليوم: {daily + 1}/10",
            c.message.chat.id, c.message.message_id,
            parse_mode="Markdown"
        )

    # ── Polling مع إعادة تشغيل تلقائي ───────────────────────
    print("🤖 Zero RTS Bot يعمل الآن...")
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                allowed_updates=['message', 'callback_query'],
                restart_on_change=False,
                skip_pending=True        # تجاهل الرسائل القديمة عند الإعادة
            )
        except Exception as e:
            err = str(e)
            if "409" in err:
                print("⚠️ خطأ 409: هناك نسخة أخرى تعمل — انتظار 60 ثانية...")
                time.sleep(60)           # انتظر طويل حتى تنتهي النسخة الأخرى
            else:
                print(f"⚠️ Bot crashed: {e} — إعادة تشغيل خلال 10 ثوانٍ")
                time.sleep(10)

if __name__ == "__main__":
    main()
