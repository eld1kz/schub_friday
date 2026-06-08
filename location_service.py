from __future__ import annotations

from datetime import datetime


class LocationService:
    def __init__(self, visit_detection, watcher_service, habit_policy_service):
        self.visit_detection = visit_detection
        self.watchers = watcher_service
        self.policies = habit_policy_service

    async def handle_location(
        self,
        user_id: str,
        latitude: float,
        longitude: float,
        accuracy_meters: float | None = None,
        received_at: datetime | None = None,
    ) -> dict | None:
        visit = await self.visit_detection.process_location_update(
            user_id, latitude, longitude, accuracy_meters, received_at
        )
        if visit:
            self.watchers.evaluate_visit(user_id, visit)
            self.policies.evaluate_passive_suggestion(user_id, visit)
        return visit
