"""Container, vessel tracking, and dispatch allocation database operations."""
import datetime
import json

def fetch_containers_dashboard(cursor):
    query = """
        SELECT c.container_number, v.vessel_name,
               DATE_FORMAT(c.lfd_datetime, '%Y-%m-%dT%H:%i:%s') as lfd_iso_string,
               c.lfd_datetime,
               IFNULL(t.dispatch_status_code, 'Pending') as current_dispatch_status,
               t.accepted_at, t.picked_up_at, da.driver_id AS assigned_driver_id,
               (psb.booking_id IS NOT NULL) AS port_slot_booked,
               dl.latitude AS driver_lat, dl.longitude AS driver_lng, dl.speed_kph, d.driver_name,
               CASE 
                   WHEN t.dispatch_status_code = 'At Warehouse' THEN 'AT WAREHOUSE'
                   WHEN t.dispatch_status_code = 'At Port' THEN 'AT PORT'
                   WHEN t.dispatch_status_code = 'Dispatched' AND da.driver_id IS NULL THEN 'EMERGENCY'
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
        LEFT JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        LEFT JOIN drivers d ON da.driver_id = d.driver_id
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
        LEFT JOIN port_slot_bookings psb ON psb.allocation_id = t.allocation_id AND psb.released_at IS NULL
        ORDER BY c.lfd_datetime ASC
    """
    cursor.execute(query)
    return cursor.fetchall()

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

def fetch_fleet_savings(cursor, emergency_driver_rate, store_rent_rate, demurrage_rate, grace_period_hours):
    cursor.execute("""
        SELECT c.lfd_datetime, c.discharge_datetime, t.allocated_at
        FROM containers c
        JOIN truck_allocations t ON c.container_number = t.container_number
        LEFT JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        WHERE t.dispatch_status_code = 'Dispatched' AND da.driver_id IS NOT NULL
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
        
        # calculate fees averted
        hours_past_lfd = max(0.0, (counterfactual_at - lfd).total_seconds() / 3600.0)
        store_rent = round(hours_past_lfd * store_rent_rate, 2)

        demurrage_start = row['discharge_datetime'] + datetime.timedelta(hours=grace_period_hours)
        hours_past_demurrage = max(0.0, (counterfactual_at - demurrage_start).total_seconds() / 3600.0)
        demurrage = round(hours_past_demurrage * demurrage_rate, 2)
        
        fees_averted += (store_rent + demurrage)

    emergency_savings = on_time_count * emergency_driver_rate
    return {
        'on_time_dispatches': on_time_count,
        'fees_averted': round(fees_averted, 2),
        'emergency_savings': round(emergency_savings, 2),
        'total_savings': round(fees_averted + emergency_savings, 2),
    }

def fetch_vessel_telemetry_live(cursor):
    cursor.execute("""
        SELECT voyage_id AS vessel_id, vessel_name, voyage_number, latitude, longitude, heading,
               speed_knots, tracking_status,
               DATE_FORMAT(eta_datetime, '%%Y-%%m-%%dT%%H:%%i:%%s') AS eta_iso
        FROM v_vessels_live
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """)
    return cursor.fetchall()

def fetch_container_by_number(cursor, container_number):
    cursor.execute("SELECT lfd_datetime, discharge_datetime FROM containers WHERE container_number = %s", (container_number,))
    return cursor.fetchone()

def record_dispatch_assignment(cursor, allocation_id, driver_id):
    """Create a pending assignment row; supersede any prior pending offer on this allocation."""
    cursor.execute(
        """
        UPDATE dispatch_assignments
        SET outcome_code = 'superseded', outcome_at = NOW()
        WHERE allocation_id = %s AND outcome_code = 'pending'
        """,
        (allocation_id,),
    )
    cursor.execute(
        """
        INSERT INTO dispatch_assignments (allocation_id, driver_id)
        VALUES (%s, %s)
        """,
        (allocation_id, driver_id),
    )
    return cursor.lastrowid

def find_pending_assignment(cursor, allocation_id, driver_id):
    cursor.execute(
        """
        SELECT assignment_id FROM dispatch_assignments
        WHERE allocation_id = %s AND driver_id = %s AND outcome_code = 'pending'
        ORDER BY assigned_at DESC LIMIT 1
        """,
        (allocation_id, driver_id),
    )
    row = cursor.fetchone()
    return row['assignment_id'] if row else None

def find_accepted_assignment(cursor, allocation_id, driver_id=None):
    query = """
        SELECT assignment_id FROM dispatch_assignments
        WHERE allocation_id = %s AND outcome_code = 'accepted'
    """
    params = [allocation_id]
    if driver_id is not None:
        query += " AND driver_id = %s"
        params.append(driver_id)
    query += " ORDER BY outcome_at DESC LIMIT 1"
    cursor.execute(query, tuple(params))
    row = cursor.fetchone()
    return row['assignment_id'] if row else None

def allocate_vessel_container(cursor, container_number, driver_id):
    cursor.execute("""
        INSERT INTO truck_allocations (container_number, urgency_score, dispatch_status_code, accepted_at)
        VALUES (%s, 95, 'Dispatched', NULL)
        ON DUPLICATE KEY UPDATE dispatch_status_code = 'Dispatched', accepted_at = NULL
    """, (container_number,))
    
    cursor.execute(
        "SELECT allocation_id FROM truck_allocations WHERE container_number = %s",
        (container_number,),
    )
    return cursor.fetchone()

def allocate_emergency_container(cursor, container_number):
    cursor.execute("""
        INSERT INTO truck_allocations (container_number, urgency_score, dispatch_status_code)
        VALUES (%s, 150, 'Dispatched') 
        ON DUPLICATE KEY UPDATE dispatch_status_code = 'Dispatched', urgency_score = 150
    """, (container_number,))
    
    cursor.execute(
        "SELECT allocation_id FROM truck_allocations WHERE container_number = %s",
        (container_number,),
    )
    return cursor.fetchone()

def fetch_allocation_by_container(cursor, container_number):
    cursor.execute(
        "SELECT t.allocation_id, da.driver_id FROM truck_allocations t LEFT JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed') WHERE t.container_number = %s",
        (container_number,),
    )
    return cursor.fetchone()

def insert_delivery_completion(cursor, assignment_id, pod_note, pod_signature, user_id):
    cursor.execute(
        """
        INSERT INTO delivery_completions
            (assignment_id, pod_note, pod_signature, confirmed_by_user_id)
        VALUES (%s, %s, %s, %s)
        """,
        (
            assignment_id,
            pod_note,
            pod_signature[:500] if pod_signature else '',
            user_id,
        ),
    )

def update_assignment_completed(cursor, assignment_id):
    cursor.execute(
        """
        UPDATE dispatch_assignments
        SET outcome_code = 'completed', outcome_at = NOW()
        WHERE assignment_id = %s
        """,
        (assignment_id,),
    )

def delete_allocation(cursor, container_number):
    cursor.execute("DELETE FROM truck_allocations WHERE container_number = %s", (container_number,))

def delete_container(cursor, container_number):
    cursor.execute("DELETE FROM containers WHERE container_number = %s", (container_number,))

def log_event(cursor, container_number, source_api, event_type, payload):
    cursor.execute(
        "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
        (container_number, source_api, event_type, json.dumps(payload)),
    )

def fetch_distinct_event_containers(cursor):
    cursor.execute("SELECT DISTINCT container_number FROM events WHERE container_number IS NOT NULL ORDER BY container_number")
    return [r['container_number'] for r in cursor.fetchall()]

def fetch_events_for_replay(cursor, container_filter=None):
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
    return cursor.fetchall()

def fetch_driver_active_job(cursor, driver_id):
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
        JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('pending', 'accepted', 'completed')
        LEFT JOIN port_slot_bookings psb ON psb.allocation_id = t.allocation_id AND psb.released_at IS NULL
        WHERE da.driver_id = %s AND t.dispatch_status_code IN ('Dispatched', 'At Warehouse')
        ORDER BY t.allocated_at DESC LIMIT 1
    """, (driver_id,))
    return cursor.fetchone()

def fetch_active_job_for_slot(cursor, driver_id):
    cursor.execute("""
        SELECT t.allocation_id, t.container_number, t.accepted_at
        FROM truck_allocations t
        JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        WHERE da.driver_id = %s AND t.dispatch_status_code = 'Dispatched'
        ORDER BY t.allocated_at DESC LIMIT 1
    """, (driver_id,))
    return cursor.fetchone()

def insert_job_rejection(cursor, assignment_id, reason):
    cursor.execute(
        "INSERT INTO job_rejections (assignment_id, reason) VALUES (%s, %s)",
        (assignment_id, reason[:500]),
    )

def update_assignment_rejected(cursor, assignment_id):
    cursor.execute(
        """
        UPDATE dispatch_assignments
        SET outcome_code = 'rejected', outcome_at = NOW()
        WHERE assignment_id = %s
        """,
        (assignment_id,),
    )

def reset_allocation_after_rejection(cursor, allocation_id):
    cursor.execute("""
        UPDATE truck_allocations
        SET dispatch_status_code = 'Pending', accepted_at = NULL
        WHERE allocation_id = %s
    """, (allocation_id,))

def accept_job_allocation(cursor, allocation_id):
    cursor.execute(
        "UPDATE truck_allocations SET accepted_at = NOW() WHERE allocation_id = %s",
        (allocation_id,),
    )

def update_assignment_accepted(cursor, assignment_id):
    cursor.execute(
        """
        UPDATE dispatch_assignments
        SET outcome_code = 'accepted', outcome_at = NOW()
        WHERE assignment_id = %s
        """,
        (assignment_id,),
    )

def fetch_all_containers_etas(cursor):
    cursor.execute("""
        SELECT c.container_number, c.lfd_datetime,
               IFNULL(t.dispatch_status_code, 'Pending') AS dispatch_status,
               t.accepted_at, t.picked_up_at, da.driver_id,
               d.driver_name,
               dl.latitude AS driver_lat, dl.longitude AS driver_lng, dl.speed_kph,
               (t.dispatch_status_code = 'At Warehouse') AS at_warehouse,
               psb.slot_number AS port_slot_number,
               rej.driver_name AS rejected_by,
               rej.reason AS rejection_reason
        FROM containers c
        LEFT JOIN truck_allocations t ON t.container_number = c.container_number
        LEFT JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('pending', 'accepted', 'completed')
        LEFT JOIN drivers d ON da.driver_id = d.driver_id
        LEFT JOIN driver_locations dl ON dl.driver_id = d.driver_id
        LEFT JOIN port_slot_bookings psb ON psb.allocation_id = t.allocation_id AND psb.released_at IS NULL
        LEFT JOIN (
            SELECT t2.container_number, d2.driver_name, jr.reason
            FROM job_rejections jr
            JOIN dispatch_assignments da ON da.assignment_id = jr.assignment_id
            JOIN drivers d2 ON d2.driver_id = da.driver_id
            JOIN truck_allocations t2 ON t2.allocation_id = da.allocation_id
            JOIN (
                SELECT da3.allocation_id, MAX(jr3.rejected_at) AS latest_rejected_at
                FROM job_rejections jr3
                JOIN dispatch_assignments da3 ON da3.assignment_id = jr3.assignment_id
                GROUP BY da3.allocation_id
            ) latest ON latest.allocation_id = da.allocation_id AND latest.latest_rejected_at = jr.rejected_at
        ) rej ON rej.container_number = c.container_number
            AND IFNULL(t.dispatch_status_code, 'Pending') = 'Pending'
    """)
    return cursor.fetchall()

def fetch_carrier_benchmarks(cursor, emergency_driver_rate, store_rent_rate, demurrage_rate, grace_period_hours):
    query = f"""
        SELECT v.vessel_name, 
               COUNT(c.container_number) AS total_dispatches,
               COUNT(CASE WHEN t.dispatch_status_code = 'Dispatched' AND da.driver_id IS NULL THEN 1 END) AS emergency_hires,
               SUM(CASE WHEN t.dispatch_status_code = 'Dispatched' AND da.driver_id IS NULL THEN {emergency_driver_rate} ELSE 0.00 END) AS total_extra_costs,
               ROUND(SUM(
                   CASE 
                       WHEN TIMESTAMPDIFF(SECOND, c.lfd_datetime, NOW()) > 0 
                       THEN (TIMESTAMPDIFF(SECOND, c.lfd_datetime, NOW()) / 3600.0) * {store_rent_rate}
                       ELSE 0.00 
                   END
               ), 2) AS accumulated_store_rent,
               ROUND(SUM(
                   CASE 
                       WHEN TIMESTAMPDIFF(SECOND, DATE_ADD(c.discharge_datetime, INTERVAL {grace_period_hours} HOUR), NOW()) > 0 
                       THEN (TIMESTAMPDIFF(SECOND, DATE_ADD(c.discharge_datetime, INTERVAL {grace_period_hours} HOUR), NOW()) / 3600.0) * {demurrage_rate}
                       ELSE 0.00 
                   END
               ), 2) AS accumulated_demurrage
        FROM vessels v
        JOIN voyages vy ON vy.vessel_id = v.vessel_id
        LEFT JOIN containers c ON c.voyage_id = vy.voyage_id
        LEFT JOIN truck_allocations t ON c.container_number = t.container_number
        LEFT JOIN dispatch_assignments da ON da.allocation_id = t.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        GROUP BY v.vessel_name 
        ORDER BY total_extra_costs DESC, accumulated_store_rent DESC, accumulated_demurrage DESC
    """
    cursor.execute(query)
    return cursor.fetchall()
