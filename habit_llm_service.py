from __future__ import annotations

import json
from datetime import timedelta

from habit_config import CONFIG
from habit_logging import metric
from habit_repository import iso, utcnow


WATCHER_SCHEMA_KEYS = {
    "intent", "rule_type", "target_category", "target_brand",
    "threshold_count", "window_days", "reminder_text", "requires_confirmation",
}


class HabitLLMService:
    WATCHER_SYSTEM = (
        "Convert one habit watcher request into strict JSON. "
        "Do not include prose. Supported rule_type values: weekly_visit_limit, "
        "rolling_window_limit, near_category_reminder, near_place_reminder. "
        "Use conservative categories such as fast_food, grocery_store, coffee_shop, restaurant. "
        "Always set requires_confirmation true."
    )
    CLASSIFY_SYSTEM = (
        "Classify one place into strict JSON with keys normalized_category, normalized_brand, "
        "confidence, policy_kind. Use neutral, review_if_repeated, or user_goal_only. "
        "Do not infer sensitive personal attributes."
    )

    def __init__(self, repo, claude_client=None, model: str = CONFIG.habit_classifier_model):
        self.repo = repo
        self.claude = claude_client
        self.model = model

    def parse_watcher_request(self, user_text: str) -> dict | None:
        if not self.claude:
            return self._local_parse_watcher(user_text)
        resp = self.claude.messages.create(
            model=self.model,
            max_tokens=250,
            system=self.WATCHER_SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"user_text": user_text})}],
        )
        self._log_usage(resp)
        return self._validate_watcher(self._json_from_response(resp))

    def classify_unknown_place(self, provider_place_id: str, place_name: str, provider_types: list[str]) -> dict | None:
        cached = self.repo.get_classification(provider_place_id)
        if cached:
            metric("llm_cache_hit", kind="place_classification")
            return cached
        if not self.claude:
            return None
        resp = self.claude.messages.create(
            model=self.model,
            max_tokens=180,
            system=self.CLASSIFY_SYSTEM,
            messages=[{"role": "user", "content": json.dumps({
                "place_name": place_name,
                "provider_types": provider_types,
            }, ensure_ascii=False)}],
        )
        self._log_usage(resp)
        data = self._json_from_response(resp)
        if not self._valid_classification(data):
            return None
        row = {
            "provider_place_id": provider_place_id,
            "place_name": place_name,
            "normalized_brand": data.get("normalized_brand"),
            "normalized_category": data["normalized_category"],
            "classification_source": "llm_fallback",
            "confidence": float(data.get("confidence") or 0),
            "created_at": iso(utcnow()),
            "updated_at": iso(utcnow()),
        }
        metric("llm_fallback_call", kind="place_classification")
        return self.repo.save_classification(row)

    def fallback_allowed(self, user_id: str) -> bool:
        since = utcnow() - timedelta(days=1)
        rows = [
            r for r in getattr(self.repo, "nudges", [])
            if r.get("user_id") == user_id and r.get("deduplication_key", "").startswith("llm:") and r.get("sent_at") and r["sent_at"] >= iso(since)
        ]
        return len(rows) < CONFIG.llm_fallback_daily_limit

    def _json_from_response(self, resp) -> dict:
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        if "{" in raw and "}" in raw:
            raw = raw[raw.find("{"): raw.rfind("}") + 1]
        return json.loads(raw)

    def _log_usage(self, resp) -> None:
        usage = getattr(resp, "usage", None)
        if usage:
            metric(
                "llm_tokens",
                prompt_tokens=getattr(usage, "input_tokens", 0),
                completion_tokens=getattr(usage, "output_tokens", 0),
            )

    def _validate_watcher(self, data: dict) -> dict | None:
        if not isinstance(data, dict) or not set(data).issubset(WATCHER_SCHEMA_KEYS):
            return None
        if data.get("intent") != "create_watcher":
            return None
        if data.get("rule_type") not in {
            "weekly_visit_limit", "rolling_window_limit", "near_category_reminder", "near_place_reminder"
        }:
            return None
        data["requires_confirmation"] = True
        return data

    def _valid_classification(self, data: dict) -> bool:
        return (
            isinstance(data, dict)
            and isinstance(data.get("normalized_category"), str)
            and data.get("policy_kind") in {"neutral", "review_if_repeated", "user_goal_only"}
            and 0 <= float(data.get("confidence") or 0) <= 1
        )

    def _local_parse_watcher(self, user_text: str) -> dict | None:
        text = user_text.lower()
        if "grocery" in text and "remind" in text:
            return {
                "intent": "create_watcher",
                "rule_type": "near_category_reminder",
                "target_category": "grocery_store",
                "target_brand": None,
                "threshold_count": None,
                "window_days": None,
                "reminder_text": user_text.split("remind me to", 1)[-1].strip() if "remind me to" in text else None,
                "requires_confirmation": True,
            }
        if "fast food" in text:
            threshold = 2 if "twice" in text or "two" in text else None
            return {
                "intent": "create_watcher",
                "rule_type": "weekly_visit_limit",
                "target_category": "fast_food",
                "target_brand": None,
                "threshold_count": threshold,
                "window_days": 7,
                "reminder_text": None,
                "requires_confirmation": True,
            }
        return None
