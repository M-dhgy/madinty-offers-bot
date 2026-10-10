"""Safe campaign broadcasting shared by the admin command and callback paths."""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db

log = logging.getLogger("madinty-bot.broadcast")


async def broadcast_campaign(bot, campaign_id: int, batch_size: int, batch_delay: float) -> dict:
    """Claim once, send to the strictly matched audience, and finalize exactly once.

    A failed or already-running campaign is deliberately not retried automatically:
    Telegram delivery may have succeeded even when a request failed ambiguously.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if batch_delay < 0:
        raise ValueError("batch_delay cannot be negative")

    campaign = await db.claim_campaign_for_broadcast(campaign_id)
    if not campaign:
        return {"status": "not_ready", "sent": 0, "failed": 0, "total": 0}

    sent = 0
    failed = 0
    total = 0
    try:
        targets = await db.find_target_users(campaign["city_id"], campaign["category_id"])
        total = len(targets)
        if total == 0:
            await db.release_campaign_without_delivery(campaign_id)
            return {"status": "no_targets", "sent": 0, "failed": 0, "total": 0}
        for offset in range(0, total, batch_size):
            batch = targets[offset:offset + batch_size]
            for user in batch:
                try:
                    await bot.send_message(
                        user["telegram_id"],
                        campaign["description"],
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🎁 احصل على كود الخصم",
                                callback_data=f"getcode_{campaign_id}",
                            )
                        ]]),
                    )
                except Exception as exc:  # noqa: BLE001 — continue the remaining batch
                    failed += 1
                    log.warning(
                        "فشل إرسال الحملة %s إلى %s: %s",
                        campaign_id, user["telegram_id"], exc,
                    )
                    continue

                sent += 1
                try:
                    await db.log_event(campaign_id, user["id"], "SENT")
                except Exception:  # noqa: BLE001 — delivery succeeded; retain its status
                    log.exception("تعذر تسجيل إرسال الحملة %s للمستخدم %s", campaign_id, user["id"])

            if batch_delay and offset + batch_size < total:
                await asyncio.sleep(batch_delay)

        # Partial success still activates the offer for people who received it;
        # zero delivered messages leave it failed and non-retryable by /send.
        finalized = await db.finish_campaign_broadcast(campaign_id, succeeded=(sent > 0))
        status = "completed" if finalized and sent > 0 else "failed"
        return {"status": status, "sent": sent, "failed": failed, "total": total}
    except Exception:  # noqa: BLE001 — do not leave a partially attempted campaign approved
        log.exception("توقف بث الحملة %s قبل اكتماله", campaign_id)
        await db.finish_campaign_broadcast(campaign_id, succeeded=(sent > 0))
        return {"status": "failed", "sent": sent, "failed": failed, "total": total}
