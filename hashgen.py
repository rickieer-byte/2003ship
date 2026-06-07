from werkzeug.security import generate_password_hash

# This will print the exact string to paste into your SQL insert statement
print(generate_password_hash('password123'))