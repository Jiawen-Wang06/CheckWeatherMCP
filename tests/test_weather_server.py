"""Deterministic unit tests for weather_server.get_weather (no network, no LLM)."""
import json
from unittest.mock import MagicMock

import pytest
import requests

import weather_server as ws

OWM_OK = {
    "name": "London",
    "weather": [{"description": "light rain"}],
    "main": {"temp": 12.5, "feels_like": 11.0, "humidity": 80},
    "wind": {"speed": 3.2},
}


def fake_response(status: int = 200, payload: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{status} error", response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def live_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "WEATHER_MOCK", False)
    monkeypatch.setattr(ws, "OPENWEATHERMAP_API_KEY", "test-key")
    monkeypatch.setattr(ws, "WEATHER_CACHE_DIR", tmp_path / "cache")  # isolated cache per test
    monkeypatch.setattr(ws, "WEATHER_CACHE_TTL_S", 300)


def test_success_parses_payload(monkeypatch):
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)

    result = ws.get_weather("London,uk")

    assert result == {
        "location": "London",
        "weather": "light rain",
        "temperature_celsius": "12.5°C",
        "feels_like_celsius": "11.0°C",
        "humidity": "80%",
        "wind_speed_mps": "3.2 m/s",
    }
    params = get.call_args.kwargs["params"]
    assert params["q"] == "London,uk" and params["units"] == "metric" and params["appid"] == "test-key"


def test_missing_api_key(monkeypatch):
    monkeypatch.setattr(ws, "OPENWEATHERMAP_API_KEY", "")
    assert "not configured" in ws.get_weather("London")["error"]


@pytest.mark.parametrize("status, expected", [
    (404, "Could not find weather data"),
    (401, "Authentication failed"),
    (500, "HTTP 500"),
    (429, "rate limit"),
])
def test_http_errors(monkeypatch, status, expected):
    monkeypatch.setattr(ws.requests, "get", MagicMock(return_value=fake_response(status)))
    assert expected in ws.get_weather("Nowhere")["error"]


def test_network_error(monkeypatch):
    monkeypatch.setattr(ws.requests, "get", MagicMock(side_effect=requests.exceptions.ConnectionError("down")))
    assert "network error" in ws.get_weather("London")["error"]


def test_unexpected_payload(monkeypatch):
    monkeypatch.setattr(ws.requests, "get", MagicMock(return_value=fake_response(200, {"name": "X"})))
    assert "unexpected data format" in ws.get_weather("London")["error"]


# ---- mock mode (used by the system eval)

def test_mock_mode_lookup_is_case_insensitive_and_ignores_country(monkeypatch, tmp_path):
    fixtures = tmp_path / "w.json"
    fixtures.write_text(json.dumps({"Tokyo": {"location": "Tokyo", "weather": "light rain"}}), encoding="utf-8")
    monkeypatch.setattr(ws, "WEATHER_MOCK", True)
    monkeypatch.setattr(ws, "WEATHER_FIXTURES", fixtures)
    get = MagicMock()
    monkeypatch.setattr(ws.requests, "get", get)

    assert ws.get_weather("tokyo, JP")["weather"] == "light rain"
    assert "Could not find" in ws.get_weather("Atlantis")["error"]
    get.assert_not_called()


def test_shipped_fixtures_are_valid():
    data = json.loads(ws.WEATHER_FIXTURES.read_text(encoding="utf-8"))
    assert data and all(isinstance(v, dict) for v in data.values())


# ---- security

def test_uses_https_and_timeout(monkeypatch):
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)
    ws.get_weather("London")
    assert get.call_args.args[0].startswith("https://")
    assert get.call_args.kwargs["timeout"] > 0


@pytest.mark.parametrize("failure", [
    fake_response(500),
    requests.exceptions.ConnectionError("Max retries exceeded with url: /data/2.5/weather?q=London&appid=test-key"),
])
def test_error_messages_never_contain_the_api_key(monkeypatch, failure):
    """Tool output reaches the model (and possibly the user), so the key in the request URL must not leak."""
    mock = MagicMock(side_effect=failure) if isinstance(failure, Exception) else MagicMock(return_value=failure)
    monkeypatch.setattr(ws.requests, "get", mock)
    result = ws.get_weather("London")
    assert "error" in result and "test-key" not in str(result) and "appid" not in str(result)


# ---- cache

def test_second_call_within_ttl_uses_cache(monkeypatch):
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)
    first = ws.get_weather("London, UK")
    second = ws.get_weather("  london,   uk ")  # same place, different spacing/case
    assert first == second and get.call_count == 1


def test_cache_expires_after_ttl(monkeypatch):
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)
    now = [1_000_000.0]
    monkeypatch.setattr(ws.time, "time", lambda: now[0])
    ws.get_weather("London")
    now[0] += 299
    ws.get_weather("London")
    assert get.call_count == 1
    now[0] += 2  # 301 s after the first fetch
    ws.get_weather("London")
    assert get.call_count == 2


def test_errors_are_not_cached(monkeypatch):
    get = MagicMock(side_effect=[fake_response(500), fake_response(200, OWM_OK)])
    monkeypatch.setattr(ws.requests, "get", get)
    assert "error" in ws.get_weather("London")
    assert ws.get_weather("London")["location"] == "London"
    assert get.call_count == 2


def test_different_locations_cached_separately(monkeypatch):
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)
    ws.get_weather("London")
    ws.get_weather("Paris")
    assert get.call_count == 2


def test_cache_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ws, "WEATHER_CACHE_TTL_S", 0)
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)
    ws.get_weather("London"); ws.get_weather("London")
    assert get.call_count == 2


def test_corrupt_cache_file_is_treated_as_miss(monkeypatch):
    get = MagicMock(return_value=fake_response(200, OWM_OK))
    monkeypatch.setattr(ws.requests, "get", get)
    path = ws._cache_path("London")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert ws.get_weather("London")["location"] == "London"


# ---- forecast

def owm_forecast_payload(tz_offset_s=3600):
    """Synthetic /forecast response: 3-hour periods starting 2026-03-01 21:00 UTC."""
    base = 1772398800  # 2026-03-01 21:00:00 UTC
    periods = []
    for i in range(16):  # 48 hours
        rainy = 4 <= i < 6  # 6 hours of rain on the first full local day
        periods.append({
            "dt": base + i * 3 * 3600,
            "main": {"temp_min": 10 + i % 5, "temp_max": 12 + i % 7},
            "weather": [{"description": "light rain" if rainy else "scattered clouds"}],
            "wind": {"speed": 2.0 + i / 10},
            "pop": 0.9 if rainy else 0.1,
            **({"rain": {"3h": 4.5}} if rainy else {}),
        })
    return {"city": {"name": "Rome", "country": "IT", "timezone": tz_offset_s}, "list": periods}


def test_summarize_forecast_groups_by_local_day_and_keeps_rain_visible():
    s = ws.summarize_forecast(owm_forecast_payload())
    dates = [d["date"] for d in s["days"]]
    # 21:00 UTC + 1h = 22:00 local on Mar 1, so Mar 1 has a single (partial) period
    assert dates == ["2026-03-01", "2026-03-02", "2026-03-03"]
    first, full = s["days"][0], s["days"][1]
    assert first["partial_day"] is True and full["partial_day"] is False
    assert full["weekday"] == "Monday"
    assert full["rain_mm"] == 9.0 and full["chance_of_rain_pct"] == 90
    # rain lasted only 6 of 24 hours, but it must still appear in the day's conditions
    assert "light rain" in full["conditions"] and "scattered clouds" in full["conditions"]
    assert full["temp_min_c"] <= full["temp_max_c"]


def test_forecast_is_cached_separately_from_current_weather(monkeypatch):
    def fake_get(url, params, timeout):
        return fake_response(200, owm_forecast_payload() if url.endswith("/forecast") else OWM_OK)
    get = MagicMock(side_effect=fake_get)
    monkeypatch.setattr(ws.requests, "get", get)
    monkeypatch.setattr(ws, "summarize_forecast", lambda d: {**SUMMARY, "location": d["city"]["name"]})
    assert ws.get_forecast("Rome")["location"] == "Rome"
    assert ws.get_forecast("Rome", "2026-03-03")["days"][0]["date"] == "2026-03-03"  # served from cache
    assert ws.get_weather("Rome")["location"] == "London"  # current weather is a different cache entry
    assert get.call_count == 2  # one forecast fetch + one current fetch
    assert all(c.args[0].startswith("https://") for c in get.call_args_list)


def test_forecast_errors_are_safe_and_not_cached(monkeypatch):
    get = MagicMock(return_value=fake_response(500))
    monkeypatch.setattr(ws.requests, "get", get)
    r1, r2 = ws.get_forecast("Rome"), ws.get_forecast("Rome")
    assert "error" in r1 and "test-key" not in str(r1) and get.call_count == 2


def test_mock_forecast_fixtures(monkeypatch):
    monkeypatch.setattr(ws, "WEATHER_MOCK", True)
    rome = ws.get_forecast("rome, it", "2026-03-03", "2026-03-04")
    assert rome["today_local"] == "2026-03-02" and [d["weekday"] for d in rome["days"]] == ["Tuesday", "Wednesday"]
    assert rome["days"][0]["chance_of_rain_pct"] >= 70
    assert "error" in ws.get_forecast("Atlantis")



# ---- forecast date ranges (today = 2026-03-02, data covers 03-02 .. 03-06)

SUMMARY = {"location": "Rome", "country": "IT", "today_local": "2026-03-02", "note": "n",
           "days": [{"date": f"2026-03-0{d}", "weekday": "x"} for d in range(2, 7)]}


def pick(start=None, end=None):
    from datetime import date as d
    return ws.select_forecast_dates(SUMMARY, d.fromisoformat(start) if start else None, d.fromisoformat(end) if end else None)


def test_no_dates_returns_every_available_day():
    r = pick()
    assert [x["date"] for x in r["days"]] == [f"2026-03-0{d}" for d in range(2, 7)]
    assert r["unavailable_dates"] == [] and (r["from_date"], r["to_date"]) == ("2026-03-02", "2026-03-06")


def test_from_date_only_is_a_single_day():
    assert [x["date"] for x in pick("2026-03-04")["days"]] == ["2026-03-04"]


def test_to_date_only_starts_today():
    assert [x["date"] for x in pick(None, "2026-03-03")["days"]] == ["2026-03-02", "2026-03-03"]


def test_dates_beyond_the_data_are_flagged_not_available_yet():
    r = pick("2026-03-05", "2026-03-09")
    assert [x["date"] for x in r["days"]] == ["2026-03-05", "2026-03-06"]
    assert r["unavailable_dates"] == [{"date": "2026-03-07", "weekday": "Saturday"},
                                      {"date": "2026-03-08", "weekday": "Sunday"},
                                      {"date": "2026-03-09", "weekday": "Monday"}]
    assert "not available yet" in r["note"]


def test_fourteen_day_limit():
    assert pick("2026-03-16")["unavailable_dates"] == [{"date": "2026-03-16", "weekday": "Monday"}]  # day 14: allowed
    err = pick("2026-03-10", "2026-03-17")["error"]
    assert "14 days" in err and "2026-03-16" in err


@pytest.mark.parametrize("start, end, expected", [
    ("2026-03-01", None, "in the past"),
    ("2026-03-05", "2026-03-03", "before from_date"),
])
def test_invalid_ranges(start, end, expected):
    assert expected in pick(start, end)["error"]


def test_bad_date_format_rejected_before_any_api_call(monkeypatch):
    get = MagicMock()
    monkeypatch.setattr(ws.requests, "get", get)
    assert "YYYY-MM-DD" in ws.get_forecast("Rome", "next friday")["error"]
    get.assert_not_called()
