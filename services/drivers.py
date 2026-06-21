"""Driver, roster, schedule, and live fleet status database operations."""
import datetime
from services.locations import upsert_driver_location
from services.shifts import driver_is_on_shift
from services.port_slots import EMERGENCY_CONTRACTOR_NAME
from services.geo_utils import haversine_km

REJECTION_COOLDOWN_HOURS = 2
DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

def is_fleet_driver(driver):
    name = driver.get('driver_name') if isinstance(driver, dict) else driver
    return name != EMERGENCY_CONTRACTOR_NAME

def fetch_drivers_for_roster(cursor, exclude_emergency=True):
    if exclude_emergency:
        cursor.execute(
            "SELECT * FROM v_drivers_live WHERE driver_name != %s ORDER BY current_status, driver_name",
            (EMERGENCY_CONTRACTOR_NAME,)
        )
    else:
        cursor.execute("SELECT * FROM v_drivers_live ORDER BY current_status, driver_name")
    return cursor.fetchall()

def fetch_all_drivers_live(cursor):
    cursor.execute("SELECT * FROM v_drivers_live ORDER BY driver_name")
    return cursor.fetchall()

def fetch_live_drivers_telemetry(cursor, exclude_emergency=True):
    if exclude_emergency:
        cursor.execute("""
            SELECT driver_id, driver_name, current_status, latitude, longitude,
                   heading, speed_kph, DATE_FORMAT(last_gps_update, '%%Y-%%m-%%dT%%H:%%i:%%s') AS last_gps_update
            FROM v_drivers_live
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
              AND driver_name != %s
        """, (EMERGENCY_CONTRACTOR_NAME,))
    else:
        cursor.execute("""
            SELECT driver_id, driver_name, current_status, latitude, longitude,
                   heading, speed_kph, DATE_FORMAT(last_gps_update, '%%Y-%%m-%%dT%%H:%%i:%%s') AS last_gps_update
            FROM v_drivers_live
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        """)
    return cursor.fetchall()

def fetch_driver_by_id(cursor, driver_id):
    cursor.execute("SELECT * FROM v_drivers_live WHERE driver_id = %s", (driver_id,))
    return cursor.fetchone()

def add_driver(cursor, name, phone, default_lat, default_lng):
    cursor.execute("INSERT INTO drivers (driver_name, phone_number, status_code) VALUES (%s, %s, 'Available')", (name, phone))
    driver_id = cursor.lastrowid
    upsert_driver_location(cursor, driver_id, default_lat, default_lng)
    for day in range(7):
        cursor.execute(
            "INSERT INTO driver_schedules (driver_id, day_of_week, shift_start, shift_end) VALUES (%s, %s, %s, %s)",
            (driver_id, day, '06:00:00', '18:00:00')
        )
    return driver_id

def update_driver(cursor, driver_id, phone, status):
    cursor.execute("UPDATE drivers SET phone_number = %s, status_code = %s WHERE driver_id = %s", (phone, status, driver_id))

def remove_driver(cursor, driver_id):
    cursor.execute("DELETE FROM drivers WHERE driver_id = %s", (driver_id,))

def fetch_driver_schedules(cursor):
    cursor.execute("""
        SELECT driver_id, day_of_week,
               TIME_FORMAT(shift_start, '%%H:%%i') AS shift_start,
               TIME_FORMAT(shift_end, '%%H:%%i') AS shift_end
        FROM driver_schedules
        ORDER BY driver_id, day_of_week
    """)
    schedule_map = {}
    for row in cursor.fetchall():
        schedule_map.setdefault(row['driver_id'], {})[row['day_of_week']] = row
    return schedule_map

def update_driver_schedule(cursor, driver_id, schedule_days):
    cursor.execute("DELETE FROM driver_schedules WHERE driver_id = %s", (driver_id,))
    for day, is_enabled, start, end in schedule_days:
        if is_enabled:
            cursor.execute(
                "INSERT INTO driver_schedules (driver_id, day_of_week, shift_start, shift_end) VALUES (%s, %s, %s, %s)",
                (driver_id, day, start, end)
            )

def driver_has_active_allocation(driver_id, cursor):
    cursor.execute("""
        SELECT 1 FROM truck_allocations t
        JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('pending', 'accepted', 'completed')
        WHERE da.driver_id = %s AND t.dispatch_status_code IN ('Dispatched', 'At Warehouse')
        LIMIT 1
    """, (driver_id,))
    return cursor.fetchone() is not None

def driver_rejected_container(driver_id, cursor, container_num=None):
    if not container_num:
        return False
    cursor.execute(
        """
        SELECT 1 FROM job_rejections jr
        JOIN dispatch_assignments da ON da.assignment_id = jr.assignment_id
        JOIN truck_allocations t ON t.allocation_id = da.allocation_id
        WHERE da.driver_id = %s AND t.container_number = %s
        LIMIT 1
        """,
        (driver_id, container_num),
    )
    return cursor.fetchone() is not None

def driver_is_dispatchable(driver_id, status_code, cursor, as_of=None, container_num=None):
    if status_code != 'Available':
        return False
    if driver_has_active_allocation(driver_id, cursor):
        return False
    if driver_rejected_container(driver_id, cursor, container_num):
        return False
    return driver_is_on_shift(driver_id, cursor, as_of)

def count_dispatchable_drivers(cursor, as_of=None, container_num=None):
    cursor.execute("SELECT driver_id, status_code FROM drivers")
    return sum(
        1 for d in cursor.fetchall()
        if driver_is_dispatchable(d['driver_id'], d['status_code'], cursor, as_of, container_num)
    )

def pick_nearest_dispatchable_driver(cursor, port_lat, port_lng, depot_lat, depot_lng, container_num=None):
    cursor.execute("""
        SELECT d.driver_id, d.driver_name, d.phone_number, d.status_code,
               dl.latitude, dl.longitude
        FROM drivers d
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
        WHERE d.status_code = 'Available' AND d.driver_name != %s
    """, (EMERGENCY_CONTRACTOR_NAME,))
    best = None
    best_dist = None
    for candidate in cursor.fetchall():
        if not driver_is_dispatchable(candidate['driver_id'], candidate['status_code'], cursor, container_num=container_num):
            continue
        lat = float(candidate['latitude'] or depot_lat)
        lng = float(candidate['longitude'] or depot_lng)
        dist = haversine_km(lat, lng, port_lat, port_lng)
        if best is None or dist < best_dist:
            best = candidate
            best_dist = dist
    if not best:
        return None, None, None
    return (
        best['driver_id'],
        best['driver_name'],
        best['phone_number'].replace(' ', '')[-4:],
    )

def format_schedule_summary(schedule_map, driver_id):
    days = schedule_map.get(driver_id, {})
    if not days:
        return "No schedule set"
    parts = []
    for dow in sorted(days):
        d = days[dow]
        parts.append(f"{DAY_LABELS[dow]} {d['shift_start']}–{d['shift_end']}")
    return ", ".join(parts)

def enrich_drivers_with_schedules(drivers, schedule_map, cursor, as_of=None):
    as_of = as_of or datetime.datetime.now()
    as_of_date = as_of.date()
    enriched = []
    for driver in drivers:
        d = dict(driver)
        driver_id = driver['driver_id']
        
        # Check if driver is on leave today (for shifts starting later today)
        cursor.execute(
            "SELECT 1 FROM leave_requests WHERE driver_id = %s AND leave_date = %s",
            (driver_id, as_of_date),
        )
        has_leave_today = cursor.fetchone() is not None
        
        d['schedule_summary'] = format_schedule_summary(schedule_map, driver_id)
        days = schedule_map.get(driver_id, {})
        dow = as_of.weekday()
        
        from services.shifts import check_shift_and_leave
        is_on_shift, is_missing_active_shift = check_shift_and_leave(driver_id, cursor, as_of)
        d['on_shift'] = is_on_shift
        
        if is_missing_active_shift or has_leave_today:
            d['on_shift'] = False
            d['today_hours'] = "On Leave"
            if is_missing_active_shift:
                d['current_status'] = "On Leave"
        else:
            if dow in days:
                d['today_hours'] = f"{days[dow]['shift_start']} – {days[dow]['shift_end']}"
            elif days:
                d['today_hours'] = "Off today"
            else:
                d['today_hours'] = "Unscheduled"
        enriched.append(d)
    return enriched

def get_fleet_status(cursor, as_of=None, container_num=None):
    as_of = as_of or datetime.datetime.now()
    cursor.execute("SELECT driver_id, status_code, driver_name FROM drivers")
    dispatchable = off_shift_available = on_delivery = offline = 0

    for driver in cursor.fetchall():
        if not is_fleet_driver(driver):
            continue
        status = driver['status_code']
        if status == 'On Delivery' or driver_has_active_allocation(driver['driver_id'], cursor):
            on_delivery += 1
        elif status == 'Offline':
            offline += 1
        elif driver_is_dispatchable(driver['driver_id'], status, cursor, as_of, container_num):
            dispatchable += 1
        elif status == 'Available':
            off_shift_available += 1

    if dispatchable > 0:
        reason = None
    elif off_shift_available > 0 and on_delivery == 0:
        reason = 'off_shift'
    elif off_shift_available > 0 and on_delivery > 0:
        reason = 'off_shift_and_busy'
    elif on_delivery > 0:
        reason = 'all_busy'
    else:
        reason = 'unavailable'

    return {
        'dispatchable': dispatchable,
        'off_shift_available': off_shift_available,
        'on_delivery': on_delivery,
        'offline': offline,
        'depletion_reason': reason,
    }

def get_next_shift_hint(cursor, as_of=None):
    as_of = as_of or datetime.datetime.now()
    cursor.execute("""
        SELECT ds.day_of_week, TIME_FORMAT(ds.shift_start, '%H:%i') AS shift_start, d.driver_name
        FROM driver_schedules ds
        JOIN drivers d ON d.driver_id = ds.driver_id
        WHERE d.status_code = 'Available'
        ORDER BY ds.day_of_week, ds.shift_start
    """)
    rows = cursor.fetchall()
    today = as_of.weekday()
    now_time = as_of.time()

    for offset in range(8):
        dow = (today + offset) % 7
        for row in rows:
            if row['day_of_week'] != dow:
                continue
            start = datetime.datetime.strptime(row['shift_start'], '%H:%M').time()
            if offset == 0 and start <= now_time:
                continue
            day_label = 'Today' if offset == 0 else DAY_LABELS[dow]
            return f"Next shift: {row['driver_name']} — {day_label} at {row['shift_start']}"
    return None

def update_driver_status(cursor, driver_id, status):
    cursor.execute("UPDATE drivers SET status_code = %s WHERE driver_id = %s", (status, driver_id))

def auto_reject_timed_out_assignments(cursor):
    cursor.execute("SELECT assignment_id FROM dispatch_assignments WHERE outcome_code = 'pending' AND assigned_at < DATE_SUB(NOW(), INTERVAL 15 MINUTE)")
    for row in cursor.fetchall():
        assignment_id = row['assignment_id']
        cursor.execute("INSERT INTO job_rejections (assignment_id, rejected_at, reason) VALUES (%s, NOW(), 'Auto-timeout after 15 minutes')", (assignment_id,))
        cursor.execute("UPDATE dispatch_assignments SET outcome_code = 'rejected', outcome_at = NOW() WHERE assignment_id = %s", (assignment_id,))
        cursor.execute("UPDATE truck_allocations SET dispatch_status_code = 'Pending', accepted_at = NULL WHERE allocation_id = (SELECT allocation_id FROM dispatch_assignments WHERE assignment_id = %s)", (assignment_id,))
