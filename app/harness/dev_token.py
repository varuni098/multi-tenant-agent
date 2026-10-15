"""Print a development JWT for a seeded user.

python -m app.harness.dev_token admin@acme.test [--tenant UUID] [--minutes 60]
"""

import argparse
import sys

from app.harness.auth import create_token
from app.services.tenants import find_user_tenants


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("email")
    parser.add_argument("--tenant", help="tenant UUID, if the user belongs to several")
    parser.add_argument("--minutes", type=int, default=None)
    args = parser.parse_args()

    memberships = [m for m in find_user_tenants(args.email) if not args.tenant or m[0] == args.tenant]
    if len(memberships) != 1:
        print(f"Expected exactly one tenant for {args.email}, found {len(memberships)}. Use --tenant.", file=sys.stderr)
        return 1
    tenant_id, role = memberships[0]
    print(create_token(tenant_id, args.email, role, args.minutes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
