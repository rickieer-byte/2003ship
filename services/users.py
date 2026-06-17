"""User authentication and registration database operations."""

def get_role_id_by_name(cursor, role_name):
    cursor.execute("SELECT role_id FROM roles WHERE role_name = %s", (role_name,))
    row = cursor.fetchone()
    return row['role_id'] if row else 1

def create_user(cursor, username, email, password_hash, role_id):
    cursor.execute(
        "INSERT INTO users (username, email, password_hash, role_id) VALUES (%s, %s, %s, %s)",
        (username, email, password_hash, role_id)
    )

def get_user_by_username(cursor, username):
    cursor.execute("""
        SELECT u.*, r.role_name AS role
        FROM users u
        JOIN roles r ON r.role_id = u.role_id
        WHERE u.username = %s
    """, (username,))
    return cursor.fetchone()
