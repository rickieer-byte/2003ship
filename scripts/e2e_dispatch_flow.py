#!/usr/bin/env python3

"""End-to-end: dispatch -> accept -> port pickup (slot freed) -> warehouse -> POD expunge."""

import json

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

PORT_LAT = 1.3015

PORT_LNG = 103.6340

WAREHOUSE_LAT = 1.3298

WAREHOUSE_LNG = 103.6954

CONTAINER = 'NYKU9012455'





def db():

    return MySQLdb.connect(

        host=os.getenv('DB_HOST', 'localhost'),

        user=os.getenv('DB_USER', 'root'),

        password=os.getenv('DB_PASSWORD', ''),

        db=os.getenv('DB_NAME', 'escalation_db'),

        cursorclass=MySQLdb.cursors.DictCursor,

    )





def client():

    jar = http.cookiejar.CookieJar()

    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar





def post_form(opener, path, data):

    body = urllib.parse.urlencode(data).encode()

    req = urllib.request.Request(BASE + path, data=body, method='POST')

    return opener.open(req)





def api_json(opener, path, payload=None, method='POST'):

    if payload is None and method == 'GET':

        req = urllib.request.Request(BASE + path, method='GET')

    elif payload is None:

        req = urllib.request.Request(BASE + path, method='POST')

    else:

        req = urllib.request.Request(

            BASE + path,

            data=json.dumps(payload).encode(),

            headers={'Content-Type': 'application/json'},

            method=method,

        )

    return json.loads(opener.open(req).read().decode())





def step(n, msg, ok=True):

    tag = 'OK' if ok else 'FAIL'

    print(f'  {n}. [{tag}] {msg}')





def allocation_row(container):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """SELECT t.*, d.driver_name, d.phone_number

           FROM truck_allocations t

           LEFT JOIN drivers d ON d.driver_id = t.driver_id

           WHERE t.container_number = %s""",

        (container,),

    )

    row = cur.fetchone()

    cur.close()

    conn.close()

    return row





def active_slot_for_allocation(allocation_id):

    conn = db()

    cur = conn.cursor()

    cur.execute(

        """SELECT booking_id FROM port_slot_bookings

           WHERE allocation_id = %s AND released_at IS NULL""",

        (allocation_id,),

    )

    row = cur.fetchone()

    cur.close()

    conn.close()

    return row





def main():

    print('=' * 60)

    print('E2E: dispatch -> port pickup -> warehouse -> POD expunge')

    print('=' * 60)



    # --- Dispatcher: allocate ---

    disp, _ = client()

    post_form(disp, '/login', {'username': 'dispatcher_user', 'password': 'password123'})



    avail = api_json(disp, '/api/dispatch/check-availability', {'container_number': CONTAINER})

    step(1, f"Availability check -> {avail.get('status')}", avail.get('status') == 'available')



    alloc = api_json(disp, '/api/dispatch/allocate', {'container_number': CONTAINER})

    step(2, f"Standard allocate -> {alloc.get('status')}", alloc.get('status') == 'success')



    row = allocation_row(CONTAINER)

    if not row or not row['driver_id']:

        step(3, 'No driver assigned after allocate', False)

        return 1

    driver_id = row['driver_id']

    allocation_id = row['allocation_id']

    phone_tail = row['phone_number'].replace(' ', '')[-4:]

    step(3, f"Assigned driver #{driver_id} ({row['driver_name']}, tail {phone_tail})")



    # --- Driver: accept job ---

    drv, _ = client()

    post_form(drv, '/driver/login', {'driver_id': str(driver_id), 'phone_tail': phone_tail})

    acc = api_json(drv, '/api/driver/accept', {'container_number': CONTAINER})

    step(4, f"Driver accept -> {acc.get('status')}", acc.get('status') == 'success')



    slot = api_json(drv, '/api/driver/port-slot/book', {})

    step(5, f"Port slot booked -> #{slot.get('slot_number')}", slot.get('status') == 'success')



    geo = api_json(drv, '/api/driver/location', {

        'latitude': PORT_LAT,

        'longitude': PORT_LNG,

        'heading': 0,

        'speed_kph': 0,

    })

    step(6, f"Port pickup -> {geo.get('port_pickups', 0)}", geo.get('port_pickups', 0) >= 1)

    step(7, f"Slot released on exit -> {geo.get('slot_released')}", geo.get('slot_released') is True)



    slot_still_active = active_slot_for_allocation(allocation_id)

    step(8, 'No active port slot after pickup', slot_still_active is None)



    conn = db()

    cur = conn.cursor()

    cur.execute("SELECT status_code FROM drivers WHERE driver_id = %s", (driver_id,))

    drv_status = cur.fetchone()['status_code']

    cur.close()

    conn.close()

    step(9, f"Driver status -> {drv_status}", drv_status == 'On Delivery')



    row = allocation_row(CONTAINER)

    step(10, f"picked_up_at set -> {row['picked_up_at'] is not None}", row['picked_up_at'] is not None)



    geo = api_json(drv, '/api/driver/location', {

        'latitude': WAREHOUSE_LAT,

        'longitude': WAREHOUSE_LNG,

        'heading': 0,

        'speed_kph': 0,

    })

    arrivals = geo.get('warehouse_arrivals', 0)

    step(11, f"Warehouse arrival(s) -> {arrivals}", arrivals >= 1)



    row = allocation_row(CONTAINER)

    step(12, f"dispatch_status -> {row['dispatch_status_code']}", row['dispatch_status_code'] == 'At Warehouse')



    conn = db()

    cur = conn.cursor()

    cur.execute("SELECT status_code FROM drivers WHERE driver_id = %s", (driver_id,))

    drv_status = cur.fetchone()['status_code']

    cur.close()

    conn.close()

    step(13, f"Driver freed at warehouse -> {drv_status}", drv_status == 'Available')



    etas = api_json(disp, '/api/containers/etas', method='GET')

    eta = etas.get(CONTAINER, {})

    step(14, f"ETA API at_warehouse -> {eta.get('at_warehouse')}", eta.get('at_warehouse') is True)



    exp = api_json(disp, '/api/container/expunge', {

        'container_number': CONTAINER,

        'pod_note': 'E2E test delivery - signed at warehouse gate',

        'pod_signature': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',

    })

    step(15, f"POD expunge -> {exp.get('status')}", exp.get('status') == 'success')



    row = allocation_row(CONTAINER)

    step(16, 'Container removed from DB', row is None)



    conn = db()

    cur = conn.cursor()

    cur.execute(

        "SELECT event_type FROM events WHERE container_number = %s ORDER BY event_id",

        (CONTAINER,),

    )

    events = [r['event_type'] for r in cur.fetchall()]

    cur.close()

    conn.close()

    step(17, f"Event trail: {', '.join(events)}", 'DELIVERY_COMPLETED' in events)



    print('\nDone.')

    return 0





if __name__ == '__main__':

    sys.exit(main())

