"""
Weather + contextual advice via Open-Meteo (free, no API key).

fetch_weather() pulls today's forecast for Seoul; advice_tips() turns it into
plain-rule tips (no LLM); weather_block() returns a short line + any tips,
ready to drop into the morning digest or answer "what's the weather?" on demand.
"""
import json
import ssl
import urllib.request

import certifi

# Seoul. Swap these if you move.
LAT, LON = 37.57, 126.98

# Advice thresholds — tweak freely.
RAIN_PROB_PCT = 40      # precip probability above this -> umbrella tip
UV_INDEX = 6            # UV max above this -> sunscreen tip
HOT_C = 30              # temp above this -> hydrate tip
COLD_C = 5              # temp below this -> bundle-up tip

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max"
    "&timezone=Asia%2FSeoul&forecast_days=1"
)


def fetch_weather() -> dict:
    """Today's Seoul forecast. Returns current/high/low temps (°C), max precip
    probability (%), and max UV index. Raises on network/parse failure."""
    req = urllib.request.Request(_URL, headers={"User-Agent": "assistant/1.0"})
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
        data = json.loads(r.read().decode())
    daily = data["daily"]
    return {
        "temp_c": data["current"]["temperature_2m"],
        "high_c": daily["temperature_2m_max"][0],
        "low_c": daily["temperature_2m_min"][0],
        "precip_prob_pct": daily["precipitation_probability_max"][0],
        "uv_index_max": daily["uv_index_max"][0],
    }


def advice_tips(wx: dict) -> list[str]:
    """Deterministic tips from the forecast — no LLM call."""
    tips = []
    if (wx.get("precip_prob_pct") or 0) > RAIN_PROB_PCT:
        tips.append("☔ Bring an umbrella/raincoat")
    if (wx.get("uv_index_max") or 0) > UV_INDEX:
        tips.append("🧴 Wear SPF, UV is high")
    if (wx.get("temp_c") or 0) > HOT_C:
        tips.append("🥵 Hot — stay hydrated")
    if (wx.get("temp_c") or 0) < COLD_C:
        tips.append("🧥 Bundle up, it's cold")
    return tips


def format_line(wx: dict) -> str:
    """One-line forecast summary."""
    return (
        f"🌤 Now {wx['temp_c']:.0f}°C · High {wx['high_c']:.0f}° / Low {wx['low_c']:.0f}° · "
        f"Rain {wx['precip_prob_pct']:.0f}% · UV {wx['uv_index_max']:.0f}"
    )


def weather_block() -> str:
    """Forecast line plus any triggered tips, each on its own line. Safe to call
    anywhere — returns a friendly note instead of raising if the API is down."""
    try:
        wx = fetch_weather()
    except Exception as e:
        return f"🌤 Weather unavailable ({e})."
    lines = [format_line(wx)] + advice_tips(wx)
    return "\n".join(lines)


if __name__ == "__main__":
    print(weather_block())
