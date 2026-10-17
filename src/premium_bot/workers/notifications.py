import asyncio
import uuid
from datetime import timedelta
from html import escape

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import exists, select

from premium_bot.application.common import enqueue
from premium_bot.bot.ui import status_label
from premium_bot.db import Broadcast, Delivery, Invoice, Job, Media, User
from premium_bot.runtime import Runtime


async def notify_generation(rt: Runtime, job_id: uuid.UUID) -> None:
    async with rt.sessions() as s:
        job = await s.get(Job, job_id)
        if job is None or (await s.get(Delivery, f"generation:{job_id}")) is not None:
            return
        items = list(
            await s.scalars(select(Media).where(Media.job_id == job_id, Media.output.is_(True)))
        )
    if job.status == "succeeded":
        for item in items:
            key = f"output:{item.id}"
            async with rt.sessions() as s:
                if await s.get(Delivery, key):
                    continue
            source = FSInputFile(rt.media.path(item.path))
            caption = f"<b>{escape(job.snapshot['name'])}</b>\nЗадание {str(job_id)[:8]} готово."
            if item.kind == "video":
                result = await rt.bot.send_video(
                    job.user_id, source, caption=caption, supports_streaming=True
                )
                file_id = result.video.file_id if result.video else None
            else:
                async with rt.redis.lock("media-processing", timeout=180, blocking_timeout=2):
                    preview = await asyncio.to_thread(rt.media.photo_preview, item)
                    try:
                        result = await rt.bot.send_photo(
                            job.user_id, FSInputFile(preview), caption=caption
                        )
                        file_id = result.photo[-1].file_id if result.photo else None
                    finally:
                        preview.unlink(missing_ok=True)
            async with rt.sessions.begin() as s:
                stored = await s.get(Media, item.id)
                if stored:
                    stored.telegram_file_id = file_id
                s.add(Delivery(key=key, user_id=job.user_id))
    else:
        await rt.bot.send_message(
            job.user_id,
            f"Задание {str(job_id)[:8]}: {escape(status_label(job.status))}.\n{escape(job.error or '')}",
        )
    async with rt.sessions.begin() as s:
        s.add(Delivery(key=f"generation:{job_id}", user_id=job.user_id))


async def broadcast(rt: Runtime, broadcast_id: uuid.UUID) -> None:
    from premium_bot.domain import now

    async with rt.redis.lock(f"broadcast:{broadcast_id}", timeout=120, blocking_timeout=1):
        async with rt.sessions.begin() as s:
            item = await s.get(Broadcast, broadcast_id, with_for_update=True)
            if item is None or item.status not in {"scheduled", "sending"}:
                return
            item.status = "sending"
            query = (
                select(User)
                .where(User.id > item.cursor, User.blocked.is_(False), User.notifications.is_(True))
                .order_by(User.id)
                .limit(10)
            )
            if item.segment == "active":
                query = query.where(User.last_seen > now() - timedelta(days=7))
            elif item.segment == "paying":
                query = query.where(
                    exists(
                        select(Invoice.id).where(
                            Invoice.user_id == User.id, Invoice.status == "paid"
                        )
                    )
                )
            users = list(await s.scalars(query))
        if not users:
            async with rt.sessions.begin() as s:
                stored = await s.get(Broadcast, broadcast_id, with_for_update=True)
                if stored and stored.status == "sending":
                    stored.status = "completed"
            return
        for user in users:
            async with rt.sessions() as s:
                current = await s.get(Broadcast, broadcast_id)
                if current is None or current.status == "canceled":
                    return
                delivered = await s.get(Delivery, f"broadcast:{broadcast_id}:{user.id}")
            if delivered:
                continue
            payload = item.payload
            buttons = payload.get("buttons", [])
            markup = (
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=b["text"], url=b["url"])] for b in buttons
                    ]
                )
                if buttons
                else None
            )
            failed = False
            try:
                text = escape(payload.get("text", ""))
                if payload["kind"] == "photo":
                    await rt.bot.send_photo(
                        user.id, payload["file_id"], caption=text, reply_markup=markup
                    )
                elif payload["kind"] == "video":
                    await rt.bot.send_video(
                        user.id, payload["file_id"], caption=text, reply_markup=markup
                    )
                else:
                    await rt.bot.send_message(user.id, text, reply_markup=markup)
            except TelegramForbiddenError:
                failed = True
            async with rt.sessions.begin() as s:
                stored = await s.get(Broadcast, broadcast_id, with_for_update=True)
                if stored:
                    stored.cursor = user.id
                    stored.sent += int(not failed)
                    stored.failed += int(failed)
                s.add(Delivery(key=f"broadcast:{broadcast_id}:{user.id}", user_id=user.id))
            # Telegram flood control also governs retry_after in the shared worker.
        async with rt.sessions.begin() as s:
            await enqueue(
                s,
                f"broadcast-batch:{broadcast_id}:{users[-1].id}",
                "broadcast",
                {"broadcast_id": str(broadcast_id)},
                delay=1,
            )
