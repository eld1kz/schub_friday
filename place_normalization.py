from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - fallback for minimal installs
    yaml = None


BRAND_ALIASES = {
    "mcdonald": "McDonald's",
    "mcdonalds": "McDonald's",
    "맥도날드": "McDonald's",
    "lotteria": "Lotteria",
    "롯데리아": "Lotteria",
    "burger king": "Burger King",
    "버거킹": "Burger King",
    "starbucks": "Starbucks",
    "스타벅스": "Starbucks",
    "cu": "CU",
    "gs25": "GS25",
}

BRAND_CATEGORY = {
    "McDonald's": "fast_food",
    "Lotteria": "fast_food",
    "Burger King": "fast_food",
    "Starbucks": "coffee_shop",
    "CU": "convenience_store",
    "GS25": "convenience_store",
}

GOOGLE_TYPE_CATEGORY = {
    "meal_takeaway": "fast_food",
    "fast_food_restaurant": "fast_food",
    "cafe": "coffee_shop",
    "coffee_shop": "coffee_shop",
    "restaurant": "restaurant",
    "food": "restaurant",
    "supermarket": "grocery_store",
    "grocery_or_supermarket": "grocery_store",
    "convenience_store": "convenience_store",
    "gym": "gym",
    "pharmacy": "pharmacy",
    "university": "university",
    "bar": "bar",
}


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("’", "'")).strip()


def normalize_brand(place_name: str) -> str | None:
    low = _norm_text(place_name)
    for alias, brand in BRAND_ALIASES.items():
        if re.search(rf"(^|\W){re.escape(alias.casefold())}(\W|$)", low):
            return brand
    return None


def normalize_category(place_name: str, provider_types: list[str] | None = None) -> tuple[str, str]:
    brand = normalize_brand(place_name)
    if brand and brand in BRAND_CATEGORY:
        return BRAND_CATEGORY[brand], "local_mapping"
    for t in provider_types or []:
        category = GOOGLE_TYPE_CATEGORY.get(str(t).lower())
        if category:
            return category, "places_api"
    return "other", "local_mapping"


def load_category_policies(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or Path(__file__).with_name("habit_category_policies.yaml")
    if yaml:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    policies: dict[str, dict[str, Any]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" ") and raw.endswith(":"):
            current = raw[:-1].strip()
            policies[current] = {}
        elif current and ":" in raw:
            key, value = raw.strip().split(":", 1)
            value = value.strip().strip('"')
            if value.isdigit():
                value = int(value)
            policies[current][key] = value
    return policies
