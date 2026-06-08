from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from habit_config import CONFIG
from habit_logging import metric


@dataclass(frozen=True)
class NearbyPlace:
    provider_place_id: str
    place_name: str
    latitude: float
    longitude: float
    types: list[str]


class PlacesProvider(Protocol):
    async def find_nearby_places(
        self,
        latitude: float,
        longitude: float,
        radius_meters: int,
    ) -> list[NearbyPlace]:
        ...


class MockPlacesProvider:
    def __init__(self, places: list[NearbyPlace] | None = None):
        self.places = places or []
        self.calls = 0

    async def find_nearby_places(
        self, latitude: float, longitude: float, radius_meters: int
    ) -> list[NearbyPlace]:
        self.calls += 1
        return list(self.places)


class GooglePlacesProvider:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")

    async def find_nearby_places(
        self, latitude: float, longitude: float, radius_meters: int
    ) -> list[NearbyPlace]:
        if not self.api_key:
            metric("places_api_unavailable", reason="missing_key")
            return []
        return await asyncio.to_thread(self._request, latitude, longitude, radius_meters)

    def _request(self, latitude: float, longitude: float, radius_meters: int) -> list[NearbyPlace]:
        body = json.dumps({
            "includedTypes": [],
            "maxResultCount": 5,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": latitude, "longitude": longitude},
                    "radius": radius_meters,
                }
            },
        }).encode()
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchNearby",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.types",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            metric("places_api_call", status="ok")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            metric("places_api_call", status="failed", error=type(e).__name__)
            return []
        places = []
        for p in payload.get("places", []):
            loc = p.get("location") or {}
            places.append(NearbyPlace(
                provider_place_id=p.get("id") or "",
                place_name=(p.get("displayName") or {}).get("text") or "Unknown place",
                latitude=float(loc.get("latitude", latitude)),
                longitude=float(loc.get("longitude", longitude)),
                types=list(p.get("types") or []),
            ))
        return [p for p in places if p.provider_place_id]


class CachedPlacesProvider:
    def __init__(self, inner: PlacesProvider, ttl_minutes: int = CONFIG.places_cache_minutes):
        self.inner = inner
        self.ttl_seconds = ttl_minutes * 60
        self._cache: dict[tuple[int, int, int], tuple[float, list[NearbyPlace]]] = {}

    def _cell(self, latitude: float, longitude: float, radius_meters: int) -> tuple[float, float, int]:
        return (round(latitude, 3), round(longitude, 3), radius_meters)

    async def find_nearby_places(
        self, latitude: float, longitude: float, radius_meters: int
    ) -> list[NearbyPlace]:
        key = self._cell(latitude, longitude, radius_meters)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] <= self.ttl_seconds:
            metric("places_cache_hit")
            return list(cached[1])
        places = await self.inner.find_nearby_places(latitude, longitude, radius_meters)
        self._cache[key] = (now, list(places))
        return places
