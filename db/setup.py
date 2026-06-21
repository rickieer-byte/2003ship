#!/usr/bin/env python3
"""Apply db/schema.sql and db/seed.sql using credentials from .env or CLI."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
pymysql.install_as_MySQLdb()

import MySQLdb
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

ROOT = os.path.dirname(os.path.abspath(__file__))


def split_sql(text):
    statements = []
    buf = []
    delimiter = ';'
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('--') or not stripped:
            continue
        if stripped.lower().startswith('delimiter '):
            delimiter = stripped.split()[1]
            continue
        buf.append(line)
        if stripped.endswith(delimiter):
            stmt = '\n'.join(buf)
            if stmt.endswith(delimiter):
                stmt = stmt[:-len(delimiter)]
            statements.append(stmt)
            buf = []
    return statements


def run_file(cursor, path):
    with open(path, encoding='utf-8') as fh:
        for stmt in split_sql(fh.read()):
            cursor.execute(stmt)


def run_setup(host, user, password, database):
    print(f'Connecting to MySQL at {host} as {user}…')
    try:
        conn = MySQLdb.connect(host=host, user=user, password=password, charset='utf8mb4')
    except MySQLdb.OperationalError as exc:
        print(f'Connection failed: {exc}')
        raise exc

    cursor = conn.cursor()
    try:
        print('Applying schema.sql…')
        run_file(cursor, os.path.join(ROOT, 'schema.sql'))
        conn.commit()
        conn.select_db(database)
        print('Applying seed.sql…')
        run_file(cursor, os.path.join(ROOT, 'seed.sql'))
        print('Applying migrate_12h_shifts.sql...')
        run_file(cursor, os.path.join(ROOT, 'migrate_12h_shifts.sql'))
        conn.commit()
        print(f'Database "{database}" is ready.')
    finally:
        cursor.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Create escalation_db schema and load seed data.')
    parser.add_argument('--host', default=os.getenv('DB_HOST'))
    parser.add_argument('--user', default=os.getenv('DB_USER'))
    parser.add_argument('--password', default=os.getenv('DB_PASSWORD'))
    parser.add_argument('--database', default=os.getenv('DB_NAME'))
    args = parser.parse_args()

    try:
        run_setup(args.host, args.user, args.password, args.database)
    except Exception as exc:
        print(f"Error during setup: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

