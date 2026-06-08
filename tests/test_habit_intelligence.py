from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from habit_config import CONFIG
from habit_llm_service import HabitLLMService
from habit_policy_service import HabitPolicyService
from habit_repository import InMemoryHabitRepository, iso
from location_service import LocationService
from nudge_service import NudgeService
from places_provider import CachedPlacesProvider, GooglePlacesProvider, MockPlacesProvider, NearbyPlace
from place_normalization import normalize_category
from visit_detection_service import VisitDetectionService
from watcher_service import WatcherService, week_start_for


MCD = NearbyPlace("mcd-1", "McDonald's", 37.0, 127.0, ["restaurant", "food"])


def run(coro):
    return asyncio.run(coro)


def services(repo=None, provider=None, config=None):
    repo = repo or InMemoryHabitRepository()
    sent = []
    nudges = NudgeService(repo, lambda uid, text, buttons=None: sent.append((uid, text, buttons)))
    watchers = WatcherService(repo, nudges)
    policies = HabitPolicyService(repo, nudges)
    detector = VisitDetectionService(
        repo,
        provider or CachedPlacesProvider(MockPlacesProvider([MCD])),
        config=config or replace(CONFIG, min_dwell_seconds=420),
    )
    return repo, sent, LocationService(detector, watchers, policies), watchers, policies


def test_walking_past_mcdonalds_two_minutes_does_not_create_visit():
    repo, sent, loc, *_ = services(config=replace(CONFIG, min_dwell_seconds=420))
    t = datetime(2026, 6, 1, 9, tzinfo=timezone.utc)
    run(loc.handle_location("1", 37.0, 127.0, received_at=t))
    visit = run(loc.handle_location("1", 37.0, 127.0, received_at=t + timedelta(minutes=2)))
    assert visit is None
    assert repo.visits == []


def test_staying_near_mcdonalds_longer_than_dwell_creates_one_visit():
    repo, sent, loc, *_ = services(config=replace(CONFIG, min_dwell_seconds=420))
    t = datetime(2026, 6, 1, 9, tzinfo=timezone.utc)
    run(loc.handle_location("1", 37.0, 127.0, received_at=t))
    visit = run(loc.handle_location("1", 37.0, 127.0, received_at=t + timedelta(minutes=8)))
    assert visit["normalized_category"] == "fast_food"
    assert len(repo.visits) == 1


def test_repeated_gps_updates_do_not_create_duplicate_visits():
    repo, sent, loc, *_ = services(config=replace(CONFIG, min_dwell_seconds=60))
    t = datetime(2026, 6, 1, 9, tzinfo=timezone.utc)
    run(loc.handle_location("1", 37.0, 127.0, received_at=t))
    run(loc.handle_location("1", 37.0, 127.0, received_at=t + timedelta(minutes=2)))
    run(loc.handle_location("1", 37.0, 127.0, received_at=t + timedelta(minutes=3)))
    assert len(repo.visits) == 1


def test_nearby_place_results_are_reused_from_cache_and_small_movements_do_not_call_google_again():
    inner = MockPlacesProvider([MCD])
    repo, sent, loc, *_ = services(
        provider=CachedPlacesProvider(inner),
        config=replace(CONFIG, min_dwell_seconds=60, min_movement_for_new_place_search_meters=50),
    )
    t = datetime(2026, 6, 1, 9, tzinfo=timezone.utc)
    run(loc.handle_location("1", 37.0000, 127.0000, received_at=t))
    run(loc.handle_location("1", 37.0001, 127.0001, received_at=t + timedelta(minutes=2)))
    assert inner.calls == 1


def test_local_mappings_classify_mcdonalds_without_llm_call():
    category, source = normalize_category("McDonald's Seoul", ["restaurant"])
    assert category == "fast_food"
    assert source == "local_mapping"


class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = None


class _Messages:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _Resp(self.text)


class _Claude:
    def __init__(self, text):
        self.messages = _Messages(text)


def test_cached_classifications_prevent_repeated_llm_calls():
    repo = InMemoryHabitRepository()
    llm = HabitLLMService(repo, _Claude('{"normalized_category":"restaurant","normalized_brand":null,"confidence":0.8,"policy_kind":"neutral"}'))
    assert llm.classify_unknown_place("x", "Unknown", ["point_of_interest"])["normalized_category"] == "restaurant"
    assert llm.classify_unknown_place("x", "Unknown", ["point_of_interest"])["normalized_category"] == "restaurant"
    assert llm.claude.messages.calls == 1


def test_three_fast_food_visits_create_no_more_than_one_passive_suggestion():
    repo, sent, loc, watchers, policies = services()
    for i in range(3):
        repo.create_visit({
            "user_id": "1", "provider_place_id": f"m{i}", "place_name": "McDonald's",
            "normalized_brand": "McDonald's", "normalized_category": "fast_food",
            "arrived_at": iso(datetime.now(timezone.utc)), "confirmed_at": iso(datetime.now(timezone.utc)),
            "last_seen_at": iso(datetime.now(timezone.utc)), "source": "telegram_live_location",
        })
    policies.evaluate_passive_suggestion("1", repo.visits[-1])
    policies.evaluate_passive_suggestion("1", repo.visits[-1])
    assert len(repo.suggestions) == 1
    assert len(sent) == 1


def test_dismissed_suggestion_does_not_reappear_during_cooldown():
    repo, sent, loc, watchers, policies = services()
    for i in range(3):
        repo.create_visit({
            "user_id": "1", "provider_place_id": f"m{i}", "place_name": "McDonald's",
            "normalized_brand": "McDonald's", "normalized_category": "fast_food",
            "arrived_at": iso(datetime.now(timezone.utc)), "confirmed_at": iso(datetime.now(timezone.utc)),
            "last_seen_at": iso(datetime.now(timezone.utc)), "source": "telegram_live_location",
        })
    s = policies.evaluate_passive_suggestion("1", repo.visits[-1])
    policies.confirm_habit_suggestion("1", s["id"], False)
    assert policies.evaluate_passive_suggestion("1", repo.visits[-1]) is None


def test_muted_category_never_produces_another_passive_suggestion():
    repo, sent, loc, watchers, policies = services()
    for i in range(3):
        repo.create_visit({
            "user_id": "1", "provider_place_id": f"m{i}", "place_name": "McDonald's",
            "normalized_brand": "McDonald's", "normalized_category": "fast_food",
            "arrived_at": iso(datetime.now(timezone.utc)), "confirmed_at": iso(datetime.now(timezone.utc)),
            "last_seen_at": iso(datetime.now(timezone.utc)), "source": "telegram_live_location",
        })
    s = policies.evaluate_passive_suggestion("1", repo.visits[-1])
    policies.mute_suggestion_category("1", s["id"])
    assert policies.evaluate_passive_suggestion("1", repo.visits[-1]) is None


def test_active_watcher_evaluation_uses_python_without_llm_call():
    repo, sent, loc, watchers, policies = services()
    watchers.create_habit_watcher("1", "weekly_visit_limit", "fast_food", threshold_count=2, window_days=7)
    for i in range(3):
        v = repo.create_visit({
            "user_id": "1", "provider_place_id": f"m{i}", "place_name": "McDonald's",
            "normalized_brand": "McDonald's", "normalized_category": "fast_food",
            "arrived_at": iso(datetime.now(timezone.utc)), "confirmed_at": iso(datetime.now(timezone.utc)),
            "last_seen_at": iso(datetime.now(timezone.utc)), "source": "telegram_live_location",
        })
    watchers.evaluate_visit("1", v)
    assert sent and "You asked me" in sent[0][1]


def test_weekly_count_resets_using_user_timezone():
    start = week_start_for("Asia/Seoul", datetime(2026, 6, 10, 12, tzinfo=timezone.utc))
    assert start.astimezone(timezone.utc).isoformat().startswith("2026-06-07T15:00:00")


def test_nudges_off_blocks_proactive_notifications():
    repo, sent, loc, watchers, policies = services()
    repo.update_settings("1", nudges_enabled=False)
    watchers.create_habit_watcher("1", "weekly_visit_limit", "fast_food", threshold_count=1, window_days=7)
    v = repo.create_visit({
        "user_id": "1", "provider_place_id": "m", "place_name": "McDonald's",
        "normalized_brand": "McDonald's", "normalized_category": "fast_food",
        "arrived_at": iso(datetime.now(timezone.utc)), "confirmed_at": iso(datetime.now(timezone.utc)),
        "last_seen_at": iso(datetime.now(timezone.utc)), "source": "telegram_live_location",
    })
    repo.create_visit({
        "user_id": "1", "provider_place_id": "m2", "place_name": "McDonald's",
        "normalized_brand": "McDonald's", "normalized_category": "fast_food",
        "arrived_at": iso(datetime.now(timezone.utc)), "confirmed_at": iso(datetime.now(timezone.utc)),
        "last_seen_at": iso(datetime.now(timezone.utc)), "source": "telegram_live_location",
    })
    watchers.evaluate_visit("1", v)
    assert sent == []


def test_location_off_blocks_processing():
    repo, sent, loc, *_ = services()
    repo.update_settings("1", tracking_enabled=False)
    run(loc.handle_location("1", 37.0, 127.0))
    assert repo.location_updates == []


def test_edited_live_location_updates_are_processed_by_same_location_service():
    repo, sent, loc, *_ = services(config=replace(CONFIG, min_dwell_seconds=60))
    t = datetime(2026, 6, 1, 9, tzinfo=timezone.utc)
    run(loc.handle_location("1", 37.0, 127.0, received_at=t))
    visit = run(loc.handle_location("1", 37.0, 127.0, received_at=t + timedelta(minutes=2)))
    assert visit is not None


def test_forget_locations_and_habits_delete_data_after_confirmation_path_calls():
    repo, *_ = services()
    repo.save_location_update({"user_id": "1", "latitude": 1, "longitude": 1, "received_at": iso(datetime.now(timezone.utc))})
    repo.create_watcher({"user_id": "1", "rule_type": "weekly_visit_limit", "is_active": True})
    repo.delete_locations("1")
    repo.delete_habits("1")
    assert repo.location_updates == []
    assert repo.watchers == []


def test_daily_nudge_limits_are_enforced():
    repo, sent, loc, watchers, policies = services()
    repo.update_settings("1", daily_nudge_limit=1)
    assert loc.watchers.nudges.send("1", "one", "k1") is True
    assert loc.watchers.nudges.send("1", "two", "k2") is False
    assert len(sent) == 1


def test_places_api_failures_fail_gracefully_without_key():
    provider = GooglePlacesProvider(api_key="")
    assert run(provider.find_nearby_places(37.0, 127.0, 60)) == []


def test_mock_provider_is_used_in_tests():
    provider = MockPlacesProvider([MCD])
    assert run(provider.find_nearby_places(0, 0, 60))[0].provider_place_id == "mcd-1"
    assert provider.calls == 1


def test_llm_fallback_classification_response_is_validated_against_schema():
    repo = InMemoryHabitRepository()
    bad = HabitLLMService(repo, _Claude('{"normalized_category":"restaurant","confidence":2,"policy_kind":"bad"}'))
    assert bad.classify_unknown_place("x", "Unknown", []) is None


def test_llm_fallback_is_rate_limited():
    repo = InMemoryHabitRepository()
    llm = HabitLLMService(repo, _Claude("{}"))
    for i in range(CONFIG.llm_fallback_daily_limit):
        repo.save_nudge({
            "user_id": "1", "message": "", "deduplication_key": f"llm:{i}",
            "sent_at": iso(datetime.now(timezone.utc)),
        })
    assert llm.fallback_allowed("1") is False


def test_unknown_classification_response_can_be_cached_when_valid():
    repo = InMemoryHabitRepository()
    good = HabitLLMService(repo, _Claude('{"normalized_category":"restaurant","normalized_brand":null,"confidence":0.7,"policy_kind":"neutral"}'))
    good.classify_unknown_place("x", "Unknown", [])
    assert repo.get_classification("x")["classification_source"] == "llm_fallback"


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            suite.addTest(unittest.FunctionTestCase(obj))
    return suite
