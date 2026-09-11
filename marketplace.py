"""Clean individual marketplace flow for Madinty Offers.

One state machine owns marketplace interactions:
market menu -> buy search OR sell photo+caption -> AI review -> confirmation.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

import ai
import db

MARKET_MENU, SELL_MESSAGE, SELL_CONFIRM, BUY_QUERY = range(40, 44)


def _user_id(context):
    return context.user_data.get("db_user_id")


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
    await query.edit_message_text(
        "🏷️ نشر غرض للبيع\n\n"
        "أرسل صورة الغرض مع كتابة جميع التفاصيل في شرح الصورة في رسالة واحدة.\n\n"
        "المعلومات المطلوبة:\n"
        "• اسم الغرض ووصفه وحالته\n"
        "• السعر\n"
        "• هل السعر نهائي أم قابل للتفاوض؟\n"
        "• العنوان ومكان وجود الغرض أو البائع\n"
        "• رقم التواصل\n"
        "• هل توجد خدمة توصيل؟\n\n"
        "مسموح بصورة أو صورتين، ولن يُرسل الإعلان للإدارة قبل اكتمال البيانات.\n\n"
        "مثال على شرح الصورة:\n"
        "هاتف Samsung A54 مستعمل بحالة ممتازة. السعر 800000 جنيه، "
        "قابل للتفاوض. الموقع: بحري السوق العربي. التواصل: 09xxxxxxxx. "
        "التوصيل: لا توجد خدمة توصيل.\n\n"
        "📷 أرسل الصورة الآن مع النص في نفس الرسالة."
    )
    return SELL_MESSAGE


async def sell_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message.photo or not (message.caption or "").strip():
        await message.reply_text("⚠️ أرسل صورة الغرض مع جميع التفاصيل في شرح الصورة في رسالة واحدة.")
        return SELL_MESSAGE

    parsed = await ai.extract_listing_data(message.caption.strip())
    missing = []
    required = {
        "اسم الغرض": parsed.get("title"),
        "العنوان والموقع": parsed.get("address"),
        "رقم التواصل": parsed.get("contact"),
    }
    for label, value in required.items():
        if value is None or value == "" or value == []:
            missing.append(label)
    missing.extend(str(x) for x in (parsed.get("issues") or []))
    if missing:
        await message.reply_text(
            "⚠️ لا يمكن إرسال الإعلان بعد.\n\nالبيانات الناقصة أو غير الواضحة:\n- "
            + "\n- ".join(dict.fromkeys(missing))
            + "\n\nأرسل صورة جديدة مع التفاصيل كاملة في رسالة واحدة."
        )
        return SELL_MESSAGE

    reviewed = await ai.review_listing(parsed)
    if reviewed.get("issues"):
        await message.reply_text(
            "⚠️ يحتاج الإعلان إلى توضيح:\n- "
            + "\n- ".join(map(str, reviewed["issues"]))
            + "\n\nأرسل الصورة مع التفاصيل المصححة."
        )
        return SELL_MESSAGE

    context.user_data["market_draft"] = {
        "title": reviewed.get("title") or parsed["title"],
        "description": reviewed.get("description") or parsed["description"],
        "price": str(parsed.get("price") or "عند التواصل"),
        "negotiable": bool(parsed["negotiable"]),
        "address": parsed["address"],
        "contact": parsed["contact"],
        "delivery": parsed.get("delivery") or "غير محدد",
        "condition": parsed.get("condition") or "غير محددة",
        "category": parsed.get("category") or "عام",
        "photo_ids": [message.photo[-1].file_id],
    }
    draft = context.user_data["market_draft"]
    price = draft["price"]
    await message.reply_text(
        "راجع إعلانك قبل إرساله:\n\n"
        f"📦 {draft['title']}\n📝 {draft['description']}\n"
        f"💰 السعر: {price}\n🔄 التفاوض: {'قابل للتفاوض' if draft['negotiable'] else 'نهائي'}\n"
        f"📍 {draft['address']}\n📞 {draft['contact']}\n🚚 {draft['delivery']}\n\n"
        "مدة الإعلان بعد الاعتماد: 72 ساعة. ويمكن تجديده قبل انتهائه.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ إرسال للمراجعة", callback_data="market_submit")],
            [InlineKeyboardButton("✏️ إعادة الإرسال", callback_data="market_resend"),
             InlineKeyboardButton("❌ إلغاء", callback_data="market_cancel")],
        ]),
    )
    return SELL_CONFIRM


async def sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "market_cancel":
        context.user_data.pop("market_draft", None)
        await query.edit_message_text("تم إلغاء إنشاء الإعلان.")
        return ConversationHandler.END
    if query.data == "market_resend":
        context.user_data.pop("market_draft", None)
        await query.edit_message_text("أرسل صورة الغرض مع جميع التفاصيل في شرح الصورة برسالة واحدة.")
        return SELL_MESSAGE
    draft = context.user_data.get("market_draft")
    if not draft:
        await query.edit_message_text("انتهت جلسة الإعلان. ابدأ من سوق الأفراد مرة أخرى.")
        return ConversationHandler.END
    listing = await db.create_listing(
        _user_id(context), draft["title"], draft["description"], draft["price"],
        draft["condition"], None, draft["category"], draft["address"],
        draft["contact"], draft["delivery"], draft["negotiable"], draft["photo_ids"],
    )
    context.user_data.pop("market_draft", None)
    await query.edit_message_text(
        f"✅ تم إرسال الإعلان #{listing['id']} إلى الإدارة للمراجعة.\n"
        "سيصلك إشعار عند القبول أو الرفض. النشر مجاني خلال فترة الإطلاق."
    )
    return ConversationHandler.END


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🛍️ البحث في سوق الأفراد\n\n"
        "اكتب ما تبحث عنه بكلمة أو جملة مختصرة.\n"
        "مثال: هاتف آيفون مستعمل أو تلفاز 50 بوصة بسعر مناسب"
    )
    return BUY_QUERY


async def buy_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if len(text) < 2:
        await update.message.reply_text("اكتب كلمة أو جملة أوضح لما تبحث عنه.")
        return BUY_QUERY
    listings = await db.list_public_listings()
    ranked = await ai.rank_listing_ids(text, listings)
    lookup = {row["id"]: row for row in listings}
    ids = [x for x in ranked if x in lookup]
    matches = [lookup[x] for x in ids[:5]]
    if not matches:
        await update.message.reply_text("لم نجد إعلانًا مطابقًا حاليًا.")
        return ConversationHandler.END
    context.user_data["market_results"] = ids
    context.user_data["market_offset"] = 5
    await update.message.reply_text(f"✅ أفضل {len(matches)} إعلانات مطابقة:")
    await _send_results(update, context, matches)
    buttons = []
    if len(ids) > 5:
        buttons.append([InlineKeyboardButton("📄 المزيد من الإعلانات", callback_data="market_more")])
    buttons.append([InlineKeyboardButton("🔎 بحث جديد", callback_data="market_new_search")])
    await update.message.reply_text("اختر إجراءً:", reply_markup=InlineKeyboardMarkup(buttons))
    return MARKET_MENU


async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ids = context.user_data.get("market_results", [])
    offset = context.user_data.get("market_offset", 5)
    rows = await db.list_public_listings()
    lookup = {row["id"]: row for row in rows}
    matches = [lookup[x] for x in ids[offset:offset + 5] if x in lookup]
    if not matches:
        await query.edit_message_text("لا توجد إعلانات إضافية مطابقة.")
        return MARKET_MENU
    await query.edit_message_text("📄 إعلانات إضافية مطابقة:")
    await _send_results(update, context, matches)
    context.user_data["market_offset"] = offset + 5
    return MARKET_MENU


async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("اكتب ما تبحث عنه بكلمة أو جملة مختصرة.")
    return BUY_QUERY


async def _send_results(update, context, rows):
    user_id = _user_id(context)
    for row in rows:
        await db.log_listing_event(row["id"], user_id, "VIEW")
        target = update.effective_chat.id
        for photo_id in (row["image_file_ids"] or [])[:2]:
            await context.bot.send_photo(target, photo_id)
        price = str(row["price"]) if row["price"] is not None else "عند التواصل"
        await context.bot.send_message(
            target,
            f"📦 {row['title']}\n💰 السعر: {price}\n"
            f"🔄 التفاوض: {'مسموح' if row['negotiable'] else 'غير مسموح'}\n"
            f"📍 {row['address'] or 'غير محدد'}\n🚚 {row['delivery'] or 'غير محدد'}\n"
            f"{row['description'] or ''}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📩 تواصل مع المعلن", callback_data=f"listing_contact_{row['id']}"),
            ]]),
        )


def states():
    return {
        MARKET_MENU: [
            CallbackQueryHandler(buy_start, pattern="^market_buy$"),
            CallbackQueryHandler(sell_start, pattern="^market_sell$"),
            CallbackQueryHandler(more_results, pattern="^market_more$"),
            CallbackQueryHandler(new_search, pattern="^market_new_search$"),
        ],
        SELL_MESSAGE: [
            MessageHandler(filters.PHOTO, sell_message),
            MessageHandler(filters.ALL & ~filters.COMMAND, sell_message),
        ],
        SELL_CONFIRM: [CallbackQueryHandler(sell_confirm, pattern="^market_(submit|resend|cancel)$")],
        BUY_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, buy_query)],
    }


def handlers():
    from telegram.ext import CallbackQueryHandler
    return [CallbackQueryHandler(market_menu, pattern="^customer_market$")]
