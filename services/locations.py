"""Persist driver GPS snapshot (3NF driver_locations table)."""


def upsert_driver_location(cursor, driver_id, lat, lng, heading=0, speed_kph=0):
    cursor.execute("""
        INSERT INTO driver_locations (driver_id, latitude, longitude, heading, speed_kph, recorded_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            latitude = VALUES(latitude),
            longitude = VALUES(longitude),
            heading = VALUES(heading),
            speed_kph = VALUES(speed_kph),
            recorded_at = NOW()
    """, (driver_id, lat, lng, heading, speed_kph))
