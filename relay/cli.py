"""Operator commands.

python -m relay.cli migrate
python -m relay.cli create-tenant "Crisp"
"""

import argparse
import sys

from relay.auth import create_tenant
from relay.config import load_settings
from relay.db import make_pool, migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations")
    tenant = sub.add_parser("create-tenant", help="create a tenant and print its API key once")
    tenant.add_argument("name")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.command == "migrate":
        applied = migrate(settings.database_url, settings.migrations_dir)
        print(f"applied migrations: {applied or 'none (already up to date)'}")
        return 0

    pool = make_pool(settings.database_url, min_size=1, max_size=1)
    try:
        key = create_tenant(pool, args.name)
    finally:
        pool.close()
    print(key)
    print("Store this key now. Relay keeps only its hash.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
