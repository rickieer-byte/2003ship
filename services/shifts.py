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


def check_shift_and_leave(driver_id, cursor, as_of=None):
    """
    Returns (is_on_shift, is_on_leave_for_shift)
    Checks if current time falls within a shift, and whether a leave request
    exists for the DATE THAT SHIFT STARTED.
    """
    as_of = as_of or datetime.datetime.now()
    as_of_date = as_of.date()
    current_time = as_of.time()
    dow = as_of.weekday()

    # Check today's shift (must be the pre-midnight portion if it's an overnight shift)
    cursor.execute(
        "SELECT shift_start, shift_end FROM driver_schedules WHERE driver_id = %s AND day_of_week = %s",
        (driver_id, dow),
    )
    row = cursor.fetchone()
    if row:
        start = _as_time(row['shift_start'])
        end = _as_time(row['shift_end'])
        # If daytime shift, just check time_in_shift
        if start <= end and start <= current_time < end:
            cursor.execute("SELECT 1 FROM leave_requests WHERE driver_id = %s AND leave_date = %s", (driver_id, as_of_date))
            is_leave = cursor.fetchone() is not None
            return (not is_leave, is_leave)
        # If overnight shift, today's shift only covers time >= start (e.g. >= 18:00 today)
        elif start > end and current_time >= start:
            cursor.execute("SELECT 1 FROM leave_requests WHERE driver_id = %s AND leave_date = %s", (driver_id, as_of_date))
            is_leave = cursor.fetchone() is not None
            return (not is_leave, is_leave)

    # Check yesterday's shift (must be the post-midnight portion if it's an overnight shift)
    prev_dow = (dow - 1) % 7
    cursor.execute(
        "SELECT shift_start, shift_end FROM driver_schedules WHERE driver_id = %s AND day_of_week = %s",
        (driver_id, prev_dow),
    )
    prev_row = cursor.fetchone()
    if prev_row:
        start = _as_time(prev_row['shift_start'])
        end = _as_time(prev_row['shift_end'])
        # Overnight shift Check: yesterday's shift covers time < end (e.g. < 06:00 today)
        if start > end and current_time < end:
            yesterday_date = as_of_date - datetime.timedelta(days=1)
            cursor.execute("SELECT 1 FROM leave_requests WHERE driver_id = %s AND leave_date = %s", (driver_id, yesterday_date))
            is_leave = cursor.fetchone() is not None
            return (not is_leave, is_leave)

    # Not on any active shift block
    return (False, False)


def driver_is_on_shift(driver_id, cursor, as_of=None):
    """True when the driver is within a scheduled 12-hour block (incl. overnight spillover) and NOT on leave for that shift."""
    is_on_shift, _ = check_shift_and_leave(driver_id, cursor, as_of)
    return is_on_shift
