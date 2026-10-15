"""
bot.py — بوت عروض مدينتي (Madinty Offers) — MVP قابل للتشغيل فعلياً

يطبّق المرحلة A بالكامل + أساسيات المرحلة B (أكواد الخصم):
  - تسجيل المستخدم (مدينة + اهتمامات)
  - تسجيل التاجر وإنشاء حملة بلغة طبيعية
  - معالجة الحملة عبر AI (استخراج بيانات + توليد نص إعلان)
  - مراجعة/اعتماد الحملات من قبل الإدارة (بدون نشر تلقائي)
  - استهداف ونشر على دفعات (queue) لتفادي حدود Telegram
  - أكواد خصم + تتبع الاستخدام + تقرير حملة مبسّط

التشغيل:
    python bot.py
(بعد ضبط متغيرات البيئة في .env — راجع README.md)
"""

import logging
import math
import os
import re
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().with_name(".env"), override=False)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import ai
import campaign_broadcast
import db
import marketplace
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("madinty-bot")


def _callback_message_reference(message, chat_id=None):
    message_id = getattr(message, "message_id", None)
    if chat_id is None:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None) or getattr(message, "chat_id", None)
    if type(chat_id) is int and type(message_id) is int:
        return {"chat_id": chat_id, "message_id": message_id}
    return None


async def _safe_answer(query, message=None, show_alert=False):
    """A Telegram callback acknowledgement timeout must not cancel its action."""
    try:
        if message is None and not show_alert:
            await query.answer()
        else:
            await query.answer(message, show_alert=show_alert)
    except TimedOut:
        log.warning("انتهت مهلة تأكيد ضغط زر في Telegram؛ سيستمر تنفيذ الإجراء")


async def _ensure_db_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("db_user_id"):
        return context.user_data["db_user_id"]
    telegram_user = update.effective_user
    if not telegram_user:
        return None
    user = await db.create_user(
        telegram_user.id, telegram_user.username, telegram_user.first_name
    )
    context.user_data["db_user_id"] = user["id"]
    return user["id"]


async def _claim_side_effecting_update(update: Update, action: str) -> bool:
    update_id = getattr(update, "update_id", None)
    if type(update_id) is not int:
        return True
    return await db.claim_telegram_update(update_id, action)


def _main_reply_keyboard(extra_rows=None):
    rows = list(extra_rows or []) + [
        ["🔎 أبحث عن عروض", "🛒 سوق الأفراد"],
        ["🏪 صاحب نشاط تجاري", "📋 إعلاناتي"],
        ["📊 إحصاءات الإعلانات", "🔄 تجديد الإعلان"],
        ["↩️ رجوع", "🏠 القائمة الرئيسية"],
        ["❌ إلغاء"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _has_market_resume(context):
    return any(context.user_data.get(key) for key in (
        "market_draft", "market_city_id", "resubmitting_listing_id"
    ))


def _user_reply_keyboard(context):
    rows = []
    if context.user_data.get("pending_campaign") and context.user_data.get("campaign_preview_id"):
        rows.append(["📄 متابعة معاينة الحملة"])
    if _has_market_resume(context):
        rows.append(["📄 متابعة مسودة الإعلان"])
    return _main_reply_keyboard(rows or None)


ROOT_KEYBOARD_TEXTS = (
    "🔎 أبحث عن عروض", "🛒 سوق الأفراد", "🏪 صاحب نشاط تجاري", "📋 إعلاناتي",
    "📊 إحصاءات الإعلانات", "🔄 تجديد الإعلان", "#إحصاء#", "#تجديد#",
    "↩️ رجوع", "🏠 القائمة الرئيسية", "❌ إلغاء",
    "📄 متابعة مسودة الإعلان",
    "➕ إنشاء حملة", "📊 حالة حملاتي", "🎟️ تحقق من كود خصم", "🏪 حالة النشاط",
    "📄 متابعة معاينة الحملة",
    "ℹ️ كيف يعمل؟", "📞 تواصل معنا",
)


def _root_keyboard_filter():
    labels = "|".join(re.escape(label) for label in ROOT_KEYBOARD_TEXTS)
    return filters.Regex(rf"^(?:{labels})$")


async def _invalidate_user_keyboard(context, key: str):
    reference = context.user_data.pop(key, None)
    if not reference or not getattr(context, "bot", None):
        return
    try:
        await context.bot.edit_message_reply_markup(**reference, reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("تعذر حذف أزرار المعاينة القديمة: %s", exc)


async def _clear_campaign_preview(context):
    await _invalidate_user_keyboard(context, "campaign_preview_message")
    context.user_data.pop("campaign_preview_id", None)


async def _reject_stale_campaign_callback(query):
    await _safe_answer(query, "انتهت صلاحية هذا الزر؛ استخدم أزرار أحدث معاينة.", show_alert=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("تعذر إزالة أزرار الحملة القديمة: %s", exc)

ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        log.warning("قيمة %s غير صالحة؛ استخدام %s", name, default)
        return default


def _nonnegative_float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        if not math.isfinite(value):
            raise ValueError
        return max(0.0, value)
    except ValueError:
        log.warning("قيمة %s غير صالحة؛ استخدام %s", name, default)
        return default


BATCH_SIZE = _positive_int_env("BROADCAST_BATCH_SIZE", 50)
BATCH_DELAY_SECONDS = _nonnegative_float_env("BROADCAST_BATCH_DELAY", 1.5)

# ---------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------
(
    SELECT_ROLE,
    CUST_CITY,
    CUST_CATEGORIES,
    MER_BIZ_NAME,
    MER_BIZ_TYPE,
    MER_BIZ_CITY,
    MER_BIZ_PHONE,
    MER_MENU,
    CAMPAIGN_DESC,
    CAMPAIGN_CONFIRM,
) = range(10)
REDEEM_CODE = 10

BIZ_TYPES = ["مطعم / كافيه", "متجر", "مركز تجميل", "خدمات"]
CATEGORY_LABELS_AR = {
    "FOOD": "مطاعم وكافيهات",
    "SHOPPING": "تسوق",
    "BEAUTY": "صحة وجمال",
    "SERVICES": "خدمات",
}
CITY_LABELS_AR = {
    "KHARTOUM": "الخرطوم",
    "OMDURMAN": "أم درمان",
    "BAHRI": "بحري",
    "UNKNOWN": "غير محددة",
}


def _campaign_preview_text(pending):
    data = pending["ai_data"]
    category = pending.get("category_name") or CATEGORY_LABELS_AR.get(
        str(data.get("category_code") or "").upper(), "فئة غير محددة"
    )
    city = pending.get("city_name") or CITY_LABELS_AR.get(
        str(data.get("city_code") or "").upper(), "مدينة غير محددة"
    )
    start_value, end_value = db.normalize_campaign_date_values(
        data.get("start"), data.get("end")
    )
    start_date, end_date = db.validate_campaign_dates(start_value, end_value)
    start = start_date.isoformat() if start_date else None
    end = end_date.isoformat() if end_date else None
    if start and end:
        period = f"{start} إلى {end} (شاملًا)"
    elif start:
        period = f"من {start} فصاعدًا"
    elif end:
        period = f"حتى {end} شاملًا"
    else:
        period = "دون تاريخ انتهاء محدد"
    return (
        f"📢 معاينة الإعلان:\n\n{pending['ad_text']}\n\n"
        f"الفئة: {category} | المدينة: {city} | الخصم: {data.get('discount')}%\n"
        f"فترة الصلاحية (UTC): {period}"
    )


MERCHANT_LAUNCH_PRICING_NOTICE = (
    "📣 تسجيل النشاط وإنشاء الحملات وإرسالها للمراجعة مجاني حاليًا خلال فترة الإطلاق المحدودة. "
    "ستُعلن أي باقات أو رسوم مستقبلية مسبقًا."
)


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ADMIN_IDS


# =================================================================
# /start — نقطة الدخول
# =================================================================
def _main_inline_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE | None = None):
    keyboard = [
        [InlineKeyboardButton("🔎 أبحث عن عروض", callback_data="role_customer")],
        [InlineKeyboardButton("🛒 سوق الأفراد", callback_data="customer_market")],
        [InlineKeyboardButton("🏪 صاحب نشاط تجاري", callback_data="role_merchant")],
        [InlineKeyboardButton("ℹ️ كيف يعمل؟", callback_data="how_it_works")],
        [InlineKeyboardButton("📞 تواصل معنا", callback_data="contact_us")],
    ]
    if context and context.user_data.get("pending_campaign") and context.user_data.get("campaign_preview_id"):
        keyboard.insert(0, [InlineKeyboardButton("📄 متابعة معاينة الحملة", callback_data="resume_campaign")])
    if context and _has_market_resume(context):
        keyboard.insert(0, [InlineKeyboardButton("📄 متابعة مسودة الإعلان", callback_data="resume_listing_flow")])
    if is_admin(update):
        keyboard.append([InlineKeyboardButton("🛠 لوحة الإدارة", callback_data="admin_menu")])
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = await db.create_user(tg_user.id, tg_user.username, tg_user.first_name)
    context.user_data["db_user_id"] = user["id"]
    context.user_data["navigation_section"] = "main"
    context.user_data.pop("navigation_parent", None)
    await update.message.reply_text(
        "أهلاً بك في *عروض مدينتي* 👋\n\nاختر نوع حسابك:",
        reply_markup=_main_inline_keyboard(update, context),
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text(
        "يمكنك أيضًا استخدام القائمة العربية أسفل الشاشة للتنقل بسهولة.",
        reply_markup=_user_reply_keyboard(context),
    )
    return SELECT_ROLE


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, notice=None):
    await _ensure_db_user(update, context)
    context.user_data["navigation_section"] = "main"
    context.user_data.pop("navigation_parent", None)
    text = notice or "🏠 القائمة الرئيسية\n\nاختر الإجراء المطلوب:"
    query = update.callback_query
    if query:
        await _safe_answer(query)
        await query.edit_message_text(text, reply_markup=_main_inline_keyboard(update, context))
        await query.message.reply_text(
            "استخدم الأزرار أسفل الشاشة للتنقل.", reply_markup=_user_reply_keyboard(context)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=_user_reply_keyboard(context)
        )
    return SELECT_ROLE


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_main_menu(update, context)


async def text_role_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج اختيارات القائمة النصية بدل إجبار المستخدم على كتابة أوامر."""
    text = (update.message.text or "").strip()
    await _ensure_db_user(update, context)
    if text == "🏠 القائمة الرئيسية":
        return await show_main_menu(update, context)
    if text == "↩️ رجوع":
        parent = context.user_data.get("navigation_parent")
        if parent == "market_menu":
            return await marketplace.market_menu(update, context)
        if parent == "merchant_menu":
            return await show_merchant_menu(update, context)
        if parent == "listings":
            await cmd_my_listings(update, context)
            return SELECT_ROLE
        return await show_main_menu(update, context, "↩️ عدنا إلى القائمة الرئيسية.")
    if text == "❌ إلغاء":
        return await cancel(update, context)
    if text == "📄 متابعة معاينة الحملة":
        return await resume_campaign_preview(update, context)
    if text == "📄 متابعة مسودة الإعلان":
        return await marketplace.resume_listing_flow(update, context)
    if text == "📋 إعلاناتي":
        await cmd_my_listings(update, context)
        return SELECT_ROLE
    if text in {"#إحصاء#", "📊 إحصاءات الإعلانات"}:
        await cmd_listing_stats(update, context)
        return SELECT_ROLE
    if text in {"#تجديد#", "🔄 تجديد الإعلان"}:
        await cmd_renew_listing(update, context)
        return SELECT_ROLE
    if text in {"➕ إنشاء حملة", "📊 حالة حملاتي", "🎟️ تحقق من كود خصم", "🏪 حالة النشاط"}:
        return await merchant_text_menu_router(update, context)
    if text == "🔎 أبحث عن عروض":
        await db.set_account_type(context.user_data["db_user_id"], "customer")
        cities = await db.list_cities()
        keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"city_{c['id']}")] for c in cities]
        keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
        await update.message.reply_text("اختر مدينتك:", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["navigation_section"] = "customer"
        context.user_data["navigation_parent"] = "main"
        return CUST_CITY
    if text == "🏪 صاحب نشاط تجاري":
        await db.set_account_type(context.user_data["db_user_id"], "merchant")
        existing = await db.get_business_by_user(context.user_data["db_user_id"])
        if existing:
            context.user_data["business_id"] = existing["id"]
            return await show_merchant_menu(update, context)
        await update.message.reply_text("لنسجّل نشاطك التجاري.\n\nما اسم النشاط؟")
        return MER_BIZ_NAME
    if text == "ℹ️ كيف يعمل؟":
        await update.message.reply_text("📌 اختر مدينتك واهتماماتك، وسنرسل لك عروضًا مناسبة من تجار مدينتك.")
        return SELECT_ROLE
    if text == "📞 تواصل معنا":
        await update.message.reply_text("للتواصل: @madinty_support")
        return SELECT_ROLE
    if text == "🛒 سوق الأفراد":
        return await marketplace.market_menu(update, context)
    return SELECT_ROLE


async def role_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "main_menu":
        return await show_main_menu(update, context)

    await _safe_answer(query)
    await _ensure_db_user(update, context)

    if query.data == "admin_menu":
        if not is_admin(update):
            await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
            return SELECT_ROLE
        await show_admin_menu(query)
        return SELECT_ROLE

    if query.data == "how_it_works":
        await query.edit_message_text(
            "📌 تختار مدينتك واهتماماتك، ونرسل لك عروضاً من تجار موثوقين في مدينتك فقط.\n"
            "التجار يمكنهم إنشاء حملة إعلانية تُراجعها الإدارة قبل النشر لضمان الجودة."
        )
        return SELECT_ROLE

    if query.data == "contact_us":
        await query.edit_message_text("للتواصل: @madinty_support")
        return SELECT_ROLE

    if query.data == "role_customer":
        await db.set_account_type(context.user_data["db_user_id"], "customer")
        cities = await db.list_cities()
        keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"city_{c['id']}")] for c in cities]
        keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
        await query.edit_message_text("اختر مدينتك:", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["navigation_section"] = "customer"
        context.user_data["navigation_parent"] = "main"
        return CUST_CITY

    if query.data == "role_merchant":
        await db.set_account_type(context.user_data["db_user_id"], "merchant")
        existing = await db.get_business_by_user(context.user_data["db_user_id"])
        if existing:
            # حفظ رقم النشاط ضروري عند إنشاء حملة في جلسة لاحقة.
            context.user_data["business_id"] = existing["id"]
            return await show_merchant_menu(query, context)
        await query.edit_message_text("لنسجّل نشاطك التجاري.\n\nما اسم النشاط؟")
        return MER_BIZ_NAME

    return SELECT_ROLE


async def show_admin_menu(query):
    await query.edit_message_text(
        "🛠 لوحة الإدارة\n\nاختر الإجراء المطلوب:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 حملات بانتظار المراجعة", callback_data="admin_pending")],
            [InlineKeyboardButton("🏪 نشاطات بانتظار المراجعة", callback_data="admin_businesses")],
            [InlineKeyboardButton("📦 إعلانات أفراد بانتظار المراجعة", callback_data="admin_listings")],
            [InlineKeyboardButton("📊 تقارير الوصول والتواصل", callback_data="admin_reports")],
            [InlineKeyboardButton("🔄 تحديث القائمة", callback_data="admin_menu")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
        ]),
    )


async def admin_reports_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    listings = await db.pool().fetch(
        """SELECT l.id, l.title,
           COUNT(*) FILTER (WHERE e.event_type='VIEW') AS views,
           COUNT(*) FILTER (WHERE e.event_type='CONTACT') AS contacts
           FROM listings l LEFT JOIN listing_events e ON e.listing_id=l.id
           WHERE l.status='approved' GROUP BY l.id ORDER BY l.id DESC LIMIT 15"""
    )
    businesses = await db.pool().fetch(
        """SELECT b.id, b.business_name,
           COUNT(*) FILTER (WHERE e.event_type='VIEW') AS views,
           COUNT(*) FILTER (WHERE e.event_type='CONTACT') AS contacts
           FROM businesses b LEFT JOIN business_events e ON e.business_id=b.id
           WHERE b.status IN ('pending','approved') GROUP BY b.id ORDER BY b.id DESC LIMIT 15"""
    )
    lines = ["📊 تقارير الوصول والتواصل\n"]
    lines.append("📦 إعلانات الأفراد:")
    lines.extend(f"#{r['id']} {r['title']} — مشاهدة: {r['views']} | تواصل: {r['contacts']}" for r in listings)
    lines.append("\n🏪 الأنشطة التجارية:")
    lines.extend(f"#{r['id']} {r['business_name']} — مشاهدة: {r['views']} | تواصل: {r['contacts']}" for r in businesses)
    await query.edit_message_text("\n".join(lines)[:4000])


async def admin_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    rows = await db.list_pending_campaigns()
    if not rows:
        await query.edit_message_text(
            "✅ لا توجد حملات بانتظار المراجعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_menu")]]),
        )
        return
    for row in rows:
        await query.message.reply_text(
            f"🆕 الحملة #{row['id']}\nالنشاط: {row['business_name']}\n\n{row['description']}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ اعتماد", callback_data=f"admin_approve_{row['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{row['id']}"),
            ]]),
        )
    await query.edit_message_text(
        "اختر اعتماد أو رفض كل حملة من الرسائل أعلاه.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_menu")]]),
    )


async def admin_pending_businesses_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    rows = await db.list_pending_businesses()
    if not rows:
        await query.edit_message_text(
            "✅ لا توجد نشاطات بانتظار المراجعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_menu")]]),
        )
        return
    await query.edit_message_text("🏪 نشاطات تجارية بانتظار المراجعة:")
    for row in rows:
        await query.message.reply_text(
            f"🏪 النشاط #{row['id']} — {row['business_name']}\n"
            f"النوع: {row['business_type'] or 'غير محدد'}\n"
            f"المدينة: {row['city_name'] or 'غير محددة'}\n"
            f"التواصل: {row['phone'] or 'غير مسجل'}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ اعتماد النشاط", callback_data=f"admin_business_approve_{row['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"admin_business_reject_{row['id']}"),
            ]]),
        )
    await query.message.reply_text(
        "إدارة النشاطات:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة الإدارة", callback_data="admin_menu")]]),
    )


async def admin_business_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    try:
        _, _, action, business_id_text = query.data.split("_")
        business_id = int(business_id_text)
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة النشاط التجاري.")
        return
    status = "approved" if action == "approve" else "rejected" if action == "reject" else None
    if status is None:
        await query.edit_message_text("إجراء المراجعة غير معروف.")
        return
    business = await db.set_business_status(business_id, status)
    if not business:
        await query.edit_message_text("هذا النشاط لم يعد بانتظار المراجعة.")
        return
    await query.edit_message_text(
        f"{'✅ تم اعتماد' if status == 'approved' else '❌ تم رفض'} النشاط #{business_id}."
    )
    try:
        business = await db.get_business(business_id)
        if business:
            notice = (
                "✅ تمت الموافقة على نشاطك التجاري. يمكنك الآن إنشاء حملات لإرسالها للمراجعة."
                if status == "approved" else "❌ لم تتم الموافقة على تسجيل نشاطك التجاري. تواصل مع الدعم للاستفسار."
            )
            await context.bot.send_message(business["owner_telegram_id"], notice)
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر إشعار صاحب النشاط %s: %s", business_id, exc)


async def admin_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    rows = await db.list_pending_listings()
    if not rows:
        await query.edit_message_text("✅ لا توجد إعلانات أفراد بانتظار المراجعة.")
        return
    await query.edit_message_text("📦 إعلانات الأفراد بانتظار المراجعة:")
    for row in rows:
        price = str(row["price"]) if row["price"] is not None else "عند التواصل"
        for photo_id in (row["image_file_ids"] or [])[:2]:
            try:
                await context.bot.send_photo(update.effective_user.id, photo_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("تعذر إرسال صورة الإعلان %s للمراجعة: %s", row["id"], exc)
        await query.message.reply_text(
            f"📦 الإعلان #{row['id']}\n{row['title']}\n"
            f"السعر: {price}\nالتفاوض: {'مسموح' if row['negotiable'] else 'غير مسموح'}\n"
            f"المدينة: {row['city_name'] or 'غير محددة'}\n"
            f"العنوان: {row['address'] or 'غير محدد'}\n"
            f"التواصل: {row['contact'] or 'غير محدد'}\nالتوصيل: {row['delivery'] or 'غير محدد'}\n\n"
            f"{row['description'] or ''}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ اعتماد", callback_data=f"listing_approve_{row['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"listing_reject_{row['id']}"),
            ]]),
        )


async def admin_listing_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    try:
        _, action, listing_id_text = query.data.split("_")
        listing_id = int(listing_id_text)
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الإعلان.")
        return
    listing = await db.get_listing(listing_id)
    if not listing or listing["status"] != "pending_review":
        await query.edit_message_text("هذا الإعلان لم يعد بانتظار المراجعة.")
        return
    status = "approved" if action == "approve" else "rejected"
    listing = await db.set_listing_status(listing_id, status)
    if not listing:
        await query.edit_message_text("هذا الإعلان لم يعد بانتظار المراجعة.")
        return
    try:
        owner = await db.get_user_by_id(listing["user_id"])
        if owner:
            stats = await db.listing_report(listing_id)
            if status == "approved":
                notice = (
                    f"✅ تم قبول إعلانك ونشره بنجاح.\n\n"
                    f"📦 الإعلان: {listing['title']}\n🔢 الرقم: #{listing_id}\n"
                    f"⏱ مدة الإعلان: 72 ساعة\n📅 تاريخ الانتهاء: {listing['expires_at']}\n\n"
                    f"📊 المشاهدات: {stats['views']}\n📩 مرات التواصل: {stats['contacts']}\n\n"
                    "استخدم الأزرار أدناه لعرض الإحصاءات أو تجديد الإعلان."
                )
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 الإحصاءات", callback_data=f"mylisting_stats_{listing_id}"),
                    InlineKeyboardButton("🔄 تجديد الإعلان", callback_data=f"mylisting_renew_{listing_id}"),
                ], [InlineKeyboardButton("📋 إعلاناتي", callback_data="mylistings")]])
            else:
                notice = (
                    f"❌ لم تتم الموافقة على إعلانك رقم #{listing_id}.\n"
                    "يمكنك تعديل بياناته وصورته ثم إعادة إرساله من الزر أدناه."
                )
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ تعديل الإعلان", callback_data=f"mylisting_edit_{listing_id}"),
                    InlineKeyboardButton("📋 إعلاناتي", callback_data="mylistings"),
                ]])
            await context.bot.send_message(
                owner["telegram_id"],
                notice,
                reply_markup=reply_markup,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر إشعار صاحب الإعلان %s: %s", listing_id, exc)
    await query.edit_message_text(
        f"{'✅ تم اعتماد' if status == 'approved' else '❌ تم رفض'} الإعلان #{listing_id}."
    )


async def notify_merchant_campaign_decision(context, campaign, status: str):
    """Notify the campaign owner after a review transition, without blocking admin feedback."""
    campaign_id = campaign["id"]
    try:
        business = await db.get_business(campaign["business_id"])
        if not business:
            return
        if status == "approved":
            notice = (
                f"✅ تمت الموافقة على حملتك #{campaign_id}: {campaign['title']}\n"
                "أصبحت جاهزة للنشر من الإدارة؛ لم يبدأ بثها للمستخدمين بعد."
            )
        else:
            notice = (
                f"❌ لم تتم الموافقة على حملتك #{campaign_id}: {campaign['title']}.\n"
                "للاستفسار أو مراجعة المحتوى، تواصل مع الدعم @madinty_support."
            )
        await context.bot.send_message(business["owner_telegram_id"], notice)
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر إشعار صاحب الحملة %s: %s", campaign_id, exc)


async def admin_campaign_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    try:
        _, action, campaign_id_text = query.data.split("_")
        campaign_id = int(campaign_id_text)
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الحملة.")
        return
    campaign = await db.review_campaign(campaign_id, action)
    if not campaign:
        await query.edit_message_text("هذه الحملة لم تعد بانتظار المراجعة.")
        return
    await notify_merchant_campaign_decision(
        context, campaign, "approved" if action == "approve" else "rejected"
    )
    if action == "approve":
        await query.edit_message_text(
            f"✅ تم اعتماد الحملة #{campaign_id}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 نشر الحملة", callback_data=f"admin_send_{campaign_id}")]]),
        )
    else:
        await query.edit_message_text(f"❌ تم رفض الحملة #{campaign_id}.")


async def admin_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query, "جارٍ التحقق من الحملة...")
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    try:
        campaign_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الحملة.")
        return
    result = await campaign_broadcast.broadcast_campaign(
        context.bot, campaign_id, BATCH_SIZE, BATCH_DELAY_SECONDS
    )
    await query.edit_message_text(_broadcast_result_text(campaign_id, result))


def _broadcast_result_text(campaign_id: int, result: dict) -> str:
    if result["status"] == "not_ready":
        return (
            "لم يبدأ الإرسال: الحملة غير معتمدة/غير صالحة أو ما زال عامل بث حديث نشطًا. "
            f"للحملات المتوقفة استخدم /resume {campaign_id} بعد انتهاء مهلة الاستعادة."
        )
    if result["status"] == "no_targets":
        return "لم يبدأ الإرسال لعدم وجود مستخدمين نشطين يطابقون المدينة والفئة. يمكنك إعادة المحاولة لاحقًا بعد انضمام مستلمين مناسبين."
    if result["status"] == "completed":
        text = f"✅ انتهت محاولة نشر الحملة #{campaign_id}. وصل الإرسال إلى {result['sent']} من {result['total']} مستخدم."
        if result["failed"]:
            text += (
                f"\nتعذر التسليم المؤكد أو بقيت النتيجة ملتبسة لدى {result['failed']} مستلم؛ "
                "لن يعاد إرسال الملتبس تلقائيًا."
            )
        return text
    if result["status"] == "retryable":
        return (
            f"⚠️ بقي {result['pending']} مستلمًا دون نتيجة نهائية في الحملة #{campaign_id}. "
            f"يمكن استئناف المستلمين المؤكد عدم إرسالهم بالأمر /resume {campaign_id}. "
            f"النتائج الملتبسة ({result['uncertain']}) لن تعاد تلقائيًا."
        )
    if result["status"] == "finalization_failed":
        return (
            f"⚠️ اكتملت معالجة المستلمين لكن تعذر حفظ إنهاء الحملة #{campaign_id}. "
            f"بعد استعادة قاعدة البيانات استخدم /resume {campaign_id}؛ لن يعاد إرسال المؤكد."
        )
    if result["status"] == "interrupted":
        return (
            f"⚠️ توقف بث الحملة #{campaign_id} أثناء المعالجة. قد تكون نتيجة آخر طلب Telegram ملتبسة، "
            f"ولا يعاد إرسالها تلقائيًا. استخدم /resume {campaign_id} بعد انتهاء مهلة العامل."
        )
    return (
        f"⚠️ تعذر إكمال نشر الحملة #{campaign_id}. وصل الإرسال إلى {result['sent']} من "
        f"{result['total']} مستخدم. لا تعاد النتائج الملتبسة تلقائيًا."
    )


# =================================================================
# تدفّق المستخدم (العميل)
# =================================================================
async def customer_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    city_id = int(query.data.split("_")[1])
    await db.set_user_city(context.user_data["db_user_id"], city_id)
    if context.user_data.get("customer_city_id") != city_id:
        context.user_data["selected_categories"] = set()
    context.user_data["customer_city_id"] = city_id

    await render_category_picker(query, context)
    return CUST_CATEGORIES


async def customer_city_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    cities = await db.list_cities()
    keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"city_{c['id']}")] for c in cities]
    keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
    await query.edit_message_text("اختر مدينتك:", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data["navigation_section"] = "customer"
    context.user_data["navigation_parent"] = "main"
    return CUST_CITY


async def render_category_picker(query, context):
    categories = await db.list_categories()
    selected = context.user_data.get("selected_categories", set())
    keyboard = []
    for c in categories:
        mark = "✅ " if c["id"] in selected else ""
        keyboard.append([InlineKeyboardButton(f"{mark}{c['name']}", callback_data=f"cat_{c['id']}")])
    keyboard.append([InlineKeyboardButton("✔️ تأكيد الاشتراك", callback_data="cat_confirm")])
    keyboard.append([InlineKeyboardButton("↩️ تغيير المدينة", callback_data="customer_city_back")])
    keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
    await query.edit_message_text(
        "اختر اهتماماتك (يمكن اختيار أكثر من واحد):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def customer_category_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data == "cat_confirm":
        selected = context.user_data.get("selected_categories", set())
        if not selected:
            await _safe_answer(query, "اختر فئة واحدة على الأقل", show_alert=True)
            return CUST_CATEGORIES
        await _safe_answer(query)
        await db.set_user_categories(context.user_data["db_user_id"], list(selected))
        await query.edit_message_text(
            "🎉 تم تسجيل تفضيلاتك بنجاح!"
        )
        campaigns = await db.list_active_campaigns_for_user(context.user_data["db_user_id"])
        if campaigns:
            await query.message.reply_text("🎁 هذه العروض المتاحة حاليًا حسب اختياراتك:")
            for campaign in campaigns:
                await query.message.reply_text(
                    f"📢 {campaign['title']}\n\n{campaign['description']}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🎁 احصل على كود الخصم",
                            callback_data=f"getcode_{campaign['id']}",
                        )
                    ]]),
                )
        else:
            await query.message.reply_text(
                "لا توجد عروض مطابقة حاليًا، وسنرسل لك أي عرض جديد يناسب مدينتك واهتماماتك."
            )
        await query.message.reply_text(
            "📍 يمكنك الآن تصفح دليل مدينتك أو سوق الأفراد:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏪 دليل الأنشطة", callback_data="customer_directory")],
                [InlineKeyboardButton("🛒 سوق الأفراد", callback_data="customer_market")],
                [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
            ]),
        )
        return ConversationHandler.END

    await _safe_answer(query)
    cat_id = int(query.data.split("_")[1])
    selected = context.user_data.setdefault("selected_categories", set())
    if cat_id in selected:
        selected.discard(cat_id)
    else:
        selected.add(cat_id)
    await render_category_picker(query, context)
    return CUST_CATEGORIES


# =================================================================
# تدفّق التاجر — تسجيل النشاط
# =================================================================
async def merchant_biz_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["biz_name"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton(t, callback_data=f"biztype_{t}")] for t in BIZ_TYPES]
    await update.message.reply_text("ما نوع النشاط؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return MER_BIZ_TYPE


async def merchant_biz_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["biz_type"] = query.data.split("_", 1)[1]
    cities = await db.list_cities()
    keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"bizcity_{c['id']}")] for c in cities]
    await query.edit_message_text("في أي مدينة يقع النشاط؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return MER_BIZ_CITY


async def merchant_biz_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["biz_city_id"] = int(query.data.split("_")[1])
    await query.edit_message_text("ما رقم التواصل (واتساب/هاتف)؟")
    return MER_BIZ_PHONE


async def merchant_biz_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    biz = await db.create_business(
        context.user_data["db_user_id"],
        context.user_data["biz_name"],
        context.user_data["biz_type"],
        context.user_data["biz_city_id"],
        phone,
    )
    context.user_data["business_id"] = biz["id"]
    await update.message.reply_text(
        "✅ تم تسجيل نشاطك التجاري (بانتظار مراجعة الإدارة للنشاطات الجديدة).\n\n"
        f"{MERCHANT_LAUNCH_PRICING_NOTICE}"
    )
    await notify_admins_new_business(context, biz["id"], biz["business_name"])
    return await show_merchant_menu(update, context)


async def show_merchant_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["navigation_section"] = "merchant"
    context.user_data["navigation_parent"] = "main"
    business = await db.get_business_by_user(context.user_data["db_user_id"])
    approved = business and business["status"] == "approved"
    if approved:
        keyboard = [
            [InlineKeyboardButton("➕ إنشاء حملة", callback_data="new_campaign")],
            [InlineKeyboardButton("📊 حالة حملاتي", callback_data="my_campaigns")],
            [InlineKeyboardButton("🎟️ تحقق من كود خصم", callback_data="redeem_menu")],
        ]
        text = f"لوحة التاجر — ماذا تريد أن تفعل؟\n\n{MERCHANT_LAUNCH_PRICING_NOTICE}"
        reply_rows = [["➕ إنشاء حملة", "📊 حالة حملاتي"], ["🎟️ تحقق من كود خصم"]]
        if context.user_data.get("pending_campaign"):
            keyboard.insert(0, [InlineKeyboardButton("📄 متابعة معاينة الحملة", callback_data="resume_campaign")])
            reply_rows.insert(0, ["📄 متابعة معاينة الحملة"])
    else:
        keyboard = [[InlineKeyboardButton("🏪 حالة مراجعة النشاط", callback_data="business_status")]]
        text = _business_status_text(business)
        reply_rows = [["🏪 حالة النشاط"]]
    reply_keyboard = _main_reply_keyboard(reply_rows)
    if hasattr(update_or_query, "message") and update_or_query.message is None:
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        await update_or_query.message.reply_text("استخدم القائمة أسفل الشاشة للتعامل مع البوت.", reply_markup=reply_keyboard)
    else:
        target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
        await target.reply_text(text, reply_markup=reply_keyboard)
    return MER_MENU


async def merchant_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("تعذر إزالة أزرار لوحة التاجر القديمة: %s", exc)
    return await show_merchant_menu(query, context)


def _campaign_draft_warning_markup(preview_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 متابعة المعاينة الحالية", callback_data="resume_campaign")],
        [InlineKeyboardButton(
            "🗑️ إلغاء المعاينة وبدء حملة جديدة",
            callback_data=f"new_campaign_discard_{preview_id}",
        )],
        [InlineKeyboardButton("↩️ لوحة التاجر", callback_data="merchant_menu")],
    ])


def _campaign_draft_warning_text(pending):
    title = (pending.get("ai_data") or {}).get("title") or "الحملة الحالية"
    return f"لديك معاينة حملة غير مرسلة: {title}. لم أستبدلها؛ استأنفها أو ألغها صراحةً قبل بدء حملة جديدة."


def _ensure_campaign_preview_id(context, pending):
    preview_id = pending.get("preview_id")
    if not isinstance(preview_id, str) or not re.fullmatch(r"[0-9a-f]{12}", preview_id):
        preview_id = uuid4().hex[:12]
        pending["preview_id"] = preview_id
    context.user_data["campaign_preview_id"] = preview_id
    return preview_id


def _business_status_text(business) -> str:
    if not business:
        return "لا يوجد نشاط تجاري مسجل لهذا الحساب."
    if business["status"] == "pending":
        return "⏳ نشاطك التجاري بانتظار مراجعة الإدارة. ستظهر لك خيارات الحملات بعد اعتماده."
    if business["status"] == "rejected":
        return "❌ لم تتم الموافقة على تسجيل نشاطك التجاري. تواصل مع الدعم للاستفسار."
    return "✅ نشاطك التجاري معتمد."


async def merchant_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)

    if query.data.startswith("new_campaign_discard_"):
        requested_id = query.data.rsplit("_", 1)[-1]
        pending = context.user_data.get("pending_campaign")
        expected_id = context.user_data.get("campaign_preview_id")
        if not pending or not expected_id or requested_id != expected_id or pending.get("preview_id") != expected_id:
            await query.edit_message_text("انتهت صلاحية هذا الإجراء؛ لم تُحذف أي معاينة.")
            return MER_MENU
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        if not business or business["status"] != "approved":
            await query.edit_message_text(_business_status_text(business))
            return MER_MENU
        await _clear_campaign_preview(context)
        context.user_data.pop("pending_campaign", None)
        context.user_data["navigation_parent"] = "merchant_menu"
        await query.edit_message_text(
            f"{MERCHANT_LAUNCH_PRICING_NOTICE}\n\n"
            "أُلغيت المعاينة غير المرسلة فقط. ✍️ اكتب وصف الحملة الجديدة الآن."
        )
        return CAMPAIGN_DESC

    if query.data == "new_campaign":
        context.user_data["navigation_parent"] = "merchant_menu"
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        if not business or business["status"] != "approved":
            await query.edit_message_text(_business_status_text(business))
            return MER_MENU
        pending = context.user_data.get("pending_campaign")
        if isinstance(pending, dict):
            preview_id = _ensure_campaign_preview_id(context, pending)
            await query.edit_message_text(
                _campaign_draft_warning_text(pending),
                reply_markup=_campaign_draft_warning_markup(preview_id),
            )
            return MER_MENU
        await _clear_campaign_preview(context)
        context.user_data.pop("pending_campaign", None)
        await query.edit_message_text(
            f"{MERCHANT_LAUNCH_PRICING_NOTICE}\n\n"
            "✍️ اكتب وصفاً حراً للعرض (مثال: عندنا خصم 30% على الوجبات البحرية "
            "من الخميس للسبت، والمطعم في الخرطوم بحري)."
        )
        return CAMPAIGN_DESC

    if query.data == "redeem_menu":
        context.user_data["navigation_parent"] = "merchant_menu"
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        if not business or business["status"] != "approved":
            await query.edit_message_text(_business_status_text(business))
            return MER_MENU
        await query.edit_message_text("🎟️ أرسل كود الخصم الذي قدمه لك العميل للتحقق منه:")
        return REDEEM_CODE

    if query.data == "business_status":
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        await query.edit_message_text(_business_status_text(business))
        return MER_MENU

    if query.data == "my_campaigns":
        biz = await db.get_business_by_user(context.user_data["db_user_id"])
        if not biz:
            await query.edit_message_text("لا يوجد نشاط مسجل بعد.")
            return MER_MENU
        rows = await db.pool().fetch(
            "SELECT id, title, status FROM campaigns WHERE business_id=$1 ORDER BY id DESC LIMIT 10",
            biz["id"],
        )
        if not rows:
            await query.edit_message_text("لا توجد حملات بعد.")
        else:
            lines = [f"#{r['id']} — {r['title']} — {_status_ar(r['status'])}" for r in rows]
            await query.edit_message_text("حملاتك:\n" + "\n".join(lines))
        return MER_MENU

    return MER_MENU


async def merchant_text_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نفس خيارات لوحة التاجر لكن من لوحة المفاتيح العربية."""
    text = (update.message.text or "").strip()
    if text == "➕ إنشاء حملة":
        context.user_data["navigation_parent"] = "merchant_menu"
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        if not business or business["status"] != "approved":
            await update.message.reply_text(_business_status_text(business))
            return MER_MENU
        pending = context.user_data.get("pending_campaign")
        if isinstance(pending, dict):
            preview_id = _ensure_campaign_preview_id(context, pending)
            await update.message.reply_text(
                _campaign_draft_warning_text(pending),
                reply_markup=_campaign_draft_warning_markup(preview_id),
            )
            return MER_MENU
        await _clear_campaign_preview(context)
        context.user_data.pop("pending_campaign", None)
        await update.message.reply_text(
            f"{MERCHANT_LAUNCH_PRICING_NOTICE}\n\n"
            "✍️ اكتب وصفاً حراً للعرض (مثال: خصم 30% على الوجبات البحرية من الخميس للسبت)."
        )
        return CAMPAIGN_DESC
    if text == "📊 حالة حملاتي":
        biz = await db.get_business_by_user(context.user_data["db_user_id"])
        if not biz:
            await update.message.reply_text("لا يوجد نشاط مسجل بعد.")
            return MER_MENU
        rows = await db.pool().fetch(
            "SELECT id, title, status FROM campaigns WHERE business_id=$1 ORDER BY id DESC LIMIT 10",
            biz["id"],
        )
        if not rows:
            await update.message.reply_text("لا توجد حملات بعد.")
        else:
            lines = [f"#{r['id']} — {r['title']} — {_status_ar(r['status'])}" for r in rows]
            await update.message.reply_text("حملاتك:\n" + "\n".join(lines))
        return MER_MENU
    if text == "🎟️ تحقق من كود خصم":
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        if not business or business["status"] != "approved":
            await update.message.reply_text(_business_status_text(business))
            return MER_MENU
        context.user_data["navigation_parent"] = "merchant_menu"
        await update.message.reply_text("🎟️ أرسل كود الخصم الذي قدمه لك العميل للتحقق منه:")
        return REDEEM_CODE
    if text == "🏪 حالة النشاط":
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        await update.message.reply_text(_business_status_text(business))
        return MER_MENU
    return MER_MENU


async def merchant_redeem_code_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    business = await db.get_business_by_user(user["id"]) if user else None
    if not business:
        await update.message.reply_text("لا يوجد نشاط تجاري مسجل لهذا الحساب.")
        return MER_MENU
    code = (update.message.text or "").strip().upper()
    try:
        row = await db.redeem_code_for_business(code, business["id"])
    except Exception as exc:  # noqa: BLE001
        log.exception("فشل التحقق من كود الخصم %s: %s", code, exc)
        await update.message.reply_text("تعذر التحقق من الكود مؤقتًا. حاول مرة أخرى بعد قليل.")
        return MER_MENU
    if row:
        await update.message.reply_text(
            f"✅ الكود {row['code']} صالح وتم تسجيل استخدامه بنجاح.\n"
            "يُرجى تطبيق الخصم للعميل حسب تفاصيل الحملة."
        )
    else:
        await update.message.reply_text(
            "❌ الكود غير صالح، أو تابع لنشاط آخر، أو تم استخدامه مسبقًا."
        )
    return MER_MENU


async def merchant_redeem_prompt_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مسار احتياطي إذا ضغط التاجر الزر بعد انتهاء حالة المحادثة."""
    query = update.callback_query
    await _safe_answer(query)
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    business = await db.get_business_by_user(user["id"]) if user else None
    if not business:
        await query.edit_message_text("لا يوجد نشاط تجاري مسجل لهذا الحساب.")
        return MER_MENU
    await query.edit_message_text("🎟️ أرسل كود الخصم الذي قدمه لك العميل للتحقق منه:")
    context.user_data["navigation_parent"] = "merchant_menu"
    return REDEEM_CODE


async def merchant_redeem_keyboard_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يدخل التاجر إلى التحقق حتى لو لم تكن جلسة ConversationHandler فعالة."""
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    business = await db.get_business_by_user(user["id"]) if user else None
    if not business:
        await update.message.reply_text("لا يوجد نشاط تجاري مسجل لهذا الحساب.")
        return
    context.user_data["awaiting_redeem_code"] = True
    await update.message.reply_text("🎟️ أرسل كود الخصم الذي قدمه لك العميل للتحقق منه:")


async def merchant_redeem_fallback_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.pop("awaiting_redeem_code", False):
        return
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    business = await db.get_business_by_user(user["id"]) if user else None
    if not business:
        await update.message.reply_text("لا يوجد نشاط تجاري مسجل لهذا الحساب.")
        return
    try:
        row = await db.redeem_code_for_business(update.message.text.strip().upper(), business["id"])
    except Exception:  # noqa: BLE001
        log.exception("فشل مسار التحقق الاحتياطي من كود الخصم")
        await update.message.reply_text("تعذر التحقق من الكود مؤقتًا. حاول مرة أخرى بعد قليل.")
        return
    await update.message.reply_text(
        "✅ الكود صالح وتم تسجيل استخدامه بنجاح."
        if row else "❌ الكود غير صالح أو مستخدم مسبقًا أو تابع لنشاط آخر."
    )


# =================================================================
# إنشاء حملة — معالجة AI ثم إرسالها لمراجعة الإدارة
# =================================================================
async def campaign_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business = await db.get_business_by_user(context.user_data["db_user_id"])
    if not business or business["status"] != "approved":
        await update.message.reply_text(_business_status_text(business))
        return await show_merchant_menu(update, context)
    raw_text = update.message.text.strip()
    await update.message.reply_text("⏳ جارٍ تحليل العرض بواسطة الذكاء الاصطناعي...")

    data = dict(await ai.extract_campaign_data(raw_text))
    data["start"], data["end"] = db.normalize_campaign_date_values(
        data.get("start"), data.get("end")
    )
    try:
        db.validate_campaign_dates(data.get("start"), data.get("end"))
    except ValueError:
        await update.message.reply_text(
            "⚠️ تاريخ الحملة غير صالح. استخدم YYYY-MM-DD، واجعل تاريخ النهاية في أو بعد تاريخ البداية."
        )
        return CAMPAIGN_DESC
    if data.get("issues"):
        issues_text = "\n".join(f"- {i}" for i in data["issues"])
        await update.message.reply_text(
            f"⚠️ يحتاج العرض توضيحاً قبل المتابعة:\n{issues_text}\n\n"
            "أعد كتابة الوصف بمعلومات أوضح (الخصم، المدينة، التاريخ)."
        )
        return CAMPAIGN_DESC

    category_row = await db.pool().fetchrow(
        "SELECT id FROM categories WHERE code=$1 AND status='active'", data.get("category_code")
    )
    city_row = await db.pool().fetchrow(
        "SELECT id FROM cities WHERE code=$1 AND status='active'", data.get("city_code")
    )
    if not category_row or not city_row or not await db.validate_campaign_target(
        business["id"], city_row["id"] if city_row else None,
        category_row["id"] if category_row else None,
    ):
        await update.message.reply_text(
            "⚠️ هدف الحملة غير صالح. استخدم مدينة مسجلة ونشطة تطابق مدينة نشاطك، "
            "وفئة نشطة من القائمة، ثم أعد كتابة العرض."
        )
        return CAMPAIGN_DESC

    ad_text = await ai.generate_ad_copy(data)
    await _clear_campaign_preview(context)
    preview_id = uuid4().hex[:12]
    context.user_data["pending_campaign"] = {
        "raw": raw_text, "ai_data": data, "ad_text": ad_text, "preview_id": preview_id,
        "submission_key": uuid4().hex,
    }

    preview_message = await update.message.reply_text(
        _campaign_preview_text(context.user_data["pending_campaign"]),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ إرسال للمراجعة", callback_data=f"campaign_{preview_id}_submit")],
                [InlineKeyboardButton("✏️ إعادة الكتابة", callback_data=f"campaign_{preview_id}_rewrite")],
                [InlineKeyboardButton("↩️ لوحة التاجر", callback_data="merchant_menu"),
                 InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
            ]
        ),
    )
    context.user_data["campaign_preview_id"] = preview_id
    reference = _callback_message_reference(preview_message, update.effective_chat.id)
    if reference:
        context.user_data["campaign_preview_message"] = reference
    return CAMPAIGN_CONFIRM


async def resume_campaign_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await _safe_answer(query)
    pending = context.user_data.get("pending_campaign")
    preview_id = context.user_data.get("campaign_preview_id")
    if not pending or not preview_id or pending.get("preview_id") != preview_id:
        text = "لا توجد معاينة محفوظة لاستئنافها. يمكنك إنشاء حملة جديدة من لوحة التاجر."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return MER_MENU
    context.user_data["navigation_parent"] = "merchant_menu"
    text = _campaign_preview_text(pending)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ إرسال للمراجعة", callback_data=f"campaign_{preview_id}_submit")],
        [InlineKeyboardButton("✏️ إعادة الكتابة", callback_data=f"campaign_{preview_id}_rewrite")],
        [InlineKeyboardButton("↩️ لوحة التاجر", callback_data="merchant_menu"),
         InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])
    if query:
        preview_message = await query.edit_message_text(text, reply_markup=markup)
    else:
        preview_message = await update.message.reply_text(text, reply_markup=markup)
    chat = update.effective_chat
    reference = _callback_message_reference(preview_message, chat.id if chat else None)
    if reference:
        context.user_data["campaign_preview_message"] = reference
    return CAMPAIGN_CONFIRM


async def campaign_confirm_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    pending = context.user_data.get("pending_campaign")
    preview_id = parts[1] if len(parts) == 3 and parts[0] == "campaign" else None
    action = parts[-1]
    if (
        not preview_id
        or not pending
        or preview_id != pending.get("preview_id")
        or preview_id != context.user_data.get("campaign_preview_id")
        or action not in {"submit", "rewrite"}
    ):
        await _reject_stale_campaign_callback(query)
        return CAMPAIGN_CONFIRM if pending else MER_MENU
    await _safe_answer(query)

    if action == "rewrite":
        context.user_data.pop("pending_campaign", None)
        context.user_data.pop("campaign_preview_id", None)
        context.user_data.pop("campaign_preview_message", None)
        await query.edit_message_text("✍️ اكتب الوصف مرة أخرى:")
        return CAMPAIGN_DESC

    data = pending["ai_data"]
    business = await db.get_business_by_user(context.user_data["db_user_id"])
    if not business or business["status"] != "approved":
        context.user_data.pop("pending_campaign", None)
        context.user_data.pop("campaign_preview_id", None)
        context.user_data.pop("campaign_preview_message", None)
        await query.edit_message_text(_business_status_text(business))
        return await show_merchant_menu(query, context)
    business_id = business["id"]
    context.user_data["business_id"] = business_id

    category_row = await db.pool().fetchrow(
        "SELECT id FROM categories WHERE code=$1 AND status='active'", data.get("category_code")
    )
    city_row = await db.pool().fetchrow(
        "SELECT id FROM cities WHERE code=$1 AND status='active'", data.get("city_code")
    )
    if not category_row or not city_row or not await db.validate_campaign_target(
        business_id, city_row["id"] if city_row else None,
        category_row["id"] if category_row else None,
    ):
        await query.edit_message_text(
            "⚠️ لا يمكن إرسال الحملة: يجب أن تكون المدينة النشطة مطابقة لموقع النشاط وأن تكون الفئة نشطة."
        )
        context.user_data.pop("pending_campaign", None)
        context.user_data.pop("campaign_preview_id", None)
        context.user_data.pop("campaign_preview_message", None)
        return await show_merchant_menu(query, context)

    campaign = await db.create_campaign(
        business_id=business_id,
        raw_input=pending["raw"],
        ai_data=data,
        ad_text=pending["ad_text"],
        category_id=category_row["id"] if category_row else None,
        city_id=city_row["id"] if city_row else None,
        submission_key=pending.get("submission_key") or pending.get("preview_id"),
    )
    if not campaign:
        context.user_data.pop("pending_campaign", None)
        context.user_data.pop("campaign_preview_id", None)
        context.user_data.pop("campaign_preview_message", None)
        await query.edit_message_text(
            "⚠️ لم تُحفظ الحملة لأن النشاط غير معتمد أو أن هدفها لم يعد صالحًا."
        )
        return await show_merchant_menu(query, context)

    if campaign.get("_created", True):
        await notify_admins_new_campaign(context, campaign["id"])
    await query.edit_message_text(
        f"✅ تم إرسال الحملة #{campaign['id']} لمراجعة الإدارة. سنبلغك عند اعتمادها."
    )
    context.user_data.pop("pending_campaign", None)
    context.user_data.pop("campaign_preview_id", None)
    context.user_data.pop("campaign_preview_message", None)
    return await show_merchant_menu(query, context)


async def notify_admins_new_campaign(context: ContextTypes.DEFAULT_TYPE, campaign_id: int):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🆕 حملة جديدة بانتظار المراجعة: #{campaign_id}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 مراجعة الحملات", callback_data="admin_pending")
                ]]),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("تعذر إشعار الأدمن %s: %s", admin_id, e)


async def notify_admins_new_business(context: ContextTypes.DEFAULT_TYPE, business_id: int, name: str):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🏪 نشاط جديد بانتظار المراجعة: #{business_id} — {name}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏪 مراجعة الأنشطة", callback_data="admin_businesses")
                ]]),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذر إشعار الأدمن %s عن النشاط %s: %s", admin_id, business_id, exc)


# =================================================================
# أوامر الإدارة — مراجعة، اعتماد، رفض، نشر، تقرير
# =================================================================
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    rows = await db.list_pending_campaigns()
    if not rows:
        await update.message.reply_text(
            "لا توجد حملات بانتظار المراجعة.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 لوحة الإدارة", callback_data="admin_menu")]]),
        )
        return
    for r in rows:
        start = r["start_date"] or "بلا بداية محددة"
        end = r["end_date"] or "مفتوحة النهاية"
        text = (
            f"#{r['id']} — {r['business_name']}\n"
            f"فترة الصلاحية UTC: {start} — {end}\n{r['description']}"
        )
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ اعتماد", callback_data=f"admin_approve_{r['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"admin_reject_{r['id']}"),
            ]]),
        )


async def cmd_approve_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cmd = update.message.text.lstrip("/")
    action, _, campaign_id_str = cmd.partition("_")
    try:
        campaign_id = int(campaign_id_str)
    except ValueError:
        await update.message.reply_text("تعذر قراءة رقم الحملة. استخدم أزرار الاعتماد أو الرفض من قائمة المراجعة.")
        return

    campaign = await db.review_campaign(campaign_id, action)
    if not campaign:
        await update.message.reply_text(
            "لم تتغير الحملة: قد لا تكون قيد المراجعة، أو أن النشاط غير معتمد/هدف الحملة غير صالح."
        )
        return

    status = "approved" if action == "approve" else "rejected"
    await update.message.reply_text(f"تم تحديث الحملة #{campaign_id}: {_status_ar(status)}")
    await notify_merchant_campaign_decision(context, campaign, status)

    if status == "approved":
        await update.message.reply_text(
            "يمكنك نشر الحملة الآن من الزر أدناه:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 نشر الحملة", callback_data=f"admin_send_{campaign_id}")]]),
        )


async def cmd_send_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نشر حملة معتمدة عبر مسار البث الآمن المشترك مع الأزرار."""
    if not is_admin(update):
        return
    cmd = update.message.text.lstrip("/")
    _, _, campaign_id_str = cmd.partition("_")
    try:
        campaign_id = int(campaign_id_str)
    except ValueError:
        await update.message.reply_text("تعذر قراءة رقم الحملة. اختر حملة معتمدة من أزرار لوحة الإدارة.")
        return

    result = await campaign_broadcast.broadcast_campaign(
        context.bot, campaign_id, BATCH_SIZE, BATCH_DELAY_SECONDS
    )
    await update.message.reply_text(_broadcast_result_text(campaign_id, result))


async def cmd_resume_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume only pending recipients; ambiguous Telegram outcomes remain skipped."""
    if not is_admin(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /resume رقم_الحملة")
        return
    try:
        campaign_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم الحملة يجب أن يكون عددًا صحيحًا.")
        return
    result = await campaign_broadcast.broadcast_campaign(
        context.bot, campaign_id, BATCH_SIZE, BATCH_DELAY_SECONDS, resume=True
    )
    await update.message.reply_text(_broadcast_result_text(campaign_id, result))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("أدخل رقم الحملة المطلوبة لعرض تقريرها.")
        return
    try:
        campaign_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("رقم الحملة يجب أن يكون رقمًا صحيحًا.")
        return
    stats = await db.campaign_report(campaign_id)
    sent = stats["sent"] or 1
    conv_rate = round((stats["redeemed"] / sent) * 100, 1)
    await update.message.reply_text(
        f"تقرير حملة #{campaign_id}\n\n"
        f"تم الإرسال: {stats['sent']}\n"
        f"التفاعل (نقر): {stats['clicked']}\n"
        f"أكواد الخصم: {stats['codes']}\n"
        f"✅ أكواد مستخدمة: {stats['redeemed']}\n\n"
        f"معدل التحويل: {conv_rate}%"
    )


# =================================================================
# المستخدم يطلب كود الخصم بعد استلام الإعلان
# =================================================================
async def get_discount_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        campaign_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.message.reply_text("تعذر قراءة الحملة. اطلب عرضًا أحدث وحاول مرة أخرى.")
        return

    await _ensure_db_user(update, context)
    user_row = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user_row:
        await query.message.reply_text("تعذر تسجيل حسابك مؤقتًا. اضغط زر القائمة الرئيسية ثم حاول مجددًا.")
        return

    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["status"] not in {"active", "sending", "broadcast_failed"}:
        await query.message.reply_text("هذا العرض غير متاح حاليًا.")
        return
    city_row = await db.pool().fetchrow("SELECT code FROM cities WHERE id=$1", campaign["city_id"])
    city_code = city_row["code"] if city_row else "OFFER"

    update_id = getattr(update, "update_id", None)
    claimed = await _claim_side_effecting_update(update, "get_discount_code")
    if not claimed:
        return
    try:
        if type(update_id) is int:
            receipt = await db.get_telegram_update_receipt(update_id)
            if not receipt or receipt["stage"] != "notification_sent":
                await db.log_campaign_event_for_update(
                    campaign_id, user_row["id"], "CLICKED", update_id, "get_discount_code"
                )
        else:
            await db.log_event(campaign_id, user_row["id"], "CLICKED")
        code_row = await db.create_discount_code(campaign_id, user_row["id"], city_code)
        if type(update_id) is int:
            await db.complete_telegram_update(update_id, "get_discount_code")
    except Exception:
        if type(update_id) is int:
            await db.release_telegram_update(update_id, "get_discount_code")
        raise
    if not code_row:
        await query.message.reply_text("هذا العرض لم يبدأ بعد أو انتهت صلاحيته، ولا يمكن إصدار كود جديد.")
        return

    await query.message.reply_text(
        f"🎁 كودك: `{code_row['code']}`\nقدّمه للتاجر عند الشراء للاستفادة من الخصم.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def customer_directory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    businesses = await db.list_public_businesses(user["city_id"] if user else None)
    if not businesses:
        await query.edit_message_text("لا توجد أنشطة مسجلة في مدينتك حاليًا.")
        return
    await query.edit_message_text("🏪 دليل الأنشطة في مدينتك:")
    for business in businesses:
        if user:
            await db.log_business_event(business["id"], user["id"], "VIEW")
        await query.message.reply_text(
            f"🏪 {business['business_name']}\n"
            f"التصنيف: {business['business_type'] or 'خدمات'}\n"
            f"المدينة: {business['city_name'] or 'غير محددة'}\n"
            f"📞 {business['phone'] or 'لا يوجد رقم مسجل'}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 تواصل مع النشاط", callback_data=f"business_contact_{business['id']}"),
            ]]),
        )


async def listing_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        listing_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الإعلان.")
        return
    listing = await db.get_public_listing(listing_id)
    buyer = await db.get_user_by_telegram_id(update.effective_user.id)
    if not listing:
        await query.message.reply_text("الإعلان غير متاح حاليًا.")
        return
    update_id = getattr(update, "update_id", None)
    claimed = await _claim_side_effecting_update(update, "listing_contact")
    if not claimed:
        await query.message.reply_text("تمت معالجة طلب التواصل هذا مسبقًا.")
        return
    try:
        owner = await db.get_user_by_id(listing["user_id"])
        receipt = (
            await db.get_telegram_update_receipt(update_id)
            if type(update_id) is int else None
        )
        stage = receipt["stage"] if receipt else None
        uncertain_notification = stage == "notification_attempting"
        if stage not in {"notification_attempting", "notification_sent"}:
            if type(update_id) is int:
                await db.log_listing_contact_for_update(
                    listing_id, buyer["id"] if buyer else None, update_id
                )
            else:
                await db.log_listing_event(listing_id, buyer["id"] if buyer else None, "CONTACT")
            if owner:
                if type(update_id) is int:
                    if not await db.set_telegram_update_stage(
                        update_id, "listing_contact", "notification_attempting"
                    ):
                        raise RuntimeError("Contact update is no longer processing")
                try:
                    await context.bot.send_message(
                        owner["telegram_id"],
                        f"📩 يوجد مستخدم مهتم بإعلانك #{listing_id}: {listing['title']}\n"
                        "يمكنك التواصل معه عبر تيليجرام.",
                    )
                except Exception as exc:
                    # Keep the notification unconfirmed; retry details only, never the owner notice.
                    uncertain_notification = type(update_id) is int
                    log.warning("تعذر تأكيد إشعار التواصل للإعلان %s: %s", listing_id, exc)
                else:
                    if type(update_id) is int and not await db.set_telegram_update_stage(
                        update_id, "listing_contact", "notification_sent"
                    ):
                        raise RuntimeError("Contact notice sent but update stage was not saved")
        # Telegram has no idempotency key: if this reply times out after delivery,
        # a resumed attempt can repeat the details message, but not the DB event or owner notice.
        await query.message.reply_text(
            f"رقم التواصل مع المعلن: {listing['contact'] or 'غير مسجل'}\n"
            f"حساب تيليجرام: {('@' + owner['username']) if owner and owner['username'] else 'غير متاح'}"
        )
        if type(update_id) is int:
            if uncertain_notification:
                if not await db.mark_telegram_update_uncertain(update_id, "listing_contact"):
                    raise RuntimeError("Unconfirmed contact notice could not be recorded")
            elif not await db.complete_telegram_update(update_id, "listing_contact"):
                raise RuntimeError("Contact details sent but update completion was not saved")
    except Exception:
        if type(update_id) is int:
            await db.release_telegram_update(update_id, "listing_contact")
        raise


async def business_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        business_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة النشاط.")
        return
    business = await db.get_business(business_id)
    buyer = await db.get_user_by_telegram_id(update.effective_user.id)
    if not business:
        await query.message.reply_text("النشاط غير متاح حاليًا.")
        return
    update_id = getattr(update, "update_id", None)
    claimed = await _claim_side_effecting_update(update, "business_contact")
    if not claimed:
        await query.message.reply_text("تمت معالجة طلب التواصل هذا مسبقًا.")
        return
    try:
        if type(update_id) is int:
            await db.log_business_contact_for_update(
                business_id, buyer["id"] if buyer else None, update_id
            )
            await db.complete_telegram_update(update_id, "business_contact")
        else:
            await db.log_business_event(business_id, buyer["id"] if buyer else None, "CONTACT")
    except Exception:
        if type(update_id) is int:
            await db.release_telegram_update(update_id, "business_contact")
        raise
    await query.message.reply_text(
        f"للتواصل مع {business['business_name']}: {business['phone'] or 'لا يوجد رقم مسجل'}"
    )


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستخدمها التاجر/الموظف: /redeem KHART-8F21"""
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("أرسل كود الخصم وحده بعد اختيار «التحقق من كود خصم».")
        return
    if not (is_admin(update) or (update.effective_user and await db.get_business_by_user(
        (await db.get_user_by_telegram_id(update.effective_user.id)) or {"id": None}
    ))):
        await update.message.reply_text("هذا الأمر مخصص للإدارة وأصحاب الأنشطة المسجلة.")
        return
    if is_admin(update):
        row = await db.redeem_code(parts[1].upper())
    else:
        user = await db.get_user_by_telegram_id(update.effective_user.id)
        business = await db.get_business_by_user(user["id"]) if user else None
        row = await db.redeem_code_for_business(parts[1].upper(), business["id"]) if business else None
    if row:
        await update.message.reply_text(f"✅ تم استخدام الكود {row['code']} بنجاح.")
    else:
        await update.message.reply_text("❌ الكود غير صالح أو مستخدم مسبقاً.")


def _status_ar(status: str) -> str:
    return {
        "pending": "بانتظار المراجعة",
        "pending_review": "بانتظار المراجعة",
        "approved": "معتمد",
        "active": "نشط",
        "sending": "قيد الإرسال",
        "broadcast_failed": "تعذر الإرسال",
        "rejected": "مرفوض",
        "expired": "منتهي",
        "sold": "تم البيع",
        "draft": "مسودة",
    }.get(status, "غير معروف")


def _listing_actions_markup(row, include_actions=True):
    listing_id = row["id"]
    keyboard = [[InlineKeyboardButton("📊 الإحصاءات", callback_data=f"mylisting_stats_{listing_id}")]]
    if include_actions:
        actions = []
        if row["status"] in {"approved", "expired"} and (row.get("image_file_ids") or []):
            actions.append(InlineKeyboardButton("🔄 تجديد الإعلان", callback_data=f"mylisting_renew_{listing_id}"))
        if row["status"] in {"approved", "pending_review"}:
            actions.append(InlineKeyboardButton("✅ تم البيع", callback_data=f"mylisting_sold_{listing_id}"))
        if row["status"] == "rejected":
            actions.append(InlineKeyboardButton("✏️ تعديل", callback_data=f"mylisting_edit_{listing_id}"))
        if actions:
            keyboard.append(actions)
    keyboard.append([
        InlineKeyboardButton("📋 إعلاناتي", callback_data="mylistings"),
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(keyboard)


LISTINGS_PAGE_SIZE = 10


async def _show_user_listings(update, context, mode="list", cursor_id=None, direction="next"):
    user_id = await _ensure_db_user(update, context)
    if user_id:
        rows, has_previous, has_next = await db.list_user_listings_page(
            user_id, mode=mode, cursor_id=cursor_id, direction=direction,
            limit=LISTINGS_PAGE_SIZE,
        )
    else:
        rows, has_previous, has_next = [], False, False

    if not rows:
        messages = {
            "list": "لا توجد إعلانات مرتبطة بحسابك حاليًا.",
            "stats": "لا توجد إعلانات لعرض إحصاءاتها.",
            "renew": "لا توجد إعلانات قابلة للتجديد. إذا انتهت صلاحية صورة الإعلان، أنشئ إعلانًا جديدًا بصورة حديثة.",
            "sold": "لا توجد إعلانات نشطة لتسجيل بيعها.",
        }
        text = messages[mode]
        if cursor_id is not None:
            text = "لم تعد هناك إعلانات في هذه الصفحة؛ ربما تغيرت حالتها أثناء التنقل. ارجع إلى بداية القائمة."
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ بداية القائمة", callback_data="mylistings")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
        ])
    else:
        lines = [
            {"list": "📋 إعلاناتك:", "stats": "📊 إحصاءات إعلاناتك:",
             "renew": "اختر الإعلان الذي تريد تجديده:", "sold": "اختر الإعلان الذي تم بيعه:"}[mode]
        ]
        keyboard = []
        for row in rows:
            if mode == "stats":
                lines.append(
                    f"\n#{row['id']} — {row['title']} — {_status_ar(row['status'])}\n"
                    f"المشاهدات: {row['views']} | مشاهدون فريدون: {row['unique_viewers']}\n"
                    f"مرات التواصل: {row['contacts']} | متواصلون فريدون: {row['unique_contacts']}"
                )
            elif mode == "list":
                lines.append(
                    f"\n#{row['id']} — {row['title']} — {_status_ar(row['status'])}\n"
                    f"المشاهدات: {row['views']} | تواصل: {row['contacts']}"
                )
            else:
                lines.append(f"\n#{row['id']} — {row['title']} — {_status_ar(row['status'])}")
            if mode == "list":
                keyboard.extend(_listing_actions_markup(row).inline_keyboard[:-1])
            elif mode == "renew":
                keyboard.append([InlineKeyboardButton("🔄 تجديد الإعلان", callback_data=f"mylisting_renew_{row['id']}")])
            elif mode == "sold":
                keyboard.append([InlineKeyboardButton("✅ تم البيع", callback_data=f"mylisting_sold_{row['id']}")])
            elif mode == "stats":
                keyboard.append([InlineKeyboardButton("📊 الإحصاءات", callback_data=f"mylisting_stats_{row['id']}")])
        page_buttons = []
        if has_previous:
            page_buttons.append(InlineKeyboardButton(
                "⬅️ السابقة", callback_data=f"mylistings_page_{mode}_prev_{rows[0]['id']}"
            ))
        if has_next:
            page_buttons.append(InlineKeyboardButton(
                "التالية ➡️", callback_data=f"mylistings_page_{mode}_next_{rows[-1]['id']}"
            ))
        if page_buttons:
            keyboard.append(page_buttons)
        if mode == "list":
            keyboard.append([
                InlineKeyboardButton("↩️ رجوع", callback_data="main_menu"),
                InlineKeyboardButton("🛒 سوق الأفراد", callback_data="customer_market"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("↩️ رجوع", callback_data="mylistings"),
                InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
            ])
        text = "\n".join(lines)[:3900]
        markup = InlineKeyboardMarkup(keyboard)

    query = getattr(update, "callback_query", None)
    context.user_data["navigation_section"] = "listings"
    context.user_data["navigation_parent"] = "main"
    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def my_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    return await _show_user_listings(update, context)


async def my_listings_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        _, _, mode, direction, cursor_text = query.data.split("_")
        cursor_id = int(cursor_text)
        if mode not in {"list", "stats", "renew", "sold"} or direction not in {"next", "prev"} or cursor_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await query.edit_message_text("انتهت صلاحية رابط الصفحة؛ افتح قائمة إعلاناتك من جديد.")
        return SELECT_ROLE
    await _show_user_listings(update, context, mode=mode, cursor_id=cursor_id, direction=direction)
    return SELECT_ROLE


async def my_listing_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        _, action, listing_id_text = query.data.split("_")
        listing_id = int(listing_id_text)
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الإعلان.")
        return
    user_id = await _ensure_db_user(update, context)
    listing = await db.get_listing(listing_id) if user_id else None
    if not listing or listing["user_id"] != user_id:
        await query.edit_message_text("هذا الإعلان غير موجود أو لا يخص حسابك.")
        return

    if action == "stats":
        stats = await db.listing_report(listing_id)
        await query.edit_message_text(
            f"📊 إحصاءات الإعلان #{listing_id}\n{listing['title']}\n\n"
            f"المشاهدات: {stats['views']}\nالمشاهدون الفريدون: {stats['unique_viewers']}\n"
            f"مرات التواصل: {stats['contacts']}\nالمتواصلون الفريدون: {stats['unique_contacts']}\n"
            f"الحالة: {_status_ar(listing['status'])}\nينتهي في: {listing['expires_at']}",
            reply_markup=_listing_actions_markup(listing),
        )
        return
    if action == "renew":
        result = await db.renew_listing(listing_id, user_id)
        text = (
            f"✅ أُعيد تقديم الإعلان #{listing_id} للمراجعة؛ وتبدأ مدة عرضه 72 ساعة بعد الموافقة."
            if result else "لا يمكن تجديد هذا الإعلان الآن؛ ربما جُدد سابقًا أو لم يعد صالحًا."
        )
    elif action == "sold":
        result = await db.mark_listing_sold(listing_id, user_id)
        text = (
            f"✅ سُجّل بيع الإعلان #{listing_id} وأُوقف عن الظهور."
            if result else "لا يمكن إيقاف هذا الإعلان؛ ربما سُجّل بيعه مسبقًا."
        )
    else:
        await query.edit_message_text("هذا الإجراء غير معروف.")
        return
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 إعلاناتي", callback_data="mylistings"),
            InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
        ]]),
    )


async def cmd_my_listings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_user_listings(update, context)


async def cmd_listing_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = getattr(context, "args", None) or []
    if not args:
        return await _show_user_listings(update, context, mode="stats")
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("أدخل رقم إعلان صحيحًا، أو استخدم زر «📊 إحصاءات الإعلانات» لعرض إحصاءات إعلاناتك.")
        return
    user_id = await _ensure_db_user(update, context)
    listing = await db.get_listing(int(args[0])) if user_id else None
    if not listing or listing["user_id"] != user_id:
        await update.message.reply_text("هذا الإعلان غير موجود أو لا يخص حسابك.")
        return
    stats = await db.listing_report(listing["id"])
    await update.message.reply_text(
        f"📊 إحصاءات الإعلان #{listing['id']}\n{listing['title']}\n\n"
        f"المشاهدات: {stats['views']}\n"
        f"المشاهدون الفريدون: {stats['unique_viewers']}\n"
        f"مرات التواصل: {stats['contacts']}\n"
        f"المتواصلون الفريدون: {stats['unique_contacts']}\n"
        f"الحالة: {_status_ar(listing['status'])}\n"
        f"ينتهي في: {listing['expires_at']}",
        reply_markup=_listing_actions_markup(listing),
    )


async def cmd_renew_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = getattr(context, "args", None) or []
    if not args:
        return await _show_user_listings(update, context, mode="renew")
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("اختر إعلانًا قابلًا للتجديد من القائمة، أو أدخل رقم إعلان صحيحًا.")
        return
    user_id = await _ensure_db_user(update, context)
    listing_id = int(args[0])
    listing = await db.renew_listing(listing_id, user_id) if user_id else None
    if listing:
        await update.message.reply_text(
            f"✅ أُعيد تقديم الإعلان #{listing['id']} للمراجعة؛ وتبدأ مدة عرضه 72 ساعة بعد الموافقة."
        )
    else:
        previous = await db.get_listing(listing_id) if user_id else None
        if (
            previous
            and previous["user_id"] == user_id
            and previous["status"] == "expired"
            and not (previous["image_file_ids"] or [])
        ):
            await update.message.reply_text(
                "انتهت صلاحية الإعلان وحُذف مرجع صورته. أنشئ إعلانًا جديدًا من سوق الأفراد وارفع صورة حديثة."
            )
        else:
            await update.message.reply_text("لا يمكن تجديد هذا الإعلان أو أنه لا يخص حسابك.")


async def cmd_sold_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = getattr(context, "args", None) or []
    if not args:
        return await _show_user_listings(update, context, mode="sold")
    if len(args) != 1 or not args[0].isdigit():
        await update.message.reply_text("اختر إعلانًا من قائمتك أو أدخل رقم إعلان صحيحًا.")
        return
    user_id = await _ensure_db_user(update, context)
    listing = await db.mark_listing_sold(int(args[0]), user_id) if user_id else None
    await update.message.reply_text(
        "✅ تم إيقاف الإعلان لأنه تم البيع." if listing else "لا يمكن إيقاف هذا الإعلان أو أنه لا يخص حسابك."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear only the active section's unsaved inputs; saved ads and unrelated drafts survive.
    market_keys = (
        "market_draft", "market_preview_id", "market_preview_message", "market_city_id",
        "edit_listing_city_id", "resubmitting_listing_id", "resubmitting_listing_title",
        "market_search_id", "market_results", "market_offset", "market_search_city_id",
        "market_search_city_name", "market_search_action_message",
    )
    campaign_keys = ("pending_campaign", "campaign_preview_id", "campaign_preview_message")
    merchant_form_keys = ("biz_name", "biz_type", "biz_city_id", "selected_categories", "awaiting_redeem_code")
    customer_keys = ("customer_city_id", "selected_categories")
    section = context.user_data.get("navigation_section")
    if section == "marketplace":
        keys_to_clear = market_keys
    elif section == "merchant":
        keys_to_clear = campaign_keys + merchant_form_keys
    elif section == "customer":
        keys_to_clear = customer_keys
    elif context.user_data.get("pending_campaign") and not context.user_data.get("market_draft"):
        keys_to_clear = campaign_keys
    elif context.user_data.get("market_draft") and not context.user_data.get("pending_campaign"):
        keys_to_clear = market_keys
    elif context.user_data.get("awaiting_redeem_code"):
        keys_to_clear = ("awaiting_redeem_code",)
    else:
        keys_to_clear = ()
    for key in keys_to_clear:
        context.user_data.pop(key, None)
    context.user_data["navigation_section"] = "main"
    context.user_data.pop("navigation_parent", None)
    await update.message.reply_text(
        "أُلغيت الخطوة غير المرسلة في القسم الحالي فقط؛ لم يتغير أي إعلان محفوظ، وبقيت المسودات الأخرى كما هي. اختر إجراءً آخر:",
        reply_markup=_user_reply_keyboard(context),
    )
    return SELECT_ROLE


# =================================================================
# تجميع التطبيق
# =================================================================
async def post_init(application: Application):
    await db.init_pool()
    log.info("تم الاتصال بقاعدة البيانات.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("خطأ غير متوقع أثناء معالجة التحديث", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("حدث خطأ مؤقت. حاول مرة أخرى بعد قليل.")
        except Exception:  # noqa: BLE001
            pass


async def post_shutdown(application: Application):
    await db.close_pool()


def _select_role_handler(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await handler(update, context)
        return SELECT_ROLE

    return wrapped


def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = (
        Application.builder()
        .token(token)
        .connection_pool_size(32)
        .pool_timeout(10)
        .connect_timeout(10)
        .read_timeout(15)
        .write_timeout(15)
        .get_updates_connection_pool_size(2)
        .get_updates_pool_timeout(10)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(10)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    conv = ConversationHandler(
        # Shared chat-level state is intentional for the mixed text/callback flows.
        per_message=False,
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
            CommandHandler("editlisting", marketplace.edit_listing_start),
            CallbackQueryHandler(marketplace.market_menu, pattern="^customer_market$"),
            CallbackQueryHandler(marketplace.resume_listing_flow, pattern="^resume_listing_flow$"),
            CallbackQueryHandler(
                marketplace.sell_confirm,
                pattern=r"^(?:market|listing)_[0-9a-f]{12}_(?:submit|resend|cancel)$",
            ),
            CallbackQueryHandler(marketplace.edit_listing_start, pattern=r"^mylisting_edit_\d+$"),
            CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
            CallbackQueryHandler(merchant_menu_callback, pattern="^merchant_menu$"),
            CallbackQueryHandler(resume_campaign_preview, pattern="^resume_campaign$"),
            CallbackQueryHandler(_select_role_handler(admin_pending_callback), pattern="^admin_pending$"),
            CallbackQueryHandler(_select_role_handler(admin_pending_businesses_callback), pattern="^admin_businesses$"),
            CallbackQueryHandler(_select_role_handler(admin_listings_callback), pattern="^admin_listings$"),
            CallbackQueryHandler(_select_role_handler(admin_reports_callback), pattern="^admin_reports$"),
            CallbackQueryHandler(_select_role_handler(admin_business_action), pattern=r"^admin_business_(approve|reject)_\d+$"),
            CallbackQueryHandler(_select_role_handler(admin_campaign_action), pattern=r"^admin_(approve|reject)_\d+$"),
            CallbackQueryHandler(_select_role_handler(admin_listing_action), pattern=r"^listing_(approve|reject)_\d+$"),
            CallbackQueryHandler(_select_role_handler(admin_send_callback), pattern=r"^admin_send_\d+$"),
            CallbackQueryHandler(_select_role_handler(get_discount_code), pattern=r"^getcode_\d+$"),
            CallbackQueryHandler(_select_role_handler(customer_directory_callback), pattern="^customer_directory$"),
            CallbackQueryHandler(_select_role_handler(listing_contact_callback), pattern=r"^listing_contact_\d+$"),
            CallbackQueryHandler(_select_role_handler(business_contact_callback), pattern=r"^business_contact_\d+$"),
            CallbackQueryHandler(_select_role_handler(my_listings_callback), pattern="^mylistings$"),
            CallbackQueryHandler(
                _select_role_handler(my_listings_page_callback),
                pattern=r"^mylistings_page_(list|stats|renew|sold)_(next|prev)_\d+$",
            ),
            CallbackQueryHandler(_select_role_handler(my_listing_action_callback), pattern=r"^mylisting_(stats|renew|sold)_\d+$"),
            CommandHandler("pending", _select_role_handler(cmd_pending)),
            CommandHandler("report", _select_role_handler(cmd_report)),
            CommandHandler("resume", _select_role_handler(cmd_resume_campaign)),
            CommandHandler("redeem", _select_role_handler(cmd_redeem)),
            CommandHandler("mylistings", _select_role_handler(cmd_my_listings)),
            CommandHandler("stats", _select_role_handler(cmd_listing_stats)),
            CommandHandler("renew", _select_role_handler(cmd_renew_listing)),
            CommandHandler("sold", _select_role_handler(cmd_sold_listing)),
            MessageHandler(filters.Regex(r"^/approve_\d+$") | filters.Regex(r"^/reject_\d+$"), _select_role_handler(cmd_approve_reject)),
            MessageHandler(filters.Regex(r"^/send_\d+$"), _select_role_handler(cmd_send_campaign)),
            CallbackQueryHandler(
                role_router,
                pattern=r"^(role_customer|role_merchant|how_it_works|contact_us|admin_menu)$",
            ),
            CallbackQueryHandler(customer_city_back, pattern="^customer_city_back$"),
            CallbackQueryHandler(merchant_redeem_prompt_fallback, pattern="^redeem_menu$"),
            MessageHandler(_root_keyboard_filter(), text_role_router),
        ],
        states={
            SELECT_ROLE: [
                CallbackQueryHandler(admin_pending_callback, pattern="^admin_pending$"),
                CallbackQueryHandler(admin_pending_businesses_callback, pattern="^admin_businesses$"),
                CallbackQueryHandler(admin_listings_callback, pattern="^admin_listings$"),
                CallbackQueryHandler(admin_reports_callback, pattern="^admin_reports$"),
                CallbackQueryHandler(admin_business_action, pattern=r"^admin_business_(approve|reject)_\d+$"),
                CallbackQueryHandler(admin_campaign_action, pattern=r"^admin_(approve|reject)_\d+$"),
                CallbackQueryHandler(admin_listing_action, pattern=r"^listing_(approve|reject)_\d+$"),
                CallbackQueryHandler(admin_send_callback, pattern=r"^admin_send_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_role_router),
            ],
            CUST_CITY: [CallbackQueryHandler(customer_city_chosen, pattern="^city_")],
            CUST_CATEGORIES: [CallbackQueryHandler(customer_category_toggle, pattern="^cat_")],
            MER_BIZ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_biz_name)],
            MER_BIZ_TYPE: [CallbackQueryHandler(merchant_biz_type, pattern="^biztype_")],
            MER_BIZ_CITY: [CallbackQueryHandler(merchant_biz_city, pattern="^bizcity_")],
            MER_BIZ_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_biz_phone)],
            MER_MENU: [
                CallbackQueryHandler(
                    merchant_menu_router,
                    pattern=r"^(?:new_campaign|new_campaign_discard_[0-9a-f]{12}|redeem_menu|my_campaigns|business_status)$",
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_text_menu_router),
            ],
            REDEEM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_redeem_code_received)],
            CAMPAIGN_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_description_received)],
            CAMPAIGN_CONFIRM: [CallbackQueryHandler(
                campaign_confirm_router,
                pattern=r"^(?:submit_campaign|rewrite_campaign|campaign_[0-9a-f]{12}_(?:submit|rewrite))$",
            )],
            **marketplace.states(),
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    # Every user/admin action is routed through the ConversationHandler so a
    # one-shot callback cannot leave a stale interactive state behind.
    application.add_error_handler(on_error)

    return application


def main():
    app = build_application()
    log.info("البوت يعمل الآن (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
