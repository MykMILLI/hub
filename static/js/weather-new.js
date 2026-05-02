/**
 * Weather Widget for Home Hub
 * Displays current weather and 7-day forecast with emoji icons
 */

(function() {
    'use strict';

    // Weather code to emoji mapping (WMO codes)
    const weatherEmojis = {
        0: '☀️',      // Clear sky
        1: '🌤️',     // Mainly clear
        2: '⛅',      // Partly cloudy
        3: '☁️',      // Overcast
        45: '🌫️',    // Foggy
        48: '🌫️',    // Depositing rime fog
        51: '🌧️',    // Light drizzle
        53: '🌧️',    // Moderate drizzle
        55: '🌧️',    // Dense drizzle
        61: '🌧️',    // Slight rain
        63: '🌧️',    // Moderate rain
        65: '⛈️',    // Heavy rain
        71: '❄️',    // Slight snow
        73: '❄️',    // Moderate snow
        75: '❄️',    // Heavy snow
        77: '❄️',    // Snow grains
        80: '🌧️',    // Slight rain showers
        81: '🌧️',    // Moderate rain showers
        82: '⛈️',    // Violent rain showers
        85: '❄️',    // Slight snow showers
        86: '❄️',    // Heavy snow showers
        95: '⛈️',    // Thunderstorm
        96: '⛈️',    // Thunderstorm with slight hail
        99: '⛈️',    // Thunderstorm with heavy hail
    };

    function getWeatherEmoji(weatherCode) {
        return weatherEmojis[weatherCode] || '🌡️';
    }

    function formatDate(dateString) {
        const date = new Date(dateString);
        return date.toLocaleDateString('en-US', { weekday: 'short' });
    }

    async function fetchWeather() {
        const loadingEl = document.getElementById('weather-loading');
        const cardEl = document.getElementById('weather-card');
        const noLocationEl = document.getElementById('weather-no-location');

        try {
            // Show loading state
            if (loadingEl) loadingEl.style.display = 'block';
            if (cardEl) cardEl.style.display = 'none';
            if (noLocationEl) noLocationEl.style.display = 'none';

            // Get location from session or config
            const locationResponse = await fetch('/api/weather/location');
            const location = await locationResponse.json();

            if (!location.latitude || !location.longitude) {
                showNoLocationState();
                return;
            }

            // Fetch weather data
            const params = new URLSearchParams({
                latitude: location.latitude,
                longitude: location.longitude,
                location_name: location.location_name || '',
            });

            const response = await fetch(`/api/weather?${params}`);
            if (!response.ok) {
                showNoLocationState();
                return;
            }

            const data = await response.json();

            if (!data.temperature) {
                showNoLocationState();
                return;
            }

            displayWeather(data);

        } catch (error) {
            console.error('Error fetching weather:', error);
            showNoLocationState();
        }
    }

    function displayWeather(data) {
        const loadingEl = document.getElementById('weather-loading');
        const cardEl = document.getElementById('weather-card');
        const noLocationEl = document.getElementById('weather-no-location');

        if (loadingEl) loadingEl.style.display = 'none';
        if (cardEl) cardEl.style.display = 'block';
        if (noLocationEl) noLocationEl.style.display = 'none';

        // Update location name
        const locationName = document.getElementById('location-name');
        if (locationName) {
            locationName.textContent = data.location_name || `${data.latitude.toFixed(2)}, ${data.longitude.toFixed(2)}`;
        }

        // Update current weather
        const weatherIcon = document.getElementById('weather-icon');
        if (weatherIcon) {
            weatherIcon.textContent = getWeatherEmoji(data.weather_code);
        }

        const temperature = document.getElementById('temperature');
        if (temperature) {
            temperature.textContent = `${Math.round(data.temperature)}°F`;
        }

        const description = document.getElementById('weather-description');
        if (description) {
            description.textContent = data.description || 'Unknown';
        }

        const feelsLike = document.getElementById('feels-like');
        if (feelsLike) {
            feelsLike.textContent = `Feels like ${Math.round(data.apparent_temperature)}°F`;
        }

        const windSpeed = document.getElementById('wind-speed');
        if (windSpeed) {
            windSpeed.textContent = `${data.wind_speed.toFixed(1)} mph`;
        }

        const humidity = document.getElementById('humidity');
        if (humidity) {
            humidity.textContent = `${Math.round(data.humidity)}%`;
        }

        // Update 7-day forecast
        if (data.daily_forecast && data.daily_forecast.length > 0) {
            const forecastContainer = document.getElementById('forecast-container');
            const forecastSection = document.getElementById('forecast-section');

            if (forecastContainer && forecastSection) {
                forecastContainer.innerHTML = '';

                data.daily_forecast.forEach(day => {
                    const card = document.createElement('div');
                    card.className = 'forecast-card flex-shrink-0 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-3 text-center min-w-24';

                    const dayName = formatDate(day.date);
                    const emoji = getWeatherEmoji(day.weather_code);
                    const maxTemp = Math.round(day.max_temp);
                    const minTemp = Math.round(day.min_temp);

                    card.innerHTML = `
                        <div class="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">${dayName}</div>
                        <div class="text-3xl mb-2">${emoji}</div>
                        <div class="flex justify-center gap-2 text-sm">
                            <span class="font-bold text-gray-900 dark:text-white">${maxTemp}°</span>
                            <span class="text-gray-500 dark:text-gray-400">${minTemp}°</span>
                        </div>
                    `;

                    forecastContainer.appendChild(card);
                });

                forecastSection.style.display = 'block';
            }
        }

        // Update last updated time
        const lastUpdated = document.getElementById('last-updated');
        if (lastUpdated && data.timestamp) {
            try {
                const date = new Date(data.timestamp);
                const now = new Date();
                const diffMin = Math.floor((now - date) / 60000);

                let relativeTime = '';
                if (diffMin < 1) relativeTime = 'just now';
                else if (diffMin === 1) relativeTime = '1 minute ago';
                else if (diffMin < 60) relativeTime = `${diffMin} minutes ago`;
                else if (diffMin < 120) relativeTime = '1 hour ago';
                else relativeTime = `${Math.floor(diffMin / 60)} hours ago`;

                lastUpdated.textContent = `Last updated: ${relativeTime}`;
            } catch (e) {
                // Silently fail on date parsing
            }
        }
    }

    function showNoLocationState() {
        const loadingEl = document.getElementById('weather-loading');
        const cardEl = document.getElementById('weather-card');
        const noLocationEl = document.getElementById('weather-no-location');

        if (loadingEl) loadingEl.style.display = 'none';
        if (cardEl) cardEl.style.display = 'none';
        if (noLocationEl) noLocationEl.style.display = 'block';
    }

    // Global functions
    window.useGPSLocation = async function() {
        if (!navigator.geolocation) {
            if (window.uiAlert) window.uiAlert('Geolocation is not supported by your browser', { title: 'GPS Not Supported' });
            else alert('Geolocation is not supported by your browser');
            return;
        }

        try {
            navigator.geolocation.getCurrentPosition(
                async (position) => {
                    const { latitude, longitude } = position.coords;

                    try {
                        // Get location name from coordinates
                        const nameResponse = await fetch(`/api/weather/location-name?latitude=${latitude}&longitude=${longitude}`);
                        let locationName = '';
                        if (nameResponse.ok) {
                            const contentType = String(nameResponse.headers.get('content-type') || '').toLowerCase();
                            if (contentType.includes('application/json')) {
                                const nameData = await nameResponse.json();
                                locationName = nameData.location_name || '';
                            }
                        } else {
                            console.warn('Location name lookup failed, saving GPS coordinates only');
                        }

                        // Save location
                        const response = await fetch('/api/weather/location', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                latitude,
                                longitude,
                                location_name: locationName,
                                use_gps_location: true,
                            }),
                        });

                        if (response.ok) {
                            globalToast('Location updated from GPS', 'success');
                            fetchWeather();
                        } else {
                            globalToast('Failed to save GPS location', 'error');
                        }
                    } catch (error) {
                        console.error('Error saving GPS location:', error);
                        globalToast('Failed to save GPS location', 'error');
                    }
                },
                (error) => {
                    console.error('Geolocation error:', error);
                    globalToast('Could not get GPS location', 'error');
                }
            );
        } catch (error) {
            console.error('Geolocation error:', error);
            globalToast('Geolocation error', 'error');
        }
    };

    window.goToLocationSettings = function() {
        // Navigate to settings page where user can configure weather location
        window.location.href = '/settings';
    };

    window.toggleWeatherSettings = function() {
        // Placeholder for weather settings toggle
        console.log('Weather settings');
    };

    // Initialize on page load
    document.addEventListener('DOMContentLoaded', fetchWeather);

    // Refresh weather every 30 minutes
    setInterval(fetchWeather, 30 * 60 * 1000);

})();
