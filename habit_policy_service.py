from __future__ import annotations

from datetime import timedelta

from habit_config import CONFIG
from habit_repository import iso, utcnow
from place_normalization import load_category_policies


class HabitPolicyService:
    def __init__(self, repo, nudge_service, policies: dict | None = None):
        self.repo = repo
        self.nudges = nudge_service
        self.policies = policies or load_category_policies()

    def evaluate_passive_suggestion(self, user_id: str, visit: dict) -> dict | None:
        settings = self.repo.ensure_settings(user_id)
        if not settings.get("habit_suggestions_enabled", True):
            return None
        category = visit["normalized_category"]
        policy = self.policies.get(category) or self.policies.get("other") or {}
        if policy.get("policy_kind") != "review_if_repeated":
            return None
        window_days = int(policy.get("window_days") or 7)
        threshold = int(policy.get("passive_suggestion_threshold") or policy.get("default_threshold") or 3)
        since = utcnow() - timedelta(days=window_days)
        count = len(self.repo.visits_by_category_since(user_id, category, since))
        if count < threshold:
            return None
        cooldown_days = int(policy.get("cooldown_days") or CONFIG.passive_suggestion_cooldown_days)
        recent = self.repo.suggestions_for_category_since(
            user_id, category, utcnow() - timedelta(days=cooldown_days)
        )
        if any(r.get("status") == "muted" for r in self.repo.suggestions_for_category_since(user_id, category, utcnow() - timedelta(days=3650))):
            return None
        if recent:
            return None
        period_key = f"{category}:{utcnow().date().isoformat()}"
        suggestion = self.repo.create_suggestion({
            "user_id": user_id,
            "category": category,
            "period_key": period_key,
            "observed_count": count,
            "status": "pending",
            "suggested_at": iso(utcnow()),
            "responded_at": None,
        })
        template = policy.get("message_template") or (
            "You have visited {target} {count} times this week. "
            "Would you like me to keep an eye on this habit?"
        )
        message = template.format(count=count, target=category.replace("_", " "))
        key = f"passive:{user_id}:{category}:{period_key}"
        self.nudges.send(
            user_id,
            message,
            key,
            habit_suggestion_id=suggestion["id"],
            place_visit_id=visit["id"],
            buttons=[
                ("Yes, track it", f"habit:yes:{suggestion['id']}"),
                ("No thanks", f"habit:no:{suggestion['id']}"),
                ("Don't suggest this again", f"habit:mute:{suggestion['id']}"),
            ],
        )
        return suggestion

    def confirm_habit_suggestion(self, user_id: str, suggestion_id: str, accepted: bool) -> str:
        suggestion = self.repo.get_suggestion(user_id, suggestion_id)
        if not suggestion:
            return "That habit suggestion has expired."
        status = "accepted" if accepted else "dismissed"
        self.repo.update_suggestion(user_id, suggestion_id, status=status, responded_at=iso(utcnow()))
        if not accepted:
            return "Got it. I will not set up tracking for that suggestion."
        # The accepted passive suggestion becomes an explicit weekly watcher.
        self.repo.create_watcher({
            "user_id": user_id,
            "rule_type": "weekly_visit_limit",
            "target_category": suggestion["category"],
            "target_brand": None,
            "target_place_id": None,
            "threshold_count": 2,
            "window_days": 7,
            "reminder_text": None,
            "is_active": True,
            "cooldown_hours": 24,
            "created_at": iso(utcnow()),
            "updated_at": iso(utcnow()),
        })
        return "Got it. I will let you know when you exceed two fast-food visits during a calendar week."

    def mute_suggestion_category(self, user_id: str, suggestion_id: str) -> str:
        self.repo.update_suggestion(user_id, suggestion_id, status="muted", responded_at=iso(utcnow()))
        return "Okay. I will not suggest that category again."
