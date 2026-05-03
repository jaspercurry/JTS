from __future__ import annotations

from . import tool


def make_weather_tools(weather):
    """Build weather tools backed by a WeatherClient. Single tool surface
    (`get_weather`) covers temperature, current condition, today's
    high/low, and rain probability — Gemini reads what it needs based on
    the user's question."""

    @tool()
    async def get_weather(location: str = "") -> dict:
        """Return current weather plus today's forecast (high/low, condition,
        will it rain). location is optional — if empty, uses the
        speaker's default location. Use this for any weather question:
        temperature, conditions, rain forecast, etc."""
        return await weather.get_weather(location)

    return [get_weather]
