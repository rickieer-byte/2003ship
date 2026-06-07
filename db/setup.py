#!/usr/bin/env python3
"""Apply db/schema.sql and db/seed.sql using credentials from .env or CLI."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MySQLdb
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

ROOT = os.path.dirname(os.path.abspath(__file__))


def split_sql(text):
    statements = []
    buf = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('--') or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(';'):
            statements.append('\n'.join(buf))
            buf = []
    return statements


def run_file(cursor, path):
    with open(path, encoding='utf-8') as fh:
        for stmt in split_sql(fh.read()):
            cursor.execute(stmt)


def main():
    parser = argparse.ArgumentParser(description='Create escalation_db schema and load seed data.')
    parser.add_argument('--host', default=os.getenv('DB_HOST', 'localhost'))
    parser.add_argument('--user', default=os.getenv('DB_USER', 'root'))
    parser.add_argument('--password', default=os.getenv('DB_PASSWORD', ''))
    parser.add_argument('--database', default=os.getenv('DB_NAME', 'escalation_db'))
    args = parser.parse_args()

    print(f'Connecting to MySQL at {args.host} as {args.user}…')
    try:
        conn = MySQLdb.connect(host=args.host, user=args.user, passwd=args.password, charset='utf8mb4')
    except MySQLdb.OperationalError as exc:
        print(f'Connection failed: {exc}')
        print('Set DB_PASSWORD in .env or pass --password (PythonAnywhere: use Databases tab credentials).')
        sys.exit(1)

    cursor = conn.cursor()
    try:
        print('Applying schema.sql…')
        run_file(cursor, os.path.join(ROOT, 'schema.sql'))
        conn.commit()
        conn.select_db(args.database)
        print('Applying seed.sql…')
        run_file(cursor, os.path.join(ROOT, 'seed.sql'))
        conn.commit()
        print(f'Database "{args.database}" is ready.')
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    main()
