#!/usr/bin/env python3
"""E2E verification for the Staff Leave System triggers, constraints, and login block."""

import datetime
import os
import sys
import urllib.parse
import urllib.request
import http.cookiejar

import pymysql
pymysql.install_as_MySQLdb()
import MySQLdb
import MySQLdb.cursors
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, '.env'))

BASE = 'http://127.0.0.1:5000'

def db():
    return MySQLdb.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        user=os.getenv('DB_USER', 'root'),
        password=os.getenv('DB_PASSWORD', ''),
        db=os.getenv('DB_NAME', 'escalation_db'),
        cursorclass=MySQLdb.cursors.DictCursor,
    )

def step(n, msg, ok=True):
    tag = 'OK' if ok else 'FAIL'
    print(f'  {n}. [{tag}] {msg}')

def main():
    print('=' * 60)
    print('E2E Staff Leave & SQL Trigger Verification')
    print('=' * 60)

    # 1. Reset database
    from db.setup import run_setup
    run_setup(
        os.getenv('DB_HOST', 'localhost'),
        os.getenv('DB_USER', 'root'),
        os.getenv('DB_PASSWORD', ''),
        os.getenv('DB_NAME', 'escalation_db')
    )
    step(1, "Database reset and seeded successfully")

    # Connect to DB
    conn = db()
    cur = conn.cursor()

    # 2. Add first driver leave request for tomorrow
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    try:
        cur.execute(
            "INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 1, %s, 'Doctor Appointment')",
            (tomorrow,)
        )
        conn.commit()
        step(2, "Approved first driver leave request for tomorrow (Driver ID 1)")
    except Exception as e:
        step(2, f"Failed to insert first driver leave: {e}", False)
        return 1

    # 3. Add second driver leave request for tomorrow
    try:
        cur.execute(
            "INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 2, %s, 'Vacation')",
            (tomorrow,)
        )
        conn.commit()
        step(3, "Approved second driver leave request for tomorrow (Driver ID 2)")
    except Exception as e:
        step(3, f"Failed to insert second driver leave: {e}", False)
        return 1

    # 4. Try to add a THIRD driver leave request for tomorrow (should trigger the SQL constraint trigger!)
    try:
        cur.execute(
            "INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 3, %s, 'Family Event')",
            (tomorrow,)
        )
        conn.commit()
        step(4, "Rejection check failed (inserted a 3rd driver leave on the same day!)", False)
        return 1
    except MySQLdb.DatabaseError as e:
        msg = str(e)
        if 'Leave limit reached' in msg or '45000' in msg:
            step(4, f"Blocked third driver leave request (Trigger working: '{msg}')")
        else:
            step(4, f"Blocked third driver leave but message mismatch: {e}", False)
            return 1

    # 5. Try to insert a duplicate leave request for driver 1 tomorrow
    try:
        cur.execute(
            "INSERT INTO leave_requests (employee_type, driver_id, leave_date, reason) VALUES ('Driver', 1, %s, 'Duplicate check')",
            (tomorrow,)
        )
        conn.commit()
        step(5, "Duplicate check failed (inserted duplicate leave request!)", False)
        return 1
    except MySQLdb.DatabaseError as e:
        step(5, f"Blocked duplicate leave request (Unique key working: {e})")

    # 6. Apply leave for Planner today and test login block
    today = datetime.date.today().isoformat()
    try:
        # planner_user is user_id 1
        cur.execute(
            "INSERT INTO leave_requests (employee_type, user_id, leave_date, reason) VALUES ('Planner', 1, %s, 'Annual Leave')",
            (today,)
        )
        conn.commit()
        step(6, "Approved planner leave request for today (User ID 1)")
    except Exception as e:
        step(6, f"Failed to insert planner leave: {e}", False)
        return 1

    cur.close()
    conn.close()

    # Try login as planner_user
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    
    login_data = urllib.parse.urlencode({'username': 'planner_user', 'password': 'password123'}).encode()
    req = urllib.request.Request(BASE + '/login', data=login_data, method='POST')
    
    try:
        resp = opener.open(req)
        body = resp.read().decode('utf-8')
        if "is registered as on leave today" in body:
            step(7, "Blocked login attempt for Planner on leave today (Warning matches)")
        else:
            step(7, "Login block check failed (Accessed pages instead of login block warnings)", False)
            return 1
    except Exception as e:
        step(7, f"Login block query failed: {e}", False)
        return 1

    print('\nAll Leave System Verification Checks Passed!')
    return 0

if __name__ == '__main__':
    sys.exit(main())
