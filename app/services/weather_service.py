"""
Weather Service Layer
Handles Open-Meteo API interactions and data transformations
"""

import requests
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from dataclasses import dataclass, asdict


@dataclass
class HourlyForecast:
    """Hourly forecast data"""
    time: str
    temperature: float
    weather_code: int
    description: str


@dataclass
class DailyForecast:
    """Daily forecast data"""
    date: str
    max_temp: float
    min_temp: float
    weather_code: int
    description: str


@dataclass
class WeatherData:
    """Current weather and forecast data"""
    location_name: str
    latitude: float
    longitude: float
    temperature: float  # in Fahrenheit
    weather_code: int  # WMO weather code
    description: str
    wind_speed: float  # in mph
    humidity: float  # percentage 0-100
    apparent_temperature: float  # "feels like" temp
    timestamp: str  # ISO timestamp
    daily_forecast: List[DailyForecast]


@dataclass
class LocationResult:
    """Location search result"""
    name: str
    latitude: float
    longitude: float
    country: str
    admin1: Optional[str] = None
    display_name: Optional[str] = None


# WMO Weather Code to description mapping
WEATHER_CODE_DESCRIPTIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherService:
    """Service for fetching and processing weather data from Open-Meteo API"""

    OPEN_METEO_FORECAST_API = "https://api.open-meteo.com/v1/forecast"
    OPEN_METEO_GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
    OPEN_METEO_REVERSE_GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/reverse"
    TIMEOUT = 10  # seconds

    @staticmethod
    def get_description(weather_code: int) -> str:
        """Get human-readable description for WMO weather code"""
        return WEATHER_CODE_DESCRIPTIONS.get(weather_code, "Unknown")

    @staticmethod
    def fahrenheit_to_celsius(fahrenheit: float) -> float:
        """Convert Fahrenheit to Celsius"""
        return (fahrenheit - 32) * 5 / 9

    @staticmethod
    def celsius_to_fahrenheit(celsius: float) -> float:
        """Convert Celsius to Fahrenheit"""
        return celsius * 9 / 5 + 32

    @classmethod
    def fetch_weather(
        cls,
        latitude: float,
        longitude: float,
        location_name: str = "",
    ) -> Optional[WeatherData]:
        """
        Fetch current weather and 7-day forecast for given coordinates.

        Args:
            latitude: The latitude coordinate
            longitude: The longitude coordinate
            location_name: Optional friendly name for the location

        Returns:
            WeatherData object or None if request fails
        """
        try:
            # Validate coordinates
            if latitude < -90 or latitude > 90 or longitude < -180 or longitude > 180:
                return None

            params = {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto",
            }

            response = requests.get(
                cls.OPEN_METEO_FORECAST_API,
                params=params,
                timeout=cls.TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            # Parse current weather
            current = data.get("current", {})
            daily = data.get("daily", {})

            if not current:
                return None

            # Parse daily forecast
            daily_forecast = []
            if daily:
                dates = daily.get("time", [])
                weather_codes = daily.get("weather_code", [])
                max_temps = daily.get("temperature_2m_max", [])
                min_temps = daily.get("temperature_2m_min", [])

                for i, date in enumerate(dates[:7]):  # Get first 7 days
                    weather_code = weather_codes[i] if i < len(weather_codes) else 0
                    max_temp = max_temps[i] if i < len(max_temps) else 0
                    min_temp = min_temps[i] if i < len(min_temps) else 0

                    daily_forecast.append(
                        DailyForecast(
                            date=date,
                            max_temp=max_temp,
                            min_temp=min_temp,
                            weather_code=weather_code,
                            description=cls.get_description(weather_code),
                        )
                    )

            # If no location name provided, reverse geocode
            if not location_name:
                location_name = cls.reverse_geocode(latitude, longitude) or f"{latitude:.2f}, {longitude:.2f}"

            weather_code = int(current.get("weather_code", 0))
            return WeatherData(
                location_name=location_name,
                latitude=latitude,
                longitude=longitude,
                temperature=round(current.get("temperature_2m", 0), 1),
                weather_code=weather_code,
                description=cls.get_description(weather_code),
                wind_speed=round(current.get("wind_speed_10m", 0), 1),
                humidity=round(current.get("relative_humidity_2m", 0), 1),
                apparent_temperature=round(current.get("apparent_temperature", 0), 1),
                timestamp=current.get("time", datetime.now(timezone.utc).isoformat()),
                daily_forecast=daily_forecast,
            )

        except requests.exceptions.RequestException:
            return None
        except (KeyError, ValueError, TypeError):
            return None

    @classmethod
    def search_locations(cls, query: str, count: int = 10) -> List[LocationResult]:
        """
        Search for locations by name using Open-Meteo Geocoding API.

        Args:
            query: Search query (city/location name)
            count: Maximum number of results to return

        Returns:
            List of LocationResult objects
        """
        if not query or len(query) < 2:
            return []

        try:
            params = {
                "name": query,
                "count": count,
                "language": "en",
                "format": "json",
            }

            response = requests.get(
                cls.OPEN_METEO_GEOCODING_API,
                params=params,
                timeout=cls.TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for result in data.get("results", []):
                admin1 = result.get("admin1")
                country = result.get("country", "")
                
                # Build display name
                parts = [result.get("name", "")]
                if admin1:
                    parts.append(admin1)
                parts.append(country)
                display_name = ", ".join(filter(None, parts))

                results.append(
                    LocationResult(
                        name=result.get("name", ""),
                        latitude=result.get("latitude"),
                        longitude=result.get("longitude"),
                        country=country,
                        admin1=admin1,
                        display_name=display_name,
                    )
                )

            return results

        except requests.exceptions.RequestException:
            return []
        except (KeyError, ValueError, TypeError):
            return []

    @classmethod
    def reverse_geocode(cls, latitude: float, longitude: float) -> Optional[str]:
        """
        Get location name from coordinates using reverse geocoding.

        Args:
            latitude: The latitude coordinate
            longitude: The longitude coordinate

        Returns:
            Location name string or None if request fails
        """
        try:
            params = {
                "latitude": latitude,
                "longitude": longitude,
                "language": "en",
                "format": "json",
            }

            response = requests.get(
                cls.OPEN_METEO_REVERSE_GEOCODING_API,
                params=params,
                timeout=cls.TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            if results:
                result = results[0]
                parts = []
                
                if result.get("name"):
                    parts.append(result["name"])
                if result.get("admin1"):
                    parts.append(result["admin1"])
                if result.get("country"):
                    parts.append(result["country"])
                
                return ", ".join(parts) if parts else None

            return None

        except requests.exceptions.RequestException:
            return None
        except (KeyError, ValueError, TypeError):
            return None
