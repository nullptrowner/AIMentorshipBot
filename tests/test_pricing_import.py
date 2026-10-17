import json
from decimal import Decimal
from pathlib import Path

import pytest

from deploy.configure_pricing import apply_pricing
from premium_bot.application import admin
from premium_bot.application.common import get_setting
from premium_bot.application.pricing import resolve_pricing, validate_profile
from premium_bot.db import AIModel
from premium_bot.domain import BusinessError


def bundle():
    return json.loads(
        (Path(__file__).resolve().parents[1] / "deploy/replicate-pricing.json").read_text()
    )


@pytest.mark.parametrize(
    "model,params,seconds,output_mp,input_mp,cost,price",
    [
        ("flux", {}, "0", "1", "0", "0.030000", "0.037500"),
        ("flux", {}, "0", "2", "3", "0.090000", "0.112500"),
        ("nano", {"resolution": "1K"}, "0", "1", "0", "0.067000", "0.083750"),
        ("nano", {"resolution": "2K"}, "0", "4", "0", "0.101000", "0.126250"),
        ("nano", {"resolution": "4K"}, "0", "16", "0", "0.151000", "0.188750"),
        ("wan", {}, "10", "0", "0", "0.200000", "0.250000"),
        ("seedance", {"resolution": "480p"}, "5", "0", "0", "0.350000", "0.437500"),
        ("seedance", {"resolution": "720p"}, "5", "0", "0", "0.750000", "0.937500"),
        ("omni", {}, "10", "0", "0", "1.600000", "2.000000"),
    ],
)
def test_reviewed_tariffs_and_existing_markup(
    model, params, seconds, output_mp, input_mp, cost, price
):
    profile = bundle()["profiles"][model]
    validate_profile(profile, "25")
    assert profile["markup_percent"] is None
    formula = resolve_pricing(profile, params, "25")
    assert formula.quote(Decimal(seconds), Decimal(output_mp), Decimal(input_mp)) == (
        Decimal(cost),
        Decimal(price),
    )


@pytest.mark.integration
async def test_import_preserves_markup_and_existing_model_settings(rt):
    async with rt.sessions.begin() as s:
        await admin.save_setting(s, rt.cfg, 1, "pricing", {"markup_percent": "25"})
        flux = await s.get(AIModel, "flux")
        flux.prompt = "Keep prompt: {user_prompt}"
        flux.defaults = {"resolution": "2 MP"}
        await apply_pricing(s, rt.cfg, bundle())
    async with rt.sessions() as s:
        assert (await get_setting(s, "pricing"))["markup_percent"] == "25"
        flux = await s.get(AIModel, "flux")
        assert flux.prompt == "Keep prompt: {user_prompt}"
        assert flux.defaults == {"resolution": "2 MP"}
        assert flux.enabled is False
        assert flux.pricing["fixed"] == "0.015"
        assert (await s.get(AIModel, "z_image")).enabled is False


@pytest.mark.integration
async def test_missing_markup_does_not_write_profiles_or_disable_models(rt):
    with pytest.raises(BusinessError, match="общую наценку"):
        async with rt.sessions.begin() as s:
            await apply_pricing(s, rt.cfg, bundle())
    async with rt.sessions() as s:
        assert (await s.get(AIModel, "z_image")).enabled is True
        assert (await s.get(AIModel, "flux")).pricing == {}
