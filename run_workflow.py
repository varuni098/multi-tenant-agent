"""Interactive CLI, authenticated with a user JWT just like the API.

TOKEN=$(docker compose exec -T tenant-mcp python -m app.harness.dev_token admin@acme.test)
docker compose exec app python run_workflow.py --token "$TOKEN"
"""

import argparse
import os
import sys
import uuid

from app.core.logging import setup_logging
from app.harness.auth import AuthError, authenticate
from app.harness.tenant_client import ConfirmationError, TenantServiceUnavailable, confirm
from app.workflow.graph import run_workflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=os.environ.get("AGENT_TOKEN"), help="user JWT (or set AGENT_TOKEN)")
    args = parser.parse_args()
    if not args.token:
        parser.error("a user JWT is required: --token or AGENT_TOKEN")

    try:
        caller = authenticate(args.token)
    except (AuthError, TenantServiceUnavailable) as e:
        print(f"Login failed: {e}")
        return 1
    identity = caller.identity
    session_id = uuid.uuid4().hex

    setup_logging()
    print("==================================================")
    print(f" Multi-Tenant Agent: {identity.user_email} ({identity.role}) @ {identity.tenant_id}")
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
            result = run_workflow(user_input, caller, request_id, session_id)
            print(f"\nAgent Response:\n{result.answer}")
            for action in result.actions:
                print(f"  - {action.message}")
            if result.confirmation and input("Confirm? [y/N] ").strip().lower() == "y":
                print(confirm(identity, result.confirmation.token, request_id))
            print("-" * 50)
        except (ConfirmationError, TenantServiceUnavailable) as e:
            print(f"Not done: {e}")
        except Exception as e:
            print(f"An error occurred: {e}")


if __name__ == "__main__":
    sys.exit(main())
