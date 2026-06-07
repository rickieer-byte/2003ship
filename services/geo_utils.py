import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(math.radians, [float(lat1), float(lng1), float(lat2), float(lng2)])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


def within_geofence(lat, lng, center_lat, center_lng, radius_km):
    return haversine_km(lat, lng, center_lat, center_lng) <= radius_km


def eta_minutes_from_gps(lat, lng, speed_kph, target_lat, target_lng):
    distance_km = haversine_km(lat, lng, target_lat, target_lng)
    speed = max(float(speed_kph or 0), 15.0)
    return round((distance_km / speed) * 60, 0)
