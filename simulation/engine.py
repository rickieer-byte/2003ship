"""Mock GPS and AIS telemetry for demo / PythonAnywhere scheduled tasks."""
import datetime
import json
import math
import random
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MySQLdb
from dotenv import load_dotenv
from services.locations import upsert_driver_location

load_dotenv()

DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'root'),
    'passwd': os.getenv('DB_PASSWORD', ''),
    'db': os.getenv('DB_NAME', 'escalation_db'),
    'cursorclass': MySQLdb.cursors.DictCursor,
}

PORT_TERMINAL = (1.3015, 103.6340)
WAREHOUSE_YARD = (1.3298, 103.6954)
FLEET_DEPOT = (1.3350, 103.7080)
VESSEL_SPAWN = (1.0500, 103.7200)

SIMULATED_CARRIERS = [
    {"vessel_id": "VES-COSCO-88", "voyage_id": "V-COSCO-88", "name": "Cosco Shipping Alps", "voyage": "045W"},
    {"vessel_id": "VES-MAERSK-12", "voyage_id": "V-MAERSK-12", "name": "Maersk Mc-Kinney Moller", "voyage": "261E"},
    {"vessel_id": "VES-ONE-CYGNUS", "voyage_id": "V-ONE-CYGNUS", "name": "ONE Cygnus", "voyage": "014N"},
    {"vessel_id": "VES-EVER-GIVEN", "voyage_id": "V-EVER-GIVEN", "name": "Ever Given", "voyage": "0993-02B"},
    {"vessel_id": "VES-HMM-ALG", "voyage_id": "V-HMM-ALG", "name": "HMM Algeciras", "voyage": "012E"},
    {"vessel_id": "VES-MSC-GUL", "voyage_id": "V-MSC-GUL", "name": "MSC Gulsun", "voyage": "318W"},
    {"vessel_id": "VES-CMA-MPO", "voyage_id": "V-CMA-MPO", "name": "CMA CGM Marco Polo", "voyage": "0FMLW1"},
    {"vessel_id": "VES-YML-UTM", "voyage_id": "V-YML-UTM", "name": "Yang Ming Utmost", "voyage": "088E"},
    {"vessel_id": "VES-HLC-BER", "voyage_id": "V-HLC-BER", "name": "Hapag-Lloyd Berlin Express", "voyage": "051N"},
    {"vessel_id": "VES-PIL-KOT", "voyage_id": "V-PIL-KOT", "name": "PIL Kota Cabot", "voyage": "024S"},
    {"vessel_id": "VES-ZIM-SGP", "voyage_id": "V-ZIM-SGP", "name": "ZIM Singapore", "voyage": "7E"},
    {"vessel_id": "VES-WHL-282", "voyage_id": "V-WHL-282", "name": "Wan Hai 282", "voyage": "E006"},
]

CONTAINER_PREFIXES = ['MSKU', 'MEDU', 'CMAU', 'COSU', 'ONEU', 'TEMU', 'NYKU', 'EMCU']


def _jitter(value, spread=0.004):
    return value + random.uniform(-spread, spread)


def _move_toward(lat, lng, target_lat, target_lng, step):
    dlat = target_lat - lat
    dlng = target_lng - lng
    dist = math.sqrt(dlat ** 2 + dlng ** 2)
    if dist <= step:
        return target_lat, target_lng, dist
    ratio = step / dist
    return lat + dlat * ratio, lng + dlng * ratio, dist - step


def _bearing(lat1, lng1, lat2, lng2):
    y = math.sin(math.radians(lng2 - lng1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lng2 - lng1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _generate_container_number():
    prefix = random.choice(CONTAINER_PREFIXES)
    return f"{prefix}{''.join(str(random.randint(0, 9)) for _ in range(7))}"


def tick_driver_gps(cursor):
    cursor.execute("""
        SELECT d.driver_id, d.status_code, dl.latitude, dl.longitude
        FROM drivers d
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
    """)
    updated = 0
    for row in cursor.fetchall():
        lat = float(row['latitude'] or FLEET_DEPOT[0])
        lng = float(row['longitude'] or FLEET_DEPOT[1])
        status = row['status_code']

        if status == 'On Delivery':
            cursor.execute("""
                SELECT t.picked_up_at,
                       EXISTS(
                           SELECT 1 FROM port_slot_bookings b
                           WHERE b.allocation_id = t.allocation_id AND b.released_at IS NULL
                       ) AS has_active_slot
                FROM truck_allocations t
                WHERE t.driver_id = %s AND t.dispatch_status_code = 'Dispatched'
                LIMIT 1
            """, (row['driver_id'],))
            job = cursor.fetchone()
            if not job:
                step, speed = 0, 0
                target = (lat, lng)
            elif not job['picked_up_at'] and not job['has_active_slot']:
                step, speed = 0, 0
                target = (lat, lng)
            elif not job['picked_up_at']:
                target = PORT_TERMINAL
                dist = math.sqrt((PORT_TERMINAL[0] - lat) ** 2 + (PORT_TERMINAL[1] - lng) ** 2)
                step = 0.011 if dist < 0.03 else 0.008
                speed = random.uniform(40, 55)
            else:
                target = WAREHOUSE_YARD
                dist = math.sqrt((WAREHOUSE_YARD[0] - lat) ** 2 + (WAREHOUSE_YARD[1] - lng) ** 2)
                step = 0.011 if dist < 0.03 else 0.008
                speed = random.uniform(35, 50)
        elif status == 'Available':
            target, step = FLEET_DEPOT, 0.001
            speed = random.uniform(0, 8)
        else:
            step, speed = 0, 0
            target = (lat, lng)

        if step > 0:
            lat, lng, _ = _move_toward(lat, lng, target[0], target[1], step)
            lat, lng = _jitter(lat, 0.0008), _jitter(lng, 0.0008)

        heading = _bearing(lat, lng, target[0], target[1]) if step > 0 else 0
        upsert_driver_location(cursor, row['driver_id'], round(lat, 6), round(lng, 6), round(heading, 1), round(speed, 1))
        updated += 1
    return updated


def tick_vessel_arrivals(cursor):
    cursor.execute("""
        SELECT vy.voyage_id, vy.vessel_id, vt.latitude, vt.longitude, vt.heading,
               vt.speed_knots, vt.eta_datetime, vt.tracking_status_code AS tracking_status
        FROM voyages vy
        LEFT JOIN vessel_tracking vt ON vt.voyage_id = vy.voyage_id
    """)
    rows = cursor.fetchall()
    arrivals = 0
    new_containers = 0

    for row in rows:
        lat = float(row['latitude'] or VESSEL_SPAWN[0])
        lng = float(row['longitude'] or VESSEL_SPAWN[1])
        status = row['tracking_status'] or 'At Sea'
        step = 0.012 if status in ('At Sea', 'Approaching', 'Berthing') else 0
        speed_knots = random.uniform(8, 14) if step else 0

        if status == 'At Berth':
            eta = row['eta_datetime']
            if eta and eta < datetime.datetime.now() - datetime.timedelta(hours=6):
                lat = VESSEL_SPAWN[0] + random.uniform(-0.05, 0.05)
                lng = VESSEL_SPAWN[1] + random.uniform(-0.05, 0.05)
                status = 'At Sea'
                eta = datetime.datetime.now() + datetime.timedelta(hours=random.choice([2, 4, 6, 8]))
            else:
                cursor.execute("""
                    UPDATE vessel_tracking SET latitude = %s, longitude = %s, speed_knots = 0,
                           heading = %s, tracking_status_code = %s, eta_datetime = %s
                    WHERE voyage_id = %s
                """, (lat, lng, row['heading'] or 0, status, eta, row['voyage_id']))
                continue

        lat, lng, remaining = _move_toward(lat, lng, PORT_TERMINAL[0], PORT_TERMINAL[1], step)
        heading = _bearing(lat, lng, PORT_TERMINAL[0], PORT_TERMINAL[1])

        if remaining < 0.015 and status in ('At Sea', 'Approaching'):
            status = 'Berthing'
            eta = datetime.datetime.now() + datetime.timedelta(minutes=random.randint(15, 45))
        elif status == 'Berthing' and remaining < 0.003:
            status = 'At Berth'
            eta = datetime.datetime.now()
            lat, lng = PORT_TERMINAL[0], PORT_TERMINAL[1]
            new_containers += _discharge_container_from_voyage(cursor, row['voyage_id'])
            arrivals += 1
        elif status == 'At Sea' and remaining < 0.08:
            status = 'Approaching'
            eta = datetime.datetime.now() + datetime.timedelta(hours=random.uniform(0.5, 2))
        else:
            eta = row['eta_datetime'] or datetime.datetime.now() + datetime.timedelta(hours=4)

        cursor.execute("""
            INSERT INTO vessel_tracking (voyage_id, latitude, longitude, heading, speed_knots, eta_datetime, tracking_status_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                latitude = VALUES(latitude), longitude = VALUES(longitude),
                heading = VALUES(heading), speed_knots = VALUES(speed_knots),
                eta_datetime = VALUES(eta_datetime), tracking_status_code = VALUES(tracking_status_code)
        """, (row['voyage_id'], round(lat, 6), round(lng, 6), round(heading, 1),
              round(speed_knots, 1), eta, status))

    return {'vessels_updated': len(rows), 'arrivals': arrivals, 'containers_discharged': new_containers}


def _discharge_container_from_voyage(cursor, voyage_id):
    container_number = _generate_container_number()
    discharge = datetime.datetime.now()
    lfd = discharge + datetime.timedelta(hours=random.choice([6, 12, 18, 24, 36, 48]))
    payload = {
        'api_source': 'PORTNET_AIS_SIMULATOR',
        'event': 'VESSEL_BERTH_CONTAINER_DISCHARGE',
        'voyage_id': voyage_id,
        'container_id': container_number,
        'discharge_datetime': discharge.strftime('%Y-%m-%d %H:%M:%S'),
        'last_free_date_deadline': lfd.strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        cursor.execute(
            "INSERT INTO containers (container_number, voyage_id, discharge_datetime, lfd_datetime) VALUES (%s, %s, %s, %s)",
            (container_number, voyage_id, discharge, lfd),
        )
        cursor.execute(
            "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
            (container_number, payload['api_source'], 'SIMULATED_VESSEL_DISCHARGE', json.dumps(payload)),
        )
        return 1
    except Exception:
        return 0


def run_simulation_tick(db=None, simulation_mode=True):
    owns_connection = db is None
    if owns_connection:
        db = MySQLdb.connect(**DB_CONFIG)
    cursor = db.cursor()
    try:
        result = {'simulation_mode': simulation_mode, 'timestamp': datetime.datetime.now().isoformat()}
        if simulation_mode:
            result['drivers_updated'] = tick_driver_gps(cursor)
            result.update(tick_vessel_arrivals(cursor))
        from services.geofence import process_port_pickups, process_warehouse_arrivals
        from config import Config
        result['port_pickups'] = process_port_pickups(cursor, Config.GEOFENCE_RADIUS_KM)
        result['warehouse_arrivals'] = process_warehouse_arrivals(cursor)
        from services.notifications import check_escalation_alerts
        result['alerts'] = check_escalation_alerts(cursor)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        raise exc
    finally:
        cursor.close()
        if owns_connection:
            db.close()


if __name__ == '__main__':
    print(run_simulation_tick())
