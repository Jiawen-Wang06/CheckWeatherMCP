import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

load_dotenv(Path(__file__).with_name(".env"))
OPENWEATHERMAP_API_KEY = os.getenv("OPENWEATHERMAP_API_KEY", "")

# Eval/test mode: serve deterministic weather from a JSON fixture instead of the live API.
WEATHER_MOCK = os.getenv("WEATHER_MOCK", "").lower() in {"1", "true", "yes"}
WEATHER_FIXTURES = Path(
    os.getenv("WEATHER_FIXTURES", Path(__file__).parent / "eval" / "fixtures" / "weather.json")
)
FORECAST_FIXTURES = Path(
    os.getenv("FORECAST_FIXTURES", Path(__file__).parent / "eval" / "fixtures" / "forecast.json")
)

# Cache for live results (see _cache_get). Set WEATHER_CACHE_TTL_S=0 to disable.
WEATHER_CACHE_TTL_S = float(os.getenv("WEATHER_CACHE_TTL_S", "300"))
WEATHER_CACHE_DIR = Path(os.getenv("WEATHER_CACHE_DIR", Path(tempfile.gettempdir()) / "mcp-weather-cache"))

# Initialize the FastMCP server
# Quiet by default; set MCP_LOG_LEVEL=INFO to see "Processing request of type ..." lines.
mcp = FastMCP("WeatherAssistant", log_level=os.getenv("MCP_LOG_LEVEL", "WARNING").upper())


def _mock_weather(location: str) -> dict:
    """Look up a location (city part only, case-insensitive) in the fixture file."""
    fixtures = {k.lower(): v for k, v in json.loads(WEATHER_FIXTURES.read_text(encoding="utf-8")).items()}
    city = location.split(",")[0].strip().lower()
    if city not in fixtures:
        return {"error": f"Could not find weather data for '{location}'. Please check the location name."}
    return fixtures[city]

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_weather(location: str) -> dict:
    """
    Fetches the CURRENT weather (right now) for a specified location using the OpenWeatherMap API.
    For future days, use get_forecast instead.

    Args:
        location: The city name and optional country code (e.g., "Toronto,ca").

    Returns:
        A dictionary containing weather information or an error message.
    """
    if WEATHER_MOCK:
        return _mock_weather(location)
    return _cached("weather", location, lambda: _fetch_current(location))


MAX_FORECAST_DAYS_AHEAD = 14


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_forecast(location: str, from_date: str | None = None, to_date: str | None = None) -> dict:
    """
    Fetches the daily weather FORECAST for upcoming dates.
    Use this for questions about tomorrow, the weekend, specific upcoming dates, or planning a trip.

    Args:
        location: The city name and optional country code (e.g., "Vancouver,ca").
        from_date: First date to return, "YYYY-MM-DD" (inclusive). Omit both dates to get every available day.
        to_date: Last date to return, "YYYY-MM-DD" (inclusive). Defaults to from_date (a single day).

    Dates must be between today and 14 days ahead (in the location's local time). Forecast data
    currently reaches about 5 days ahead; requested dates beyond that are returned in
    "unavailable_dates" (not available yet, check again closer to the date).

    Returns:
        {"location", "country", "today_local", "from_date", "to_date",
         "days": [{"date", "weekday", "temp_min_c", "temp_max_c", "conditions", "chance_of_rain_pct",
                   "rain_mm", "snow_mm", "wind_max_mps", "partial_day"}],
         "unavailable_dates": [{"date", "weekday"}, ...], "note"} or {"error": ...}.
        "partial_day" is true when only part of that day is covered by the forecast.
    """
    try:
        start = date.fromisoformat(from_date) if from_date else None
        end = date.fromisoformat(to_date) if to_date else None
    except ValueError:
        return {"error": "Dates must use the format YYYY-MM-DD, for example 2026-03-05."}

    result = _mock_forecast(location) if WEATHER_MOCK else _cached("forecast", location, lambda: _fetch_forecast(location))
    if "error" in result:
        return result
    return select_forecast_dates(result, start, end)


def select_forecast_dates(result: dict, start: date | None, end: date | None) -> dict:
    """Validate the requested range against the location's local today and pick those days."""
    today = date.fromisoformat(result["today_local"])
    limit = today + timedelta(days=MAX_FORECAST_DAYS_AHEAD)
    available = [d for d in result["days"]]
    last_available = date.fromisoformat(available[-1]["date"]) if available else today

    if start is None and end is None:
        start, end = today, last_available
    elif start is None:
        start = today
    elif end is None:
        end = start
    if end < start:
        return {"error": f"to_date ({end}) is before from_date ({start})."}
    if start < today:
        return {"error": f"{start} is in the past; today in {result['location']} is {today}. Forecasts are for today or later."}
    if end > limit:
        return {"error": f"Forecasts can only be requested up to {MAX_FORECAST_DAYS_AHEAD} days ahead, i.e. until {limit} "
                         f"(today in {result['location']} is {today})."}

    days = [d for d in available if start <= date.fromisoformat(d["date"]) <= end]
    first_missing = max(start, last_available + timedelta(days=1))
    # Include weekdays: models are unreliable at working them out for dates without forecast data.
    unavailable = [{"date": d.isoformat(), "weekday": d.strftime("%A")}
                   for d in (first_missing + timedelta(days=i) for i in range((end - first_missing).days + 1))]
    note = result.get("note", "")
    if unavailable:
        note = (f"Forecast data currently only reaches {last_available}. The weather for {len(unavailable)} requested "
                f"date(s) is not available yet; check again closer to the date.")
    return {"location": result["location"], "country": result.get("country", ""), "today_local": result["today_local"],
            "from_date": start.isoformat(), "to_date": end.isoformat(), "days": days,
            "unavailable_dates": unavailable, "note": note}


# ---- OpenWeatherMap calls
# Error messages must never include the request URL: it contains the API key, and tool output
# is shown to the model (and from there can reach the user).

def _owm_get(endpoint: str, location: str) -> tuple[dict | None, dict | None]:
    """Returns (data, None) on success or (None, {"error": ...})."""
    if not OPENWEATHERMAP_API_KEY:
        return None, {"error": "OpenWeatherMap API key is not configured on the server."}
    params = {"q": location, "appid": OPENWEATHERMAP_API_KEY, "units": "metric"}
    try:
        # HTTPS: the API key is part of the request
        response = requests.get(f"https://api.openweathermap.org/data/2.5/{endpoint}", params=params, timeout=10)
    except requests.exceptions.Timeout:
        return None, {"error": "The weather service took too long to respond. Please try again."}
    except requests.exceptions.RequestException as req_err:
        return None, {"error": f"A network error occurred while contacting the weather service ({type(req_err).__name__})."}

    if response.status_code == 404:
        return None, {"error": f"Could not find weather data for '{location}'. Please check the location name."}
    if response.status_code == 401:
        return None, {"error": "Authentication failed. The API key is likely invalid or inactive."}
    if response.status_code == 429:
        return None, {"error": "The weather service rate limit was reached. Please try again in a minute."}
    if response.status_code >= 400:
        return None, {"error": f"The weather service returned an error (HTTP {response.status_code})."}
    try:
        return response.json(), None
    except ValueError:
        return None, {"error": "Received unexpected data format from the weather API."}


def _fetch_current(location: str) -> dict:
    data, error = _owm_get("weather", location)
    if error:
        return error
    try:
        return {
            "location": data["name"],
            "weather": data["weather"][0]["description"],
            "temperature_celsius": f"{data['main']['temp']}°C",
            "feels_like_celsius": f"{data['main']['feels_like']}°C",
            "humidity": f"{data['main']['humidity']}%",
            "wind_speed_mps": f"{data['wind']['speed']} m/s",
        }
    except (KeyError, IndexError, TypeError):
        return {"error": "Received unexpected data format from the weather API."}


def _fetch_forecast(location: str) -> dict:
    data, error = _owm_get("forecast", location)
    if error:
        return error
    try:
        return summarize_forecast(data)
    except (KeyError, IndexError, TypeError, ValueError):
        return {"error": "Received unexpected data format from the weather API."}


def summarize_forecast(data: dict) -> dict:
    """Roll OpenWeatherMap's 3-hour forecast periods up into one summary per local calendar day."""
    tz = timedelta(seconds=data["city"].get("timezone", 0))
    by_day: dict[str, list] = {}
    for period in data["list"]:
        local = datetime.fromtimestamp(period["dt"], timezone.utc) + tz
        by_day.setdefault(local.strftime("%Y-%m-%d"), []).append(period)

    days = []
    for day, periods in by_day.items():
        descriptions = [p["weather"][0]["description"] for p in periods]
        days.append({
            "date": day,
            "weekday": datetime.strptime(day, "%Y-%m-%d").strftime("%A"),
            "temp_min_c": round(min(p["main"]["temp_min"] for p in periods), 1),
            "temp_max_c": round(max(p["main"]["temp_max"] for p in periods), 1),
            # All of the day's conditions, most frequent first: picking only the most common one would hide
            # e.g. a 6-hour downpour on an otherwise cloudy day ("scattered clouds" with 19 mm of rain).
            "conditions": ", ".join(d for d, _ in Counter(descriptions).most_common(3)),
            "chance_of_rain_pct": round(100 * max(p.get("pop", 0) for p in periods)),
            "rain_mm": round(sum(p.get("rain", {}).get("3h", 0) for p in periods), 1),
            "snow_mm": round(sum(p.get("snow", {}).get("3h", 0) for p in periods), 1),
            "wind_max_mps": round(max(p["wind"]["speed"] for p in periods), 1),
            "partial_day": len(periods) < 8,  # 8 three-hour periods = a full day
        })
    today_local = (datetime.now(timezone.utc) + tz).strftime("%Y-%m-%d")
    return {
        "location": data["city"]["name"],
        "country": data["city"].get("country", ""),
        "today_local": today_local,
        "days": days,
        "note": "Daily summary of OpenWeather's 3-hour forecast. Covers up to 5 days ahead; later dates are not available.",
    }


def _mock_forecast(location: str) -> dict:
    """Deterministic forecast for evals, from eval/fixtures/forecast.json."""
    fixtures = {k.lower(): v for k, v in json.loads(FORECAST_FIXTURES.read_text(encoding="utf-8")).items()}
    city = location.split(",")[0].strip().lower()
    if city not in fixtures:
        return {"error": f"Could not find weather data for '{location}'. Please check the location name."}
    return fixtures[city]


# ---- Cache
# The MCP client starts a fresh server process for each tool call, so an in-memory cache would never
# be hit. Results are cached on disk instead, shared by every call and every user (weather is public
# data). OpenWeatherMap refreshes current conditions roughly every 10 minutes, so 5 minutes is safe.

def _cache_path(location: str, kind: str = "weather") -> Path:
    key = kind + "|" + " ".join(location.lower().split())  # "Toronto, CA" and "toronto,  ca" share an entry
    return WEATHER_CACHE_DIR / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def _cached(kind: str, location: str, fetch) -> dict:
    """Serve from the disk cache if fresh; otherwise fetch, and cache successful results only."""
    hit = _cache_get(location, kind)
    if hit is not None:
        return hit
    result = fetch()
    if "error" not in result:  # never cache failures
        _cache_put(location, result, kind)
    return result


def _cache_get(location: str, kind: str = "weather") -> dict | None:
    if WEATHER_CACHE_TTL_S <= 0:
        return None
    try:
        entry = json.loads(_cache_path(location, kind).read_text(encoding="utf-8"))
        if time.time() - entry["fetched_at"] <= WEATHER_CACHE_TTL_S:
            return entry["data"]
    except (OSError, ValueError, KeyError, TypeError):
        pass  # missing or corrupt entry: treat as a miss
    return None


def _cache_put(location: str, data: dict, kind: str = "weather") -> None:
    if WEATHER_CACHE_TTL_S <= 0:
        return
    try:
        WEATHER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(location, kind)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic, so concurrent calls never read a half-written file
    except OSError:
        pass  # caching is best-effort; never fail the tool call because of it


@mcp.prompt()
def compare_weather_prompt(location_a: str, location_b: str) -> str:
    """
    Generates a clear, comparative summary of the weather between two specified locations.
    This is the best choice when a user asks to compare, contrast, or see the difference in weather between two places.
    
    Args:
        location_a: The first city for comparison (e.g., "Toronto").
        location_b: The second city for comparison (e.g., "Vancouver").
    """
    return f"""
    You are acting as a helpful weather analyst. Your goal is to provide a clear and easy-to-read comparison of the weather in two different locations for a user.

    The user wants to compare the weather between "{location_a}" and "{location_b}".

    To accomplish this, follow these steps:
    1.  First, gather the necessary weather data for both "{location_a}" and "{location_b}".
    2.  Once you have the weather data for both locations, DO NOT simply list the raw results.
    3.  Instead, synthesize the information into a concise summary. Your final response should highlight the key differences, focusing on temperature, the general conditions (e.g., 'sunny' vs 'rainy'), and wind speed.
    4.  Present the comparison in a structured format, like a markdown table or a clear bulleted list, to make it easy for the user to understand at a glance.
    """

@mcp.resource("file://delivery_log")
def delivery_log_resource() -> list[str]:
    """
    Reads a delivery log file and returns its contents as a list of lines.
    Each line contains an order number and a delivery location.
    """
    try:
        log_file = Path("delivery_log.txt")
        if not log_file.exists():
            return ["Error: The delivery_log.txt file was not found on the server."]
        
        # Read the file, remove leading/trailing whitespace, and split into lines
        return log_file.read_text(encoding="utf-8").strip().splitlines()
        
    except Exception as e:
        return [f"An unexpected error occurred while reading the delivery log: {str(e)}"]

if __name__ == "__main__":
    mcp.run(transport="stdio")