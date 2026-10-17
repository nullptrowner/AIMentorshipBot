import asyncio
import json
import os
import time
import uuid
import warnings
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from premium_bot.config import Settings
from premium_bot.db import Media
from premium_bot.domain import BusinessError, Json, now
from premium_bot.infrastructure.security import check_download_url, media_signature, resolve_public


async def run_media(*args: str) -> bytes:
    # Uploaded playlists must not cause ffmpeg/ffprobe to fetch URLs or other demuxers.
    args = (
        args[0],
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        "wav,mp3,ogg,flac,aac,matroska,webm,mov,aiff,asf",
        *args[1:],
    )
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=45)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise BusinessError("Не удалось прочитать медиафайл.")
    return out


def inspect_image(path: Path) -> tuple[str, Decimal]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in {"JPEG", "PNG", "WEBP", "GIF"} or getattr(
                    image, "is_animated", False
                ):
                    raise BusinessError("Нужен статичный JPEG, PNG или WebP.")
                if image.width * image.height > 24_000_000:
                    raise BusinessError("Изображение слишком большое: максимум 24 мегапикселя.")
                mime = Image.MIME[image.format]
                pixels = Decimal(image.width * image.height) / 1_000_000
                image.verify()
                return mime, pixels
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise BusinessError("Файл не является корректным изображением.") from exc


async def probe(path: Path) -> Json:
    raw = await run_media(
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)
    )
    data: Json = json.loads(raw)
    return data


class MediaStore:
    def __init__(self, cfg: Settings, client: httpx.AsyncClient) -> None:
        self.cfg, self.client = cfg, client
        cfg.media_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, name: str) -> Path:
        if Path(name).name != name:
            raise BusinessError("Недопустимый путь.")
        return self.cfg.media_dir / name

    def photo_preview(self, item: Media) -> Path:
        preview = self.path(uuid.uuid4().hex + ".preview.jpg")
        with Image.open(self.path(item.path)) as image:
            image.thumbnail((2048, 2048))
            image.convert("RGB").save(preview, format="JPEG", quality=90)
        return preview

    async def ensure_space(self, needed: int) -> None:
        total = await asyncio.to_thread(
            lambda: sum(p.stat().st_size for p in self.cfg.media_dir.iterdir() if p.is_file())
        )
        free = await asyncio.to_thread(
            lambda: (
                os.statvfs(self.cfg.media_dir).f_bavail * os.statvfs(self.cfg.media_dir).f_frsize
            )
        )
        if total + needed > self.cfg.media_quota_bytes or free < needed + 500_000_000:
            raise BusinessError(
                "Хранилище заполнено. Администратор уже может проверить его состояние."
            )

    async def inspect(self, path: Path, uid: int, kind: str) -> Media:
        size = (await asyncio.to_thread(path.stat)).st_size
        if not size or size > self.cfg.max_upload_bytes:
            raise BusinessError("Файл пустой или превышает допустимый размер.")
        seconds, mp = Decimal(0), Decimal(0)
        if kind == "photo":
            mime, mp = await asyncio.to_thread(inspect_image, path)
        else:
            data = await probe(path)
            if not any(s.get("codec_type") == "audio" for s in data.get("streams", [])) or any(
                s.get("codec_type") == "video" for s in data.get("streams", [])
            ):
                raise BusinessError("Отправьте аудиофайл или голосовое сообщение.")
            seconds = Decimal(str(data.get("format", {}).get("duration", "0")))
            if not seconds.is_finite() or not 0 < seconds <= self.cfg.max_audio_seconds:
                raise BusinessError(
                    f"Длительность аудио: от 1 до {self.cfg.max_audio_seconds} секунд."
                )
            normalized = self.path(uuid.uuid4().hex + ".wav")
            try:
                await run_media(
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    "-threads",
                    "1",
                    "-y",
                    str(normalized),
                )
                await asyncio.to_thread(path.unlink, missing_ok=True)
                path = normalized
                size = (await asyncio.to_thread(path.stat)).st_size
            except BaseException:
                normalized.unlink(missing_ok=True)
                raise
            mime = "audio/wav"
        return Media(
            user_id=uid,
            kind=kind,
            path=path.name,
            mime=mime,
            size=size,
            seconds=seconds,
            megapixels=mp,
            expires_at=now() + timedelta(hours=self.cfg.input_retention_hours),
        )

    def url(self, item: Media) -> str:
        expires = int(time.time()) + 3600
        signature = media_signature(str(item.id), expires, self.cfg.media_secret.get_secret_value())
        return f"{self.cfg.public_url}/media/{item.id}?expires={expires}&signature={signature}"

    async def download(self, url: str, uid: int, job_id: uuid.UUID, kind: str) -> Media:
        await self.ensure_space(self.cfg.max_output_bytes)
        name = uuid.uuid4().hex + (".mp4" if kind == "video" else ".img")
        path = self.path(name)
        try:
            for _ in range(4):
                host, port = check_download_url(url)
                await asyncio.to_thread(resolve_public, host, port)
                async with self.client.stream(
                    "GET", url, follow_redirects=False, timeout=120
                ) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers["location"]))
                        continue
                    response.raise_for_status()
                    size = 0
                    with path.open("xb") as file:
                        async for chunk in response.aiter_bytes(65536):
                            size += len(chunk)
                            if size > self.cfg.max_output_bytes:
                                raise BusinessError("Результат превышает лимит доставки Telegram.")
                            file.write(chunk)
                    break
            else:
                raise BusinessError("Слишком много перенаправлений при скачивании.")
            if kind == "photo":
                mime, mp = await asyncio.to_thread(inspect_image, path)
                seconds = Decimal(0)
            else:
                data = await probe(path)
                if not any(s.get("codec_type") == "video" for s in data.get("streams", [])):
                    raise BusinessError("Провайдер вернул некорректное видео.")
                mime, mp = "video/mp4", Decimal(0)
                seconds = Decimal(str(data["format"]["duration"]))
            return Media(
                user_id=uid,
                job_id=job_id,
                kind=kind,
                path=name,
                mime=mime,
                size=size,
                seconds=seconds,
                megapixels=mp,
                output=True,
                expires_at=now() + timedelta(hours=self.cfg.output_retention_hours),
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
