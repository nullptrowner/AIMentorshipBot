from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError

from premium_bot.domain import BusinessError, Json


@dataclass(frozen=True)
class ModelInfo:
    key: str
    slug: str
    name: str
    kind: str
    tier: str
    max_images: int
    audio: bool
    max_audio_seconds: int | None = None
    min_images: int = 0
    requires_audio: bool = False


class ModelProvider(Protocol):
    info: ModelInfo

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json: ...


class ZImage:
    info = ModelInfo(
        "z_image", "prunaai/z-image-turbo", "Z-Image Turbo", "photo", "Бюджет", 0, False
    )

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json:
        if images or audio:
            raise BusinessError(
                "Z-Image Turbo принимает только текст. Для редактирования выберите другую модель."
            )
        if params.get("num_outputs", 1) != 1:
            raise BusinessError("Этот endpoint Z-Image поддерживает один результат за запуск.")
        return {**params, "prompt": prompt}


class Flux:
    info = ModelInfo(
        "flux", "black-forest-labs/flux-2-pro", "FLUX.2 Pro", "photo", "Стандарт", 8, False
    )

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json:
        return {**params, "prompt": prompt, "input_images": images}


class NanoBanana:
    info = ModelInfo("nano", "google/nano-banana-2", "Nano Banana 2", "photo", "Про", 14, False)

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json:
        return {**params, "prompt": prompt, "image_input": images}


class Wan:
    info = ModelInfo(
        "wan",
        "wan-video/wan-2.2-s2v",
        "Wan 2.2 S2V",
        "video",
        "Бюджет",
        1,
        True,
        min_images=1,
        requires_audio=True,
    )

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json:
        if len(images) != 1 or not audio:
            raise BusinessError("Для Wan нужны одно фото и аудио.")
        return {**params, "prompt": prompt, "image": images[0], "audio": audio}


class Seedance:
    info = ModelInfo(
        "seedance",
        "bytedance/seedance-2.0-fast",
        "Seedance 2.0 Fast",
        "video",
        "Стандарт",
        9,
        True,
        15,
    )

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json:
        if audio:
            if not images:
                raise BusinessError("Для аудиореференса Seedance требуется изображение.")
            return {
                **params,
                "prompt": prompt,
                "reference_images": images,
                "reference_audios": [audio],
                "duration": max(
                    1, min(15, int(seconds.to_integral_value(rounding="ROUND_CEILING")))
                ),
            }
        return {**params, "prompt": prompt, **({"reference_images": images} if images else {})}


class OmniHuman:
    info = ModelInfo(
        "omni",
        "bytedance/omni-human-1.5",
        "OmniHuman 1.5",
        "video",
        "Про",
        1,
        True,
        35,
        min_images=1,
        requires_audio=True,
    )

    def build(
        self, prompt: str, params: Json, images: list[str], audio: str | None, seconds: Decimal
    ) -> Json:
        if len(images) != 1 or not audio or seconds >= 35:
            raise BusinessError("OmniHuman: одно фото и аудио короче 35 секунд.")
        return {**params, "prompt": prompt, "image": images[0], "audio": audio}


REGISTRY: dict[str, ModelProvider] = {
    adapter.info.key: adapter
    for adapter in (ZImage(), Flux(), NanoBanana(), Wan(), Seedance(), OmniHuman())
}


def validate_input(schema: Json, data: Json) -> None:
    try:
        inp = schema["components"]["schemas"]["Input"]
        Draft7Validator({**inp, "components": schema["components"]}).validate(data)
        unknown = data.keys() - inp.get("properties", {}).keys()
        if unknown:
            raise BusinessError("Неподдерживаемые параметры: " + ", ".join(sorted(unknown)))
    except (KeyError, TypeError) as exc:
        raise BusinessError("Схема модели не подтверждена. Обратитесь к администратору.") from exc
    except ValidationError as exc:
        raise BusinessError(
            "Параметры не соответствуют API модели: " + ".".join(str(x) for x in exc.path)
        ) from exc


class Replicate:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        self.token, self.client = token, client

    async def request(
        self, method: str, path: str, data: Json | None = None, cancel_after: int | None = None
    ) -> Json:
        if not self.token:
            raise BusinessError("Replicate ещё не настроен.")
        headers = {"Authorization": f"Bearer {self.token}"}
        if cancel_after:
            headers["Cancel-After"] = f"{cancel_after}s"
        response = await self.client.request(
            method, "https://api.replicate.com/v1/" + path, json=data, headers=headers, timeout=30
        )
        response.raise_for_status()
        return dict(response.json())

    async def schema(self, slug: str) -> tuple[str, Json]:
        data = await self.request("GET", "models/" + slug)
        version = data.get("latest_version")
        if not version or not version.get("openapi_schema"):
            raise BusinessError("Replicate не вернул версию и схему модели.")
        return str(version["id"]), dict(version["openapi_schema"])

    async def create(self, version: str, data: Json, webhook: str, max_seconds: int) -> Json:
        return await self.request(
            "POST",
            "predictions",
            {
                "version": version,
                "input": data,
                "webhook": webhook,
                "webhook_events_filter": ["start", "completed"],
            },
            cancel_after=max_seconds,
        )

    async def get(self, prediction_id: str) -> Json:
        return await self.request("GET", "predictions/" + prediction_id)

    async def cancel(self, prediction_id: str) -> Json:
        return await self.request("POST", f"predictions/{prediction_id}/cancel")
