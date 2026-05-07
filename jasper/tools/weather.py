from __future__ import annotations

from . import tool


def make_weather_tools(weather):
    """Build weather tools backed by a WeatherClient. Single tool surface
    (`get_weather`) covers temperature, current condition, today's
    high/low, and rain probability — Gemini reads what it needs based on
    the user's question."""

    @tool()
    async def get_weather(location: str = "") -> dict:
        """Return current conditions, today/tomorrow forecasts, hourly
        slots for the next 7 days, and daily summaries for the next 14
        days. location is optional — if empty, uses the speaker's default
        location.

        Response shape:
          location, current_local_time, units ('°C' or '°F')
          now: {temperature, condition}
          today: {date, temperature_high, temperature_low, condition,
                  precipitation_probability, will_rain}
          tomorrow: same shape as today
          hourly_forecast: list of {time, temperature, condition,
                  precipitation_probability} for 168 hours (7 days)
                  starting at the current hour. Use this for any
                  specific-hour question within the next week.
          daily_next_14d: list of 14 daily summaries (same shape as
                  today), index 0 = today, index 13 = today + 13 days

        Pick the relevant sub-object based on the user's question:
          'now' / 'right now'             → response.now
          'today' / 'is it raining'       → response.today
          'tomorrow'                      → response.tomorrow
          'this evening' / 'tonight' /
          'tomorrow morning' /
          'what time will it rain
           on Saturday' / etc.            → filter hourly_forecast by
                                            the entry's 'time' field
                                            (match date YYYY-MM-DD and
                                            hour against the user's ref)
          'this week'                     → daily_next_14d[0:7], summarise
                                            (highs, lows, rainy days)
          'next week'                     → daily_next_14d[7:14], summarise
          'on Friday' / 'this weekend'    → filter daily_next_14d by date,
                                            then for time-specific
                                            follow-ups drill into
                                            hourly_forecast for that date

        For rain questions, lead with the precipitation_probability
        percentage (e.g. 'There's a 70% chance of rain tonight').

        Week-scope answers should be brief: lead with high/low ranges
        and call out any rainy days. Example: 'Highs in the low 70s,
        lows around 55. Mostly sunny except Thursday with a 60%
        chance of rain.'
        """
        return await weather.get_weather(location)

    return [get_weather]
