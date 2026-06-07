import datetime
import uuid
import os
import json
import jwt
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, make_response, g
from flask_mysqldb import MySQL
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from config import Config
from services.geo_utils import eta_minutes_from_gps, haversine_km
from services.locations import upsert_driver_location

app = Flask(__name__)
app.config.from_object(Config)
mysql = MySQL(app)

# Financial and DSS Metric Parameters
EMERGENCY_DRIVER_FLAT_RATE = 250.00
STORE_RENT_HOURLY_RATE = 12.00       # Port terminal storage after LFD breach
DEMURRAGE_HOURLY_RATE = 15.00        # Carrier demurrage after post-discharge grace period
GRACE_PERIOD_HOURS = 48              # Free time after vessel discharge before demurrage accrues
DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

# Map reference points exposed to the tracking UI
MAP_PORT = {'lat': 1.3015, 'lng': 103.6340, 'label': 'Tuas Port Terminal'}
MAP_WAREHOUSE = {'lat': 1.3298, 'lng': 103.6954, 'label': 'Jurong De-stuff Yard', 'id': 'WH-JURONG'}
MAP_DEPOT = {'lat': 1.3350, 'lng': 103.7080, 'label': 'Fleet Depot'}

def calculate_port_fees(lfd_datetime, discharge_datetime, as_of=None):
    """Compute store rent (post-LFD) and demurrage (post-discharge grace) separately."""
    as_of = as_of or datetime.datetime.now()
    hours_past_lfd = max(0.0, (as_of - lfd_datetime).total_seconds() / 3600.0)
    store_rent = round(hours_past_lfd * STORE_RENT_HOURLY_RATE, 2)

    demurrage_start = discharge_datetime + datetime.timedelta(hours=GRACE_PERIOD_HOURS)
    hours_past_demurrage = max(0.0, (as_of - demurrage_start).total_seconds() / 3600.0)
    demurrage = round(hours_past_demurrage * DEMURRAGE_HOURLY_RATE, 2)

    return {
        "store_rent": store_rent,
        "demurrage": demurrage,
        "total": round(store_rent + demurrage, 2),
        "grace_period_hours": GRACE_PERIOD_HOURS,
    }

def _as_time(value):
    if isinstance(value, datetime.time):
        return value
    if isinstance(value, datetime.timedelta):
        return (datetime.datetime.min + value).time()
    return value

def driver_is_on_shift(driver_id, cursor, as_of=None):
    """True only when the driver has a schedule row for today and current time is within it."""
    as_of = as_of or datetime.datetime.now()
    cursor.execute(
        "SELECT shift_start, shift_end FROM driver_schedules WHERE driver_id = %s AND day_of_week = %s",
        (driver_id, as_of.weekday()),
    )
    row = cursor.fetchone()
    if not row:
        return False
    current_time = as_of.time()
    start = _as_time(row['shift_start'])
    end = _as_time(row['shift_end'])
    return start <= current_time < end

def driver_has_active_allocation(driver_id, cursor):
    cursor.execute("""
        SELECT 1 FROM truck_allocations
        WHERE driver_id = %s AND dispatch_status_code IN ('Dispatched', 'At Warehouse')
        LIMIT 1
    """, (driver_id,))
    return cursor.fetchone() is not None

def driver_is_dispatchable(driver_id, status_code, cursor, as_of=None):
    if status_code != 'Available':
        return False
    if driver_has_active_allocation(driver_id, cursor):
        return False
    return driver_is_on_shift(driver_id, cursor, as_of)

def count_dispatchable_drivers(cursor, as_of=None):
    cursor.execute("SELECT driver_id, status_code FROM drivers")
    return sum(
        1 for d in cursor.fetchall()
        if driver_is_dispatchable(d['driver_id'], d['status_code'], cursor, as_of)
    )

def pick_nearest_dispatchable_driver(cursor, port_lat, port_lng):
    cursor.execute("""
        SELECT d.driver_id, d.driver_name, d.phone_number, d.status_code,
               dl.latitude, dl.longitude
        FROM drivers d
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
        WHERE d.status_code = 'Available' AND d.driver_name != 'Emergency Contractor'
    """)
    best = None
    best_dist = None
    for candidate in cursor.fetchall():
        if not driver_is_dispatchable(candidate['driver_id'], candidate['status_code'], cursor):
            continue
        lat = float(candidate['latitude'] or MAP_DEPOT['lat'])
        lng = float(candidate['longitude'] or MAP_DEPOT['lng'])
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

def fetch_driver_schedules(cursor):
    cursor.execute("""
        SELECT driver_id, day_of_week,
               TIME_FORMAT(shift_start, '%H:%i') AS shift_start,
               TIME_FORMAT(shift_end, '%H:%i') AS shift_end
        FROM driver_schedules
        ORDER BY driver_id, day_of_week
    """)
    schedule_map = {}
    for row in cursor.fetchall():
        schedule_map.setdefault(row['driver_id'], {})[row['day_of_week']] = row
    return schedule_map

def format_schedule_summary(schedule_map, driver_id):
    days = schedule_map.get(driver_id, {})
    if not days:
        return "No schedule set"
    parts = []
    for dow in sorted(days):
        d = days[dow]
        parts.append(f"{DAY_LABELS[dow]} {d['shift_start']}–{d['shift_end']}")
    return ", ".join(parts)

def compute_fleet_savings(cursor):
    cursor.execute("""
        SELECT c.lfd_datetime, c.discharge_datetime, t.allocated_at
        FROM containers c
        JOIN truck_allocations t ON c.container_number = t.container_number
        WHERE t.dispatch_status_code = 'Dispatched' AND t.driver_id IS NOT NULL
    """)
    internal_dispatches = cursor.fetchall()

    fees_averted = 0.0
    on_time_count = 0
    for row in internal_dispatches:
        lfd = row['lfd_datetime']
        allocated = row['allocated_at']
        if allocated <= lfd:
            on_time_count += 1
        counterfactual_at = lfd + datetime.timedelta(hours=24)
        fees_averted += calculate_port_fees(lfd, row['discharge_datetime'], as_of=counterfactual_at)['total']

    emergency_savings = on_time_count * EMERGENCY_DRIVER_FLAT_RATE
    return {
        'on_time_dispatches': on_time_count,
        'fees_averted': round(fees_averted, 2),
        'emergency_savings': round(emergency_savings, 2),
        'total_savings': round(fees_averted + emergency_savings, 2),
    }

def enrich_drivers_with_schedules(drivers, schedule_map, as_of=None):
    as_of = as_of or datetime.datetime.now()
    enriched = []
    for driver in drivers:
        d = dict(driver)
        d['schedule_summary'] = format_schedule_summary(schedule_map, driver['driver_id'])
        days = schedule_map.get(driver['driver_id'], {})
        dow = as_of.weekday()
        if dow in days:
            day = days[dow]
            d['today_hours'] = f"{day['shift_start']} – {day['shift_end']}"
            start = datetime.datetime.strptime(day['shift_start'], '%H:%M').time()
            end = datetime.datetime.strptime(day['shift_end'], '%H:%M').time()
            t = as_of.time()
            d['on_shift'] = start <= t < end
        elif days:
            d['today_hours'] = "Off today"
            d['on_shift'] = False
        else:
            d['today_hours'] = "Unscheduled"
            d['on_shift'] = False
        enriched.append(d)
    return enriched

def get_fleet_status(cursor, as_of=None):
    as_of = as_of or datetime.datetime.now()
    cursor.execute("SELECT driver_id, status_code FROM drivers")
    dispatchable = off_shift_available = on_delivery = offline = 0

    for driver in cursor.fetchall():
        status = driver['status_code']
        if status == 'On Delivery' or driver_has_active_allocation(driver['driver_id'], cursor):
            on_delivery += 1
        elif status == 'Offline':
            offline += 1
        elif driver_is_dispatchable(driver['driver_id'], status, cursor, as_of):
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

DEPLETION_MESSAGES = {
    'off_shift': {
        'title': 'ALL DRIVERS OFF SHIFT',
        'body': 'No drivers are within scheduled work hours. Authorize an emergency hire to dispatch immediately, or defer until the next shift.',
    },
    'off_shift_and_busy': {
        'title': 'FLEET UNAVAILABLE',
        'body': 'Drivers are either off shift or on active deliveries. Emergency dispatch can cover this container now.',
    },
    'all_busy': {
        'title': 'ALL DRIVERS ON DELIVERY',
        'body': 'Every on-shift driver is currently assigned. Emergency dispatch is available to avoid port penalties.',
    },
    'unavailable': {
        'title': 'INTERNAL FLEET RESOURCES EXHAUSTED',
        'body': 'No operational drivers are available. Delaying allocation risks port storage charges and carrier demurrage penalties.',
    },
}

def build_emergency_response(cursor, container, hours_until_breach):
    projected_at = datetime.datetime.now() + datetime.timedelta(hours=24.0)
    fees = calculate_port_fees(container['lfd_datetime'], container['discharge_datetime'], as_of=projected_at)
    if fees['total'] == 0 and hours_until_breach <= 12:
        fees['store_rent'] = 50.00
        fees['demurrage'] = 25.00
        fees['total'] = 75.00

    fleet = get_fleet_status(cursor)
    reason = fleet['depletion_reason'] or 'unavailable'
    msg = DEPLETION_MESSAGES.get(reason, DEPLETION_MESSAGES['unavailable'])
    next_shift = get_next_shift_hint(cursor) if reason in ('off_shift', 'off_shift_and_busy') else None

    return {
        'status': 'depleted',
        'depletion_reason': reason,
        'modal_title': msg['title'],
        'modal_message': msg['body'],
        'next_shift_hint': next_shift,
        'fleet': fleet,
        'emergency_cost': EMERGENCY_DRIVER_FLAT_RATE,
        'projected_store_rent': fees['store_rent'],
        'projected_demurrage': fees['demurrage'],
        'projected_total': fees['total'],
        'grace_period_hours': fees['grace_period_hours'],
        'financial_recommendation': 'PROCEED' if EMERGENCY_DRIVER_FLAT_RATE < fees['total'] else 'HOLD',
    }

def log_event(cursor, container_number, source_api, event_type, payload):
    cursor.execute(
        "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
        (container_number, source_api, event_type, json.dumps(payload)),
    )

def fetch_inbound_vessels(cursor):
    cursor.execute("""
        SELECT vessel_name, voyage_number, tracking_status, speed_knots,
               DATE_FORMAT(eta_datetime, '%Y-%m-%d %H:%i') AS eta_display,
               DATE_FORMAT(eta_datetime, '%Y-%m-%dT%H:%i:%s') AS eta_iso
        FROM v_vessels_live
        WHERE tracking_status != 'At Berth'
        ORDER BY eta_datetime ASC
        LIMIT 10
    """)
    return cursor.fetchall()

def enrich_containers_with_eta(containers):
    for c in containers:
        if not c.get('driver_lat') or c['current_dispatch_status'] not in ('Dispatched', 'At Warehouse'):
            c['eta_minutes'] = None
            continue
        if c.get('picked_up_at'):
            target_lat, target_lng = MAP_WAREHOUSE['lat'], MAP_WAREHOUSE['lng']
        elif c.get('accepted_at') and c.get('port_slot_booked'):
            target_lat, target_lng = MAP_PORT['lat'], MAP_PORT['lng']
        else:
            c['eta_minutes'] = None
            continue
        c['eta_minutes'] = int(eta_minutes_from_gps(
            c['driver_lat'], c['driver_lng'], c['speed_kph'] or 30,
            target_lat, target_lng,
        ))
    return containers

def get_driver_from_cookie():
    driver_id = request.cookies.get('driver_id')
    if not driver_id:
        return None
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM v_drivers_live WHERE driver_id = %s", (driver_id,))
    driver = cursor.fetchone()
    cursor.close()
    return driver

# Server Instance Run Token Key Generation
SERVER_RUN_ID = str(uuid.uuid4())
JWT_SECRET = app.config['SECRET_KEY']

print(f"[*] App System Initialized. Active Server Run ID: {SERVER_RUN_ID}")

# -------------------------------------------------------------
# PYJWT AUTHENTICATION DECORATORS (PATCHED)
# -------------------------------------------------------------
def requires_authenticated_session():
    """ Enforces valid cryptographic token signatures across system endpoints """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = request.cookies.get("access_token")
            if not token:
                flash("Authentication required.", "warning")
                return redirect(url_for("login"))
            try:
                # Decrypt and decode signature check
                payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], leeway=10)
                
                # Check claim for application restart stale sessions
                if payload.get("run_id") != SERVER_RUN_ID:
                    print("[SECURITY] Stale session token from a previous server run detected. Voiding session.")
                    flash("Your session expired because the operations server restarted.", "warning")
                    response = make_response(redirect(url_for("login")))
                    response.delete_cookie("access_token", path="/")
                    return response
                    
                # Bind thread parameters safely to Flask global application context
                # PATCH: Explicitly handle string-to-int conversion to protect against type errors
                g.current_user_id = int(payload["sub"])
                g.current_user_username = str(payload["username"])
                g.current_user_role = str(payload["role"])
                
            except Exception as e:
                print(f"[DEBUG] INTERCEPTOR REJECTION LOG -> Decode failure: {e}")
                flash("Session invalid, expired, or server key changed.", "danger")
                response = make_response(redirect(url_for("login")))
                response.delete_cookie("access_token", path="/")
                return response
                
            return f(*args, **kwargs)
        return decorated
    return decorator

def requires_role(required_role_name):
    """ Enforces rigid role access separation across corporate layouts """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = request.cookies.get("access_token")
            if not token:
                flash("Authentication required.", "warning")
                return redirect(url_for("login"))
            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], leeway=10)
                
                if payload.get("run_id") != SERVER_RUN_ID:
                    print("[SECURITY] Auditor token out-of-sync with active server instance. Evicting.")
                    flash("Your session expired because the operations server restarted.", "warning")
                    response = make_response(redirect(url_for("login")))
                    response.delete_cookie("access_token", path="/")
                    return response
                    
                # PATCH: Explicitly handle string-to-int conversion here as well
                g.current_user_id = int(payload["sub"])
                g.current_user_username = str(payload["username"])
                g.current_user_role = str(payload["role"])
                
            except Exception as e:
                print(f"[DEBUG] ROLE CHECK INTERCEPTOR FAILURE -> {e}")
                flash("Session expired or invalid.", "danger")
                response = make_response(redirect(url_for("login")))
                response.delete_cookie("access_token", path="/")
                return response
            
            if g.current_user_role != required_role_name:
                flash("Access Denied: Insufficient privilege scope for this department layout area.", "danger")
                return redirect(url_for("mis_dashboard"))
                
            return f(*args, **kwargs)
        return decorated
    return decorator

# -------------------------------------------------------------
# SYSTEM AUTHENTICATION MANAGEMENT ENDPOINTS
# -------------------------------------------------------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        role = request.form.get('role', 'Planner')
        
        hashed_password = generate_password_hash(password)
        cursor = mysql.connection.cursor()
        try:
            cursor.execute("SELECT role_id FROM roles WHERE role_name = %s", (role,))
            role_row = cursor.fetchone()
            role_id = role_row['role_id'] if role_row else 1
            cursor.execute("INSERT INTO users (username, email, password_hash, role_id) VALUES (%s, %s, %s, %s)", 
                           (username, email, hashed_password, role_id))
            mysql.connection.commit()
            flash("Account provisioned successfully! Please sign in.", "success")
            return redirect(url_for('login'))
        except Exception:
            mysql.connection.rollback()
            flash("Registration error: Username or Email string signature already exists.", "danger")
        finally:
            cursor.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password_provided = request.form['password']
        
        cursor = mysql.connection.cursor()
        cursor.execute("""
            SELECT u.*, r.role_name AS role
            FROM users u
            JOIN roles r ON r.role_id = u.role_id
            WHERE u.username = %s
        """, (username,))
        user = cursor.fetchone()
        cursor.close()
        
        if user and check_password_hash(user['password_hash'], password_provided):
            # PATCH: Cast user['user_id'] directly to a standard string format to satisfy standard JWT subject specifications
            payload = {
                "sub": str(user['user_id']),
                "username": user['username'],
                "role": user['role'],
                "run_id": SERVER_RUN_ID,
                "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
            }
            token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
            
            
            response = make_response(redirect(url_for('mis_dashboard')))
            response.set_cookie(
                "access_token",
                token,
                httponly=True,
                samesite="Lax"
            )
            return response
        else:
            flash("Invalid credentials token supplied.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    flash("Session terminated. Secure exit complete.", "info")
    response = make_response(redirect(url_for('login')))
    response.delete_cookie("access_token", path="/")
    return response

# -------------------------------------------------------------
# OPERATIONS MANAGEMENT ENDPOINTS (MIS DASHBOARD)
# -------------------------------------------------------------
@app.route('/')
@requires_authenticated_session()
def mis_dashboard():
    cursor = mysql.connection.cursor()
    query = """
        SELECT c.container_number, v.vessel_name,
               DATE_FORMAT(c.lfd_datetime, '%Y-%m-%dT%H:%i:%s') as lfd_iso_string,
               c.lfd_datetime,
               IFNULL(t.dispatch_status_code, 'Pending') as current_dispatch_status,
               t.accepted_at, t.picked_up_at, t.driver_id AS assigned_driver_id,
               (psb.booking_id IS NOT NULL) AS port_slot_booked,
               dl.latitude AS driver_lat, dl.longitude AS driver_lng, dl.speed_kph, d.driver_name,
               CASE 
                   WHEN t.dispatch_status_code = 'At Warehouse' THEN 'AT WAREHOUSE'
                   WHEN t.dispatch_status_code = 'At Port' THEN 'AT PORT'
                   WHEN t.dispatch_status_code = 'Dispatched' AND t.driver_id IS NULL THEN 'EMERGENCY'
                   WHEN t.dispatch_status_code = 'Dispatched' AND t.accepted_at IS NULL THEN 'AWAITING'
                   WHEN t.dispatch_status_code = 'Dispatched' AND t.picked_up_at IS NULL AND t.accepted_at IS NOT NULL AND psb.booking_id IS NULL THEN 'SLOT WAIT'
                   WHEN t.dispatch_status_code = 'Dispatched' AND t.picked_up_at IS NULL THEN 'TO PORT'
                   WHEN t.dispatch_status_code = 'Dispatched' THEN 'TO WAREHOUSE'
                   WHEN TIMESTAMPDIFF(HOUR, NOW(), c.lfd_datetime) <= 12 THEN 'RED'
                   WHEN TIMESTAMPDIFF(HOUR, NOW(), c.lfd_datetime) <= 24 THEN 'YELLOW'
                   ELSE 'GREEN'
               END as alert_status
        FROM containers c
        JOIN voyages vy ON vy.voyage_id = c.voyage_id
        JOIN vessels v ON v.vessel_id = vy.vessel_id
        LEFT JOIN truck_allocations t ON c.container_number = t.container_number
        LEFT JOIN drivers d ON t.driver_id = d.driver_id
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
        LEFT JOIN port_slot_bookings psb ON psb.allocation_id = t.allocation_id AND psb.released_at IS NULL
        ORDER BY c.lfd_datetime ASC
    """
    cursor.execute(query)
    containers = enrich_containers_with_eta(cursor.fetchall())
    fleet = get_fleet_status(cursor)
    savings = compute_fleet_savings(cursor) if g.current_user_role == 'Fleet Manager' else None
    inbound_vessels = fetch_inbound_vessels(cursor)
    from services.notifications import check_escalation_alerts
    from services.port_slots import get_slot_status
    alert_result = check_escalation_alerts(cursor)
    port_slots = get_slot_status(cursor)
    mysql.connection.commit()
    cursor.close()
    return render_template('dashboard.html', containers=containers, fleet=fleet, savings=savings,
                           inbound_vessels=inbound_vessels, alert_result=alert_result,
                           port_slots=port_slots,
                           simulation_mode=Config.SIMULATION_MODE,
                           username=g.current_user_username, role=g.current_user_role)

# -------------------------------------------------------------
# OPTIMIZATION DECISION ENGINE ENDPOINTS (DSS LAYER)
# -------------------------------------------------------------
@app.route('/api/dispatch/check-availability', methods=['POST'])
@requires_authenticated_session()
def check_availability():
    data = request.json
    container_num = data.get('container_number')
    
    cursor = mysql.connection.cursor()
    fleet = get_fleet_status(cursor)

    if fleet['dispatchable'] > 0:
        cursor.close()
        return jsonify({"status": "available", "count": fleet['dispatchable']})
        
    cursor.execute("SELECT lfd_datetime, discharge_datetime FROM containers WHERE container_number = %s", (container_num,))
    container = cursor.fetchone()
    
    if not container:
        cursor.close()
        return jsonify({"status": "error", "message": "Cargo token identifier invalid"}), 404
        
    hours_until_breach = (container['lfd_datetime'] - datetime.datetime.now()).total_seconds() / 3600.0
    response = build_emergency_response(cursor, container, hours_until_breach)
    cursor.close()
    return jsonify(response)

@app.route('/api/dispatch/allocate', methods=['POST'])
@requires_authenticated_session()
def allocate_truck():
    data = request.json
    container_num = data.get('container_number')
    
    cursor = mysql.connection.cursor()
    try:
        driver_id, driver_name, phone_tail = pick_nearest_dispatchable_driver(
            cursor, MAP_PORT['lat'], MAP_PORT['lng'],
        )
        if not driver_id:
            return jsonify({"status": "depleted", "message": "No on-shift drivers available. Use emergency dispatch."}), 409
        
        cursor.execute("""
            INSERT INTO truck_allocations (container_number, driver_id, urgency_score, dispatch_status_code, accepted_at)
            VALUES (%s, %s, 95, 'Dispatched', NULL)
            ON DUPLICATE KEY UPDATE driver_id = %s, dispatch_status_code = 'Dispatched', accepted_at = NULL
        """, (container_num, driver_id, driver_id))
        
        log_event(cursor, container_num, 'DISPATCH_ENGINE', 'DISPATCH_ASSIGNED', {
            'driver_id': driver_id, 'driver_name': driver_name, 'urgency_score': 95,
        })
        
        mysql.connection.commit()
        return jsonify({
            "status": "success",
            "driver_id": driver_id,
            "driver_name": driver_name,
            "phone_tail": phone_tail,
        })
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()

@app.route('/api/dispatch/allocate-emergency', methods=['POST'])
@requires_authenticated_session()
def allocate_emergency():
    data = request.json
    container_num = data.get('container_number')
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("""
            INSERT INTO truck_allocations (container_number, driver_id, urgency_score, dispatch_status_code)
            VALUES (%s, NULL, 150, 'Dispatched') 
            ON DUPLICATE KEY UPDATE dispatch_status_code = 'Dispatched', urgency_score = 150, driver_id = NULL
        """, (container_num,))
        cursor.execute(
            "SELECT allocation_id FROM truck_allocations WHERE container_number = %s",
            (container_num,),
        )
        allocation = cursor.fetchone()
        from services.port_slots import book_slot_for_emergency, allocation_has_active_slot, get_slot_status
        slot_number = None
        if allocation:
            if allocation_has_active_slot(cursor, allocation['allocation_id']):
                slot_number = allocation_has_active_slot(cursor, allocation['allocation_id'])
            else:
                slot_number = book_slot_for_emergency(cursor, allocation['allocation_id'], container_num)
        log_event(cursor, container_num, 'DISPATCH_ENGINE', 'EMERGENCY_DISPATCH', {
            'cost': EMERGENCY_DRIVER_FLAT_RATE,
            'port_slot_number': slot_number,
        })
        port_slots = get_slot_status(cursor)
        mysql.connection.commit()
        return jsonify({
            "status": "success",
            "slot_number": slot_number,
            "port_slots": port_slots,
        })
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()

@app.route('/api/container/expunge', methods=['POST'])
@requires_authenticated_session()
def expunge_container():
    data = request.json
    container_num = data.get('container_number')
    pod_note = data.get('pod_note', '')
    pod_signature = data.get('pod_signature', '')
    cursor = mysql.connection.cursor()
    try:
        cursor.execute(
            "SELECT allocation_id, driver_id FROM truck_allocations WHERE container_number = %s",
            (container_num,),
        )
        allocation = cursor.fetchone()
        if allocation and allocation['driver_id']:
            cursor.execute("UPDATE drivers SET status_code = 'Available' WHERE driver_id = %s", (allocation['driver_id'],))
        if allocation:
            from services.port_slots import release_slots_for_allocation
            release_slots_for_allocation(cursor, allocation['allocation_id'])
        log_event(cursor, container_num, 'POD_SYSTEM', 'DELIVERY_COMPLETED', {
            'pod_note': pod_note,
            'pod_signature': pod_signature[:500] if pod_signature else '',
            'completed_by': g.current_user_username,
        })
        cursor.execute("DELETE FROM truck_allocations WHERE container_number = %s", (container_num,))
        cursor.execute("DELETE FROM containers WHERE container_number = %s", (container_num,))
        
        mysql.connection.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        cursor.close()

# -------------------------------------------------------------
# FLEET LOGISTICS DIRECTORY PANEL ROUTES (CRUD)
# -------------------------------------------------------------
@app.route('/drivers', methods=['GET'])
@requires_authenticated_session()
def drivers_dashboard():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM v_drivers_live ORDER BY current_status, driver_name")
    drivers = cursor.fetchall()
    schedule_map = fetch_driver_schedules(cursor)
    cursor.close()
    drivers = enrich_drivers_with_schedules(drivers, schedule_map)
    return render_template('drivers.html', drivers=drivers, username=g.current_user_username, role=g.current_user_role)

@app.route('/fleet', methods=['GET'])
@requires_role('Fleet Manager')
def fleet_dashboard():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM v_drivers_live ORDER BY driver_name")
    drivers = cursor.fetchall()
    schedule_map = fetch_driver_schedules(cursor)
    cursor.close()
    drivers = enrich_drivers_with_schedules(drivers, schedule_map)
    return render_template('fleet.html', drivers=drivers, schedule_map=schedule_map,
                           day_labels=DAY_LABELS, username=g.current_user_username, role=g.current_user_role)

@app.route('/fleet/add', methods=['POST'])
@requires_role('Fleet Manager')
def add_driver():
    name = request.form['driver_name']
    phone = request.form['phone_number']
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("INSERT INTO drivers (driver_name, phone_number, status_code) VALUES (%s, %s, 'Available')", (name, phone))
        driver_id = cursor.lastrowid
        upsert_driver_location(cursor, driver_id, MAP_DEPOT['lat'], MAP_DEPOT['lng'])
        mysql.connection.commit()
        flash(f"Driver {name} integrated successfully into asset hub database.", "success")
    except Exception:
        mysql.connection.rollback()
        flash("Failed to append driver asset parameters.", "danger")
    finally:
        cursor.close()
    return redirect(url_for('fleet_dashboard'))

@app.route('/fleet/update/<int:driver_id>', methods=['POST'])
@requires_role('Fleet Manager')
def update_driver(driver_id):
    phone = request.form['phone_number']
    status = request.form['current_status']
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("UPDATE drivers SET phone_number = %s, status_code = %s WHERE driver_id = %s", (phone, status, driver_id))
        mysql.connection.commit()
        flash("Driver parameters amended successfully.", "success")
    except Exception:
        mysql.connection.rollback()
        flash("Update assertion failed.", "danger")
    finally:
        cursor.close()
    return redirect(url_for('fleet_dashboard'))

@app.route('/fleet/delete/<int:driver_id>', methods=['POST'])
@requires_role('Fleet Manager')
def remove_driver(driver_id):
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("DELETE FROM drivers WHERE driver_id = %s", (driver_id,))
        mysql.connection.commit()
        flash("Logistics operator asset decommissioned.", "info")
    except Exception:
        mysql.connection.rollback()
        flash("Rejection: Active route constraints linked to this asset ID target exist.", "danger")
    finally:
        cursor.close()
    return redirect(url_for('fleet_dashboard'))

@app.route('/fleet/schedule/<int:driver_id>', methods=['POST'])
@requires_role('Fleet Manager')
def update_driver_schedule(driver_id):
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("DELETE FROM driver_schedules WHERE driver_id = %s", (driver_id,))
        for day in range(7):
            if request.form.get(f'day_{day}_enabled') == 'on':
                start = request.form.get(f'day_{day}_start', '08:00')
                end = request.form.get(f'day_{day}_end', '17:00')
                cursor.execute(
                    "INSERT INTO driver_schedules (driver_id, day_of_week, shift_start, shift_end) VALUES (%s, %s, %s, %s)",
                    (driver_id, day, start, end),
                )
        mysql.connection.commit()
        flash("Driver work schedule updated.", "success")
    except Exception:
        mysql.connection.rollback()
        flash("Failed to update driver schedule.", "danger")
    finally:
        cursor.close()
    return redirect(url_for('fleet_dashboard'))

# -------------------------------------------------------------
# LIVE TELEMETRY SIMULATION (GPS + AIS)
# -------------------------------------------------------------
def _simulation_is_stale(cursor):
    cursor.execute("SELECT MAX(recorded_at) AS last_tick FROM driver_locations")
    row = cursor.fetchone()
    last_tick = row['last_tick'] if row else None
    if not last_tick:
        return True
    return (datetime.datetime.now() - last_tick).total_seconds() > Config.SIM_TICK_STALE_SECONDS

def _maybe_advance_simulation(force=False):
    cursor = mysql.connection.cursor()
    try:
        if force or _simulation_is_stale(cursor):
            from simulation.engine import run_simulation_tick
            return run_simulation_tick(mysql.connection, simulation_mode=Config.SIMULATION_MODE)
    finally:
        cursor.close()
    return None

@app.route('/tracking')
@requires_authenticated_session()
def live_tracking():
    return render_template('tracking.html', username=g.current_user_username, role=g.current_user_role,
                           map_port=MAP_PORT, map_depot=MAP_DEPOT, map_warehouse=MAP_WAREHOUSE,
                           simulation_mode=Config.SIMULATION_MODE)

@app.route('/api/telemetry/live')
@requires_authenticated_session()
def telemetry_live():
    tick_result = _maybe_advance_simulation()
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT driver_id, driver_name, current_status, latitude, longitude,
               heading, speed_kph, DATE_FORMAT(last_gps_update, '%Y-%m-%dT%H:%i:%s') AS last_gps_update
        FROM v_drivers_live
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    drivers = cursor.fetchall()
    cursor.execute("""
        SELECT voyage_id AS vessel_id, vessel_name, voyage_number, latitude, longitude, heading,
               speed_knots, tracking_status,
               DATE_FORMAT(eta_datetime, '%Y-%m-%dT%H:%i:%s') AS eta_iso
        FROM v_vessels_live
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    vessels = cursor.fetchall()
    cursor.close()
    return jsonify({
        'drivers': drivers,
        'vessels': vessels,
        'port': MAP_PORT,
        'depot': MAP_DEPOT,
        'warehouse': MAP_WAREHOUSE,
        'simulation_tick': tick_result,
        'simulation_mode': Config.SIMULATION_MODE,
        'refreshed_at': datetime.datetime.now().isoformat(),
    })

@app.route('/api/simulation/tick', methods=['POST'])
@requires_role('Fleet Manager')
def simulation_tick_manual():
    result = _maybe_advance_simulation(force=True)
    return jsonify({'status': 'ok', 'result': result})

# -------------------------------------------------------------
# EVENT REPLAY
# -------------------------------------------------------------
@app.route('/replay')
@requires_authenticated_session()
def event_replay():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT DISTINCT container_number FROM events WHERE container_number IS NOT NULL ORDER BY container_number")
    container_ids = [r['container_number'] for r in cursor.fetchall()]
    cursor.close()
    return render_template('replay.html', container_ids=container_ids,
                           username=g.current_user_username, role=g.current_user_role)

@app.route('/api/replay/events')
@requires_authenticated_session()
def replay_events_api():
    container_filter = request.args.get('container')
    cursor = mysql.connection.cursor()
    if container_filter:
        cursor.execute("""
            SELECT event_id, container_number, source_api, event_type, event_timestamp, raw_payload
            FROM events WHERE container_number = %s ORDER BY event_timestamp ASC, event_id ASC
        """, (container_filter,))
    else:
        cursor.execute("""
            SELECT event_id, container_number, source_api, event_type, event_timestamp, raw_payload
            FROM events ORDER BY event_timestamp DESC, event_id DESC LIMIT 100
        """)
    events = cursor.fetchall()
    for ev in events:
        ts = ev.get('event_timestamp')
        if ts and hasattr(ts, 'isoformat'):
            ev['event_timestamp'] = ts.isoformat()
    cursor.close()
    return jsonify({'events': events})

# -------------------------------------------------------------
# DRIVER MOBILE PWA
# -------------------------------------------------------------
@app.route('/driver')
def driver_login_page():
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT driver_id, driver_name, phone_number, current_status FROM v_drivers_live ORDER BY driver_name")
    drivers = cursor.fetchall()
    cursor.close()
    return render_template('driver_login.html', drivers=drivers)

@app.route('/driver/login', methods=['POST'])
def driver_login():
    driver_id = request.form.get('driver_id')
    phone_tail = request.form.get('phone_tail', '').strip()
    cursor = mysql.connection.cursor()
    cursor.execute("SELECT * FROM v_drivers_live WHERE driver_id = %s", (driver_id,))
    driver = cursor.fetchone()
    cursor.close()
    if not driver or not driver['phone_number'].replace(' ', '').endswith(phone_tail):
        flash('Invalid driver or phone verification.', 'danger')
        return redirect(url_for('driver_login_page'))
    response = make_response(redirect(url_for('driver_portal')))
    response.set_cookie('driver_id', str(driver_id), httponly=True, samesite='Lax', max_age=86400 * 7)
    return response

@app.route('/driver/portal')
def driver_portal():
    driver = get_driver_from_cookie()
    if not driver:
        return redirect(url_for('driver_login_page'))
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT t.*, c.lfd_datetime, c.container_number, v.vessel_name,
               t.dispatch_status_code AS dispatch_status,
               (t.dispatch_status_code = 'At Warehouse') AS at_warehouse,
               psb.slot_number AS port_slot_number, psb.booked_at AS port_slot_booked_at,
               t.picked_up_at
        FROM truck_allocations t
        JOIN containers c ON c.container_number = t.container_number
        JOIN voyages vy ON vy.voyage_id = c.voyage_id
        JOIN vessels v ON v.vessel_id = vy.vessel_id
        LEFT JOIN port_slot_bookings psb ON psb.allocation_id = t.allocation_id AND psb.released_at IS NULL
        WHERE t.driver_id = %s AND t.dispatch_status_code IN ('Dispatched', 'At Warehouse')
        ORDER BY t.allocated_at DESC LIMIT 1
    """, (driver['driver_id'],))
    active_job = cursor.fetchone()
    from services.port_slots import get_slot_status
    port_slots = get_slot_status(cursor)
    cursor.close()
    return render_template('driver_portal.html', driver=driver, active_job=active_job,
                           port_slots=port_slots, map_warehouse=MAP_WAREHOUSE,
                           map_port=MAP_PORT, simulation_mode=Config.SIMULATION_MODE)

@app.route('/driver/logout')
def driver_logout():
    response = make_response(redirect(url_for('driver_login_page')))
    response.delete_cookie('driver_id', path='/')
    return response

@app.route('/api/driver/accept', methods=['POST'])
def driver_accept_job():
    driver = get_driver_from_cookie()
    if not driver:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401
    data = request.json or {}
    container_num = data.get('container_number')
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("""
            UPDATE truck_allocations SET accepted_at = NOW()
            WHERE container_number = %s AND driver_id = %s AND accepted_at IS NULL
        """, (container_num, driver['driver_id']))
        if cursor.rowcount == 0:
            return jsonify({'status': 'error', 'message': 'No pending job to accept'}), 404
        cursor.execute(
            "UPDATE drivers SET status_code = 'On Delivery' WHERE driver_id = %s",
            (driver['driver_id'],),
        )
        log_event(cursor, container_num, 'DRIVER_APP', 'JOB_ACCEPTED', {'driver_id': driver['driver_id']})
        mysql.connection.commit()
        return jsonify({'status': 'success'})
    finally:
        cursor.close()

@app.route('/api/port/slots')
@requires_authenticated_session()
def port_slots_api():
    cursor = mysql.connection.cursor()
    from services.port_slots import get_slot_status
    status = get_slot_status(cursor)
    cursor.close()
    return jsonify(status)

@app.route('/api/driver/port-slot/check', methods=['GET'])
def driver_check_port_slot():
    driver = get_driver_from_cookie()
    if not driver:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401
    cursor = mysql.connection.cursor()
    from services.port_slots import get_slot_status, allocation_has_active_slot
    slot_status = get_slot_status(cursor)
    cursor.execute("""
        SELECT t.allocation_id, t.accepted_at, t.container_number
        FROM truck_allocations t
        WHERE t.driver_id = %s AND t.dispatch_status_code = 'Dispatched'
        ORDER BY t.allocated_at DESC LIMIT 1
    """, (driver['driver_id'],))
    job = cursor.fetchone()
    if not job:
        cursor.close()
        return jsonify({'status': 'error', 'message': 'No active dispatch'}), 404
    if not job['accepted_at']:
        cursor.close()
        return jsonify({
            'status': 'pending_accept',
            'message': 'Accept the job before requesting a port slot.',
            **slot_status,
        })
    booked_slot = allocation_has_active_slot(cursor, job['allocation_id'])
    cursor.close()
    return jsonify({
        'status': 'booked' if booked_slot else ('available' if slot_status['available'] > 0 else 'full'),
        'slot_number': booked_slot,
        'can_book': not booked_slot and slot_status['available'] > 0,
        **slot_status,
    })

@app.route('/api/driver/port-slot/book', methods=['POST'])
def driver_book_port_slot():
    driver = get_driver_from_cookie()
    if not driver:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("""
            SELECT t.allocation_id, t.container_number, t.accepted_at
            FROM truck_allocations t
            WHERE t.driver_id = %s AND t.dispatch_status_code = 'Dispatched'
            ORDER BY t.allocated_at DESC LIMIT 1
        """, (driver['driver_id'],))
        job = cursor.fetchone()
        if not job:
            return jsonify({'status': 'error', 'message': 'No active dispatch'}), 404
        if not job['accepted_at']:
            return jsonify({'status': 'error', 'message': 'Accept the job before booking a port slot.'}), 409

        from services.port_slots import book_slot, get_slot_status
        slot_number = book_slot(
            cursor, job['allocation_id'], driver['driver_id'], job['container_number'],
        )
        if slot_number is None:
            mysql.connection.rollback()
            slot_status = get_slot_status(cursor)
            return jsonify({
                'status': 'full',
                'message': 'All prime mover slots are occupied. Retry when a slot frees up.',
                **slot_status,
            }), 409

        mysql.connection.commit()
        slot_status = get_slot_status(cursor)
        return jsonify({
            'status': 'success',
            'slot_number': slot_number,
            'message': f'Prime mover slot {slot_number} reserved at Tuas Port.',
            **slot_status,
        })
    except Exception as exc:
        mysql.connection.rollback()
        return jsonify({'status': 'error', 'message': str(exc)}), 500
    finally:
        cursor.close()

@app.route('/api/driver/location', methods=['POST'])
def driver_update_location():
    driver = get_driver_from_cookie()
    if not driver:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401
    data = request.json or {}
    lat, lng = data.get('latitude'), data.get('longitude')
    if lat is None or lng is None:
        return jsonify({'status': 'error', 'message': 'Missing coordinates'}), 400
    cursor = mysql.connection.cursor()
    try:
        upsert_driver_location(cursor, driver['driver_id'], lat, lng,
                               data.get('heading', 0), data.get('speed_kph', 0))
        from services.geofence import process_port_pickups, process_warehouse_arrivals
        port_pickups = process_port_pickups(cursor, Config.GEOFENCE_RADIUS_KM)
        warehouse_arrivals = process_warehouse_arrivals(cursor)
        mysql.connection.commit()
        return jsonify({
            'status': 'success',
            'port_pickups': port_pickups,
            'warehouse_arrivals': warehouse_arrivals,
            'slot_released': port_pickups > 0,
        })
    finally:
        cursor.close()

@app.route('/api/containers/etas')
@requires_authenticated_session()
def container_etas_api():
    _maybe_advance_simulation()
    cursor = mysql.connection.cursor()
    cursor.execute("""
        SELECT c.container_number, IFNULL(t.dispatch_status_code, 'Pending') AS dispatch_status,
               t.accepted_at, t.picked_up_at, t.driver_id,
               d.driver_name,
               dl.latitude AS driver_lat, dl.longitude AS driver_lng, dl.speed_kph,
               (t.dispatch_status_code = 'At Warehouse') AS at_warehouse,
               psb.slot_number AS port_slot_number
        FROM containers c
        LEFT JOIN truck_allocations t ON t.container_number = c.container_number
        LEFT JOIN drivers d ON t.driver_id = d.driver_id
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
        LEFT JOIN port_slot_bookings psb ON psb.allocation_id = t.allocation_id AND psb.released_at IS NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    result = {}
    for row in rows:
        eta = None
        accepted = bool(row['accepted_at'])
        at_warehouse = bool(row['at_warehouse'])
        dispatch_status = row['dispatch_status']
        slot_booked = row['port_slot_number'] is not None
        picked_up = bool(row['picked_up_at'])
        if row['driver_lat'] and dispatch_status == 'Dispatched' and accepted and not at_warehouse:
            if picked_up:
                target_lat, target_lng = MAP_WAREHOUSE['lat'], MAP_WAREHOUSE['lng']
            elif slot_booked:
                target_lat, target_lng = MAP_PORT['lat'], MAP_PORT['lng']
            else:
                target_lat = target_lng = None
            if target_lat is not None:
                eta = int(eta_minutes_from_gps(
                    row['driver_lat'], row['driver_lng'], row['speed_kph'] or 30,
                    target_lat, target_lng,
                ))
        if at_warehouse:
            alert_status = 'AT WAREHOUSE'
        elif dispatch_status == 'Dispatched' and row['driver_id'] and not accepted:
            alert_status = 'AWAITING'
        elif dispatch_status == 'Dispatched' and row['driver_id'] and accepted and not slot_booked and not picked_up:
            alert_status = 'SLOT WAIT'
        elif dispatch_status == 'Dispatched' and row['driver_id'] and accepted and not picked_up:
            alert_status = 'TO PORT'
        elif dispatch_status == 'Dispatched' and row['driver_id'] and accepted:
            alert_status = 'TO WAREHOUSE'
        elif dispatch_status == 'Dispatched' and not row['driver_id']:
            alert_status = 'EMERGENCY'
        else:
            alert_status = None
        emergency_hire = dispatch_status == 'Dispatched' and not row['driver_id']
        result[row['container_number']] = {
            'eta_minutes': eta,
            'at_warehouse': at_warehouse,
            'dispatch_status': dispatch_status,
            'accepted': accepted,
            'slot_booked': slot_booked,
            'picked_up': picked_up,
            'port_slot_number': row['port_slot_number'],
            'driver_name': row['driver_name'],
            'alert_status': alert_status,
            'can_expunge': at_warehouse or emergency_hire,
        }
    return jsonify(result)

# -------------------------------------------------------------
# STRATEGIC EXECUTION LAYER OPERATIONS
# -------------------------------------------------------------
@app.route('/analytics')
@requires_role('Fleet Manager')
def contract_negotiation_insights():
    cursor = mysql.connection.cursor()
    
    # REFACTORED QUERY: Dynamically calculates actual accrued delay risk exposure 
    # for all containers linked to a vessel, active or historical.
    query = f"""
        SELECT v.vessel_name, 
               COUNT(c.container_number) AS total_dispatches,
               COUNT(CASE WHEN t.dispatch_status_code = 'Dispatched' AND t.driver_id IS NULL THEN 1 END) AS emergency_hires,
               SUM(CASE WHEN t.dispatch_status_code = 'Dispatched' AND t.driver_id IS NULL THEN {EMERGENCY_DRIVER_FLAT_RATE} ELSE 0.00 END) AS total_extra_costs,
               ROUND(SUM(
                   CASE 
                       WHEN TIMESTAMPDIFF(SECOND, c.lfd_datetime, NOW()) > 0 
                       THEN (TIMESTAMPDIFF(SECOND, c.lfd_datetime, NOW()) / 3600.0) * {STORE_RENT_HOURLY_RATE}
                       ELSE 0.00 
                   END
               ), 2) AS accumulated_store_rent,
               ROUND(SUM(
                   CASE 
                       WHEN TIMESTAMPDIFF(SECOND, DATE_ADD(c.discharge_datetime, INTERVAL {GRACE_PERIOD_HOURS} HOUR), NOW()) > 0 
                       THEN (TIMESTAMPDIFF(SECOND, DATE_ADD(c.discharge_datetime, INTERVAL {GRACE_PERIOD_HOURS} HOUR), NOW()) / 3600.0) * {DEMURRAGE_HOURLY_RATE}
                       ELSE 0.00 
                   END
               ), 2) AS accumulated_demurrage
        FROM vessels v
        JOIN voyages vy ON vy.vessel_id = v.vessel_id
        LEFT JOIN containers c ON c.voyage_id = vy.voyage_id
        LEFT JOIN truck_allocations t ON c.container_number = t.container_number
        GROUP BY v.vessel_name 
        ORDER BY total_extra_costs DESC, accumulated_store_rent DESC, accumulated_demurrage DESC
    """
    cursor.execute(query)
    carrier_benchmarks = cursor.fetchall()
    
    total_leakage = sum(float(item['total_extra_costs'] or 0.0) for item in carrier_benchmarks)
    total_store_rent = sum(float(item['accumulated_store_rent'] or 0.0) for item in carrier_benchmarks)
    total_demurrage = sum(float(item['accumulated_demurrage'] or 0.0) for item in carrier_benchmarks)
    total_fines = round(total_store_rent + total_demurrage, 2)
    savings = compute_fleet_savings(cursor)
    
    cursor.close()
    
    return render_template('analytics.html', 
                           benchmarks=carrier_benchmarks, 
                           total_leakage=total_leakage, 
                           total_store_rent=total_store_rent,
                           total_demurrage=total_demurrage,
                           total_fines=total_fines,
                           savings=savings,
                           grace_period_hours=GRACE_PERIOD_HOURS,
                           username=g.current_user_username, 
                           role=g.current_user_role)
if __name__ == '__main__':
    app.run(debug=True, port=5000)