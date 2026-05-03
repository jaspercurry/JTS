from __future__ import annotations

from . import tool


def make_weather_tools(weather):
    """Build weather tools backed by a WeatherClient. Single tool surface
    (`get_weather`) covers temperature, current condition, today's
    high/low, and rain probability — Gemini reads what it needs based on
    the user's question."""

    @tool()
    async def get_weather(location: str = "") -> dict:
        """Return current conditions plus today's and tomorrow's forecasts
        plus the next 24 hourly slots. location is optional — if empty,
        uses the speaker's default location.

        Response shape:
          location, current_local_time, units ('°C' or '°F')
          now: {temperature, condition}
          today: {temperature_high, temperature_low, condition,
                  precipitation_probability, will_rain}
          tomorrow: same shape as today
          hourly_next_24h: list of {time, temperature, condition,
                  precipitation_probability} starting at the current hour

        Pick the relevant sub-object based on the user's question:
          'now' / 'right now'        → response.now
          'today' / 'is it raining'  → response.today
          'tomorrow'                 → response.tomorrow
          'this evening' / 'tonight' /
          'tomorrow morning' / etc.  → filter response.hourly_next_24h
                                       by hour-of-day in 'time' field
                                       (compared to current_local_time)

        For rain questions, lead with the precipitation_probability
        percentage (e.g. 'There's a 70% chance of rain tonight').
        """
        return await weather.get_weather(location)

    return [get_weather]
