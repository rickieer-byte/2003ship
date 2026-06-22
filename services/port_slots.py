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
        SELECT b.booking_id, b.slot_number, b.allocation_id, da.driver_id,
               b.booked_at, COALESCE(d.driver_name, 'Emergency Contractor') AS driver_name, t.container_number
        FROM port_slot_bookings b
        JOIN truck_allocations t ON t.allocation_id = b.allocation_id
        LEFT JOIN dispatch_assignments da ON da.allocation_id = b.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        LEFT JOIN drivers d ON d.driver_id = da.driver_id
        WHERE b.port_id = %s AND b.released_at IS NULL AND b.slot_number IS NOT NULL
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
    if not row:
        return None
    return row['slot_number'] if row['slot_number'] is not None else "queued"


def book_slot(cursor, allocation_id, driver_id, container_number, port_id=DEFAULT_PORT_ID):
    """Reserve the next free prime-mover slot. Returns slot_number or 'queued' if port is full."""
    active = allocation_has_active_slot(cursor, allocation_id)
    if active:
        return active

    capacity, _ = get_port_capacity(cursor, port_id)
    if capacity <= 0:
        return None

    cursor.execute("""
        SELECT slot_number FROM port_slot_bookings
        WHERE port_id = %s AND released_at IS NULL AND slot_number IS NOT NULL
    """, (port_id,))
    taken = {int(r['slot_number']) for r in cursor.fetchall()}

    slot_number = next((n for n in range(1, capacity + 1) if n not in taken), None)

    cursor.execute("""
        INSERT INTO port_slot_bookings (port_id, slot_number, allocation_id)
        VALUES (%s, %s, %s)
    """, (port_id, slot_number, allocation_id))

    if slot_number is not None:
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
    else:
        cursor.execute(
            "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
            (
                container_number,
                'PORT_SLOT_SYSTEM',
                'PRIME_MOVER_QUEUED',
                json.dumps({
                    'port_id': port_id,
                    'driver_id': driver_id,
                }),
            ),
        )
        return "queued"


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
    
    for row in rows:
        if row['slot_number'] is not None:
            _assign_next_queued(cursor, row['port_id'], row['slot_number'])
            
    return len(rows)

def _assign_next_queued(cursor, port_id, freed_slot_number):
    cursor.execute("""
        SELECT b.booking_id, b.allocation_id, t.container_number, da.driver_id
        FROM port_slot_bookings b
        JOIN truck_allocations t ON t.allocation_id = b.allocation_id
        LEFT JOIN dispatch_assignments da ON da.allocation_id = b.allocation_id AND da.outcome_code IN ('accepted', 'completed')
        WHERE b.port_id = %s AND b.released_at IS NULL AND b.slot_number IS NULL
        ORDER BY b.booked_at ASC
        LIMIT 1
    """, (port_id,))
    next_booking = cursor.fetchone()
    
    if next_booking:
        cursor.execute("""
            UPDATE port_slot_bookings SET slot_number = %s WHERE booking_id = %s
        """, (freed_slot_number, next_booking['booking_id']))
        
        cursor.execute(
            "INSERT INTO events (container_number, source_api, event_type, raw_payload) VALUES (%s, %s, %s, %s)",
            (
                next_booking['container_number'],
                'PORT_SLOT_SYSTEM',
                'PRIME_MOVER_SLOT_BOOKED',
                json.dumps({
                    'port_id': port_id,
                    'slot_number': freed_slot_number,
                    'driver_id': next_booking['driver_id'],
                }),
            ),
        )
