"""Clean individual marketplace flow for Madinty Offers.

One state machine owns marketplace interactions:
market menu -> buy city+query OR sell city+photo+caption -> review -> confirmation.
Rejected listings can be edited and resubmitted by their owner with /editlisting ID.
"""
import logging
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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


def _user_id(context):
    return context.user_data.get("db_user_id")


def _city_keyboard(cities, callback_prefix: str):
    keyboard = [
        [InlineKeyboardButton(city["name"], callback_data=f"{callback_prefix}{city['id']}")]
        for city in cities
    ]
    return InlineKeyboardMarkup(keyboard)


def _listing_prompt() -> str:
    return (
        "أرسل صورة الغرض مع كتابة جميع التفاصيل في شرح الصورة في رسالة واحدة.\n\n"
        "المعلومات المطلوبة: اسم الغرض، العنوان أو مكان وجوده، ورقم التواصل. "
        "ويمكنك إضافة الوصف والسعر والحالة والتفاوض والتوصيل.\n\n"
        "مثال: هاتف Samsung A54 مستعمل بحالة ممتازة. السعر 800000 جنيه، "
        "قابل للتفاوض. الموقع: بحري السوق العربي. التواصل: 09xxxxxxxx. "
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


async def _remove_stale_buttons(query, message_ar: str):
    await query.answer(message_ar, show_alert=True)
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
    ])
    return InlineKeyboardMarkup(actions)


async def market_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🛒 سوق الأفراد\n\nاختر العملية المطلوبة:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍️ شراء", callback_data="market_buy")],
            [InlineKeyboardButton("🏷️ بيع", callback_data="market_sell")],
        ]),
    )
    return MARKET_MENU


async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cities = await db.list_cities()
    if not cities:
        await query.edit_message_text("لا توجد مدن متاحة حاليًا لنشر إعلان.")
        return MARKET_MENU
    await _invalidate_message_buttons(context, "market_preview_message")
    context.user_data.pop("resubmitting_listing_id", None)
    context.user_data.pop("market_draft", None)
    context.user_data.pop("market_preview_id", None)
    await query.edit_message_text(
        "🏷️ نشر غرض للبيع\n\nاختر المدينة التي يوجد فيها الغرض:",
        reply_markup=_city_keyboard(cities, "market_sellcity_"),
    )
    return SELL_CITY


async def sell_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        city_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة المدينة. ابدأ النشر من جديد.")
        return MARKET_MENU
    if city_id not in {city["id"] for city in await db.list_cities()}:
        await query.edit_message_text("هذه المدينة غير متاحة حاليًا. ابدأ النشر من جديد.")
        return MARKET_MENU
    context.user_data["market_city_id"] = city_id
    await query.edit_message_text(_listing_prompt())
    return SELL_MESSAGE


async def edit_listing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("أرسل /start أولًا ثم حاول مجددًا.")
        return ConversationHandler.END
    context.user_data["db_user_id"] = user["id"]
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("الاستخدام: /editlisting رقم_الإعلان")
        return ConversationHandler.END
    listing_id = int(context.args[0])
    listing = await db.get_listing(listing_id)
    if not listing or listing["user_id"] != user["id"]:
        await update.message.reply_text("هذا الإعلان غير موجود أو لا يخص حسابك.")
        return ConversationHandler.END
    if listing["status"] != "rejected":
        await update.message.reply_text("يمكن تعديل الإعلانات المرفوضة فقط عبر هذا المسار.")
        return ConversationHandler.END
    cities = await db.list_cities()
    if not cities:
        await update.message.reply_text("لا توجد مدن متاحة حاليًا لإعادة إرسال الإعلان.")
        return ConversationHandler.END
    context.user_data["resubmitting_listing_id"] = listing_id
    await _invalidate_message_buttons(context, "market_preview_message")
    context.user_data.pop("market_preview_id", None)
    context.user_data.pop("market_draft", None)
    await update.message.reply_text(
        f"✏️ تعديل الإعلان #{listing_id}: {listing['title']}\n\n"
        "اختر المدينة التي يوجد فيها الغرض، ثم أرسل الصورة والتفاصيل المصححة.",
        reply_markup=_city_keyboard(cities, "market_editcity_"),
    )
    return EDIT_LISTING_CITY


async def edit_listing_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        city_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("تعذر قراءة المدينة. أعد الأمر /editlisting.")
        return ConversationHandler.END
    if city_id not in {city["id"] for city in await db.list_cities()}:
        await query.edit_message_text("هذه المدينة غير متاحة حاليًا. أعد الأمر /editlisting.")
        return ConversationHandler.END
    context.user_data["edit_listing_city_id"] = city_id
    await query.edit_message_text(_listing_prompt())
    return EDIT_LISTING_MESSAGE


async def sell_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.photo or not (message.caption or "").strip():
        await message.reply_text("⚠️ أرسل صورة الغرض مع جميع التفاصيل في شرح الصورة في رسالة واحدة.")
        return EDIT_LISTING_MESSAGE if context.user_data.get("resubmitting_listing_id") else SELL_MESSAGE

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

    reviewed = await ai.review_listing(parsed)
    reviewed = reviewed if isinstance(reviewed, dict) else {}
    # Review is used to polish the listing, not to override the deterministic
    # required-field check above with another possibly contradictory AI verdict.

    city_id = context.user_data.get("edit_listing_city_id" if is_edit else "market_city_id")
    if not city_id:
        await message.reply_text("⚠️ اختر المدينة أولًا عبر بدء النشر أو /editlisting.")
        return ConversationHandler.END
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
    price = draft["price"]
    is_edit = bool(context.user_data.get("resubmitting_listing_id"))
    await _invalidate_message_buttons(context, "market_preview_message")
    preview_id = uuid4().hex[:12]
    draft["preview_id"] = preview_id
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
    await query.answer()
    if action == "cancel":
        context.user_data.pop("market_draft", None)
        context.user_data.pop("market_preview_id", None)
        context.user_data.pop("market_preview_message", None)
        context.user_data.pop("resubmitting_listing_id", None)
        context.user_data.pop("edit_listing_city_id", None)
        await query.edit_message_text("تم إلغاء تعديل الإعلان." if is_edit else "تم إلغاء إنشاء الإعلان.")
        return ConversationHandler.END
    if action == "resend":
        context.user_data.pop("market_draft", None)
        context.user_data.pop("market_preview_id", None)
        context.user_data.pop("market_preview_message", None)
        await query.edit_message_text("أرسل صورة الغرض مع جميع التفاصيل المصححة في شرح الصورة برسالة واحدة.")
        return EDIT_LISTING_MESSAGE if is_edit else SELL_MESSAGE

    if not draft:
        await query.edit_message_text("انتهت جلسة الإعلان. ابدأ من سوق الأفراد مرة أخرى.")
        return ConversationHandler.END
    if is_edit:
        listing_id = context.user_data.get("resubmitting_listing_id")
        listing = await db.resubmit_listing(
            listing_id, _user_id(context), draft["title"], draft["description"], draft["price"],
            draft["condition"], draft["city_id"], draft["category"], draft["address"],
            draft["contact"], draft["delivery"], draft["negotiable"], draft["photo_ids"],
        )
        if not listing:
            context.user_data.pop("market_preview_id", None)
            await query.edit_message_text("تعذر إعادة إرسال الإعلان؛ تأكد أنه مرفوض ويتبع حسابك ثم حاول مجددًا.")
            return ConversationHandler.END
        context.user_data.pop("resubmitting_listing_id", None)
        context.user_data.pop("edit_listing_city_id", None)
        success_text = f"✅ أُعيد إرسال الإعلان #{listing['id']} إلى الإدارة للمراجعة."
    else:
        listing = await db.create_listing(
            _user_id(context), draft["title"], draft["description"], draft["price"],
            draft["condition"], draft["city_id"], draft["category"], draft["address"],
            draft["contact"], draft["delivery"], draft["negotiable"], draft["photo_ids"],
        )
        if not listing:
            context.user_data.pop("market_preview_id", None)
            await query.edit_message_text("تعذر إنشاء الإعلان؛ تحقق من اختيار مدينة نشطة ثم حاول مجددًا.")
            return ConversationHandler.END
        success_text = f"✅ تم إرسال الإعلان #{listing['id']} إلى الإدارة للمراجعة.\nالنشر مجاني خلال فترة الإطلاق."
    context.user_data.pop("market_draft", None)
    context.user_data.pop("market_preview_id", None)
    context.user_data.pop("market_preview_message", None)
    await query.edit_message_text(success_text + "\nستصلك رسالة عند القبول أو الرفض.")
    return ConversationHandler.END


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cities = await db.list_cities()
    keyboard = [
        [InlineKeyboardButton(city["name"], callback_data=f"market_buycity_{city['id']}")]
        for city in cities
    ]
    keyboard.append([InlineKeyboardButton("🌐 كل المدن", callback_data="market_buycity_all")])
    await query.edit_message_text(
        "🛍️ البحث في سوق الأفراد\n\nاختر مدينة البحث:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BUY_CITY


async def buy_city_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        "مثال: هاتف آيفون مستعمل أو تلفاز 50 بوصة بسعر مناسب"
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
    listings = await db.list_public_listings(city_id=city_id)
    ranked = await ai.rank_listing_ids(text, listings)
    if not isinstance(ranked, (list, tuple)):
        ranked = []
    lookup = {row["id"]: row for row in listings}
    ids = [x for x in ranked if x in lookup]
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
    await query.answer()
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
    await query.answer()
    city_name = context.user_data.get("market_search_city_name", "كل المدن")
    context.user_data.pop("market_results", None)
    context.user_data.pop("market_offset", None)
    context.user_data.pop("market_search_id", None)
    context.user_data.pop("market_search_action_message", None)
    await query.edit_message_text(f"🔎 البحث في {city_name}. اكتب ما تبحث عنه بكلمة أو جملة مختصرة.")
    return BUY_QUERY


async def _send_results(update, context, rows):
    user_id = _user_id(context)
    for row in rows:
        await db.log_listing_event(row["id"], user_id, "VIEW")
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
