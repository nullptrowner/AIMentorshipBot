import base64
import hashlib
import hmac
from decimal import Decimal

import pytest

from premium_bot.domain import BusinessError, PlanInput, Pricing, money
from premium_bot.infrastructure.replicate import REGISTRY, validate_input
from premium_bot.infrastructure.security import (
    check_download_url,
    media_signature,
    verify_crypto,
    verify_media,
    verify_replicate,
)


def test_price_decimal_precision_and_rounding():
    rule = Pricing(fixed="0.005", per_second="0.02", markup_percent="25", source="test")
    cost, price = rule.quote(Decimal("7.125"), Decimal(0), Decimal(0))
    assert cost == Decimal("0.147500")
    assert price == Decimal("0.184375")
    assert money("0.0000001") == Decimal("0.000001")


def test_step_pricing_covers_megapixel_boundaries():
    rule = Pricing(
        markup_percent="20",
        source="test-only",
        output_tiers=[
            {"max_mp": "0.5", "cost": "0.0025"},
            {"max_mp": "1", "cost": "0.005"},
            {"max_mp": "2", "cost": "0.01"},
        ],
    )
    assert rule.quote(Decimal(0), Decimal("1.048576"), Decimal(0)) == (
        Decimal("0.010000"),
        Decimal("0.012000"),
    )
    with pytest.raises(BusinessError):
        rule.quote(Decimal(0), Decimal("3"), Decimal(0))


@pytest.mark.parametrize(
    "key,images,audio",
    [
        ("z_image", [], None),
        ("flux", ["image"], None),
        ("nano", ["image"], None),
        ("wan", ["image"], "audio"),
        ("seedance", ["image"], "audio"),
        ("omni", ["image"], "audio"),
    ],
)
def test_each_adapter_builds_its_own_model_input(key, images, audio):
    result = REGISTRY[key].build("prompt", {}, images, audio, Decimal(5))
    assert result["prompt"] == "prompt"
    if key == "flux":
        assert result["input_images"] == images
    if key == "nano":
        assert result["image_input"] == images
    if key in {"wan", "omni"}:
        assert result["image"] == "image" and result["audio"] == "audio"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e10000000"])
def test_nonfinite_money_rejected(value):
    with pytest.raises(ValueError):
        money(value)


def test_six_adapters_and_no_silent_model_substitution():
    assert len(REGISTRY) == 6
    with pytest.raises(BusinessError):
        REGISTRY["z_image"].build("portrait", {}, ["image"], None, Decimal(0))
    result = REGISTRY["seedance"].build("move", {}, ["photo"], "audio", Decimal("5.2"))
    assert result["reference_audios"] == ["audio"]
    assert result["duration"] == 6
    assert "audio" not in result
    with pytest.raises(BusinessError):
        REGISTRY["omni"].build("move", {}, ["photo"], "audio", Decimal(35))


def test_api_schema_rejects_unsupported_and_invalid_fields():
    schema = {
        "components": {
            "schemas": {
                "Input": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                }
            }
        }
    }
    validate_input(schema, {"prompt": "test"})
    with pytest.raises(BusinessError):
        validate_input(schema, {"prompt": "test", "invented_parameter": True})
    with pytest.raises(BusinessError):
        validate_input(schema, {"prompt": 123})


def test_replicate_webhook_raw_body_and_replay_window():
    body = b'{"id":"abc"}'
    secret = b"secret"
    signature = base64.b64encode(hmac.digest(secret, b"event.1000." + body, "sha256")).decode()
    headers = {
        "webhook-id": "event",
        "webhook-timestamp": "1000",
        "webhook-signature": "v1," + signature,
    }
    key = "whsec_" + base64.b64encode(secret).decode()
    assert verify_replicate(body, headers, key, clock=1001)
    assert not verify_replicate(body + b" ", headers, key, clock=1001)
    assert not verify_replicate(body, headers, key, clock=1400)
    assert not verify_replicate(body, {}, key, clock=1001)
    assert not verify_replicate(body, headers, "", clock=1001)
    assert not verify_replicate(body, headers, "whsec_", clock=1001)


def test_crypto_signature_is_not_token_hmac():
    body, token = b'{"update_id":1}', "test-token"
    signature = hmac.new(hashlib.sha256(token.encode()).digest(), body, hashlib.sha256).hexdigest()
    assert verify_crypto(body, signature, token)
    assert not verify_crypto(body + b" ", signature, token)
    assert not verify_crypto(body, "", token)


@pytest.mark.parametrize(
    "url",
    [
        "http://replicate.delivery/a",
        "https://127.0.0.1/a",
        "https://replicate.delivery.attacker.example/a",
        "https://user:pass@replicate.delivery/a",
        "https://replicate.delivery:444/a",
    ],
)
def test_ssrf_url_rejection(url):
    with pytest.raises(BusinessError):
        check_download_url(url)


def test_plan_duration_buckets_are_independent_and_validated():
    plan = PlanInput(
        name="Team",
        months=1,
        price="20",
        limits={
            "wan": [{"max_seconds": 5, "daily_count": 2}, {"max_seconds": 10, "daily_count": 1}]
        },
    )
    assert len(plan.limits["wan"]) == 2
    with pytest.raises(ValueError):
        PlanInput(
            name="Team",
            months=2,
            price="20",
            limits={"wan": [{"max_seconds": 5, "daily_count": 2}]},
        )


def test_media_signatures_expire_and_bind_identity():
    import time

    stamp = int(time.time()) + 60
    signature = media_signature("file", stamp, "secret")
    assert verify_media("file", stamp, signature, "secret")
    assert not verify_media("other", stamp, signature, "secret")
    assert not verify_media("file", 1, signature, "secret")
