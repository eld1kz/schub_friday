import os
from dataclasses import dataclass


@dataclass(frozen=True)
class HabitConfig:
    places_cache_minutes: int = int(os.environ.get("PLACES_CACHE_MINUTES", "20"))
    place_search_radius_meters: int = int(os.environ.get("PLACE_SEARCH_RADIUS_METERS", "60"))
    min_movement_for_new_place_search_meters: int = int(
        os.environ.get("MIN_MOVEMENT_FOR_NEW_PLACE_SEARCH_METERS", "50")
    )
    min_dwell_seconds: int = int(os.environ.get("MIN_DWELL_SECONDS", "420"))
    visit_deduplication_hours: int = int(os.environ.get("VISIT_DEDUPLICATION_HOURS", "3"))
    location_freshness_minutes: int = int(os.environ.get("LOCATION_FRESHNESS_MINUTES", "15"))
    location_retention_days: int = int(os.environ.get("LOCATION_RETENTION_DAYS", "7"))
    default_daily_nudge_limit: int = int(os.environ.get("DEFAULT_DAILY_NUDGE_LIMIT", "3"))
    passive_suggestion_cooldown_days: int = int(
        os.environ.get("PASSIVE_SUGGESTION_COOLDOWN_DAYS", "14")
    )
    habit_classifier_model: str = os.environ.get("HABIT_CLASSIFIER_MODEL", "claude-haiku-4-5")
    llm_fallback_daily_limit: int = int(os.environ.get("HABIT_LLM_FALLBACK_DAILY_LIMIT", "5"))


CONFIG = HabitConfig()
