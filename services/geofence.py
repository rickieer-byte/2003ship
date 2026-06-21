"""Geofence: port container pickup (slot released on exit) then warehouse delivery."""
import datetime
import json
from services.geo_utils import within_geofence
from services.port_slots import release_slots_for_allocation

PORT_LAT = 1.3015
PORT_LNG = 103.6340
DEFAULT_WAREHOUSE_LAT = 1.3298
DEFAULT_WAREHOUSE_LNG = 103.6954


def _as_time(value):
    if isinstance(value, datetime.time):
        return value
    if isinstance(value, datetime.timedelta):
        return (datetime.datetime.min + value).time()
    return value


def driver_is_on_shift(cursor, driver_id, as_of=None):
    from services.shifts import driver_is_on_shift as check_shift
    return check_shift(driver_id, cursor, as_of)


def _fetch_warehouse(cursor, warehouse_id):
    cursor.execute(
        "SELECT warehouse_id, warehouse_name, latitude, longitude, geofence_radius_km FROM warehouses WHERE warehouse_id = %s",
        (warehouse_id,),
    )
    return cursor.fetchone()


def process_port_pickups(cursor, radius_km=2.0):
    """Driver enters port with a booked slot, loads container, slot freed on departure."""
    cursor.execute("""
        SELECT t.container_number, da.driver_id, t.allocation_id,
               dl.latitude, dl.longitude, d.driver_name
        FROM truck_allocations t
        JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        JOIN drivers d ON d.driver_id = da.driver_id
        JOIN driver_locations dl ON dl.driver_id = d.driver_id
        WHERE t.dispatch_status_code = 'Dispatched'
          AND t.accepted_at IS NOT NULL
          AND t.picked_up_at IS NULL
          AND EXISTS (
              SELECT 1 FROM port_slot_bookings b
              WHERE b.allocation_id = t.allocation_id AND b.released_at IS NULL
          )
    """)
    pickups = 0
    for row in cursor.fetchall():
        if not within_geofence(row['latitude'], row['longitude'], PORT_LAT, PORT_LNG, radius_km):
            continue
        cursor.execute(
            "UPDATE truck_allocations SET picked_up_at = NOW() WHERE container_number = %s",
            (row['container_number'],),
        )
        released = release_slots_for_allocation(cursor, row['allocation_id'])
        payload = json.dumps({
            'driver_id': row['driver_id'],
            'driver_name': row['driver_name'],
            'lat': float(row['latitude']),
            'lng': float(row['longitude']),
            'slots_released': released,
        })
        cursor.execute(
            "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
            (row['container_number'], 'PORT_GATE', 'CONTAINER_PICKED_UP', payload),
        )
        if released:
            cursor.execute(
                "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
                (row['container_number'], 'PORT_SLOT_SYSTEM', 'PRIME_MOVER_SLOT_RELEASED', payload),
            )
        pickups += 1
    return pickups


def process_warehouse_arrivals(cursor):
    """Container delivered to de-stuff yard — ready for POD; driver freed if still on shift."""
    cursor.execute("""
        SELECT t.container_number, da.driver_id, t.allocation_id, t.warehouse_id,
               dl.latitude, dl.longitude, d.driver_name
        FROM truck_allocations t
        JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        JOIN drivers d ON d.driver_id = da.driver_id
        JOIN driver_locations dl ON dl.driver_id = d.driver_id
        WHERE t.dispatch_status_code = 'Dispatched'
          AND t.picked_up_at IS NOT NULL
    """)
    arrivals = 0
    for row in cursor.fetchall():
        wh = _fetch_warehouse(cursor, row['warehouse_id']) or {
            'latitude': DEFAULT_WAREHOUSE_LAT,
            'longitude': DEFAULT_WAREHOUSE_LNG,
            'geofence_radius_km': 1.5,
            'warehouse_name': 'Warehouse',
        }
        if not within_geofence(
            row['latitude'], row['longitude'],
            wh['latitude'], wh['longitude'], float(wh['geofence_radius_km']),
        ):
            continue
        cursor.execute(
            "UPDATE truck_allocations SET dispatch_status_code = 'At Warehouse', at_port_at = NOW() WHERE container_number = %s",
            (row['container_number'],),
        )
        on_shift = driver_is_on_shift(cursor, row['driver_id'])
        if on_shift:
            cursor.execute(
                "UPDATE drivers SET status_code = 'Available' WHERE driver_id = %s",
                (row['driver_id'],),
            )
        payload = json.dumps({
            'driver_id': row['driver_id'],
            'driver_name': row['driver_name'],
            'warehouse_id': row['warehouse_id'],
            'warehouse_name': wh.get('warehouse_name'),
            'lat': float(row['latitude']),
            'lng': float(row['longitude']),
            'driver_freed': on_shift,
        })
        cursor.execute(
            "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
            (row['container_number'], 'WAREHOUSE_SYSTEM', 'DELIVERED_AT_WAREHOUSE', payload),
        )
        arrivals += 1
    return arrivals
