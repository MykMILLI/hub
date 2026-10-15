from flask import jsonify, request, current_app, session
from ..services.weather_service import WeatherService
from ..blueprints import main_bp
from dataclasses import asdict
from typing import Optional
from ..models import FamilyMember, db
from ..security import sanitize_text


def _current_member_name() -> str:
    member = sanitize_text(session.get('current_member') or '').strip()
    if member:
        return member
    return sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip() or 'Administrator'


def _member_weather_payload(row: Optional[FamilyMember]) -> dict:
    if not row:
        return {}
    if row.weather_latitude is None or row.weather_longitude is None:
        return {}
    return {
        'latitude': float(row.weather_latitude),
        'longitude': float(row.weather_longitude),
        'location_name': str(row.weather_location_name or ''),
        'use_gps_location': bool(row.weather_use_gps_location),
    }


def _ensure_member_row(member_name: str) -> Optional[FamilyMember]:
    clean_name = sanitize_text(member_name or '').strip()
    if not clean_name:
        return None
    row = FamilyMember.query.filter_by(name=clean_name).first()
    if row:
        return row
    admin_name = sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip() or 'Administrator'
    row = FamilyMember(name=clean_name, role='admin' if clean_name == admin_name else 'standard')
    db.session.add(row)
    db.session.flush()
    return row


@main_bp.route('/api/weather', methods=['GET'])
def get_weather():
    """
    Get current weather and 7-day forecast for user's location.
    
    Query parameters:
    - latitude (float): User's latitude
    - longitude (float): User's longitude
    - location_name (string, optional): Friendly location name
    
    Returns:
        JSON with weather data or error message
    """
    try:
        latitude = request.args.get('latitude', type=float)
        longitude = request.args.get('longitude', type=float)
        location_name = request.args.get('location_name', default='')
        
        if latitude is None or longitude is None:
            return jsonify({'error': 'Missing latitude or longitude parameter'}), 400
        
        # Use the weather service to fetch data
        weather_data = WeatherService.fetch_weather(latitude, longitude, location_name)
        
        if not weather_data:
            return jsonify({'error': 'Failed to fetch weather data'}), 502
        
        # Convert dataclass to dict and include daily forecast
        result = asdict(weather_data)
        result['daily_forecast'] = [asdict(d) for d in weather_data.daily_forecast]
        
        return jsonify(result)
    
    except ValueError:
        return jsonify({'error': 'Invalid coordinate format'}), 400
    except Exception as e:
        current_app.logger.error(f'Weather API error: {e}')
        return jsonify({'error': 'Internal server error'}), 500


@main_bp.route('/api/weather/search', methods=['GET'])
def search_locations():
    """
    Search for locations by name.
    
    Query parameters:
    - q (string): Search query (city/location name)
    - count (int, optional): Max results to return (default: 10)
    
    Returns:
        JSON list of location results
    """
    try:
        query = request.args.get('q', '').strip()
        count = request.args.get('count', 10, type=int)
        
        if not query:
            return jsonify({'error': 'Search query required'}), 400
        
        results = WeatherService.search_locations(query, count)
        
        # Convert LocationResult objects to dicts
        return jsonify([asdict(r) for r in results])
    
    except Exception as e:
        current_app.logger.error(f'Location search error: {e}')
        return jsonify({'error': 'Search failed'}), 500


@main_bp.route('/api/weather/location', methods=['GET', 'POST'])
def manage_location():
    """
    Manage user's weather location preference.
    
    GET: Retrieve current location settings
    POST: Save new location settings
        Body: {
            "latitude": float,
            "longitude": float,
            "location_name": string,
            "use_gps_location": boolean
        }
    
    Returns:
        JSON with location data
    """
    try:
        member_name = _current_member_name()
        member_row = FamilyMember.query.filter_by(name=member_name).first() if member_name else None

        if request.method == 'GET':
            location = _member_weather_payload(member_row)
            if not location:
                # Backward compatibility with session-only storage.
                location = session.get('weather_location', {}) if isinstance(session.get('weather_location', {}), dict) else {}
            if location:
                session['weather_location'] = location
                session.modified = True
            return jsonify(location)
        
        else:  # POST
            data = request.get_json() or {}
            
            # Validate required fields
            if 'latitude' not in data or 'longitude' not in data:
                return jsonify({'error': 'Missing latitude or longitude'}), 400
            
            location = {
                'latitude': float(data['latitude']),
                'longitude': float(data['longitude']),
                'location_name': str(data.get('location_name', '')),
                'use_gps_location': bool(data.get('use_gps_location', False)),
            }

            if not member_row:
                member_row = _ensure_member_row(member_name)

            if member_row:
                member_row.weather_latitude = location['latitude']
                member_row.weather_longitude = location['longitude']
                member_row.weather_location_name = location['location_name']
                member_row.weather_use_gps_location = location['use_gps_location']
                db.session.commit()

            # Keep session copy for current request cycle + legacy clients.
            session['weather_location'] = location
            session.modified = True
            
            return jsonify(location)
    
    except (ValueError, TypeError) as e:
        return jsonify({'error': 'Invalid data format'}), 400
    except Exception as e:
        current_app.logger.error(f'Location management error: {e}')
        return jsonify({'error': 'Internal server error'}), 500


@main_bp.route('/api/weather/location-name', methods=['GET'])
def get_location_name():
    """
    Get location name from coordinates using reverse geocoding.
    
    Query parameters:
    - latitude (float): Latitude coordinate
    - longitude (float): Longitude coordinate
    
    Returns:
        JSON with location name or error
    """
    try:
        latitude = request.args.get('latitude', type=float)
        longitude = request.args.get('longitude', type=float)
        
        if latitude is None or longitude is None:
            return jsonify({'error': 'Missing latitude or longitude parameter'}), 400
        
        location_name = WeatherService.reverse_geocode(latitude, longitude)
        
        if not location_name:
            return jsonify({'error': 'Could not determine location'}), 502
        
        return jsonify({'location_name': location_name})
    
    except ValueError:
        return jsonify({'error': 'Invalid coordinate format'}), 400
    except Exception as e:
        current_app.logger.error(f'Reverse geocoding error: {e}')
        return jsonify({'error': 'Internal server error'}), 500
