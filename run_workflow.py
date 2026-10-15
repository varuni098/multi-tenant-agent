"""Interactive CLI for a local operator acting as a seeded user.

python run_workflow.py --email admin@acme.test
docker compose exec app python run_workflow.py --email editor@acme.test
"""

import argparse
import sys
import uuid
from typing import cast

from app.core.logging import setup_logging
from app.harness.confirmations import ConfirmationError, confirm
from app.harness.context import Identity, Role
from app.services.errors import DomainError
from app.services.tenants import find_user_tenants
from app.workflow.graph import run_workflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="admin@acme.test")
    parser.add_argument("--tenant", help="tenant UUID, if the user belongs to several")
    args = parser.parse_args()

    memberships = [m for m in find_user_tenants(args.email) if not args.tenant or m[0] == args.tenant]
    if len(memberships) != 1:
        print(f"Expected exactly one tenant for {args.email}, found {len(memberships)}. Use --tenant.")
        return 1
    tenant_id, role = memberships[0]
    identity = Identity(tenant_id=tenant_id, user_email=args.email, role=cast(Role, role))
    session_id = uuid.uuid4().hex

    setup_logging()
    print("==================================================")
    print(f" Multi-Tenant Agent: {args.email} ({role}) @ {tenant_id}")
    print(" Type your request below (or type 'exit' to quit)")
    print("==================================================")

    while True:
        try:
            user_input = input("\nEnter your prompt: ")
            if user_input.lower() in ["exit", "quit"]:
                print("Exiting. Goodbye!")
                return 0
            if not user_input.strip():
                continue

            request_id = uuid.uuid4().hex
            result = run_workflow(user_input, identity, request_id, session_id)
            print(f"\nAgent Response:\n{result.answer}")
            for action in result.actions:
                print(f"  - {action.message}")
            if result.confirmation and input("Confirm? [y/N] ").strip().lower() == "y":
                print(confirm(identity, result.confirmation.token, request_id).message)
            print("-" * 50)
        except (ConfirmationError, DomainError) as e:
            print(f"Not done: {e}")
        except Exception as e:
            print(f"An error occurred: {e}")


if __name__ == "__main__":
    sys.exit(main())
