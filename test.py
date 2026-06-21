from app import app
from db.setup import run_setup
from config import Config
from app import mysql

with app.app_context():
    cursor = mysql.connection.cursor()
    try:
        run_setup(Config.MYSQL_HOST, Config.MYSQL_USER, Config.MYSQL_PASSWORD, Config.MYSQL_DB)
        cursor.execute("""
            INSERT INTO truck_allocations (container_number, urgency_score, dispatch_status_code)
            VALUES ('NYKU9012455', 95, 'Dispatched')
        """)
        cursor.execute("SELECT allocation_id FROM truck_allocations WHERE container_number = 'NYKU9012455'")
        alloc = cursor.fetchone()
        cursor.execute("INSERT INTO dispatch_assignments (allocation_id, driver_id, outcome_code) VALUES (%s, 1, 'pending')", (alloc['allocation_id'],))
        cursor.execute("UPDATE drivers SET status_code = 'On Delivery' WHERE driver_id = 1")
        mysql.connection.commit()
        print('SUCCESS')
    except Exception as e:
        print('FAILED:', e)
