import pymysql
pymysql.install_as_MySQLdb()

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
from services.port_slots import EMERGENCY_CONTRACTOR_NAME
from services.shifts import driver_is_on_shift as shift_driver_is_on_shift

from services.users import get_role_id_by_name, create_user, get_user_by_username
from services.leave import (
    fetch_all_leaves, apply_leave, cancel_leave,
    fetch_eligible_planners, fetch_eligible_dispatchers, fetch_eligible_drivers
)
from services.drivers import (
    is_fleet_driver, fetch_drivers_for_roster, fetch_all_drivers_live,
    fetch_live_drivers_telemetry, fetch_driver_by_id, add_driver as db_add_driver,
    update_driver as db_update_driver, remove_driver as db_remove_driver, fetch_driver_schedules,
    update_driver_schedule as db_update_driver_schedule, enrich_drivers_with_schedules,
    get_fleet_status, get_next_shift_hint, pick_nearest_dispatchable_driver, update_driver_status
)
from services.containers import (
    fetch_containers_dashboard, fetch_inbound_vessels, fetch_fleet_savings,
    fetch_vessel_telemetry_live, fetch_container_by_number,
    record_dispatch_assignment, find_pending_assignment, find_accepted_assignment,
    allocate_vessel_container, allocate_emergency_container, fetch_allocation_by_container,
    insert_delivery_completion, update_assignment_completed, delete_allocation,
    delete_container, log_event, fetch_distinct_event_containers, fetch_events_for_replay,
    fetch_driver_active_job, fetch_active_job_for_slot, insert_job_rejection,
    update_assignment_rejected, reset_allocation_after_rejection, accept_job_allocation,
    update_assignment_accepted, fetch_all_containers_etas, fetch_carrier_benchmarks
)

app = Flask(__name__)
app.config.from_object(Config)
mysql = MySQL(app)

# Financial and DSS Metric Parameters
EMERGENCY_DRIVER_FLAT_RATE = 250.00
STORE_RENT_HOURLY_RATE = 12.00       # Port terminal storage after LFD breach
DEMURRAGE_HOURLY_RATE = 15.00        # Carrier demurrage after post-discharge grace period
GRACE_PERIOD_HOURS = 48              # Free time after vessel discharge before demurrage accrues
REJECTION_COOLDOWN_HOURS = 2         # Auto-dispatch blackout after a driver declines a job
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
    driver = fetch_driver_by_id(cursor, driver_id)
    cursor.close()
    return driver

def auto_init_db():
    try:
        import MySQLdb
        try:
            conn = MySQLdb.connect(
                host=Config.MYSQL_HOST,
                user=Config.MYSQL_USER,
                password=Config.MYSQL_PASSWORD,
                charset='utf8mb4'
            )
        except Exception as e:
            print(f"[*] Database auto-init check: MySQL server at {Config.MYSQL_HOST} not reachable: {e}")
            return
            
        cursor = conn.cursor()
        try:
            cursor.execute(f"SHOW DATABASES LIKE '{Config.MYSQL_DB}'")
            db_exists = cursor.fetchone()
            
            should_setup = False
            if not db_exists:
                print(f"[*] Database auto-init check: Database '{Config.MYSQL_DB}' does not exist. Initializing...")
                should_setup = True
            else:
                conn.select_db(Config.MYSQL_DB)
                cursor.execute("SHOW TABLES LIKE 'users'")
                has_users_table = cursor.fetchone()
                if not has_users_table:
                    print("[*] Database auto-init check: Tables not found. Initializing...")
                    should_setup = True
                else:
                    cursor.execute("SELECT COUNT(*) AS cnt FROM users")
                    cnt = cursor.fetchone()[0] if hasattr(cursor, 'fetchone') else 0
                    if cnt == 0:
                        print("[*] Database auto-init check: Database is empty. Initializing...")
                        should_setup = True
            
            if should_setup:
                from db.setup import run_setup
                run_setup(Config.MYSQL_HOST, Config.MYSQL_USER, Config.MYSQL_PASSWORD, Config.MYSQL_DB)
                print("[*] Database auto-init check: Initialization complete.")
            else:
                print("[*] Database auto-init check: Database is already initialized and seeded.")
        finally:
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"[*] Database auto-init check failed: {e}")

auto_init_db()

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
            role_id = get_role_id_by_name(cursor, role)
            create_user(cursor, username, email, hashed_password, role_id)
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
        user = get_user_by_username(cursor, username)
        cursor.close()
        
        if user and check_password_hash(user['password_hash'], password_provided):
            # Verify if user is currently registered as on leave today
            cursor = mysql.connection.cursor()
            cursor.execute(
                "SELECT 1 FROM leave_requests WHERE user_id = %s AND leave_date = CURDATE()",
                (user['user_id'],)
            )
            on_leave_today = cursor.fetchone() is not None
            cursor.close()
            
            if on_leave_today:
                flash(f"Access denied: {user['username']} is registered as on leave today.", "danger")
                return redirect(url_for('login'))
                
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
            
    cursor = mysql.connection.cursor()
    drivers = fetch_eligible_drivers(cursor)
    cursor.close()
    return render_template('login.html', drivers=drivers)

@app.route('/logout')
def logout():
    flash("Session terminated. Secure exit complete.", "info")
    response = make_response(redirect(url_for('login')))
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("driver_id", path="/")
    return response

# -------------------------------------------------------------
# STAFF LEAVE MANAGEMENT SYSTEM
# -------------------------------------------------------------
@app.route('/leave', methods=['GET'])
@requires_authenticated_session()
def leave_dashboard():
    cursor = mysql.connection.cursor()
    leaves = fetch_all_leaves(cursor)
    planners = fetch_eligible_planners(cursor)
    dispatchers = fetch_eligible_dispatchers(cursor)
    drivers = fetch_eligible_drivers(cursor)
    cursor.close()
    return render_template(
        'leave.html',
        leaves=leaves,
        planners=planners,
        dispatchers=dispatchers,
        drivers=drivers,
        username=g.current_user_username,
        role=g.current_user_role
    )

@app.route('/leave/apply', methods=['POST'])
@requires_authenticated_session()
def leave_apply():
    employee_type = request.form.get('employee_type')
    leave_date_str = request.form.get('leave_date')
    reason = request.form.get('reason', '')
    
    if employee_type == 'Planner':
        employee_id = request.form.get('planner_id')
    elif employee_type == 'Dispatcher':
        employee_id = request.form.get('dispatcher_id')
    elif employee_type == 'Driver':
        employee_id = request.form.get('driver_id')
    else:
        flash("Invalid employee category selected.", "danger")
        return redirect(url_for('leave_dashboard'))
        
    if not employee_id or not leave_date_str:
        flash("Missing employee selection or leave date.", "danger")
        return redirect(url_for('leave_dashboard'))
        
    cursor = mysql.connection.cursor()
    try:
        apply_leave(cursor, employee_type, int(employee_id), leave_date_str, reason)
        mysql.connection.commit()
        flash("Leave request successfully approved.", "success")
    except ValueError as exc:
        mysql.connection.rollback()
        flash(str(exc), "danger")
    except Exception as exc:
        mysql.connection.rollback()
        flash(f"Unexpected database error: {exc}", "danger")
    finally:
        cursor.close()
        
    return redirect(url_for('leave_dashboard'))

@app.route('/leave/cancel/<int:leave_id>', methods=['POST'])
@requires_authenticated_session()
def leave_cancel(leave_id):
    cursor = mysql.connection.cursor()
    try:
        cancel_leave(cursor, leave_id)
        mysql.connection.commit()
        flash("Leave request cancelled successfully.", "info")
    except Exception as exc:
        mysql.connection.rollback()
        flash(f"Failed to cancel leave: {exc}", "danger")
    finally:
        cursor.close()
    return redirect(url_for('leave_dashboard'))

# -------------------------------------------------------------
# LECTURER INTERACTIVE WALKTHROUGHS
# -------------------------------------------------------------
@app.route('/walkthroughs', methods=['GET'])
def walkthroughs_dashboard():
    token = request.cookies.get("access_token")
    username = None
    role = None
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], leeway=10)
            if payload.get("run_id") == SERVER_RUN_ID:
                username = payload.get("username")
                role = payload.get("role")
        except Exception:
            pass
    return render_template('walkthroughs.html', username=username, role=role)

def generate_auto_login_token(user_id, username, role):
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "run_id": SERVER_RUN_ID,
        "exp": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

@app.route('/api/walkthrough/setup/<int:flow_id>', methods=['POST'])
def walkthrough_setup(flow_id):
    from db.setup import run_setup
    cursor = mysql.connection.cursor()
    try:
        run_setup(Config.MYSQL_HOST, Config.MYSQL_USER, Config.MYSQL_PASSWORD, Config.MYSQL_DB)
        
        target_url = url_for('mis_dashboard')
        login_user = None
        login_driver_id = None
        
        if flow_id == 1:
            login_user = (2, 'dispatcher_user', 'Dispatcher')
            target_url = url_for('mis_dashboard')
            
        elif flow_id == 2:
            login_user = (2, 'dispatcher_user', 'Dispatcher')
            cursor.execute("""
                INSERT INTO truck_allocations (container_number, driver_id, urgency_score, dispatch_status_code)
                VALUES ('NYKU9012455', 1, 95, 'Dispatched')
            """)
            cursor.execute("SELECT allocation_id FROM truck_allocations WHERE container_number = 'NYKU9012455'")
            alloc = cursor.fetchone()
            cursor.execute("INSERT INTO dispatch_assignments (allocation_id, driver_id, outcome_code) VALUES (%s, 1, 'pending')", (alloc['allocation_id'],))
            cursor.execute("UPDATE drivers SET status_code = 'On Delivery' WHERE driver_id = 1")
            mysql.connection.commit()
            
            login_driver_id = 1
            target_url = url_for('driver_portal')
            
        elif flow_id == 3:
            login_user = (2, 'dispatcher_user', 'Dispatcher')
            cursor.execute("UPDATE drivers SET status_code = 'Offline' WHERE driver_name != 'Emergency Contractor'")
            mysql.connection.commit()
            target_url = url_for('mis_dashboard')
            
        elif flow_id == 4:
            login_user = (3, 'manager_user', 'Fleet Manager')
            target_url = url_for('leave_dashboard')
            
        elif flow_id == 5:
            login_user = (3, 'manager_user', 'Fleet Manager')
            tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
            cursor.execute("INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 1, %s, 'Vacation')", (tomorrow,))
            cursor.execute("INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 2, %s, 'Sick')", (tomorrow,))
            mysql.connection.commit()
            target_url = url_for('leave_dashboard')
            
        elif flow_id == 6:
            login_user = (2, 'dispatcher_user', 'Dispatcher')
            today = datetime.date.today().isoformat()
            cursor.execute("INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 2, %s, 'Vacation')", (today,))
            mysql.connection.commit()
            target_url = url_for('mis_dashboard')
            
        elif flow_id == 7:
            login_user = (3, 'manager_user', 'Fleet Manager')
            target_url = url_for('fleet_dashboard')
            
        elif flow_id == 8:
            login_user = (1, 'planner_user', 'Planner')
            target_url = url_for('mis_dashboard')
            
        elif flow_id == 9:
            login_user = (3, 'manager_user', 'Fleet Manager')
            target_url = url_for('event_replay')
            
        elif flow_id == 10:
            login_user = (3, 'manager_user', 'Fleet Manager')
            cursor.execute("UPDATE containers SET discharge_datetime = DATE_SUB(NOW(), INTERVAL 5 DAY), lfd_datetime = DATE_SUB(NOW(), INTERVAL 3 DAY) WHERE container_number = 'REDU0000001'")
            cursor.execute("INSERT INTO truck_allocations (container_number, driver_id, urgency_score, dispatch_status_code) VALUES ('REDU0000001', 1, 95, 'At Warehouse')")
            cursor.execute("SELECT allocation_id FROM truck_allocations WHERE container_number = 'REDU0000001'")
            alloc = cursor.fetchone()
            cursor.execute("INSERT INTO dispatch_assignments (allocation_id, driver_id, outcome_code, outcome_at) VALUES (%s, 1, 'completed', NOW())", (alloc['allocation_id'],))
            cursor.execute("SELECT assignment_id FROM dispatch_assignments WHERE allocation_id = %s", (alloc['allocation_id'],))
            assign = cursor.fetchone()
            cursor.execute("INSERT INTO delivery_completions (assignment_id, pod_note, pod_signature, confirmed_by_user_id) VALUES (%s, 'Delivered', 'data:image/png;base64,...', 3)", (assign['assignment_id'],))
            mysql.connection.commit()
            target_url = url_for('contract_negotiation_insights')
            
        response = make_response(jsonify({'status': 'success', 'redirect': target_url}))
        
        if login_user:
            uid, name, role = login_user
            token = generate_auto_login_token(uid, name, role)
            response.set_cookie("access_token", token, httponly=True, samesite="Lax")
            
        if login_driver_id:
            response.set_cookie("driver_id", str(login_driver_id), httponly=True, samesite="Lax", max_age=86400 * 7)
        else:
            response.delete_cookie("driver_id", path="/")
            
        return response
        
    except Exception as e:
        mysql.connection.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        cursor.close()

# -------------------------------------------------------------
# OPERATIONS MANAGEMENT ENDPOINTS (MIS DASHBOARD)
# -------------------------------------------------------------
@app.route('/')
@requires_authenticated_session()
def mis_dashboard():
    cursor = mysql.connection.cursor()
    containers = enrich_containers_with_eta(fetch_containers_dashboard(cursor))
    fleet = get_fleet_status(cursor)
    savings = fetch_fleet_savings(cursor, EMERGENCY_DRIVER_FLAT_RATE, STORE_RENT_HOURLY_RATE, DEMURRAGE_HOURLY_RATE, GRACE_PERIOD_HOURS) if g.current_user_role == 'Fleet Manager' else None
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
        
    container = fetch_container_by_number(cursor, container_num)
    
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
            cursor, MAP_PORT['lat'], MAP_PORT['lng'], MAP_DEPOT['lat'], MAP_DEPOT['lng']
        )
        if not driver_id:
            return jsonify({"status": "depleted", "message": "No on-shift drivers available. Use emergency dispatch."}), 409
        
        allocation = allocate_vessel_container(cursor, container_num, driver_id)
        if allocation:
            record_dispatch_assignment(cursor, allocation['allocation_id'], driver_id)

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
        allocation = fetch_allocation_by_container(cursor, container_num)
        assignment_id = None
        driver_id = allocation['driver_id'] if allocation else None
        if driver_id:
            update_driver_status(cursor, driver_id, 'Available')
        if allocation:
            assignment_id = find_accepted_assignment(
                cursor, allocation['allocation_id'], driver_id,
            )
            if assignment_id:
                insert_delivery_completion(cursor, assignment_id, pod_note, pod_signature, g.current_user_id)
                update_assignment_completed(cursor, assignment_id)
            from services.port_slots import release_slots_for_allocation
            release_slots_for_allocation(cursor, allocation['allocation_id'])
        log_event(cursor, container_num, 'POD_SYSTEM', 'DELIVERY_COMPLETED', {
            'pod_note': pod_note,
            'pod_signature': pod_signature[:500] if pod_signature else '',
            'completed_by': g.current_user_username,
            'assignment_id': assignment_id if allocation else None,
        })
        delete_allocation(cursor, container_num)
        delete_container(cursor, container_num)
        
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
    drivers = fetch_drivers_for_roster(cursor)
    schedule_map = fetch_driver_schedules(cursor)
    drivers = enrich_drivers_with_schedules(drivers, schedule_map, cursor)
    cursor.close()
    return render_template('drivers.html', drivers=drivers, username=g.current_user_username, role=g.current_user_role)

@app.route('/fleet', methods=['GET'])
@requires_role('Fleet Manager')
def fleet_dashboard():
    cursor = mysql.connection.cursor()
    drivers = fetch_all_drivers_live(cursor)
    schedule_map = fetch_driver_schedules(cursor)
    drivers = enrich_drivers_with_schedules(drivers, schedule_map, cursor)
    cursor.close()
    return render_template('fleet.html', drivers=drivers, schedule_map=schedule_map,
                           day_labels=DAY_LABELS, username=g.current_user_username, role=g.current_user_role)

@app.route('/fleet/add', methods=['POST'])
@requires_role('Fleet Manager')
def add_driver():
    name = request.form['driver_name']
    phone = request.form['phone_number']
    cursor = mysql.connection.cursor()
    try:
        db_add_driver(cursor, name, phone, MAP_DEPOT['lat'], MAP_DEPOT['lng'])
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
        db_update_driver(cursor, driver_id, phone, status)
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
        db_remove_driver(cursor, driver_id)
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
        schedule_days = []
        for day in range(7):
            is_enabled = request.form.get(f'day_{day}_enabled') == 'on'
            start = request.form.get(f'day_{day}_start', '08:00')
            end = request.form.get(f'day_{day}_end', '17:00')
            schedule_days.append((day, is_enabled, start, end))
            
        db_update_driver_schedule(cursor, driver_id, schedule_days)
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
    drivers = fetch_live_drivers_telemetry(cursor)
    vessels = fetch_vessel_telemetry_live(cursor)
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
    container_ids = fetch_distinct_event_containers(cursor)
    cursor.close()
    return render_template('replay.html', container_ids=container_ids,
                           username=g.current_user_username, role=g.current_user_role)

@app.route('/api/replay/events')
@requires_authenticated_session()
def replay_events_api():
    container_filter = request.args.get('container')
    cursor = mysql.connection.cursor()
    events = fetch_events_for_replay(cursor, container_filter)
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
    return redirect(url_for('login', tab='driver'))

@app.route('/driver/login', methods=['POST'])
def driver_login():
    driver_id = request.form.get('driver_id')
    phone_tail = request.form.get('phone_tail', '').strip()
    cursor = mysql.connection.cursor()
    driver = fetch_driver_by_id(cursor, driver_id)
    cursor.close()
    if (
        not driver
        or not is_fleet_driver(driver)
        or not driver['phone_number'].replace(' ', '').endswith(phone_tail)
    ):
        flash('Invalid driver or phone verification.', 'danger')
        return redirect(url_for('login', tab='driver'))
    response = make_response(redirect(url_for('driver_portal')))
    response.set_cookie('driver_id', str(driver_id), httponly=True, samesite='Lax', max_age=86400 * 7)
    return response

@app.route('/driver/portal')
def driver_portal():
    driver = get_driver_from_cookie()
    if not driver:
        return redirect(url_for('login', tab='driver'))
    cursor = mysql.connection.cursor()
    active_job = fetch_driver_active_job(cursor, driver['driver_id'])
    from services.port_slots import get_slot_status
    port_slots = get_slot_status(cursor)
    cursor.close()
    return render_template('driver_portal.html', driver=driver, active_job=active_job,
                           port_slots=port_slots, map_warehouse=MAP_WAREHOUSE,
                           map_port=MAP_PORT, simulation_mode=Config.SIMULATION_MODE)

@app.route('/driver/logout')
def driver_logout():
    response = make_response(redirect(url_for('login', tab='driver')))
    response.delete_cookie('driver_id', path='/')
    return response

@app.route('/api/driver/reject', methods=['POST'])
def driver_reject_job():
    driver = get_driver_from_cookie()
    if not driver:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401
    data = request.json or {}
    container_num = data.get('container_number')
    reason = (data.get('reason') or '').strip()
    if not container_num:
        return jsonify({'status': 'error', 'message': 'Container number is required'}), 400
    if len(reason) < 5:
        return jsonify({'status': 'error', 'message': 'Please provide a rejection reason (at least 5 characters).'}), 400
    cursor = mysql.connection.cursor()
    try:
        cursor.execute("""
            SELECT allocation_id, driver_id, accepted_at
            FROM truck_allocations
            WHERE container_number = %s AND driver_id = %s AND dispatch_status_code = 'Dispatched'
        """, (container_num, driver['driver_id']))
        allocation = cursor.fetchone()
        if not allocation:
            return jsonify({'status': 'error', 'message': 'No active assignment to reject'}), 404
        if allocation['accepted_at']:
            return jsonify({'status': 'error', 'message': 'Cannot reject a job that has already been accepted'}), 409
        assignment_id = find_pending_assignment(
            cursor, allocation['allocation_id'], driver['driver_id'],
        )
        if not assignment_id:
            return jsonify({'status': 'error', 'message': 'No pending assignment record found'}), 404
        insert_job_rejection(cursor, assignment_id, reason)
        update_assignment_rejected(cursor, assignment_id)
        reset_allocation_after_rejection(cursor, allocation['allocation_id'])
        update_driver_status(cursor, driver['driver_id'], 'Available')
        log_event(cursor, container_num, 'DRIVER_APP', 'JOB_REJECTED', {
            'assignment_id': assignment_id,
            'reason': reason[:500],
        })
        mysql.connection.commit()
        return jsonify({'status': 'success', 'message': 'Job declined. Dispatcher will be notified to re-allocate.'})
    except Exception as exc:
        mysql.connection.rollback()
        return jsonify({'status': 'error', 'message': str(exc)}), 500
    finally:
        cursor.close()

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
            SELECT allocation_id FROM truck_allocations
            WHERE container_number = %s AND driver_id = %s AND accepted_at IS NULL
        """, (container_num, driver['driver_id']))
        allocation = cursor.fetchone()
        if not allocation:
            return jsonify({'status': 'error', 'message': 'No pending job to accept'}), 404
        assignment_id = find_pending_assignment(
            cursor, allocation['allocation_id'], driver['driver_id'],
        )
        if not assignment_id:
            return jsonify({'status': 'error', 'message': 'No pending assignment record found'}), 404
        accept_job_allocation(cursor, allocation['allocation_id'])
        update_assignment_accepted(cursor, assignment_id)
        update_driver_status(cursor, driver['driver_id'], 'On Delivery')
        log_event(cursor, container_num, 'DRIVER_APP', 'JOB_ACCEPTED', {'assignment_id': assignment_id})
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
    job = fetch_active_job_for_slot(cursor, driver['driver_id'])
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
        job = fetch_active_job_for_slot(cursor, driver['driver_id'])
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

def lfd_traffic_light(lfd_datetime, as_of=None):
    as_of = as_of or datetime.datetime.now()
    hours = (lfd_datetime - as_of).total_seconds() / 3600.0
    if hours <= 12:
        return 'RED'
    if hours <= 24:
        return 'YELLOW'
    return 'GREEN'

@app.route('/api/containers/etas')
@requires_authenticated_session()
def container_etas_api():
    _maybe_advance_simulation()
    cursor = mysql.connection.cursor()
    rows = fetch_all_containers_etas(cursor)
    fleet = get_fleet_status(cursor)
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
        elif dispatch_status == 'Pending':
            alert_status = lfd_traffic_light(row['lfd_datetime'])
        else:
            alert_status = None
        emergency_hire = dispatch_status == 'Dispatched' and not row['driver_id']
        rejection_hint = None
        if dispatch_status == 'Pending' and row['rejected_by']:
            rejection_hint = f"{row['rejected_by']} declined"
            if row['rejection_reason']:
                rejection_hint += f" ({row['rejection_reason']})"
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
            'rejection_hint': rejection_hint,
        }
    result['_fleet'] = fleet
    result['_refreshed_at'] = datetime.datetime.now().isoformat()
    return jsonify(result)

# -------------------------------------------------------------
# STRATEGIC EXECUTION LAYER OPERATIONS
# -------------------------------------------------------------
@app.route('/analytics/export')
@requires_role('Fleet Manager')
def analytics_export():
    from services.report_export import build_analytics_csv_zip
    from flask import send_file
    cursor = mysql.connection.cursor()
    try:
        buffer = build_analytics_csv_zip(cursor)
    finally:
        cursor.close()
    
    # Save a fail-safe copy to the workspace root directory
    try:
        workspace_zip = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fleet_analytics.zip')
        with open(workspace_zip, 'wb') as f:
            f.write(buffer.getvalue())
    except Exception as e:
        print(f"[*] Fail-safe local ZIP write failed: {e}")

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M')
    filename = f'fleet_analytics_{stamp}.zip'
    return send_file(
        buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename
    )

@app.route('/analytics')
@requires_role('Fleet Manager')
def contract_negotiation_insights():
    cursor = mysql.connection.cursor()
    carrier_benchmarks = fetch_carrier_benchmarks(
        cursor, EMERGENCY_DRIVER_FLAT_RATE, STORE_RENT_HOURLY_RATE, DEMURRAGE_HOURLY_RATE, GRACE_PERIOD_HOURS
    )
    
    total_leakage = sum(float(item['total_extra_costs'] or 0.0) for item in carrier_benchmarks)
    total_store_rent = sum(float(item['accumulated_store_rent'] or 0.0) for item in carrier_benchmarks)
    total_demurrage = sum(float(item['accumulated_demurrage'] or 0.0) for item in carrier_benchmarks)
    total_fines = round(total_store_rent + total_demurrage, 2)
    savings = fetch_fleet_savings(
        cursor, EMERGENCY_DRIVER_FLAT_RATE, STORE_RENT_HOURLY_RATE, DEMURRAGE_HOURLY_RATE, GRACE_PERIOD_HOURS
    )
    
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
    app.run(debug=True, port=5000, use_reloader=False)