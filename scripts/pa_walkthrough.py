#!/usr/bin/env python3
"""
PythonAnywhere-style role walkthrough against a running Flask app.
Usage:
  1. python db/setup.py          # load schema + seed
  2. python app.py               # in another terminal
  3. python scripts/pa_walkthrough.py
"""
import json
import re
import sys
import urllib.parse
import urllib.request
import http.cookiejar

BASE = 'http://127.0.0.1:5000'

USERS = [
    ('Planner', 'planner_user', 'password123'),
    ('Dispatcher', 'dispatcher_user', 'password123'),
    ('Fleet Manager', 'manager_user', 'password123'),
]


def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def post_form(opener, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body, method='POST')
    return opener.open(req)


def get(opener, path):
    return opener.open(BASE + path)


def login(opener, username, password):
    post_form(opener, '/login', {'username': username, 'password': password})
    page = get(opener, '/').read().decode('utf-8', errors='replace')
    return username in page and 'Logged Session' in page


def check_path(opener, path, needle):
    try:
        body = get(opener, path).read().decode('utf-8', errors='replace')
        return needle in body, body[:200]
    except urllib.error.HTTPError as exc:
        return False, f'HTTP {exc.code}'


def api_json(opener, path, payload=None):
    if payload is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
    resp = opener.open(req)
    return json.loads(resp.read().decode())


def main():
    print('=' * 60)
    print('PythonAnywhere role walkthrough')
    print('=' * 60)

    for role_name, username, password in USERS:
        print(f'\n## {role_name} ({username})')
        opener, _ = client()
        if not login(opener, username, password):
            print('  FAIL login')
            continue
        print('  OK   logged in -> ops hub')

        for path, label, needle in [
            ('/', 'Ops hub', 'Container ID'),
            ('/drivers', 'Drivers roster', 'Driver Roster'),
            ('/tracking', 'Live map', 'Live Fleet'),
            ('/replay', 'Event replay', 'Container Journey'),
        ]:
            ok, detail = check_path(opener, path, needle)
            print(f'  {"OK" if ok else "DENY/FAIL"}  {label} ({path})')

        ok, _ = check_path(opener, '/fleet', 'Fleet Asset Management')
        print(f'  {"OK" if ok else "DENY"}  Manage fleet (/fleet) — expected {"DENY" if role_name != "Fleet Manager" else "OK"}')

        ok, _ = check_path(opener, '/analytics', 'Fleet Performance Savings')
        print(f'  {"OK" if ok else "DENY"}  Analytics (/analytics)')

        if role_name in ('Planner', 'Dispatcher'):
            try:
                avail = api_json(opener, '/api/dispatch/check-availability', {'container_number': 'NYKU9012455'})
                print(f'  OK   dispatch check -> {avail.get("status")}')
            except Exception as exc:
                print(f'  FAIL dispatch API: {exc}')

        if role_name == 'Fleet Manager':
            try:
                tick = api_json(opener, '/api/simulation/tick', {})
                print(f'  OK   simulation tick -> drivers={tick.get("result", {}).get("drivers_updated")}')
            except Exception as exc:
                print(f'  FAIL simulation tick: {exc}')

    print('\n## Driver (dispatch then accept)')
    opener, _ = client()
    login(opener, 'dispatcher_user', 'password123')
    try:
        alloc = api_json(opener, '/api/dispatch/allocate', {'container_number': 'NYKU9012455'})
        driver_id = alloc.get('driver_id')
        phone_tail = alloc.get('phone_tail')
        driver_name = alloc.get('driver_name', '?')
        print(f'  OK   allocated NYKU9012455 -> {driver_name} (tail {phone_tail})')
    except Exception as exc:
        print(f'  FAIL allocate: {exc}')
        driver_id = phone_tail = None

    if driver_id and phone_tail:
        opener, _ = client()
        post_form(opener, '/driver/login', {'driver_id': str(driver_id), 'phone_tail': phone_tail})
        ok, body = check_path(opener, '/driver/portal', driver_name)
        print(f'  {"OK" if ok else "FAIL"}  driver portal login')
        try:
            acc = api_json(opener, '/api/driver/accept', {'container_number': 'NYKU9012455'})
            print(f'  OK   accept job -> {acc.get("status")}')
            slot = api_json(opener, '/api/driver/port-slot/book', {})
            print(f'  OK   port slot -> #{slot.get("slot_number")}')
        except Exception as exc:
            print(f'  FAIL driver flow: {exc}')

    print('\nDone.')


if __name__ == '__main__':
    main()
