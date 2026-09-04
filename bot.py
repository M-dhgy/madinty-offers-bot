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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        [InlineKeyboardButton("🏪 صاحب نشاط تجاري", callback_data="role_merchant")],
        [InlineKeyboardButton("ℹ️ كيف يعمل؟", callback_data="how_it_works")],
        [InlineKeyboardButton("📞 تواصل معنا", callback_data="contact_us")],
    ]
    await update.message.reply_text(
        "أهلاً بك في *عروض مدينتي* 👋\n\nاختر نوع حسابك:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SELECT_ROLE


async def role_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

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
            return await show_merchant_menu(query, context)
        await query.edit_message_text("لنسجّل نشاطك التجاري.\n\nما اسم النشاط؟")
        return MER_BIZ_NAME

    return SELECT_ROLE


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
            "🎉 تم تسجيلك بنجاح! سنرسل لك عروضاً من مدينتك حسب اهتماماتك."
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
    ]
    text = "لوحة التاجر — ماذا تريد أن تفعل؟"
    if hasattr(update_or_query, "message") and update_or_query.message is None:
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        target = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
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
    category_row = await db.pool().fetchrow(
        "SELECT id FROM categories WHERE code=$1", data.get("category_code")
    )
    city_row = await db.pool().fetchrow("SELECT id FROM cities WHERE code=$1", data.get("city_code"))

    campaign = await db.create_campaign(
        business_id=context.user_data["business_id"],
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
    campaign_id = int(campaign_id_str)

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
    campaign_id = int(parts[1])
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
    campaign_id = int(query.data.split("_")[1])

    user_row = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user_row:
        await query.answer("الرجاء بدء البوت أولاً بالأمر /start", show_alert=True)
        return

    campaign = await db.get_campaign(campaign_id)
    city_row = await db.pool().fetchrow("SELECT code FROM cities WHERE id=$1", campaign["city_id"])
    city_code = city_row["code"] if city_row else "OFFER"

    await db.log_event(campaign_id, user_row["id"], "CLICKED")
    code_row = await db.create_discount_code(campaign_id, user_row["id"], city_code)

    await query.message.reply_text(
        f"🎁 كودك: `{code_row['code']}`\nقدّمه للتاجر عند الشراء للاستفادة من الخصم.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستخدمها التاجر/الموظف: /redeem KHART-8F21"""
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("الاستخدام: /redeem <الكود>")
        return
    row = await db.redeem_code(parts[1].upper())
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


async def post_shutdown(application: Application):
    await db.close_pool()


def build_application() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    application = Application.builder().token(token).post_init(post_init).post_shutdown(post_shutdown).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_ROLE: [CallbackQueryHandler(role_router)],
            CUST_CITY: [CallbackQueryHandler(customer_city_chosen, pattern="^city_")],
            CUST_CATEGORIES: [CallbackQueryHandler(customer_category_toggle, pattern="^cat_")],
            MER_BIZ_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_biz_name)],
            MER_BIZ_TYPE: [CallbackQueryHandler(merchant_biz_type, pattern="^biztype_")],
            MER_BIZ_CITY: [CallbackQueryHandler(merchant_biz_city, pattern="^bizcity_")],
            MER_BIZ_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, merchant_biz_phone)],
            MER_MENU: [CallbackQueryHandler(merchant_menu_router)],
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
    application.add_handler(CallbackQueryHandler(get_discount_code, pattern="^getcode_"))

    return application


def main():
    app = build_application()
    log.info("البوت يعمل الآن (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
