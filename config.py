import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

def _bool(val, default=False):
    if val is None:
        return default
    return str(val).lower() in ('1', 'true', 'yes', 'on')

class Config:
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-fallback-key')
    MYSQL_HOST = os.getenv('DB_HOST', 'localhost')
    MYSQL_USER = os.getenv('DB_USER', 'root')
    MYSQL_PASSWORD = os.getenv('DB_PASSWORD', '')
    MYSQL_DB = os.getenv('DB_NAME', 'escalation_db')
    MYSQL_CURSORCLASS = 'DictCursor'

    SIMULATION_MODE = _bool(os.getenv('SIMULATION_MODE', 'true'), default=True)
    GEOFENCE_RADIUS_KM = float(os.getenv('GEOFENCE_RADIUS_KM', '2.0'))
    SIM_TICK_STALE_SECONDS = int(os.getenv(
        'SIM_TICK_STALE_SECONDS',
        '3' if _bool(os.getenv('SIMULATION_MODE', 'true'), default=True) else '120',
    ))

    SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL', '')
    SMTP_HOST = os.getenv('SMTP_HOST', '')
    SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
    SMTP_USE_TLS = _bool(os.getenv('SMTP_USE_TLS', 'true'), default=True)
    SMTP_USER = os.getenv('SMTP_USER', '')
    SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
    ALERT_EMAIL_FROM = os.getenv('ALERT_EMAIL_FROM', 'alerts@logistics.local')
    ALERT_EMAIL_TO = os.getenv('ALERT_EMAIL_TO', '')
