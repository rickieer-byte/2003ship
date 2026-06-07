"""Mock PSA/Tuas prime-mover slot capacity at the port gate."""
import json

DEFAULT_PORT_ID = 'TUAS-PSA'
EMERGENCY_CONTRACTOR_NAME = 'Emergency Contractor'


def get_emergency_contractor_id(cursor):
    """Synthetic driver used only for emergency-hire port slot bookings."""
    cursor.execute(
        "SELECT driver_id FROM drivers WHERE driver_name = %s LIMIT 1",
        (EMERGENCY_CONTRACTOR_NAME,),
    )
    row = cursor.fetchone()
    if row:
        return row['driver_id']
    cursor.execute(
        "INSERT INTO drivers (driver_name, phone_number, status_code) VALUES (%s, %s, 'Available')",
        (EMERGENCY_CONTRACTOR_NAME, '+65 9999 0000'),
    )
    return cursor.lastrowid


def get_port_capacity(cursor, port_id=DEFAULT_PORT_ID):
    cursor.execute(
        "SELECT max_prime_movers, port_name FROM ports WHERE port_id = %s",
        (port_id,),
    )
    row = cursor.fetchone()
    if not row:
        return 0, port_id
    return int(row['max_prime_movers']), row['port_name']


def fetch_active_bookings(cursor, port_id=DEFAULT_PORT_ID):
    cursor.execute("""
        SELECT b.booking_id, b.slot_number, b.allocation_id, b.driver_id,
               b.booked_at, d.driver_name, t.container_number
        FROM port_slot_bookings b
        JOIN drivers d ON d.driver_id = b.driver_id
        JOIN truck_allocations t ON t.allocation_id = b.allocation_id
        WHERE b.port_id = %s AND b.released_at IS NULL
        ORDER BY b.slot_number
    """, (port_id,))
    return cursor.fetchall()


def get_slot_status(cursor, port_id=DEFAULT_PORT_ID):
    capacity, port_name = get_port_capacity(cursor, port_id)
    bookings = fetch_active_bookings(cursor, port_id)
    occupied = len(bookings)
    return {
        'port_id': port_id,
        'port_name': port_name,
        'capacity': capacity,
        'occupied': occupied,
        'available': max(0, capacity - occupied),
        'slots': [
            {
                'slot_number': b['slot_number'],
                'driver_name': b['driver_name'],
                'container_number': b['container_number'],
                'booked_at': b['booked_at'].isoformat() if b['booked_at'] else None,
            }
            for b in bookings
        ],
    }


def allocation_has_active_slot(cursor, allocation_id):
    cursor.execute("""
        SELECT slot_number FROM port_slot_bookings
        WHERE allocation_id = %s AND released_at IS NULL
        LIMIT 1
    """, (allocation_id,))
    row = cursor.fetchone()
    return row['slot_number'] if row else None


def book_slot(cursor, allocation_id, driver_id, container_number, port_id=DEFAULT_PORT_ID):
    """Reserve the next free prime-mover slot. Returns slot_number or None if port is full."""
    if allocation_has_active_slot(cursor, allocation_id):
        return allocation_has_active_slot(cursor, allocation_id)

    capacity, _ = get_port_capacity(cursor, port_id)
    if capacity <= 0:
        return None

    cursor.execute("""
        SELECT slot_number FROM port_slot_bookings
        WHERE port_id = %s AND released_at IS NULL
    """, (port_id,))
    taken = {int(r['slot_number']) for r in cursor.fetchall()}

    slot_number = next((n for n in range(1, capacity + 1) if n not in taken), None)
    if slot_number is None:
        return None

    cursor.execute("""
        INSERT INTO port_slot_bookings (port_id, slot_number, allocation_id, driver_id)
        VALUES (%s, %s, %s, %s)
    """, (port_id, slot_number, allocation_id, driver_id))

    cursor.execute(
        "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
        (
            container_number,
            'PORT_SLOT_SYSTEM',
            'PRIME_MOVER_SLOT_BOOKED',
            json.dumps({
                'port_id': port_id,
                'slot_number': slot_number,
                'driver_id': driver_id,
            }),
        ),
    )
    return slot_number


def book_slot_for_emergency(cursor, allocation_id, container_number, port_id=DEFAULT_PORT_ID):
    """Reserve a prime-mover slot for an emergency hire (no fleet driver on the allocation)."""
    contractor_id = get_emergency_contractor_id(cursor)
    return book_slot(cursor, allocation_id, contractor_id, container_number, port_id=port_id)


def release_slots_for_allocation(cursor, allocation_id):
    cursor.execute("""
        SELECT booking_id, slot_number, port_id FROM port_slot_bookings
        WHERE allocation_id = %s AND released_at IS NULL
    """, (allocation_id,))
    rows = cursor.fetchall()
    if not rows:
        return 0
    cursor.execute("""
        UPDATE port_slot_bookings SET released_at = NOW()
        WHERE allocation_id = %s AND released_at IS NULL
    """, (allocation_id,))
    return len(rows)
