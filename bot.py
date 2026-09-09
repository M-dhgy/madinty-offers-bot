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

import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
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
import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("madinty-bot")

ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()}
BATCH_SIZE = int(os.environ.get("BROADCAST_BATCH_SIZE", "50"))
BATCH_DELAY_SECONDS = float(os.environ.get("BROADCAST_BATCH_DELAY", "1.5"))

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
LISTING_TITLE, LISTING_DESCRIPTION, LISTING_PRICE, LISTING_CONDITION, LISTING_CITY, LISTING_CATEGORY = range(11, 17)
LISTING_ADDRESS, LISTING_CONTACT, LISTING_DELIVERY = range(17, 20)

BIZ_TYPES = ["مطعم / كافيه", "متجر", "مركز تجميل", "خدمات"]


def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ADMIN_IDS


# =================================================================
# /start — نقطة الدخول
# =================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    user = await db.create_user(tg_user.id, tg_user.username, tg_user.first_name)
    context.user_data["db_user_id"] = user["id"]

    keyboard = [
        [InlineKeyboardButton("🔎 أبحث عن عروض", callback_data="role_customer")],
        [InlineKeyboardButton("🛒 سوق الأفراد", callback_data="customer_market")],
        [InlineKeyboardButton("🏪 صاحب نشاط تجاري", callback_data="role_merchant")],
        [InlineKeyboardButton("ℹ️ كيف يعمل؟", callback_data="how_it_works")],
        [InlineKeyboardButton("📞 تواصل معنا", callback_data="contact_us")],
    ]
    if is_admin(update):
        keyboard.append([InlineKeyboardButton("🛠 لوحة الإدارة", callback_data="admin_menu")])
    await update.message.reply_text(
        "أهلاً بك في *عروض مدينتي* 👋\n\nاختر نوع حسابك:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text(
        "يمكنك أيضًا استخدام القائمة العربية أسفل الشاشة للتنقل بسهولة.",
        reply_markup=ReplyKeyboardMarkup(
            [["🔎 أبحث عن عروض", "🛒 سوق الأفراد"], ["🏪 صاحب نشاط تجاري"], ["ℹ️ كيف يعمل؟", "📞 تواصل معنا"]],
            resize_keyboard=True,
            is_persistent=True,
        ),
    )
    return SELECT_ROLE


async def text_role_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعالج اختيارات القائمة النصية بدل إجبار المستخدم على كتابة أوامر."""
    text = (update.message.text or "").strip()
    if text == "🔎 أبحث عن عروض":
        await db.set_account_type(context.user_data["db_user_id"], "customer")
        cities = await db.list_cities()
        keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"city_{c['id']}")] for c in cities]
        await update.message.reply_text("اختر مدينتك:", reply_markup=InlineKeyboardMarkup(keyboard))
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
        await update.message.reply_text(
            "🛒 سوق الأفراد — النشر مجاني خلال فترة الإطلاق.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ نشر غرض للبيع", callback_data="listing_start")
            ]]),
        )
        return SELECT_ROLE
    return SELECT_ROLE


async def role_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

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
        await query.edit_message_text("اختر مدينتك:", reply_markup=InlineKeyboardMarkup(keyboard))
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
            [InlineKeyboardButton("📦 إعلانات أفراد بانتظار المراجعة", callback_data="admin_listings")],
            [InlineKeyboardButton("📊 تقارير الوصول والتواصل", callback_data="admin_reports")],
            [InlineKeyboardButton("🔄 تحديث القائمة", callback_data="admin_menu")],
        ]),
    )


async def admin_reports_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
    await query.answer()
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


async def admin_listings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        await query.message.reply_text(
            f"📦 الإعلان #{row['id']}\n{row['title']}\n"
            f"السعر: {price}\nالعنوان: {row['address'] or 'غير محدد'}\n"
            f"التواصل: {row['contact'] or 'غير محدد'}\nالتوصيل: {row['delivery'] or 'غير محدد'}\n\n"
            f"{row['description'] or ''}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ اعتماد", callback_data=f"listing_approve_{row['id']}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"listing_reject_{row['id']}"),
            ]]),
        )


async def admin_listing_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
    await db.set_listing_status(listing_id, status)
    try:
        owner = await db.get_user_by_id(listing["user_id"])
        if owner:
            await context.bot.send_message(
                owner["telegram_id"],
                f"{'✅ تم اعتماد' if status == 'approved' else '❌ تم رفض'} إعلانك رقم #{listing_id}.\n"
                + ("أصبح ظاهرًا الآن في سوق الأفراد." if status == "approved" else "يمكنك تعديل البيانات وإعادة المحاولة لاحقًا."),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر إشعار صاحب الإعلان %s: %s", listing_id, exc)
    await query.edit_message_text(
        f"{'✅ تم اعتماد' if status == 'approved' else '❌ تم رفض'} الإعلان #{listing_id}."
    )


async def admin_campaign_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    try:
        _, action, campaign_id_text = query.data.split("_")
        campaign_id = int(campaign_id_text)
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الحملة.")
        return
    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["status"] != "pending_review":
        await query.edit_message_text("هذه الحملة لم تعد بانتظار المراجعة.")
        return
    if action == "approve":
        await db.set_campaign_status(campaign_id, "approved")
        await query.edit_message_text(
            f"✅ تم اعتماد الحملة #{campaign_id}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📣 نشر الحملة", callback_data=f"admin_send_{campaign_id}")]]),
        )
    else:
        await db.set_campaign_status(campaign_id, "rejected")
        await query.edit_message_text(f"❌ تم رفض الحملة #{campaign_id}.")


async def admin_send_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("بدأ النشر...")
    if not is_admin(update):
        await query.edit_message_text("غير مصرح لك باستخدام لوحة الإدارة.")
        return
    try:
        campaign_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الحملة.")
        return
    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["status"] != "approved":
        await query.edit_message_text("الحملة غير موجودة أو غير معتمدة.")
        return
    targets = await db.find_target_users(campaign["city_id"], campaign["category_id"])
    sent = 0
    for i in range(0, len(targets), BATCH_SIZE):
        for user in targets[i:i + BATCH_SIZE]:
            try:
                await context.bot.send_message(
                    user["telegram_id"], campaign["description"],
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🎁 احصل على كود الخصم", callback_data=f"getcode_{campaign_id}")
                    ]]),
                )
                await db.log_event(campaign_id, user["id"], "SENT")
                sent += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("فشل إرسال الحملة %s إلى %s: %s", campaign_id, user["telegram_id"], exc)
        await asyncio.sleep(BATCH_DELAY_SECONDS)
    await db.mark_campaign_published(campaign_id)
    await query.edit_message_text(f"✅ تم نشر الحملة #{campaign_id} وإرسالها إلى {sent} مستخدم.")


# =================================================================
# تدفّق المستخدم (العميل)
# =================================================================
async def customer_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city_id = int(query.data.split("_")[1])
    await db.set_user_city(context.user_data["db_user_id"], city_id)
    context.user_data["selected_categories"] = set()

    await render_category_picker(query, context)
    return CUST_CATEGORIES


async def render_category_picker(query, context):
    categories = await db.list_categories()
    selected = context.user_data.get("selected_categories", set())
    keyboard = []
    for c in categories:
        mark = "✅ " if c["id"] in selected else ""
        keyboard.append([InlineKeyboardButton(f"{mark}{c['name']}", callback_data=f"cat_{c['id']}")])
    keyboard.append([InlineKeyboardButton("✔️ تأكيد الاشتراك", callback_data="cat_confirm")])
    await query.edit_message_text(
        "اختر اهتماماتك (يمكن اختيار أكثر من واحد):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def customer_category_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cat_confirm":
        selected = context.user_data.get("selected_categories", set())
        if not selected:
            await query.answer("اختر فئة واحدة على الأقل", show_alert=True)
            return CUST_CATEGORIES
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
            ]),
        )
        return ConversationHandler.END

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
    await query.answer()
    context.user_data["biz_type"] = query.data.split("_", 1)[1]
    cities = await db.list_cities()
    keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"bizcity_{c['id']}")] for c in cities]
    await query.edit_message_text("في أي مدينة يقع النشاط؟", reply_markup=InlineKeyboardMarkup(keyboard))
    return MER_BIZ_CITY


async def merchant_biz_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        "✅ تم تسجيل نشاطك التجاري (بانتظار مراجعة الإدارة للنشاطات الجديدة)."
    )
    return await show_merchant_menu(update, context)


async def show_merchant_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ إنشاء حملة", callback_data="new_campaign")],
        [InlineKeyboardButton("📊 حالة حملاتي", callback_data="my_campaigns")],
        [InlineKeyboardButton("🎟️ تحقق من كود خصم", callback_data="redeem_menu")],
    ]
    text = "لوحة التاجر — ماذا تريد أن تفعل؟"
    reply_keyboard = ReplyKeyboardMarkup(
        [["➕ إنشاء حملة", "📊 حالة حملاتي"], ["🎟️ تحقق من كود خصم"], ["🏠 القائمة الرئيسية"]],
        resize_keyboard=True,
        is_persistent=True,
    )
    if hasattr(update_or_query, "message") and update_or_query.message is None:
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        await update_or_query.message.reply_text("استخدم القائمة أسفل الشاشة للتعامل مع البوت.", reply_markup=reply_keyboard)
    else:
        target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
        await target.reply_text(text, reply_markup=reply_keyboard)
    return MER_MENU


async def merchant_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "new_campaign":
        await query.edit_message_text(
            "✍️ اكتب وصفاً حراً للعرض (مثال: عندنا خصم 30% على الوجبات البحرية "
            "من الخميس للسبت، والمطعم في الخرطوم بحري)."
        )
        return CAMPAIGN_DESC

    if query.data == "redeem_menu":
        await query.edit_message_text("🎟️ أرسل كود الخصم الذي قدمه لك العميل للتحقق منه:")
        return REDEEM_CODE

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
            lines = [f"#{r['id']} — {r['title']} — {r['status']}" for r in rows]
            await query.edit_message_text("حملاتك:\n" + "\n".join(lines))
        return MER_MENU

    return MER_MENU


async def merchant_text_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نفس خيارات لوحة التاجر لكن من لوحة المفاتيح العربية."""
    text = (update.message.text or "").strip()
    if text == "➕ إنشاء حملة":
        await update.message.reply_text(
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
            lines = [f"#{r['id']} — {r['title']} — {r['status']}" for r in rows]
            await update.message.reply_text("حملاتك:\n" + "\n".join(lines))
        return MER_MENU
    if text == "🏠 القائمة الرئيسية":
        return await start(update, context)
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
    await query.answer()
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    business = await db.get_business_by_user(user["id"]) if user else None
    if not business:
        await query.edit_message_text("لا يوجد نشاط تجاري مسجل لهذا الحساب.")
        return
    await query.edit_message_text("🎟️ أرسل كود الخصم الذي قدمه لك العميل للتحقق منه:")
    context.user_data["awaiting_redeem_code"] = True


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
    raw_text = update.message.text.strip()
    await update.message.reply_text("⏳ جارٍ تحليل العرض بواسطة الذكاء الاصطناعي...")

    data = await ai.extract_campaign_data(raw_text)
    if data.get("issues"):
        issues_text = "\n".join(f"- {i}" for i in data["issues"])
        await update.message.reply_text(
            f"⚠️ يحتاج العرض توضيحاً قبل المتابعة:\n{issues_text}\n\n"
            "أعد كتابة الوصف بمعلومات أوضح (الخصم، المدينة، التاريخ)."
        )
        return CAMPAIGN_DESC

    ad_text = await ai.generate_ad_copy(data)
    context.user_data["pending_campaign"] = {"raw": raw_text, "ai_data": data, "ad_text": ad_text}

    await update.message.reply_text(
        f"📢 معاينة الإعلان:\n\n{ad_text}\n\n"
        f"الفئة: {data.get('category_code')} | المدينة: {data.get('city_code')} | "
        f"الخصم: {data.get('discount')}%",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ إرسال للمراجعة", callback_data="submit_campaign")],
                [InlineKeyboardButton("✏️ إعادة الكتابة", callback_data="rewrite_campaign")],
            ]
        ),
    )
    return CAMPAIGN_CONFIRM


async def campaign_confirm_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "rewrite_campaign":
        await query.edit_message_text("✍️ اكتب الوصف مرة أخرى:")
        return CAMPAIGN_DESC

    pending = context.user_data.get("pending_campaign")
    if not pending:
        await query.edit_message_text("انتهت صلاحية المعاينة، ابدأ حملة جديدة من القائمة.")
        return await show_merchant_menu(query, context)

    data = pending["ai_data"]
    business_id = context.user_data.get("business_id")
    if not business_id:
        business = await db.get_business_by_user(context.user_data["db_user_id"])
        if business:
            business_id = business["id"]
            context.user_data["business_id"] = business_id
    if not business_id:
        await query.edit_message_text("لا يوجد نشاط تجاري مسجل. أعد تسجيل نشاطك من /start.")
        return ConversationHandler.END

    category_row = await db.pool().fetchrow(
        "SELECT id FROM categories WHERE code=$1", data.get("category_code")
    )
    city_row = await db.pool().fetchrow("SELECT id FROM cities WHERE code=$1", data.get("city_code"))

    campaign = await db.create_campaign(
        business_id=business_id,
        raw_input=pending["raw"],
        ai_data=data,
        ad_text=pending["ad_text"],
        category_id=category_row["id"] if category_row else None,
        city_id=city_row["id"] if city_row else None,
    )
    context.user_data.pop("pending_campaign", None)

    await query.edit_message_text(
        f"✅ تم إرسال الحملة #{campaign['id']} لمراجعة الإدارة. سنبلغك عند اعتمادها."
    )
    await notify_admins_new_campaign(context, campaign["id"])
    return await show_merchant_menu(query, context)


async def notify_admins_new_campaign(context: ContextTypes.DEFAULT_TYPE, campaign_id: int):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"🆕 حملة جديدة بانتظار المراجعة: #{campaign_id}\nاستخدم /pending لعرض التفاصيل.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("تعذر إشعار الأدمن %s: %s", admin_id, e)


# =================================================================
# أوامر الإدارة — مراجعة، اعتماد، رفض، نشر، تقرير
# =================================================================
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    rows = await db.list_pending_campaigns()
    if not rows:
        await update.message.reply_text("لا توجد حملات بانتظار المراجعة.")
        return
    for r in rows:
        text = (
            f"#{r['id']} — {r['business_name']}\n{r['description']}\n\n"
            f"/approve_{r['id']}  /reject_{r['id']}"
        )
        await update.message.reply_text(text)


async def cmd_approve_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    cmd = update.message.text.lstrip("/")
    action, _, campaign_id_str = cmd.partition("_")
    try:
        campaign_id = int(campaign_id_str)
    except ValueError:
        await update.message.reply_text("صيغة غير صحيحة. استخدم /approve_123 أو /reject_123")
        return

    status = "approved" if action == "approve" else "rejected"
    await db.set_campaign_status(campaign_id, status)
    await update.message.reply_text(f"تم تحديث الحملة #{campaign_id} إلى: {status}")

    if status == "approved":
        await update.message.reply_text(f"لنشر الحملة الآن استخدم: /send_{campaign_id}")


async def cmd_send_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نشر حملة معتمدة على المستخدمين المستهدفين، على دفعات (queue)."""
    if not is_admin(update):
        return
    cmd = update.message.text.lstrip("/")
    _, _, campaign_id_str = cmd.partition("_")
    try:
        campaign_id = int(campaign_id_str)
    except ValueError:
        await update.message.reply_text("صيغة غير صحيحة. استخدم /send_123")
        return

    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["status"] != "approved":
        await update.message.reply_text("الحملة غير موجودة أو غير معتمدة بعد.")
        return

    targets = await db.find_target_users(campaign["city_id"], campaign["category_id"])
    await update.message.reply_text(f"سيتم الإرسال إلى {len(targets)} مستخدم على دفعات...")

    sent = 0
    for i in range(0, len(targets), BATCH_SIZE):
        batch = targets[i : i + BATCH_SIZE]
        for u in batch:
            try:
                await context.bot.send_message(
                    u["telegram_id"],
                    campaign["description"],
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🎁 احصل على كود الخصم", callback_data=f"getcode_{campaign_id}")]]
                    ),
                )
                await db.log_event(campaign_id, u["id"], "SENT")
                sent += 1
            except Exception as e:  # noqa: BLE001
                log.warning("فشل الإرسال للمستخدم %s: %s", u["telegram_id"], e)
        await asyncio.sleep(BATCH_DELAY_SECONDS)

    await db.mark_campaign_published(campaign_id)
    await update.message.reply_text(f"✅ تم نشر الحملة #{campaign_id}. تم الإرسال إلى {sent} مستخدم.")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("الاستخدام: /report <رقم الحملة>")
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
    await query.answer()
    try:
        campaign_id = int(query.data.split("_")[1])
    except (IndexError, ValueError):
        await query.answer("تعذر قراءة الحملة.", show_alert=True)
        return

    user_row = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user_row:
        await query.answer("الرجاء بدء البوت أولاً بالأمر /start", show_alert=True)
        return

    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["status"] != "active":
        await query.answer("هذا العرض غير متاح حاليًا.", show_alert=True)
        return
    city_row = await db.pool().fetchrow("SELECT code FROM cities WHERE id=$1", campaign["city_id"])
    city_code = city_row["code"] if city_row else "OFFER"

    await db.log_event(campaign_id, user_row["id"], "CLICKED")
    code_row = await db.create_discount_code(campaign_id, user_row["id"], city_code)

    await query.message.reply_text(
        f"🎁 كودك: `{code_row['code']}`\nقدّمه للتاجر عند الشراء للاستفادة من الخصم.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def customer_directory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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


async def customer_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    # سوق الأفراد عام؛ العنوان يكتبه المعلن داخل تفاصيل الإعلان.
    listings = await db.list_public_listings()
    if not listings:
        await query.edit_message_text(
            "🛒 لا توجد أغراض منشورة في مدينتك حاليًا.\n"
            "يمكنك أن تكون أول من ينشر غرضًا!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ نشر غرض للبيع", callback_data="listing_start")
            ]]),
        )
        return
    await query.edit_message_text("🛒 أحدث الأغراض المعروضة في مدينتك:")
    for listing in listings:
        if user:
            await db.log_listing_event(listing["id"], user["id"], "VIEW")
        price = str(listing["price"]) if listing["price"] is not None else "السعر عند التواصل"
        await query.message.reply_text(
            f"📦 {listing['title']}\nالسعر: {price}\n"
            f"الحالة: {listing['condition'] or 'غير محددة'}\n"
            f"📍 العنوان: {listing['address'] or 'غير محدد'}\n"
            f"📞 التواصل: {listing['contact'] or 'غير محدد'}\n"
            f"🚚 التوصيل: {listing['delivery'] or 'غير محدد'}\n\n{listing['description'] or ''}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 تواصل مع المعلن", callback_data=f"listing_contact_{listing['id']}"),
            ]]),
        )
    await query.message.reply_text(
        "هل تريد بيع غرض؟ النشر مجاني خلال فترة الإطلاق.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ نشر غرض للبيع", callback_data="listing_start")
        ]]),
    )


async def listing_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        listing_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة الإعلان.")
        return
    listing = await db.get_listing(listing_id)
    buyer = await db.get_user_by_telegram_id(update.effective_user.id)
    if not listing or listing["status"] != "approved":
        await query.answer("الإعلان غير متاح حاليًا.", show_alert=True)
        return
    owner = await db.get_user_by_id(listing["user_id"])
    await db.log_listing_event(listing_id, buyer["id"] if buyer else None, "CONTACT")
    if owner:
        await context.bot.send_message(
            owner["telegram_id"],
            f"📩 يوجد مستخدم مهتم بإعلانك #{listing_id}: {listing['title']}\n"
            "يمكنك التواصل معه عبر Telegram.",
        )
    await query.message.reply_text(
        f"للتواصل مع المعلن: {('@' + owner['username']) if owner and owner['username'] else 'سيصلك إشعار من المعلن قريبًا.'}"
    )


async def business_contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        business_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة النشاط.")
        return
    business = await db.get_business(business_id)
    buyer = await db.get_user_by_telegram_id(update.effective_user.id)
    if not business:
        await query.answer("النشاط غير متاح حاليًا.", show_alert=True)
        return
    await db.log_business_event(business_id, buyer["id"] if buyer else None, "CONTACT")
    await query.message.reply_text(
        f"للتواصل مع {business['business_name']}: {business['phone'] or 'لا يوجد رقم مسجل'}"
    )


async def listing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "🛒 لنشر غرضك، اكتب اسم الغرض بوضوح (مثال: هاتف سامسونج A54)."
        )
    else:
        await update.message.reply_text("🛒 لنشر غرضك، اكتب اسم الغرض بوضوح (مثال: هاتف سامسونج A54).")
    return LISTING_TITLE


async def listing_title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip()
    if len(title) < 3 or len(title) > 120:
        await update.message.reply_text("اكتب اسمًا بين 3 و120 حرفًا.")
        return LISTING_TITLE
    context.user_data["listing_title"] = title
    await update.message.reply_text("اكتب وصف الغرض ومواصفاته وحالته بالتفصيل.")
    return LISTING_DESCRIPTION


async def listing_description_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = (update.message.text or "").strip()
    if len(description) < 5:
        await update.message.reply_text("أضف وصفًا أوضح ليسهل على المشتري فهم الغرض.")
        return LISTING_DESCRIPTION
    context.user_data["listing_description"] = description
    await update.message.reply_text("ما السعر؟ اكتب الرقم فقط، أو اكتب: عند التواصل")
    return LISTING_PRICE


async def listing_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = (update.message.text or "").strip()
    if price not in {"عند التواصل", "غير محدد"}:
        try:
            if float(price.replace(",", ".")) < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("اكتب سعرًا صحيحًا أو اكتب: عند التواصل")
            return LISTING_PRICE
    context.user_data["listing_price"] = None if price in {"عند التواصل", "غير محدد"} else price
    await update.message.reply_text("اختر حالة الغرض:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("جديد", callback_data="listing_condition_جديد")],
        [InlineKeyboardButton("مستعمل", callback_data="listing_condition_مستعمل")],
    ]))
    return LISTING_CONDITION


async def listing_condition_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["listing_condition"] = query.data.rsplit("_", 1)[1]
    await query.edit_message_text("اكتب تصنيف الغرض (مثال: هواتف، أثاث، أجهزة كهربائية).")
    return LISTING_CATEGORY


async def listing_city_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["listing_city_id"] = int(query.data.rsplit("_", 1)[1])
    await query.edit_message_text("اكتب تصنيف الغرض (مثال: هواتف، أثاث، أجهزة كهربائية).")
    return LISTING_CATEGORY


async def listing_category_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["listing_category"] = (update.message.text or "").strip()[:80]
    await update.message.reply_text("اكتب عنوان أو موقع الغرض بالتفصيل، مثل الحي أو السوق.")
    return LISTING_ADDRESS


async def listing_address_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["listing_address"] = (update.message.text or "").strip()[:200]
    await update.message.reply_text("اكتب رقم الهاتف أو وسيلة التواصل التي تريد إظهارها للمشتري.")
    return LISTING_CONTACT


async def listing_contact_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["listing_contact"] = (update.message.text or "").strip()[:120]
    await update.message.reply_text("هل توجد خدمة توصيل؟ اكتب نعم مع التفاصيل أو لا.")
    return LISTING_DELIVERY


async def listing_delivery_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = context.user_data["db_user_id"]
    delivery = (update.message.text or "").strip()[:160]
    listing = await db.create_listing(
        user_id,
        context.user_data["listing_title"],
        context.user_data["listing_description"],
        context.user_data.get("listing_price"),
        context.user_data["listing_condition"],
        None,
        context.user_data["listing_category"],
        context.user_data["listing_address"],
        context.user_data["listing_contact"],
        delivery,
    )
    await update.message.reply_text(
        f"✅ تم استلام إعلانك رقم #{listing['id']} وأصبح قيد مراجعة الإدارة.\n"
        "سيظهر في سوق الأفراد بعد اعتماده. النشر مجاني خلال فترة الإطلاق."
    )
    for key in list(context.user_data):
        if key.startswith("listing_"):
            context.user_data.pop(key, None)
    return ConversationHandler.END


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستخدمها التاجر/الموظف: /redeem KHART-8F21"""
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("الاستخدام: /redeem <الكود>")
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


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء. أرسل /start للبدء من جديد.")
    return ConversationHandler.END


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


def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()

    # هذه المعالجات تسبق المحادثة حتى يعمل زر التحقق بعد إعادة تشغيل البوت أيضًا.
    application.add_handler(MessageHandler(
        filters.Regex(r"^🎟️ تحقق من كود خصم$"), merchant_redeem_keyboard_entry
    ), group=0)
    application.add_handler(CallbackQueryHandler(
        merchant_redeem_prompt_fallback, pattern=r"^redeem_menu$"
    ), group=0)
    application.add_handler(MessageHandler(
        filters.Regex(r"^[A-Za-z]{2,10}-[A-Za-z0-9]{4,12}$"), merchant_redeem_fallback_message
    ), group=0)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(listing_start, pattern="^listing_start$"),
        ],
        states={
            SELECT_ROLE: [
                CallbackQueryHandler(admin_pending_callback, pattern="^admin_pending$"),
                CallbackQueryHandler(admin_listings_callback, pattern="^admin_listings$"),
                CallbackQueryHandler(admin_reports_callback, pattern="^admin_reports$"),
                CallbackQueryHandler(admin_campaign_action, pattern=r"^admin_(approve|reject)_\d+$"),
                CallbackQueryHandler(admin_listing_action, pattern=r"^listing_(approve|reject)_\d+$"),
                CallbackQueryHandler(admin_send_callback, pattern=r"^admin_send_\d+$"),
                CallbackQueryHandler(customer_market_callback, pattern="^customer_market$"),
                CallbackQueryHandler(role_router),
                CallbackQueryHandler(listing_start, pattern="^listing_start$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text_role_router),
            ],
            CUST_CITY: [CallbackQueryHandler(customer_city_chosen, pattern="^city_")],
            CUST_CATEGORIES: [CallbackQueryHandler(customer_category_toggle, pattern="^cat_")],
            MER_BIZ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_biz_name)],
            MER_BIZ_TYPE: [CallbackQueryHandler(merchant_biz_type, pattern="^biztype_")],
            MER_BIZ_CITY: [CallbackQueryHandler(merchant_biz_city, pattern="^bizcity_")],
            MER_BIZ_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_biz_phone)],
            MER_MENU: [
                CallbackQueryHandler(merchant_menu_router),
                MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_text_menu_router),
            ],
            REDEEM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_redeem_code_received)],
            LISTING_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_title_received)],
            LISTING_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_description_received)],
            LISTING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_price_received)],
            LISTING_CONDITION: [CallbackQueryHandler(listing_condition_received, pattern="^listing_condition_")],
            LISTING_CITY: [CallbackQueryHandler(listing_city_received, pattern="^listing_city_")],
            LISTING_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_category_received)],
            LISTING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_address_received)],
            LISTING_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_contact_received)],
            LISTING_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, listing_delivery_received)],
            CAMPAIGN_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_description_received)],
            CAMPAIGN_CONFIRM: [CallbackQueryHandler(campaign_confirm_router)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    application.add_handler(conv)

    # أوامر الإدارة (خارج المحادثة الرئيسية)
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("report", cmd_report))
    application.add_handler(CommandHandler("redeem", cmd_redeem))
    application.add_handler(
        MessageHandler(filters.Regex(r"^/approve_\d+$") | filters.Regex(r"^/reject_\d+$"), cmd_approve_reject)
    )
    application.add_handler(MessageHandler(filters.Regex(r"^/send_\d+$"), cmd_send_campaign))

    # استلام كود الخصم من رسالة الحملة المُرسلة للعميل
    application.add_handler(CallbackQueryHandler(admin_pending_callback, pattern="^admin_pending$"))
    application.add_handler(CallbackQueryHandler(admin_listings_callback, pattern="^admin_listings$"))
    application.add_handler(CallbackQueryHandler(admin_reports_callback, pattern="^admin_reports$"))
    application.add_handler(CallbackQueryHandler(admin_campaign_action, pattern=r"^admin_(approve|reject)_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_listing_action, pattern=r"^listing_(approve|reject)_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_send_callback, pattern=r"^admin_send_\d+$"))
    application.add_handler(CallbackQueryHandler(get_discount_code, pattern="^getcode_"))
    application.add_handler(CallbackQueryHandler(merchant_redeem_prompt_fallback, pattern="^redeem_menu$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_redeem_fallback_message))
    application.add_handler(CallbackQueryHandler(customer_directory_callback, pattern="^customer_directory$"))
    application.add_handler(CallbackQueryHandler(customer_market_callback, pattern="^customer_market$"))
    application.add_handler(CallbackQueryHandler(listing_contact_callback, pattern=r"^listing_contact_\d+$"))
    application.add_handler(CallbackQueryHandler(business_contact_callback, pattern=r"^business_contact_\d+$"))
    application.add_handler(CallbackQueryHandler(listing_start, pattern="^listing_start$"))
    application.add_error_handler(on_error)

    return application


def main():
    app = build_application()
    log.info("البوت يعمل الآن (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
