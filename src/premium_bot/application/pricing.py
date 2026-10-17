from premium_bot.domain import BusinessError, Json, Pricing


def resolve_pricing(raw: Json, params: Json, default_markup: object) -> Pricing:
    """Optional per-parameter cost overrides, e.g. resolution -> 720p -> per_second."""
    result = dict(raw)
    variants = result.pop("variants", {})
    if not isinstance(variants, dict):
        raise BusinessError("Варианты цены должны быть объектом.")
    for field, choices in variants.items():
        if not isinstance(choices, dict):
            raise BusinessError("Некорректные варианты цены.")
        override = choices.get(str(params.get(field)))
        if override is not None:
            if not isinstance(override, dict) or "variants" in override:
                raise BusinessError("Некорректный вариант цены.")
            result.update(override)
    if result.get("markup_percent") is None:
        result["markup_percent"] = default_markup
    return Pricing.model_validate(result)


def validate_profile(raw: Json, default_markup: object) -> None:
    resolve_pricing(raw, {}, default_markup)
    for field, choices in raw.get("variants", {}).items():
        for value in choices:
            resolve_pricing(raw, {field: value}, default_markup)
