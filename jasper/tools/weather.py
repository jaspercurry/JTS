from __future__ import annotations

from . import tool


def make_weather_tools(weather):
    """Build weather tools backed by a WeatherClient. Single tool surface
    (`get_weather`) covers temperature, current condition, today's
    high/low, and rain probability — Gemini reads what it needs based on
    the user's question."""

    @tool()
    async def get_weather(location: str = "") -> dict:
        """Return current conditions, today/tomorrow forecasts,
        hourly slots for the next 7 days, daily summaries for the
        next 14 days, plus daily sunrise/sunset for any day in
        that range.

        Call this for any weather, temperature, rain, sunrise, or
        sunset question.

        Args:
          location: Optional place named by the user. When the user
            asks about the default/home/current area without naming a
            different place, omit this argument or pass an empty string;
            the speaker's weather default will be used. When the user
            names a place, pass the user's place text here, including
            qualifiers such as state, province, or country when spoken:
            "Tampa, Florida", "Cortez, FL", "Denver, Colorado",
            "Paris, France", "London, Ontario". Do not strip the
            qualifier down to only the city.

        Response shape:
          location, current_local_time, units ('°C' or '°F')
          now: {temperature, condition}
          today: {date, temperature_high, temperature_low, condition,
                  precipitation_probability, will_rain,
                  sunrise, sunset}
          tomorrow: same shape as today
          hourly_forecast: list of {time, temperature, condition,
                  precipitation_probability} for 168 hours (7 days)
                  starting at the current hour. Use this for any
                  specific-hour question within the next week.
          daily_next_14d: list of 14 daily summaries (same shape as
                  today, including sunrise/sunset), index 0 = today,
                  index 13 = today + 13 days
          next_rain_window: {start, end, peak_probability,
                  duration_hours, ends_after_forecast} for the next
                  contiguous block of rainy hours, or null when no
                  rain is expected. `end` is the first DRY hour after
                  the window — when ends_after_forecast=true, rain
                  runs past the end of the data and `end` is null
                  (say "rain continues past <last hour>" instead of
                  quoting an end time).

        Sunrise/sunset are ISO 8601 local-time strings (e.g.
        "2026-05-21T20:14"). Convert to spoken form for the user
        ("Sunset is at 8:14 PM.").

        Pick the relevant sub-object based on the user's question:
          'now' / 'right now'             → response.now
          'today' / 'is it raining'       → response.today
          'tomorrow'                      → response.tomorrow
          'what time does the sun set' /
          'when does the sun rise'        → response.today.sunset /
                                            response.today.sunrise
                                            (or response.tomorrow.* for
                                            "tomorrow's sunset")
          'when will it rain' /
          'what time is it going to rain' /
          'when will rain stop'           → response.next_rain_window —
                                            always quote BOTH start and
                                            end (or note "into tomorrow"
                                            when ends_after_forecast is
                                            true). Null means no rain
                                            in the forecast window.
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

        For "will it rain?" questions, lead with the
        precipitation_probability percentage (e.g. 'There's a 70%
        chance of rain tonight'). If precipitation_probability is
        null, fall back to the boolean `will_rain` ('Yes, rain is
        expected tonight' / 'No rain expected tonight').

        When the user asks about rain TIMING (when will it start /
        stop / how long), use `next_rain_window` and quote BOTH
        endpoints — 'Rain starts around noon and clears by Monday
        morning.' When `next_rain_window.ends_after_forecast` is
        true, say "continues past <last hour>" instead of an end
        time. When `next_rain_window` is null, say no rain is
        expected in the forecast window.

        Week-scope answers should be brief: lead with high/low
        ranges and call out any rainy days. Example: 'Highs in the
        low 70s, lows around 55. Mostly sunny except Thursday with
        a 60% chance of rain.'
        """
        return await weather.get_weather(location)

    return [get_weather]
