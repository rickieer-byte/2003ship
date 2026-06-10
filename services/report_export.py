"""Build CSV analytics export (ZIP bundle of three report files)."""
import csv
import io
import zipfile

from services.port_slots import EMERGENCY_CONTRACTOR_NAME

EMERGENCY_DRIVER_FLAT_RATE = 250.00
STORE_RENT_HOURLY_RATE = 12.00
DEMURRAGE_HOURLY_RATE = 15.00
GRACE_PERIOD_HOURS = 48


def _csv_bytes(headers, rows):
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(headers)
    writer.writerows(rows)
    return text.getvalue().encode('utf-8-sig')


def fetch_jobs_sheet(cursor):
    cursor.execute("""
        SELECT
            c.container_number,
            v.vessel_name,
            vy.voyage_number,
            t.dispatch_status_code,
            DATE_FORMAT(t.allocated_at, '%Y-%m-%d %H:%i') AS allocated_at,
            DATE_FORMAT(t.accepted_at, '%Y-%m-%d %H:%i') AS accepted_at,
            DATE_FORMAT(t.picked_up_at, '%Y-%m-%d %H:%i') AS picked_up_at,
            d_assigned.driver_name AS assigned_driver,
            done.completed_by,
            rej.rejections_summary,
            rej.rejected_by
        FROM containers c
        JOIN voyages vy ON vy.voyage_id = c.voyage_id
        JOIN vessels v ON v.vessel_id = vy.vessel_id
        LEFT JOIN truck_allocations t ON t.container_number = c.container_number
        LEFT JOIN drivers d_assigned ON d_assigned.driver_id = t.driver_id
        LEFT JOIN (
            SELECT
                t2.container_number,
                d.driver_name AS completed_by
            FROM delivery_completions dc
            JOIN dispatch_assignments da ON da.assignment_id = dc.assignment_id
            JOIN drivers d ON d.driver_id = da.driver_id
            JOIN truck_allocations t2 ON t2.allocation_id = da.allocation_id
        ) done ON done.container_number = c.container_number
        LEFT JOIN (
            SELECT
                t3.container_number,
                GROUP_CONCAT(d.driver_name ORDER BY jr.rejected_at SEPARATOR '; ') AS rejected_by,
                GROUP_CONCAT(
                    CONCAT(d.driver_name, ': ', jr.reason)
                    ORDER BY jr.rejected_at SEPARATOR ' | '
                ) AS rejections_summary
            FROM job_rejections jr
            JOIN dispatch_assignments da ON da.assignment_id = jr.assignment_id
            JOIN drivers d ON d.driver_id = da.driver_id
            JOIN truck_allocations t3 ON t3.allocation_id = da.allocation_id
            GROUP BY t3.container_number
        ) rej ON rej.container_number = c.container_number
        ORDER BY c.container_number
    """)
    rows = cursor.fetchall()
    headers = [
        'Container', 'Vessel', 'Voyage', 'Status', 'Allocated At', 'Accepted At',
        'Picked Up At', 'Assigned Driver', 'Completed By', 'Rejected By', 'Rejection Reasons',
    ]
    data = [
        [
            r['container_number'], r['vessel_name'], r['voyage_number'],
            r['dispatch_status_code'] or 'Unallocated',
            r['allocated_at'] or '', r['accepted_at'] or '', r['picked_up_at'] or '',
            r['assigned_driver'] or '', r['completed_by'] or '',
            r['rejected_by'] or '', r['rejections_summary'] or '',
        ]
        for r in rows
    ]
    return headers, data


def fetch_carrier_performance_sheet(cursor):
    query = f"""
        SELECT v.vessel_name,
               COUNT(c.container_number) AS total_dispatches,
               COUNT(CASE WHEN t.dispatch_status_code = 'Dispatched' AND t.driver_id IS NULL THEN 1 END) AS emergency_hires,
               SUM(CASE WHEN t.dispatch_status_code = 'Dispatched' AND t.driver_id IS NULL
                   THEN {EMERGENCY_DRIVER_FLAT_RATE} ELSE 0.00 END) AS total_extra_costs,
               ROUND(SUM(
                   CASE WHEN TIMESTAMPDIFF(SECOND, c.lfd_datetime, NOW()) > 0
                   THEN (TIMESTAMPDIFF(SECOND, c.lfd_datetime, NOW()) / 3600.0) * {STORE_RENT_HOURLY_RATE}
                   ELSE 0.00 END
               ), 2) AS accumulated_store_rent,
               ROUND(SUM(
                   CASE WHEN TIMESTAMPDIFF(SECOND,
                       DATE_ADD(c.discharge_datetime, INTERVAL {GRACE_PERIOD_HOURS} HOUR), NOW()) > 0
                   THEN (TIMESTAMPDIFF(SECOND,
                       DATE_ADD(c.discharge_datetime, INTERVAL {GRACE_PERIOD_HOURS} HOUR), NOW()) / 3600.0)
                       * {DEMURRAGE_HOURLY_RATE}
                   ELSE 0.00 END
               ), 2) AS accumulated_demurrage
        FROM vessels v
        JOIN voyages vy ON vy.vessel_id = v.vessel_id
        LEFT JOIN containers c ON c.voyage_id = vy.voyage_id
        LEFT JOIN truck_allocations t ON c.container_number = t.container_number
        GROUP BY v.vessel_name
        ORDER BY total_extra_costs DESC, accumulated_store_rent DESC
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    headers = [
        'Vessel', 'Total Containers', 'Emergency Hires', 'Premium Ad-hoc Loss',
        'Accrued Store Rent', 'Accrued Demurrage', 'Recommendation',
    ]
    data = []
    for r in rows:
        emergency_hires = int(r['emergency_hires'] or 0)
        recommendation = 'CRITICAL RISK: Renegotiate Terms' if emergency_hires >= 3 else 'PERFORMANCE STABLE'
        data.append([
            r['vessel_name'],
            int(r['total_dispatches'] or 0),
            emergency_hires,
            float(r['total_extra_costs'] or 0),
            float(r['accumulated_store_rent'] or 0),
            float(r['accumulated_demurrage'] or 0),
            recommendation,
        ])
    return headers, data


def fetch_employee_metrics_sheet(cursor):
    cursor.execute("""
        SELECT
            d.driver_id,
            d.driver_name,
            COALESCE(assigned.jobs_given, 0) AS jobs_given,
            COALESCE(completed.jobs_done, 0) AS jobs_done,
            COALESCE(rejected.jobs_rejected, 0) AS jobs_rejected
        FROM drivers d
        LEFT JOIN (
            SELECT driver_id, COUNT(*) AS jobs_given
            FROM dispatch_assignments
            GROUP BY driver_id
        ) assigned ON assigned.driver_id = d.driver_id
        LEFT JOIN (
            SELECT da.driver_id, COUNT(*) AS jobs_done
            FROM delivery_completions dc
            JOIN dispatch_assignments da ON da.assignment_id = dc.assignment_id
            GROUP BY da.driver_id
        ) completed ON completed.driver_id = d.driver_id
        LEFT JOIN (
            SELECT da.driver_id, COUNT(*) AS jobs_rejected
            FROM job_rejections jr
            JOIN dispatch_assignments da ON da.assignment_id = jr.assignment_id
            GROUP BY da.driver_id
        ) rejected ON rejected.driver_id = d.driver_id
        WHERE d.driver_name != %s
        ORDER BY d.driver_name
    """, (EMERGENCY_CONTRACTOR_NAME,))
    rows = cursor.fetchall()
    headers = ['Driver', 'Jobs Given', 'Jobs Done', 'Jobs Rejected', 'Acceptance Rate']
    data = []
    for r in rows:
        given = int(r['jobs_given'] or 0)
        done = int(r['jobs_done'] or 0)
        rejected = int(r['jobs_rejected'] or 0)
        rate = f"{round((done / given) * 100, 1)}%" if given else 'N/A'
        data.append([r['driver_name'], given, done, rejected, rate])
    return headers, data


def build_analytics_csv_zip(cursor):
    """Return a ZIP archive containing jobs, carrier performance, and employee metrics CSVs."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for filename, fetcher in (
            ('jobs.csv', fetch_jobs_sheet),
            ('carrier_performance.csv', fetch_carrier_performance_sheet),
            ('employee_metrics.csv', fetch_employee_metrics_sheet),
        ):
            headers, rows = fetcher(cursor)
            archive.writestr(filename, _csv_bytes(headers, rows))
    buffer.seek(0)
    return buffer
