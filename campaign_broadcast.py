"""Durable, resumable campaign broadcasting with conservative Telegram semantics."""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter

import db

log = logging.getLogger("madinty-bot.broadcast")
MAX_RETRY_AFTER_RETRIES = 3


def _retry_delay_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return max(0.0, value.total_seconds() if hasattr(value, "total_seconds") else float(value))


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:1000]


def _result(status, summary):
    return {
        "status": status,
        "sent": int(summary.get("sent", 0)),
        "failed": int(summary.get("rejected", 0)) + int(summary.get("uncertain", 0)),
        "total": int(summary.get("total", 0)),
        "pending": int(summary.get("pending", 0)),
        "uncertain": int(summary.get("uncertain", 0)),
    }


async def broadcast_campaign(
    bot, campaign_id: int, batch_size: int, batch_delay: float, *, resume: bool = False
) -> dict:
    """Broadcast a frozen audience snapshot; never resend an ambiguous attempt.

    Telegram has no idempotency key for sendMessage. A transport timeout, process
    crash after request submission, or database failure after Telegram accepts a
    message is therefore recorded as uncertain and is not retried automatically.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    if batch_delay < 0:
        raise ValueError("batch_delay cannot be negative")

    campaign = await db.claim_campaign_for_broadcast(campaign_id, resume=resume)
    if not campaign:
        return _result("not_ready", {})

    try:
        before = await db.campaign_delivery_summary(campaign_id)
        pending = await db.list_pending_campaign_recipients(campaign_id)
        if before["total"] == 0:
            await db.release_campaign_without_delivery(campaign_id)
            return _result("no_targets", before)

        for offset, target in enumerate(pending):
            claimed = await db.claim_campaign_recipient(campaign_id, target["id"])
            if not claimed:
                continue

            state = None
            error_text = None
            retry_after_count = 0
            while True:
                try:
                    await bot.send_message(
                        target["telegram_id"],
                        campaign["description"],
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🎁 احصل على كود الخصم",
                                callback_data=f"getcode_{campaign_id}",
                            )
                        ]]),
                    )
                except RetryAfter as exc:
                    if retry_after_count >= MAX_RETRY_AFTER_RETRIES:
                        # Telegram explicitly rejected each throttled request; keep
                        # it pending so an administrator may resume later.
                        state = "pending"
                        error_text = _error_text(exc)
                        break
                    retry_after_count += 1
                    await asyncio.sleep(_retry_delay_seconds(exc))
                    continue
                except (Forbidden, BadRequest) as exc:
                    # Telegram returned a definitive non-delivery response.
                    state = "rejected"
                    error_text = _error_text(exc)
                    break
                except Exception as exc:  # noqa: BLE001 — the remote result may be ambiguous
                    state = "uncertain"
                    error_text = _error_text(exc)
                    log.warning(
                        "نتيجة إرسال الحملة %s إلى %s ملتبسة: %s",
                        campaign_id, target["telegram_id"], error_text,
                    )
                    break
                else:
                    state = "sent"
                    break

            recorded = await db.record_campaign_recipient_result(
                campaign_id, target["id"], state, error_text
            )
            if not recorded:
                raise RuntimeError(
                    f"Recipient state changed while recording campaign {campaign_id}"
                )

            if batch_delay and (offset + 1) % batch_size == 0 and offset + 1 < len(pending):
                await asyncio.sleep(batch_delay)
    except Exception as exc:  # noqa: BLE001 — preserve the sending lease for recovery
        log.exception("توقف بث الحملة %s؛ سيُستأنف بعد التحقق من الحالات", campaign_id)
        try:
            summary = await db.campaign_delivery_summary(campaign_id)
        except Exception:  # noqa: BLE001
            summary = {}
        result = _result("interrupted", summary)
        result["error"] = _error_text(exc)
        return result

    try:
        summary = await db.campaign_delivery_summary(campaign_id)
        finalized = await db.finish_campaign_broadcast(
            campaign_id, succeeded=(summary["sent"] > 0)
        )
    except Exception as exc:  # noqa: BLE001 — retain sending state for later finalization
        log.exception("تعذر إنهاء بث الحملة %s", campaign_id)
        result = _result("finalization_failed", summary)
        result["error"] = _error_text(exc)
        return result

    if not finalized:
        return _result("finalization_failed", summary)
    if finalized["status"] == "active":
        status = "completed"
    elif summary["pending"]:
        status = "retryable"
    else:
        status = "failed"
    return _result(status, summary)
