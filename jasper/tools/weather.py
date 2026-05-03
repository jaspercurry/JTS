from __future__ import annotations

from . import tool


def make_weather_tools(weather):
    """Build weather tools backed by a WeatherClient. Single tool surface
    (`get_weather`) covers temperature, current condition, today's
    high/low, and rain probability — Gemini reads what it needs based on
    the user's question."""

    @tool()
    async def get_weather(location: str = "") -> dict:
        """Return current weather plus today's forecast. location is
        optional — if empty, uses the speaker's default location.

        Response fields:
          - temperature_now / _high_today / _low_today (numbers)
          - condition_now / condition_today (e.g. 'partly cloudy', 'light rain')
          - precipitation_probability_today (0-100 percent — prefer this when
            answering rain questions: 'There's a 60% chance of rain today.')
          - will_rain_today (boolean fallback when probability is null)
          - units ('°C' or '°F')
        """
        return await weather.get_weather(location)

    return [get_weather]
