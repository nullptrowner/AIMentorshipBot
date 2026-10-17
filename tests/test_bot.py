from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from premium_bot.bot.dispatcher import dispatcher
from premium_bot.bot.user import Studio

pytestmark = pytest.mark.integration


async def seedance_model(rt):
    from premium_bot.db import AIModel

    async with rt.sessions.begin() as s:
        model = await s.get(AIModel, "seedance")
        model.enabled = True
        model.version = "test-seedance"
        model.pricing = {"per_second": "0.15", "markup_percent": "25", "source": "test"}
        model.api_schema = {
            "components": {
                "schemas": {
                    "Input": {
                        "type": "object",
                        "required": ["prompt"],
                        "properties": {
                            "prompt": {"type": "string"},
                            "duration": {
                                "type": "integer",
                                "default": 5,
                                "minimum": -1,
                                "maximum": 15,
                            },
                            "reference_images": {"type": "array", "items": {"type": "string"}},
                            "reference_audios": {"type": "array", "items": {"type": "string"}},
                        },
                    }
                }
            }
        }


@pytest.mark.parametrize("change_duration,expected_seconds", [(False, 5), (True, 10)])
async def test_seedance_prompt_only_reaches_quote_with_correct_duration(
    rt, change_duration, expected_seconds
):
    from sqlalchemy import select

    from premium_bot.db import Job

    await seedance_model(rt)
    rt.bot.session.make_request = AsyncMock(return_value=True)
    dp = dispatcher(rt)
    base = message(1, "", 100)
    context = FSMContext(storage=dp.storage, key=StorageKey(bot_id=rt.bot.id, chat_id=1, user_id=1))

    async def callback(data, number):
        cb = CallbackQuery(
            id=str(number),
            from_user=base.from_user,
            chat_instance="optional",
            message=base,
            data=data,
        )
        await dp.feed_update(rt.bot, Update(update_id=number, callback_query=cb), rt=rt)

    await callback("model:seedance", 101)
    sent = next(
        call.args[1]
        for call in reversed(rt.bot.session.make_request.call_args_list)
        if getattr(call.args[1], "reply_markup", None) is not None
    )
    assert any(b.text == "Без фото" for row in sent.reply_markup.inline_keyboard for b in row)
    await callback("images:done", 102)
    assert await context.get_state() == Studio.prompt.state
    await dp.feed_update(
        rt.bot, Update(update_id=103, message=message(1, "A camera flies over a city", 103)), rt=rt
    )
    sent = rt.bot.session.make_request.call_args.args[1]
    buttons = [b.callback_data for row in sent.reply_markup.inline_keyboard for b in row]
    assert "duration:next" in buttons
    assert "trim" not in buttons
    if change_duration:
        await callback("duration:next", 104)
    await callback("quote", 105)
    async with rt.sessions() as s:
        job = await s.scalar(select(Job).where(Job.model_key == "seedance"))
        assert job is not None and job.input_ids == []
        assert job.seconds == expected_seconds
        assert job.price == Decimal(expected_seconds) * Decimal("0.1875")
    await dp.storage.close()


async def test_seedance_photo_can_skip_audio_but_s2v_cannot(rt):
    from datetime import timedelta

    from premium_bot.db import Media
    from premium_bot.domain import now

    await seedance_model(rt)
    async with rt.sessions.begin() as s:
        photo = Media(
            user_id=1,
            kind="photo",
            path="test-photo.png",
            mime="image/png",
            size=10,
            megapixels=1,
            expires_at=now() + timedelta(hours=1),
        )
        s.add(photo)
        await s.flush()
        photo_id = str(photo.id)
    rt.bot.session.make_request = AsyncMock(return_value=True)
    dp = dispatcher(rt)
    context = FSMContext(storage=dp.storage, key=StorageKey(bot_id=rt.bot.id, chat_id=1, user_id=1))
    base = message(1, "", 110)

    async def callback(data, number):
        cb = CallbackQuery(
            id=str(number),
            from_user=base.from_user,
            chat_instance="optional",
            message=base,
            data=data,
        )
        await dp.feed_update(rt.bot, Update(update_id=number, callback_query=cb), rt=rt)

    await context.set_state(Studio.images)
    await context.set_data({"model": "seedance", "media": [photo_id], "params": {}})
    await callback("images:done", 111)
    assert await context.get_state() == Studio.audio.state
    await callback("audio:skip", 112)
    assert await context.get_state() == Studio.prompt.state
    assert (await context.get_data())["media"] == [photo_id]
    for key in ("wan", "omni"):
        await context.set_state(Studio.images)
        await context.set_data({"model": key, "media": [], "params": {}})
        await callback("images:done", 113)
        assert await context.get_state() == Studio.images.state
        await context.set_state(Studio.audio)
        await callback("audio:skip", 114)
        assert await context.get_state() == Studio.audio.state
    await dp.storage.close()


def message(uid, text, number):
    return Message(
        message_id=number,
        date=datetime.now(UTC),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Team"),
        text=text,
    )


async def test_main_menu_fsm_quote_and_old_buttons(rt):
    rt.bot.session.make_request = AsyncMock(return_value=True)
    dp = dispatcher(rt)
    # Administrator bypasses membership, so this test does not fake membership API success.
    async with rt.sessions.begin() as s:
        from decimal import Decimal

        from premium_bot.application.wallet import adjust

        await adjust(s, 1, Decimal(5), "test-admin", "test")
    start = message(1, "/start", 1)
    await dp.feed_update(rt.bot, Update(update_id=1, message=start), rt=rt)
    sent = rt.bot.session.make_request.call_args.args[1]
    assert "AI Студия" in sent.text
    assert any(
        b.callback_data == "a:home" for row in sent.reply_markup.inline_keyboard for b in row
    )

    async def callback(data, number):
        cb = CallbackQuery(
            id=str(number),
            from_user=start.from_user,
            chat_instance="test",
            message=start,
            data=data,
        )
        await dp.feed_update(rt.bot, Update(update_id=number, callback_query=cb), rt=rt)

    await callback("model:z_image", 2)
    context = FSMContext(storage=dp.storage, key=StorageKey(bot_id=rt.bot.id, chat_id=1, user_id=1))
    assert await context.get_state() == Studio.prompt.state
    await dp.feed_update(rt.bot, Update(update_id=3, message=message(1, "portrait", 3)), rt=rt)
    assert await context.get_state() == Studio.parameters.state
    await callback("quote", 4)
    sent_methods = [call.args[1] for call in rt.bot.session.make_request.call_args_list]
    assert any("Цена зафиксирована" in str(getattr(method, "text", "")) for method in sent_methods)
    await callback("unknown:old-button", 5)
    assert "устарела" in rt.bot.session.make_request.call_args.args[1].text
    await dp.storage.close()


async def test_membership_denies_user_without_configured_chats_and_admin_is_protected(rt):
    rt.bot.session.make_request = AsyncMock(return_value=True)
    dp = dispatcher(rt)
    await dp.feed_update(rt.bot, Update(update_id=10, message=message(2, "/start", 10)), rt=rt)
    assert "ограничено" in rt.bot.session.make_request.call_args.args[1].text
    cb = CallbackQuery(
        id="11",
        from_user=message(2, "", 11).from_user,
        chat_instance="x",
        message=message(2, "", 11),
        data="a:home",
    )
    await dp.feed_update(rt.bot, Update(update_id=11, callback_query=cb), rt=rt)
    assert "прав" in rt.bot.session.make_request.call_args.args[1].text
    await dp.storage.close()


async def test_admin_editor_can_be_canceled_with_command(rt):
    from premium_bot.bot.admin import Editor

    rt.bot.session.make_request = AsyncMock(return_value=True)
    dp = dispatcher(rt)
    context = FSMContext(storage=dp.storage, key=StorageKey(bot_id=rt.bot.id, chat_id=1, user_id=1))
    await context.set_state(Editor.value)
    await context.set_data({"action": "prompt", "target": "z_image"})
    await dp.feed_update(rt.bot, Update(update_id=20, message=message(1, "/cancel", 20)), rt=rt)
    assert await context.get_state() is None
    await dp.storage.close()


async def test_uploaded_photo_is_persisted_after_handler_returns(rt):
    import io

    from aiogram.types import PhotoSize
    from PIL import Image
    from sqlalchemy import select

    from premium_bot.db import Media

    rt.bot.session.make_request = AsyncMock(return_value=True)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format="PNG")

    async def download(file, destination, **kwargs):
        destination.write_bytes(buffer.getvalue())

    rt.bot.download = download
    dp = dispatcher(rt)
    context = FSMContext(storage=dp.storage, key=StorageKey(bot_id=rt.bot.id, chat_id=1, user_id=1))
    await context.set_state(Studio.images)
    await context.set_data({"model": "nano", "media": [], "params": {}})
    photo = message(1, None, 30).model_copy(
        update={
            "photo": [
                PhotoSize(
                    file_id="test-file",
                    file_unique_id="unique",
                    width=64,
                    height=64,
                    file_size=len(buffer.getvalue()),
                )
            ]
        }
    )
    await dp.feed_update(rt.bot, Update(update_id=30, message=photo), rt=rt)
    async with rt.sessions() as s:
        item = await s.scalar(select(Media))
        assert item and rt.media.path(item.path).exists()
    await dp.storage.close()


async def test_video_fsm_accepts_audio_and_explicit_duration_trim(rt):
    import io
    import wave

    from aiogram.types import PhotoSize, Voice
    from PIL import Image
    from sqlalchemy import select

    from premium_bot.db import AIModel, Job

    rt.bot.session.make_request = AsyncMock(return_value=True)
    photo_bytes = io.BytesIO()
    Image.new("RGB", (64, 64)).save(photo_bytes, format="PNG")
    audio_bytes = io.BytesIO()
    with wave.open(audio_bytes, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 64000)

    async def download(file, destination, **kwargs):
        destination.write_bytes(
            photo_bytes.getvalue() if file == "photo" else audio_bytes.getvalue()
        )

    rt.bot.download = download
    async with rt.sessions.begin() as s:
        model = await s.get(AIModel, "omni")
        model.enabled, model.version = True, "video-test-version"
        model.api_schema = {
            "components": {
                "schemas": {
                    "Input": {
                        "type": "object",
                        "required": ["prompt", "image", "audio"],
                        "properties": {
                            "prompt": {"type": "string"},
                            "image": {"type": "string"},
                            "audio": {"type": "string"},
                        },
                    }
                }
            }
        }
        model.pricing = {"per_second": "0.1", "markup_percent": "0", "source": "test only"}
    dp = dispatcher(rt)
    context = FSMContext(storage=dp.storage, key=StorageKey(bot_id=rt.bot.id, chat_id=1, user_id=1))
    await context.set_state(Studio.images)
    await context.set_data({"model": "omni", "media": [], "params": {}})
    base = message(1, None, 40)
    photo = base.model_copy(
        update={"photo": [PhotoSize(file_id="photo", file_unique_id="p", width=64, height=64)]}
    )
    await dp.feed_update(rt.bot, Update(update_id=40, message=photo), rt=rt)

    async def callback(data, number):
        cb = CallbackQuery(
            id=str(number), from_user=base.from_user, chat_instance="video", message=base, data=data
        )
        await dp.feed_update(rt.bot, Update(update_id=number, callback_query=cb), rt=rt)

    await callback("images:done", 41)
    assert await context.get_state() == Studio.audio.state
    voice = base.model_copy(
        update={
            "voice": Voice(
                file_id="voice",
                file_unique_id="v",
                duration=4,
                file_size=len(audio_bytes.getvalue()),
            )
        }
    )
    await dp.feed_update(rt.bot, Update(update_id=42, message=voice), rt=rt)
    assert await context.get_state() == Studio.prompt.state
    await dp.feed_update(rt.bot, Update(update_id=43, message=message(1, "Turn head", 43)), rt=rt)
    await callback("trim", 44)
    await dp.feed_update(rt.bot, Update(update_id=45, message=message(1, "2", 45)), rt=rt)
    assert await context.get_state() == Studio.parameters.state
    await callback("quote", 46)
    async with rt.sessions() as s:
        job = await s.scalar(select(Job).where(Job.model_key == "omni"))
        assert job and job.seconds == 2
        assert job.price == Decimal("0.200000")
    await dp.storage.close()
