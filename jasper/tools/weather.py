# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from . import (
    DEFAULT_TOOL_TIMEOUT_SEC,
    PythonExecutor,
    Tool,
    ToolDefinition,
)

WEATHER_TOOL_NAME = "get_weather"
WEATHER_TOOL_LABELS = ("weather", "utility")
WEATHER_TOOL_TIMEOUT_SEC = DEFAULT_TOOL_TIMEOUT_SEC

WEATHER_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "location": {"type": "string"},
    },
}

WEATHER_TOOL_DESCRIPTION = """Return current conditions, today/tomorrow forecasts,
hourly slots for the next 7 days, daily summaries for the next 14 days, plus
daily sunrise/sunset for any day in that range.

Call this for any weather, temperature, rain, sunrise, or sunset question.

Args:
  location: Optional place named by the user. When the user asks about the
    default/home/current area without naming a different place, omit this
    argument or pass an empty string; the speaker's weather default will be
    used. When the user names a place, pass the user's place text here,
    including qualifiers such as state, province, or country when spoken:
    "Tampa, Florida", "Cortez, FL", "Denver, Colorado", "Paris, France",
    "London, Ontario". Do not strip the qualifier down to only the city.

Response shape:
  location, current_local_time, units ('°C' or '°F')
  now: {temperature, condition}
  today: {date, temperature_high, temperature_low, condition,
          precipitation_probability, will_rain, sunrise, sunset}
  tomorrow: same shape as today
  hourly_forecast: list of {time, temperature, condition,
          precipitation_probability} for 168 hours (7 days) starting at the
          current hour. Use this for any specific-hour question within the
          next week.
  daily_next_14d: list of 14 daily summaries (same shape as today, including
          sunrise/sunset), index 0 = today, index 13 = today + 13 days
  next_rain_window: {start, end, peak_probability, duration_hours,
          ends_after_forecast} for the next contiguous block of rainy hours,
          or null when no rain is expected. `end` is the first DRY hour after
          the window. When ends_after_forecast=true, rain runs past the end of
          the data and `end` is null.
  error: technical error string when the weather service failed
  spoken_error: short user-facing failure sentence when present

Sunrise/sunset are ISO 8601 local-time strings (e.g. "2026-05-21T20:14").

When the response includes `spoken_error`, say that briefly and do not add
technical details from `error`.

Pick the relevant sub-object based on the user's question:
  'now' / 'right now'             -> response.now
  'today' / 'is it raining'       -> response.today
  'tomorrow'                      -> response.tomorrow
  'what time does the sun set' /
  'when does the sun rise'        -> response.today.sunset /
                                    response.today.sunrise
                                    (or response.tomorrow.* for
                                    "tomorrow's sunset")
  'when will it rain' /
  'what time is it going to rain' /
  'when will rain stop'           -> response.next_rain_window
  'this evening' / 'tonight' /
  'tomorrow morning' /
  'what time will it rain
   on Saturday' / etc.            -> filter hourly_forecast by the entry's
                                    'time' field
  'this week'                     -> daily_next_14d[0:7], summarise
  'next week'                     -> daily_next_14d[7:14], summarise
  'on Friday' / 'this weekend'    -> filter daily_next_14d by date, then for
                                    time-specific follow-ups drill into
                                    hourly_forecast for that date

For "will it rain?" questions, lead with the precipitation_probability
percentage. If precipitation_probability is null, fall back to the boolean
`will_rain`.

When the user asks about rain TIMING (when will it start / stop / how long),
use `next_rain_window` and quote BOTH endpoints. When
`next_rain_window.ends_after_forecast` is true, say "continues past <last
hour>" instead of an end time. When `next_rain_window` is null, say no rain is
expected in the forecast window.

Week-scope answers should be brief: lead with high/low ranges and call out
any rainy days.
"""

WEATHER_TOOL_LLM_DESCRIPTION = """Return current weather, rain timing,
sunrise/sunset, hourly forecast, and 14-day daily forecast data.

Call this for any weather, temperature, rain, sunrise, or sunset question.

location is optional. When the user asks about the default, home, current, or
local area, omit location or pass an empty string. When the user names a place,
pass the complete spoken place text, including qualifiers such as "Tampa,
Florida", "Cortez, FL", "Paris, France", or "London, Ontario". Do not strip
the qualifier down to only the city.

Use the response object that matches the question: now for current conditions,
today/tomorrow for day-level answers, hourly_forecast for specific hours in
the next 7 days, daily_next_14d for week or named-day questions, and
today/tomorrow sunrise or sunset for sun-time questions.

For rain timing questions, including when rain starts, stops, or how long it
lasts, use next_rain_window and quote BOTH endpoints. If
next_rain_window.ends_after_forecast is true, say the rain continues past the
last forecast hour instead of inventing an end time. If next_rain_window is
null, say no rain is expected in the forecast window.

When the response includes spoken_error, say that briefly and do not add
technical details from error.
"""


def make_weather_tools(weather):
    """Build the keyless, API-backed weather capability.

    Weather intentionally registers even when the household has not saved a
    default location: explicit user-named places still work. The /weather/
    wizard only configures the bare-location default; failures for missing
    default state come back from WeatherClient as a normal tool payload.
    """

    async def get_weather(location: str = "") -> dict:
        return await weather.get_weather(location)

    return [
        Tool(
            definition=ToolDefinition(
                name=WEATHER_TOOL_NAME,
                description=WEATHER_TOOL_DESCRIPTION,
                parameters=WEATHER_TOOL_PARAMETERS,
                timeout=WEATHER_TOOL_TIMEOUT_SEC,
                log_payload=True,
                log_args=True,
                llm_description=WEATHER_TOOL_LLM_DESCRIPTION,
                labels=WEATHER_TOOL_LABELS,
                untrusted_output=False,
                consequential=False,
            ),
            executor=PythonExecutor(get_weather),
        ),
    ]
