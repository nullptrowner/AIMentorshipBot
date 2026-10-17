import wave

import httpx
import pytest
from PIL import Image

from premium_bot.domain import BusinessError
from premium_bot.infrastructure.media import MediaStore, inspect_image


def test_reject_fake_image_and_read_actual_mime(tmp_path):
    path = tmp_path / "photo.exe"
    path.write_bytes(b"not an image")
    with pytest.raises(BusinessError):
        inspect_image(path)
    Image.new("RGB", (100, 200)).save(path, format="PNG")
    mime, mp = inspect_image(path)
    assert mime == "image/png"
    assert str(mp) == "0.02"


async def test_audio_normalization_and_duration(cfg):
    cfg.media_dir.mkdir()
    path = cfg.media_dir / "input.bin"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(16000)
        file.writeframes(b"\x00\x00" * 16000)
    async with httpx.AsyncClient() as client:
        store = MediaStore(cfg, client)
        item = await store.inspect(path, 2, "audio")
        assert item.mime == "audio/wav"
        assert item.seconds == 1
        assert store.path(item.path).exists()
        assert not path.exists()


async def test_audio_duration_limit(cfg):
    cfg.media_dir.mkdir()
    cfg.max_audio_seconds = 1
    path = cfg.media_dir / "long.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\x00\x00" * 16000)
    async with httpx.AsyncClient() as client:
        with pytest.raises(BusinessError):
            await MediaStore(cfg, client).inspect(path, 2, "audio")


async def test_download_checks_redirect_target(cfg, monkeypatch):
    import premium_bot.infrastructure.media as module

    monkeypatch.setattr(module, "resolve_public", lambda host, port: None)

    async def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = MediaStore(cfg, client)
        import uuid

        with pytest.raises(BusinessError):
            await store.download("https://replicate.delivery/test", 2, uuid.uuid4(), "photo")
        assert not list(cfg.media_dir.iterdir())


async def test_audio_playlist_cannot_fetch_network(cfg):
    cfg.media_dir.mkdir()
    path = cfg.media_dir / "disguised.mp3"
    path.write_text("#EXTM3U\nhttp://127.0.0.1:9/private\n")
    async with httpx.AsyncClient() as client:
        with pytest.raises(BusinessError):
            await MediaStore(cfg, client).inspect(path, 2, "audio")
