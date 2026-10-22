#!/usr/bin/env python3
"""Create a consistent SQLite backup, including while the bot is running."""
import argparse
import os
from pathlib import Path
import sqlite3


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', help='Existing database, e.g. /var/data/leads.sqlite3')
    parser.add_argument('destination', help='New backup filename; must not already exist')
    args = parser.parse_args()
    source = Path(args.database).resolve()
    destination = Path(args.destination).resolve()
    if not source.is_file():
        parser.error('Source database does not exist.')
    if destination.exists():
        parser.error('Destination exists; choose a new filename.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the filename exclusively, rather than overwriting another backup.
    with destination.open('xb'):
        pass
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    print('Backup created:', destination)


if __name__ == '__main__':
    main()
