from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from habit_config import CONFIG
from habit_logging import metric
from habit_repository import iso, parse_dt, utcnow
from place_normalization import normalize_brand, normalize_category


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class VisitDetectionService:
    def __init__(self, repo, places_provider, llm_service=None, config=CONFIG):
        self.repo = repo
        self.places = places_provider
        self.llm = llm_service
        self.config = config

    async def process_location_update(
        self,
        user_id: str,
        latitude: float,
        longitude: float,
        accuracy_meters: float | None = None,
        received_at: datetime | None = None,
    ) -> dict | None:
        settings = self.repo.ensure_settings(user_id)
        if not settings.get("tracking_enabled", True):
            metric("location_update_blocked", reason="location_off")
            return None
        received_at = received_at or utcnow()
        previous = self.repo.get_last_location_update(user_id)
        self.repo.save_location_update({
            "user_id": user_id,
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_meters": accuracy_meters,
            "received_at": iso(received_at),
        })
        self.repo.update_settings(user_id, last_location_at=iso(received_at))
        metric("location_update_received", user_id=user_id)

        if previous:
            moved = haversine_meters(
                float(previous["latitude"]), float(previous["longitude"]), latitude, longitude
            )
            if moved < self.config.min_movement_for_new_place_search_meters:
                provider_places = await self.places.find_nearby_places(
                    float(previous["latitude"]), float(previous["longitude"]),
                    self.config.place_search_radius_meters,
                )
            else:
                provider_places = await self.places.find_nearby_places(
                    latitude, longitude, self.config.place_search_radius_meters,
                )
        else:
            provider_places = await self.places.find_nearby_places(
                latitude, longitude, self.config.place_search_radius_meters,
            )
        if not provider_places:
            return None
        place = min(
            provider_places,
            key=lambda p: haversine_meters(latitude, longitude, p.latitude, p.longitude),
        )
        category, source = normalize_category(place.place_name, place.types)
        brand = normalize_brand(place.place_name)
        if category == "other" and self.llm:
            cached = self.repo.get_classification(place.provider_place_id)
            if cached:
                metric("llm_cache_hit", kind="place_classification")
                category = cached["normalized_category"]
                brand = cached.get("normalized_brand")
            else:
                classified = self.llm.classify_unknown_place(
                    place.provider_place_id, place.place_name, place.types
                )
                if classified:
                    category = classified["normalized_category"]
                    brand = classified.get("normalized_brand")
        else:
            self.repo.save_classification({
                "provider_place_id": place.provider_place_id,
                "place_name": place.place_name,
                "normalized_brand": brand,
                "normalized_category": category,
                "classification_source": source,
                "confidence": 0.95,
                "created_at": iso(utcnow()),
                "updated_at": iso(utcnow()),
            })
            metric("local_category_match", category=category)

        existing = self.repo.find_candidate(user_id, place.provider_place_id)
        if existing:
            first_seen = parse_dt(existing["first_seen_at"])
            dwell = max(0, int((received_at - first_seen).total_seconds()))
            candidate = self.repo.upsert_candidate({
                **existing,
                "place_name": place.place_name,
                "normalized_brand": brand,
                "normalized_category": category,
                "latitude": place.latitude,
                "longitude": place.longitude,
                "last_seen_at": iso(received_at),
                "accumulated_dwell_seconds": dwell,
            })
        else:
            candidate = self.repo.upsert_candidate({
                "user_id": user_id,
                "provider_place_id": place.provider_place_id,
                "place_name": place.place_name,
                "normalized_brand": brand,
                "normalized_category": category,
                "latitude": place.latitude,
                "longitude": place.longitude,
                "first_seen_at": iso(received_at),
                "last_seen_at": iso(received_at),
                "accumulated_dwell_seconds": 0,
                "status": "candidate",
            })
        if int(candidate.get("accumulated_dwell_seconds") or 0) < self.config.min_dwell_seconds:
            return None
        since = received_at - timedelta(hours=self.config.visit_deduplication_hours)
        recent = self.repo.recent_visit(user_id, place.provider_place_id, since)
        if recent:
            metric("visit_deduplicated")
            self.repo.mark_candidate(candidate["id"], "confirmed")
            return None
        visit = self.repo.create_visit({
            "user_id": user_id,
            "provider_place_id": place.provider_place_id,
            "place_name": place.place_name,
            "normalized_brand": brand,
            "normalized_category": category,
            "arrived_at": candidate["first_seen_at"],
            "confirmed_at": iso(received_at),
            "last_seen_at": iso(received_at),
            "source": "telegram_live_location",
        })
        self.repo.mark_candidate(candidate["id"], "confirmed")
        metric("place_visit_confirmed", category=category)
        return visit
