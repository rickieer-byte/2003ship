"""Driver shift window checks (12-hour staggered schedules, including overnight blocks)."""
import datetime


def _as_time(value):
    if isinstance(value, datetime.time):
        return value
    if isinstance(value, datetime.timedelta):
        return (datetime.datetime.min + value).time()
    return value


def time_in_shift(current_time, shift_start, shift_end):
    """Half-open interval [start, end) so 12-hour blocks hand off cleanly at boundaries."""
    start = _as_time(shift_start)
    end = _as_time(shift_end)
    if start <= end:
        return start <= current_time < end
    return current_time >= start or current_time < end


def driver_is_on_shift(driver_id, cursor, as_of=None):
    """True when the driver is within a scheduled 12-hour block (incl. overnight spillover)."""
    as_of = as_of or datetime.datetime.now()
    as_of_date = as_of.date()
    
    # Exclude driver if they have an active leave request today
    cursor.execute(
        "SELECT 1 FROM leave_requests WHERE employee_type = 'Driver' AND driver_id = %s AND leave_date = %s",
        (driver_id, as_of_date),
    )
    if cursor.fetchone():
        return False

    current_time = as_of.time()
    dow = as_of.weekday()

    cursor.execute(
        "SELECT shift_start, shift_end FROM driver_schedules WHERE driver_id = %s AND day_of_week = %s",
        (driver_id, dow),
    )
    row = cursor.fetchone()
    if row and time_in_shift(current_time, row['shift_start'], row['shift_end']):
        return True

    prev_dow = (dow - 1) % 7
    cursor.execute(
        "SELECT shift_start, shift_end FROM driver_schedules WHERE driver_id = %s AND day_of_week = %s",
        (driver_id, prev_dow),
    )
    prev_row = cursor.fetchone()
    if prev_row:
        start = _as_time(prev_row['shift_start'])
        end = _as_time(prev_row['shift_end'])
        if start > end and current_time < end:
            return True
    return False
