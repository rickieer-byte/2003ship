"""Database operations for the staff leave requests system."""

import datetime
import MySQLdb

def fetch_all_leaves(cursor):
    """Retrieve all approved leave requests with details of Planners, Dispatchers, and Drivers."""
    cursor.execute("""
        SELECT lr.leave_id, lr.employee_type, lr.leave_date, lr.reason, lr.created_at,
               u.username AS user_name, u.email AS user_email,
               d.driver_name, d.phone_number AS driver_phone
        FROM leave_requests lr
        LEFT JOIN users u ON lr.user_id = u.user_id
        LEFT JOIN drivers d ON lr.driver_id = d.driver_id
        ORDER BY lr.leave_date DESC, lr.created_at DESC
    """)
    return cursor.fetchall()

def apply_leave(cursor, employee_type, employee_id, leave_date, reason):
    """
    Attempt to insert a new leave request.
    Validation constraint (max 2 people of that type on leave on the same day)
    is enforced by SQL triggers in the database.
    """
    user_id = None
    driver_id = None
    if employee_type == 'Driver':
        driver_id = employee_id
    else:
        user_id = employee_id
        
    try:
        cursor.execute(
            """
            INSERT INTO leave_requests (employee_type, user_id, driver_id, leave_date, reason)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (employee_type, user_id, driver_id, leave_date, reason)
        )
    except (MySQLdb.DatabaseError, Exception) as exc:
        msg = str(exc)
        if 'Leave limit reached' in msg or 'duplicate' in msg.lower() or '1062' in msg:
            if 'duplicate' in msg.lower() or '1062' in msg:
                raise ValueError("This employee is already registered as on leave for this date.")
            # Extract trigger error message
            # OperationalError representation contains the message text inside brackets or trailing
            # E.g. (1644, 'Leave limit reached: ...')
            if isinstance(exc.args, tuple) and len(exc.args) > 1:
                raise ValueError(exc.args[1])
            raise ValueError(msg)
        raise exc

def cancel_leave(cursor, leave_id):
    """Cancel a leave request."""
    cursor.execute("DELETE FROM leave_requests WHERE leave_id = %s", (leave_id,))

def fetch_eligible_planners(cursor):
    cursor.execute("SELECT user_id, username FROM users WHERE role_id = 1 ORDER BY username")
    return cursor.fetchall()

def fetch_eligible_dispatchers(cursor):
    cursor.execute("SELECT user_id, username FROM users WHERE role_id = 2 ORDER BY username")
    return cursor.fetchall()

def fetch_eligible_drivers(cursor):
    cursor.execute("SELECT driver_id, driver_name FROM drivers WHERE driver_name != 'Emergency Contractor' ORDER BY driver_name")
    return cursor.fetchall()
