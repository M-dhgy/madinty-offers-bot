"""Clean individual marketplace flow for Madinty Offers.

One state machine owns marketplace interactions:
market menu -> buy city+query OR sell city+photo+caption -> review -> confirmation.
Rejected listings can be edited and resubmitted by their owner with /editlisting ID.
"""
import logging
import asyncio
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

import ai
import db

log = logging.getLogger(__name__)

MARKET_MENU = 40
SELL_CITY = 41
SELL_MESSAGE = 42
SELL_CONFIRM = 43
BUY_CITY = 44
BUY_QUERY = 45
EDIT_LISTING_CITY = 46
EDIT_LISTING_MESSAGE = 47
EDIT_LISTING_CONFIRM = 48


async def _safe_answer(query, message=None, show_alert=False):
    """A Telegram callback-ack timeout must not prevent the requested action."""
    try:
        if message is None and not show_alert:
            await query.answer()
        else:
            await query.answer(message, show_alert=show_alert)
    except TimedOut:
        log.warning("انتهت مهلة تأكيد ضغط زر في Telegram؛ سيستمر تنفيذ الإجراء")


def _user_id(context):
    return context.user_data.get("db_user_id")


async def _ensure_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _user_id(context):
        return _user_id(context)
    user = update.effective_user
    if not user:
        return None
    row = await db.create_user(user.id, user.username, user.first_name)
    context.user_data["db_user_id"] = row["id"]
    return row["id"]


def _city_keyboard(cities, callback_prefix: str):
    keyboard = [
        [InlineKeyboardButton(city["name"], callback_data=f"{callback_prefix}{city['id']}")]
        for city in cities
    ]
    keyboard.append([
        InlineKeyboardButton("↩️ رجوع", callback_data="customer_market"),
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(keyboard)


def _market_navigation_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ رجوع", callback_data="customer_market"),
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ]])


def _market_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ شراء", callback_data="market_buy")],
        [InlineKeyboardButton("🏷️ بيع", callback_data="market_sell")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])


def _listing_prompt() -> str:
    return (
        "أرسل صورة الغرض مع كتابة جميع التفاصيل في شرح الصورة في رسالة واحدة.\n\n"
        "المعلومات المطلوبة: اسم الغرض، العنوان أو مكان وجوده، ورقم التواصل. "
        "ويمكنك إضافة الوصف والسعر والحالة والتفاوض والتوصيل.\n\n"
        "مثال: هاتف سامسونج A54 مستعمل بحالة ممتازة. السعر 800000 جنيه، "
        "قابل للتفاوض. الموقع: بحري السوق العربي. التواصل: 09••••••••. "
        "التوصيل: لا توجد خدمة توصيل."
    )


def _missing_listing_fields(parsed: dict) -> list[str]:
    """Reject required fields unless the model returned non-empty strings."""
    required = (
        ("اسم الغرض", "title"),
        ("العنوان والموقع", "address"),
        ("رقم التواصل", "contact"),
    )
    missing = []
    for label, key in required:
        value = parsed.get(key)
        if not isinstance(value, str) or not value.strip():
            missing.append(label)
    return missing


def _optional_string(parsed: dict, key: str, default: str) -> str:
    value = parsed.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else default


def _optional_price(parsed: dict) -> str:
    value = parsed.get("price")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if type(value) in {int, float}:
        return str(value)
    return "عند التواصل"


def _message_reference(message, chat_id=None):
    message_id = getattr(message, "message_id", None)
    if chat_id is None:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None) or getattr(message, "chat_id", None)
    if type(chat_id) is int and type(message_id) is int:
        return {"chat_id": chat_id, "message_id": message_id}
    return None


async def _invalidate_message_buttons(context, key: str):
    reference = context.user_data.pop(key, None)
    if not reference or not getattr(context, "bot", None):
        return
    try:
        await context.bot.edit_message_reply_markup(**reference, reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("تعذر حذف أزرار المعاينة القديمة: %s", exc)


def _market_draft_discard_markup(
    preview_id: str, start_new_listing: bool = False, resumable_preview: bool = True
):
    discard_action = "market_sell_discard" if start_new_listing else "market_draft_discard"
    discard_label = (
        "🗑️ إلغاء المسودة وبدء إعلان جديد"
        if start_new_listing else "🗑️ إلغاء المسودة غير المرسلة"
    )
    rows = []
    if resumable_preview:
        rows.append([InlineKeyboardButton("📄 متابعة المسودة الحالية", callback_data="resume_listing_flow")])
    else:
        rows.append([InlineKeyboardButton("📋 إعلاناتي", callback_data="mylistings")])
    rows.extend([
        [InlineKeyboardButton(discard_label, callback_data=f"{discard_action}_{preview_id}")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])
    return InlineKeyboardMarkup(rows)


async def _remove_stale_buttons(query, message_ar: str):
    await _safe_answer(query, message_ar, show_alert=True)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log.debug("تعذر إزالة أزرار قديمة من الرسالة: %s", exc)


def _search_actions_keyboard(has_more: bool, search_id: str):
    actions = []
    if has_more:
        actions.append([InlineKeyboardButton("📄 المزيد من الإعلانات", callback_data=f"market_more_{search_id}")])
    actions.extend([
        [InlineKeyboardButton("🔎 بحث جديد", callback_data="market_new_search")],
        [InlineKeyboardButton("🏙 اختيار مدينة أخرى", callback_data="market_buy")],
        [InlineKeyboardButton("↩️ رجوع", callback_data="customer_market"),
         InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])
    return InlineKeyboardMarkup(actions)


async def market_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _ensure_user(update, context)
    context.user_data["navigation_section"] = "marketplace"
    context.user_data["navigation_parent"] = "main"
    text = "🛒 سوق الأفراد\n\nاختر العملية المطلوبة:"
    if query:
        await _safe_answer(query)
        await query.edit_message_text(text, reply_markup=_market_menu_markup())
    else:
        await update.message.reply_text(text, reply_markup=_market_menu_markup())
    return MARKET_MENU


async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    draft = context.user_data.get("market_draft")
    if draft or context.user_data.get("resubmitting_listing_id"):
        preview_id = (draft or {}).get("preview_id") or context.user_data.get("market_preview_id")
        if not preview_id:
            preview_id = uuid4().hex[:12]
        if draft:
            draft["preview_id"] = preview_id
        context.user_data["market_preview_id"] = preview_id
        title = (
            (draft or {}).get("title")
            or context.user_data.get("resubmitting_listing_title")
            or "إعلان"
        )
        resumable_preview = True
        await query.edit_message_text(
            f"لديك مسودة غير مرسلة: {title}. لم أستبدلها. يمكنك استئنافها أو إلغاؤها صراحةً قبل بدء إعلان جديد.",
            reply_markup=_market_draft_discard_markup(
                preview_id, start_new_listing=True, resumable_preview=resumable_preview
            ),
        )
        return MARKET_MENU
    return await _begin_new_listing(query, context)


async def _begin_new_listing(query, context):
    context.user_data["navigation_parent"] = "market_menu"
    cities = await db.list_cities()
    if not cities:
        await query.edit_message_text("لا توجد مدن متاحة حاليًا لنشر إعلان.")
        return MARKET_MENU
    await _invalidate_message_buttons(context, "market_preview_message")
    context.user_data.pop("resubmitting_listing_id", None)
    context.user_data.pop("resubmitting_listing_title", None)
    context.user_data.pop("edit_listing_city_id", None)
    context.user_data.pop("market_city_id", None)
    context.user_data.pop("market_draft", None)
    context.user_data.pop("market_preview_id", None)
    await query.edit_message_text(
        "🏷️ نشر غرض للبيع\n\nاختر المدينة التي يوجد فيها الغرض:",
        reply_markup=_city_keyboard(cities, "market_sellcity_"),
    )
    return SELL_CITY


async def discard_market_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    expected = context.user_data.get("market_preview_id")
    requested = query.data.rsplit("_", 1)[-1]
    if not expected or requested != expected:
        await _remove_stale_buttons(query, "انتهت صلاحية هذا الإجراء؛ افتح أحدث قائمة للإعلانات.")
        return MARKET_MENU
    await _invalidate_message_buttons(context, "market_preview_message")
    for key in (
        "market_draft", "market_preview_id", "market_preview_message", "market_city_id",
        "resubmitting_listing_id", "resubmitting_listing_title", "edit_listing_city_id",
    ):
        context.user_data.pop(key, None)
    context.user_data["navigation_parent"] = "main"
    await query.edit_message_text(
        "أُلغيت المسودة غير المرسلة فقط؛ لم يتغير أي إعلان محفوظ. أعد اختيار تعديل الإعلان أو ابدأ إعلانًا جديدًا.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 إعلاناتي", callback_data="mylistings")],
            [InlineKeyboardButton("🛒 سوق الأفراد", callback_data="customer_market")],
            [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
        ]),
    )
    return MARKET_MENU


async def sell_discard_and_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    expected = context.user_data.get("market_preview_id")
    requested = query.data.rsplit("_", 1)[-1]
    if not expected or requested != expected:
        await _remove_stale_buttons(query, "انتهت صلاحية هذا الإجراء؛ استخدم أحدث معاينة.")
        return MARKET_MENU
    await _invalidate_message_buttons(context, "market_preview_message")
    for key in (
        "market_draft", "market_preview_id", "market_preview_message", "market_city_id",
        "resubmitting_listing_id", "resubmitting_listing_title", "edit_listing_city_id",
    ):
        context.user_data.pop(key, None)
    return await _begin_new_listing(query, context)


async def sell_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        city_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة المدينة. ابدأ النشر من جديد.")
        return MARKET_MENU
    if city_id not in {city["id"] for city in await db.list_cities()}:
        await query.edit_message_text("هذه المدينة غير متاحة حاليًا. ابدأ النشر من جديد.")
        return MARKET_MENU
    context.user_data["market_city_id"] = city_id
    await query.edit_message_text(_listing_prompt(), reply_markup=_market_navigation_markup())
    return SELL_MESSAGE


async def edit_listing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = getattr(update, "callback_query", None)
    if query:
        await _safe_answer(query)
        raw_id = query.data.rsplit("_", 1)[1]
    else:
        args = getattr(context, "args", None) or []
        raw_id = args[0] if len(args) == 1 else ""
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    user_id = user["id"] if user else await _ensure_user(update, context)
    if user_id:
        context.user_data["db_user_id"] = user_id
    if not user_id:
        if query:
            await query.edit_message_text("تعذر تسجيل حسابك مؤقتًا. أعد المحاولة لاحقًا.")
        else:
            await update.message.reply_text("تعذر تسجيل حسابك مؤقتًا. أعد المحاولة لاحقًا.")
        return ConversationHandler.END
    if not raw_id.isdigit():
        text = "اختر إعلانًا مرفوضًا من قائمة إعلاناتك لتعديله، أو ارجع إلى القائمة الرئيسية."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return ConversationHandler.END
    listing_id = int(raw_id)
    listing = await db.get_listing(listing_id)
    if not listing or listing["user_id"] != user_id:
        text = "هذا الإعلان غير موجود أو لا يخص حسابك."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return ConversationHandler.END
    if listing["status"] != "rejected":
        text = "يمكن تعديل الإعلانات المرفوضة فقط."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return ConversationHandler.END
    cities = await db.list_cities()
    if not cities:
        text = "لا توجد مدن متاحة حاليًا لإعادة إرسال الإعلان."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return ConversationHandler.END
    existing_draft = context.user_data.get("market_draft")
    active_edit_id = context.user_data.get("resubmitting_listing_id")
    if existing_draft or active_edit_id:
        preview_id = (existing_draft or {}).get("preview_id") or context.user_data.get("market_preview_id")
        if not preview_id:
            preview_id = uuid4().hex[:12]
        if existing_draft:
            existing_draft["preview_id"] = preview_id
        context.user_data["market_preview_id"] = preview_id
        context.user_data["navigation_section"] = "marketplace"
        context.user_data["navigation_parent"] = "listings"
        current_title = (existing_draft or {}).get("title") or context.user_data.get("resubmitting_listing_title") or "إعلان"
        text = f"لديك عملية غير مكتملة للإعلان {current_title}. لم أستبدلها بتعديل الإعلان #{listing_id}. ألغِها صراحةً ثم أعد طلب التعديل."
        markup = _market_draft_discard_markup(
            preview_id, resumable_preview=True
        )
        if query:
            await query.edit_message_text(text, reply_markup=markup)
        else:
            await update.message.reply_text(text, reply_markup=markup)
        return MARKET_MENU
    context.user_data["resubmitting_listing_id"] = listing_id
    context.user_data["resubmitting_listing_title"] = listing["title"]
    context.user_data.pop("market_city_id", None)
    context.user_data.pop("edit_listing_city_id", None)
    context.user_data["navigation_parent"] = "listings"
    context.user_data["navigation_section"] = "marketplace"
    await _invalidate_message_buttons(context, "market_preview_message")
    context.user_data.pop("market_preview_id", None)
    context.user_data.pop("market_draft", None)
    text = (
        f"✏️ تعديل الإعلان #{listing_id}: {listing['title']}\n\n"
        "اختر المدينة التي يوجد فيها الغرض، ثم أرسل الصورة والتفاصيل المصححة."
    )
    markup = _city_keyboard(cities, "market_editcity_")
    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)
    return EDIT_LISTING_CITY


async def edit_listing_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    try:
        city_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text(
            "تعذر قراءة المدينة. ابدأ من سوق الأفراد مرة أخرى.",
            reply_markup=_market_menu_markup(),
        )
        return MARKET_MENU
    if city_id not in {city["id"] for city in await db.list_cities()}:
        await query.edit_message_text(
            "هذه المدينة غير متاحة حاليًا. ابدأ من سوق الأفراد مرة أخرى.",
            reply_markup=_market_menu_markup(),
        )
        return MARKET_MENU
    context.user_data["edit_listing_city_id"] = city_id
    await query.edit_message_text(_listing_prompt(), reply_markup=_market_navigation_markup())
    return EDIT_LISTING_MESSAGE


async def sell_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.photo or not (message.caption or "").strip():
        await message.reply_text(
            "⚠️ أرسل صورة الغرض مع جميع التفاصيل في شرح الصورة في رسالة واحدة.",
            reply_markup=_market_navigation_markup(),
        )
        return EDIT_LISTING_MESSAGE if context.user_data.get("resubmitting_listing_id") else SELL_MESSAGE

    await message.reply_text("⏳ جارٍ تحليل الإعلان، سأعرض النتيجة فور اكتمالها.")
    parsed = await ai.extract_listing_data(message.caption.strip())
    parsed = parsed if isinstance(parsed, dict) else {}
    # The model's `issues` can contradict its own extracted fields. Progress is
    # therefore gated only by the actual required values, not by that advisory.
    missing = _missing_listing_fields(parsed)
    is_edit = bool(context.user_data.get("resubmitting_listing_id"))
    message_state = EDIT_LISTING_MESSAGE if is_edit else SELL_MESSAGE
    if missing:
        await message.reply_text(
            "⚠️ لا يمكن إرسال الإعلان بعد.\n\nالبيانات الناقصة أو غير الواضحة:\n- "
            + "\n- ".join(dict.fromkeys(missing))
            + "\n\nأرسل الصورة مع التفاصيل المصححة كاملة في رسالة واحدة."
        )
        return message_state

    try:
        reviewed = await asyncio.wait_for(ai.review_listing(parsed), timeout=8.0)
    except asyncio.TimeoutError:
        reviewed = {}
        log.info("تجاوزت مراجعة الإعلان الاختيارية مهلة 8 ثوانٍ؛ سيُستخدم النص المستخرج")
    reviewed = reviewed if isinstance(reviewed, dict) else {}
    # Review is used to polish the listing, not to override the deterministic
    # required-field check above with another possibly contradictory AI verdict.

    city_id = context.user_data.get("edit_listing_city_id" if is_edit else "market_city_id")
    if not city_id:
        await message.reply_text(
            "⚠️ اختر المدينة من سوق الأفراد أولًا، ثم أعد إرسال الإعلان.",
            reply_markup=_market_menu_markup(),
        )
        return MARKET_MENU
    context.user_data["market_draft"] = {
        "title": (reviewed.get("title") if isinstance(reviewed.get("title"), str) and reviewed["title"].strip() else parsed["title"].strip()),
        "description": (reviewed.get("description") if isinstance(reviewed.get("description"), str) and reviewed["description"].strip() else _optional_string(parsed, "description", "غير مذكور")),
        "price": _optional_price(parsed),
        "negotiable": parsed.get("negotiable") if type(parsed.get("negotiable")) is bool else False,
        "address": parsed["address"].strip(),
        "contact": parsed["contact"].strip(),
        "delivery": _optional_string(parsed, "delivery", "غير محدد"),
        "condition": _optional_string(parsed, "condition", "غير محددة"),
        "category": _optional_string(parsed, "category", "عام"),
        "city_id": city_id,
        "photo_ids": [message.photo[-1].file_id],
    }
    draft = context.user_data["market_draft"]
    cities = await db.list_cities()
    city_name = next((city["name"] for city in cities if city["id"] == city_id), "غير محددة")
    draft["city_name"] = city_name
    price = draft["price"]
    is_edit = bool(context.user_data.get("resubmitting_listing_id"))
    await _invalidate_message_buttons(context, "market_preview_message")
    preview_id = uuid4().hex[:12]
    draft["preview_id"] = preview_id
    draft.setdefault("submission_key", uuid4().hex)
    button_prefix = "listing" if is_edit else "market"
    preview_message = await message.reply_text(
        "راجع إعلانك قبل إرساله:\n\n"
        f"📦 {draft['title']}\n📝 {draft['description']}\n"
        f"💰 السعر: {price}\n🔄 التفاوض: {'قابل للتفاوض' if draft['negotiable'] else 'نهائي'}\n"
        f"📍 المدينة: {city_name} — {draft['address']}\n📞 {draft['contact']}\n🚚 {draft['delivery']}\n\n"
        "مدة الإعلان 72 ساعة تبدأ بعد الاعتماد.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ إرسال للمراجعة", callback_data=f"{button_prefix}_{preview_id}_submit")],
            [InlineKeyboardButton("✏️ إعادة الإدخال", callback_data=f"{button_prefix}_{preview_id}_resend"),
             InlineKeyboardButton("❌ إلغاء", callback_data=f"{button_prefix}_{preview_id}_cancel")],
            [InlineKeyboardButton("↩️ رجوع إلى سوق الأفراد", callback_data="customer_market"),
             InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
        ]),
    )
    context.user_data["market_preview_id"] = preview_id
    reference = _message_reference(preview_message, getattr(getattr(message, "chat", None), "id", None))
    if reference:
        context.user_data["market_preview_message"] = reference
    return EDIT_LISTING_CONFIRM if is_edit else SELL_CONFIRM


async def sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    is_edit = parts[0] == "listing"
    action = parts[-1]
    preview_id = parts[1] if len(parts) == 3 else None  # Legacy fixed callbacks are stale.
    draft = context.user_data.get("market_draft")
    current_preview_id = (draft or {}).get("preview_id")
    if not preview_id or preview_id != current_preview_id or preview_id != context.user_data.get("market_preview_id"):
        await _remove_stale_buttons(query, "انتهت صلاحية هذا الزر؛ استخدم أزرار أحدث معاينة.")
        return EDIT_LISTING_CONFIRM if context.user_data.get("resubmitting_listing_id") else SELL_CONFIRM
    await _safe_answer(query)
    if action == "cancel":
        context.user_data.pop("market_draft", None)
        context.user_data.pop("market_preview_id", None)
        context.user_data.pop("market_preview_message", None)
        context.user_data.pop("resubmitting_listing_id", None)
        context.user_data.pop("resubmitting_listing_title", None)
        context.user_data.pop("edit_listing_city_id", None)
        context.user_data.pop("market_city_id", None)
        await query.edit_message_text(
            ("تم إلغاء تعديل الإعلان." if is_edit else "تم إلغاء إنشاء الإعلان.")
            + "\n\nيمكنك بدء عملية أخرى الآن.",
            reply_markup=_market_menu_markup(),
        )
        context.user_data["navigation_parent"] = "main"
        return MARKET_MENU
    if action == "resend":
        context.user_data.pop("market_draft", None)
        context.user_data.pop("market_preview_id", None)
        context.user_data.pop("market_preview_message", None)
        await query.edit_message_text(
            "أرسل صورة الغرض مع جميع التفاصيل المصححة في شرح الصورة برسالة واحدة.",
            reply_markup=_market_navigation_markup(),
        )
        return EDIT_LISTING_MESSAGE if is_edit else SELL_MESSAGE

    if not draft:
        await query.edit_message_text(
            "انتهت جلسة الإعلان. يمكنك بدء عملية أخرى من سوق الأفراد.",
            reply_markup=_market_menu_markup(),
        )
        context.user_data["navigation_parent"] = "main"
        return MARKET_MENU
    if is_edit:
        listing_id = context.user_data.get("resubmitting_listing_id")
        listing = await db.resubmit_listing(
            listing_id, _user_id(context), draft["title"], draft["description"], draft["price"],
            draft["condition"], draft["city_id"], draft["category"], draft["address"],
            draft["contact"], draft["delivery"], draft["negotiable"], draft["photo_ids"],
        )
        if not listing:
            previous = await db.get_listing(listing_id)
            if previous and previous["user_id"] == _user_id(context) and previous["status"] == "pending_review":
                success_text = f"✅ تم إرسال تعديل الإعلان #{listing_id} للمراجعة مسبقًا."
            else:
                context.user_data.pop("market_preview_id", None)
                await query.edit_message_text(
                    "تعذر إعادة إرسال الإعلان؛ تأكد أنه مرفوض ويتبع حسابك ثم حاول مجددًا.",
                    reply_markup=_market_menu_markup(),
                )
                context.user_data["navigation_parent"] = "main"
                return MARKET_MENU
        else:
            success_text = f"✅ أُعيد إرسال الإعلان #{listing['id']} إلى الإدارة للمراجعة."
    else:
        listing = await db.create_listing(
            _user_id(context), draft["title"], draft["description"], draft["price"],
            draft["condition"], draft["city_id"], draft["category"], draft["address"],
            draft["contact"], draft["delivery"], draft["negotiable"], draft["photo_ids"],
            submission_key=draft.get("submission_key"),
        )
        if not listing:
            context.user_data.pop("market_preview_id", None)
            await query.edit_message_text(
                "تعذر إنشاء الإعلان؛ تحقق من اختيار مدينة نشطة ثم حاول مجددًا.",
                reply_markup=_market_menu_markup(),
            )
            context.user_data["navigation_parent"] = "main"
            return MARKET_MENU
        success_text = f"✅ تم إرسال الإعلان #{listing['id']} إلى الإدارة للمراجعة.\nالنشر مجاني خلال فترة الإطلاق."
    await query.edit_message_text(
        success_text + "\nستصلك رسالة عند القبول أو الرفض.\n\nيمكنك بدء عملية أخرى الآن.",
        reply_markup=_market_menu_markup(),
    )
    context.user_data.pop("market_draft", None)
    context.user_data.pop("market_preview_id", None)
    context.user_data.pop("market_preview_message", None)
    context.user_data.pop("market_city_id", None)
    context.user_data.pop("resubmitting_listing_id", None)
    context.user_data.pop("resubmitting_listing_title", None)
    context.user_data.pop("edit_listing_city_id", None)
    context.user_data["navigation_parent"] = "main"
    return MARKET_MENU


async def resume_listing_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = getattr(update, "callback_query", None)
    if query:
        await _safe_answer(query)
    data = context.user_data
    draft = data.get("market_draft")
    is_edit = bool(data.get("resubmitting_listing_id"))
    data["navigation_parent"] = "listings" if is_edit else "market_menu"

    if draft:
        preview_id = data.get("market_preview_id")
        if not preview_id or draft.get("preview_id") != preview_id:
            preview_id = uuid4().hex[:12]
            draft["preview_id"] = preview_id
            data["market_preview_id"] = preview_id
        draft.setdefault("submission_key", uuid4().hex)
        button_prefix = "listing" if is_edit else "market"
        text = (
            "راجع إعلانك قبل إرساله:\n\n"
            f"📦 {draft['title']}\n📝 {draft['description']}\n"
            f"💰 السعر: {draft['price']}\n🔄 التفاوض: {'قابل للتفاوض' if draft['negotiable'] else 'نهائي'}\n"
            f"📍 المدينة: {draft.get('city_name') or 'غير محددة'} — {draft['address']}\n"
            f"📞 {draft['contact']}\n🚚 {draft['delivery']}\n\n"
            "مدة الإعلان 72 ساعة تبدأ بعد الاعتماد."
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ إرسال للمراجعة", callback_data=f"{button_prefix}_{preview_id}_submit")],
            [InlineKeyboardButton("✏️ إعادة الإدخال", callback_data=f"{button_prefix}_{preview_id}_resend"),
             InlineKeyboardButton("❌ إلغاء", callback_data=f"{button_prefix}_{preview_id}_cancel")],
            [InlineKeyboardButton("↩️ رجوع إلى سوق الأفراد", callback_data="customer_market"),
             InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
        ])
        if query:
            preview_message = await query.edit_message_text(text, reply_markup=markup)
        else:
            preview_message = await update.message.reply_text(text, reply_markup=markup)
        reference = _message_reference(
            preview_message, update.effective_chat.id if update.effective_chat else None
        )
        if reference:
            data["market_preview_message"] = reference
        return EDIT_LISTING_CONFIRM if is_edit else SELL_CONFIRM

    if is_edit:
        if data.get("edit_listing_city_id"):
            text = _listing_prompt()
            markup = _market_navigation_markup()
            state = EDIT_LISTING_MESSAGE
        else:
            cities = await db.list_cities()
            text = (
                f"✏️ متابعة تعديل الإعلان #{data['resubmitting_listing_id']}: "
                f"{data.get('resubmitting_listing_title') or 'إعلانك'}\n\n"
                "اختر المدينة التي يوجد فيها الغرض."
            )
            markup = _city_keyboard(cities, "market_editcity_")
            state = EDIT_LISTING_CITY
    elif data.get("market_city_id"):
        text = _listing_prompt()
        markup = _market_navigation_markup()
        state = SELL_MESSAGE
    else:
        text = "لا توجد مسودة إعلان لمتابعتها. اختر عملية من سوق الأفراد."
        markup = _market_menu_markup()
        data["navigation_parent"] = "main"
        state = MARKET_MENU

    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)
    return state


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["navigation_parent"] = "market_menu"
    cities = await db.list_cities()
    keyboard = [
        [InlineKeyboardButton(city["name"], callback_data=f"market_buycity_{city['id']}")]
        for city in cities
    ]
    keyboard.append([InlineKeyboardButton("🌐 كل المدن", callback_data="market_buycity_all")])
    keyboard.append([
        InlineKeyboardButton("↩️ رجوع", callback_data="customer_market"),
        InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu"),
    ])
    await query.edit_message_text(
        "🛍️ البحث في سوق الأفراد\n\nاختر مدينة البحث:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BUY_CITY


async def buy_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    raw_city = query.data.rsplit("_", 1)[1]
    if raw_city == "all":
        context.user_data["market_search_city_id"] = None
        city_name = "كل المدن"
    else:
        try:
            city_id = int(raw_city)
        except ValueError:
            await query.edit_message_text("تعذر قراءة المدينة. ابدأ البحث من جديد.")
            return MARKET_MENU
        cities = await db.list_cities()
        city = next((city for city in cities if city["id"] == city_id), None)
        if not city:
            await query.edit_message_text("هذه المدينة غير متاحة حاليًا. ابدأ البحث من جديد.")
            return MARKET_MENU
        context.user_data["market_search_city_id"] = city_id
        city_name = city["name"]
    context.user_data["market_search_city_name"] = city_name
    context.user_data.pop("market_results", None)
    context.user_data.pop("market_offset", None)
    context.user_data.pop("market_search_id", None)
    await _invalidate_message_buttons(context, "market_search_action_message")
    await query.edit_message_text(
        f"🔎 المدينة: {city_name}\nاكتب ما تبحث عنه بكلمة أو جملة مختصرة.\n"
        "مثال: هاتف آيفون مستعمل أو تلفاز 50 بوصة بسعر مناسب",
        reply_markup=_market_navigation_markup(),
    )
    return BUY_QUERY


async def buy_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if len(text) < 2:
        await update.message.reply_text("اكتب كلمة أو جملة أوضح لما تبحث عنه.")
        return BUY_QUERY
    await _invalidate_message_buttons(context, "market_search_action_message")
    search_id = uuid4().hex[:12]
    context.user_data["market_search_id"] = search_id
    city_id = context.user_data.get("market_search_city_id")
    await update.message.reply_text("⏳ جارٍ البحث في الإعلانات المتاحة...")
    listings = await db.list_public_listings(city_id=city_id)
    local_ids = ai.rank_listing_ids_locally(text, listings)
    try:
        ranked = await asyncio.wait_for(ai.rank_listing_ids(text, listings), timeout=8.0)
    except asyncio.TimeoutError:
        log.info("تجاوز ترتيب نتائج السوق مهلة 8 ثوانٍ؛ سيُستخدم الترتيب المحلي")
        ranked = local_ids
    except Exception:
        log.exception("فشل ترتيب نتائج السوق؛ سيُستخدم الترتيب المحلي")
        ranked = local_ids
    if not isinstance(ranked, (list, tuple)):
        ranked = []
    lookup = {row["id"]: row for row in listings}
    relevant_ids = set(local_ids)
    ids = []
    seen_ids = set()
    for value in list(ranked) + list(local_ids):
        if type(value) is int:
            listing_id = value
        elif isinstance(value, str) and value.isdigit():
            listing_id = int(value)
        else:
            continue
        if listing_id in relevant_ids and listing_id in lookup and listing_id not in seen_ids:
            ids.append(listing_id)
            seen_ids.add(listing_id)
    matches = [lookup[x] for x in ids[:5]]
    if not matches:
        context.user_data["market_results"] = []
        context.user_data["market_offset"] = 0
        city_name = context.user_data.get("market_search_city_name", "كل المدن")
        sent_message = await update.message.reply_text(
            f"لم نجد إعلانًا مطابقًا في {city_name} حاليًا. يمكنك البحث بعبارة أخرى أو اختيار مدينة مختلفة.",
            reply_markup=_search_actions_keyboard(False, search_id),
        )
        reference = _message_reference(sent_message, update.effective_chat.id)
        if reference:
            context.user_data["market_search_action_message"] = reference
        return MARKET_MENU
    context.user_data["market_results"] = ids
    context.user_data["market_offset"] = 5
    await update.message.reply_text(f"✅ أفضل {len(matches)} إعلانات مطابقة — {context.user_data.get('market_search_city_name', 'كل المدن')}:")
    await _send_results(update, context, matches)
    sent_message = await update.message.reply_text(
        "اختر إجراءً:", reply_markup=_search_actions_keyboard(len(ids) > 5, search_id)
    )
    reference = _message_reference(sent_message, update.effective_chat.id)
    if reference:
        context.user_data["market_search_action_message"] = reference
    return MARKET_MENU


async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    search_id = parts[2] if len(parts) == 3 else None
    if not search_id or search_id != context.user_data.get("market_search_id"):
        await _remove_stale_buttons(query, "انتهت صلاحية صفحة النتائج؛ ابدأ بحثًا جديدًا.")
        return MARKET_MENU
    await _safe_answer(query)
    ids = context.user_data.get("market_results", [])
    offset = context.user_data.get("market_offset", 5)
    rows = await db.list_public_listings(city_id=context.user_data.get("market_search_city_id"))
    lookup = {row["id"]: row for row in rows}
    page_ids = ids[offset:offset + 5]
    matches = [lookup[x] for x in page_ids if x in lookup]
    if not matches:
        await query.edit_message_text(
            "لا توجد إعلانات إضافية مطابقة.",
            reply_markup=_search_actions_keyboard(False, search_id),
        )
        reference = _message_reference(getattr(query, "message", None))
        if reference:
            context.user_data["market_search_action_message"] = reference
        return MARKET_MENU
    next_offset = offset + len(page_ids)
    context.user_data["market_offset"] = next_offset
    await query.edit_message_text(
        "📄 إعلانات إضافية مطابقة:",
        reply_markup=_search_actions_keyboard(next_offset < len(ids), search_id),
    )
    reference = _message_reference(getattr(query, "message", None))
    if reference:
        context.user_data["market_search_action_message"] = reference
    await _send_results(update, context, matches)
    return MARKET_MENU


async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    city_name = context.user_data.get("market_search_city_name", "كل المدن")
    context.user_data.pop("market_results", None)
    context.user_data.pop("market_offset", None)
    context.user_data.pop("market_search_id", None)
    context.user_data.pop("market_search_action_message", None)
    await query.edit_message_text(
        f"🔎 البحث في {city_name}. اكتب ما تبحث عنه بكلمة أو جملة مختصرة.",
        reply_markup=_market_navigation_markup(),
    )
    return BUY_QUERY


async def _send_results(update, context, rows):
    user_id = _user_id(context)
    await db.log_listing_views([row["id"] for row in rows], user_id)
    for row in rows:
        target = update.effective_chat.id
        for photo_id in (row["image_file_ids"] or [])[:2]:
            try:
                await context.bot.send_photo(target, photo_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("تعذر إرسال صورة الإعلان %s: %s", row["id"], exc)
        price = str(row["price"]) if row["price"] is not None else "عند التواصل"
        await context.bot.send_message(
            target,
            f"📦 {row['title']}\n💰 السعر: {price}\n"
            f"🔄 التفاوض: {'مسموح' if row['negotiable'] else 'غير مسموح'}\n"
            f"📍 المدينة: {row.get('city_name') or 'غير محددة'} — {row['address'] or 'غير محدد'}\n"
            f"📞 التواصل: {row['contact'] or 'غير مسجل'}\n🚚 {row['delivery'] or 'غير محدد'}\n"
            f"{row['description'] or ''}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 إشعار المعلن باهتمامي", callback_data=f"listing_contact_{row['id']}"),
            ]]),
        )


def states():
    return {
        MARKET_MENU: [
            CallbackQueryHandler(buy_start, pattern="^market_buy$"),
            CallbackQueryHandler(sell_start, pattern="^market_sell$"),
            CallbackQueryHandler(sell_discard_and_start, pattern=r"^market_sell_discard_[0-9a-f]{12}$"),
            CallbackQueryHandler(discard_market_draft, pattern=r"^market_draft_discard_[0-9a-f]{12}$"),
            CallbackQueryHandler(more_results, pattern=r"^market_more(?:_[0-9a-f]{12})?$"),
            CallbackQueryHandler(new_search, pattern="^market_new_search$"),
        ],
        SELL_CITY: [CallbackQueryHandler(sell_city_chosen, pattern=r"^market_sellcity_\d+$")],
        SELL_MESSAGE: [
            MessageHandler(filters.PHOTO, sell_message),
            MessageHandler(filters.ALL & ~filters.COMMAND, sell_message),
        ],
        SELL_CONFIRM: [CallbackQueryHandler(sell_confirm, pattern=r"^market_(?:[0-9a-f]{12}_)?(submit|resend|cancel)$")],
        BUY_CITY: [CallbackQueryHandler(buy_city_chosen, pattern=r"^market_buycity_(\d+|all)$")],
        BUY_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_query)],
        EDIT_LISTING_CITY: [CallbackQueryHandler(edit_listing_city_chosen, pattern=r"^market_editcity_\d+$")],
        EDIT_LISTING_MESSAGE: [
            MessageHandler(filters.PHOTO, sell_message),
            MessageHandler(filters.ALL & ~filters.COMMAND, sell_message),
        ],
        EDIT_LISTING_CONFIRM: [CallbackQueryHandler(sell_confirm, pattern=r"^listing_(?:[0-9a-f]{12}_)?(submit|resend|cancel)$")],
    }
