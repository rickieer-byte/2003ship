import datetime
import json
import smtplib
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from config import Config


def _alert_recently_sent(cursor, alert_type, container_number, cooldown_minutes=60):
    cursor.execute("""
        SELECT alert_id FROM alerts_log
        WHERE alert_type = %s AND container_number <=> %s
          AND sent_at > DATE_SUB(NOW(), INTERVAL %s MINUTE)
        LIMIT 1
    """, (alert_type, container_number, cooldown_minutes))
    return cursor.fetchone() is not None


def _log_alert(cursor, alert_type, container_number, message, channel):
    cursor.execute(
        "INSERT INTO alerts_log (alert_type, container_number, message, channel) VALUES (%s, %s, %s, %s)",
        (alert_type, container_number, message, channel),
    )


def send_email(subject, body):
    if not Config.ALERT_EMAIL_TO or not Config.SMTP_HOST:
        return False
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = Config.ALERT_EMAIL_FROM
    msg['To'] = Config.ALERT_EMAIL_TO
    with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
        if Config.SMTP_USE_TLS:
            server.starttls()
        if Config.SMTP_USER and Config.SMTP_PASSWORD:
            server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
        server.send_message(msg)
    return True


def send_slack(message):
    if not Config.SLACK_WEBHOOK_URL:
        return False
    data = json.dumps({'text': message}).encode('utf-8')
    req = urllib.request.Request(
        Config.SLACK_WEBHOOK_URL, data=data, headers={'Content-Type': 'application/json'},
    )
    urllib.request.urlopen(req, timeout=10)
    return True


def check_escalation_alerts(cursor):
    cursor.execute("""
        SELECT c.container_number, c.lfd_datetime, v.vessel_name
        FROM containers c
        JOIN voyages vy ON vy.voyage_id = c.voyage_id
        JOIN vessels v ON v.vessel_id = vy.vessel_id
        LEFT JOIN truck_allocations t ON t.container_number = c.container_number
        WHERE (t.dispatch_status_code IS NULL OR t.dispatch_status_code = 'Pending')
          AND TIMESTAMPDIFF(HOUR, NOW(), c.lfd_datetime) <= 12
    """)
    red_containers = cursor.fetchall()
    if not red_containers:
        return {'sent': 0}

    cursor.execute("SELECT COUNT(*) AS cnt FROM drivers WHERE status_code = 'Available'")
    available = cursor.fetchone()['cnt']
    sent = 0
    for row in red_containers:
        alert_type = 'RED_NO_DISPATCH'
        if _alert_recently_sent(cursor, alert_type, row['container_number']):
            continue
        msg = (
            f"ESCALATION: Container {row['container_number']} ({row['vessel_name']}) "
            f"is RED — LFD {row['lfd_datetime']}. Available drivers: {available}. "
            f"Consider emergency dispatch."
        )
        channels = []
        try:
            if send_slack(msg):
                channels.append('slack')
        except (urllib.error.URLError, OSError):
            pass
        try:
            if send_email('[Logistics Alert] RED container — no dispatch', msg):
                channels.append('email')
        except (smtplib.SMTPException, OSError):
            pass
        if channels:
            _log_alert(cursor, alert_type, row['container_number'], msg, ','.join(channels))
            sent += 1
        elif not Config.SLACK_WEBHOOK_URL and not Config.ALERT_EMAIL_TO:
            _log_alert(cursor, alert_type, row['container_number'], msg, 'logged_only')
            sent += 1
    return {'sent': sent, 'red_count': len(red_containers)}
