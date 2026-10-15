"""Interactive CLI: `python run_workflow.py` (or `docker compose exec app python run_workflow.py`)."""

from app.core.logging import setup_logging
from app.workflow.graph import run_workflow
from app.workflow.utils import extract_uuid

if __name__ == "__main__":
    setup_logging()
    print("==================================================")
    print(" Multi-Tenant Workflow Initialized (LangGraph)")
    print(" Type your request below (or type 'exit' to quit)")
    print("==================================================")

    while True:
        try:
            user_input = input("\nEnter your prompt: ")

            if user_input.lower() in ["exit", "quit"]:
                print("Exiting workflow. Goodbye!")
                break

            if not user_input.strip():
                continue

            print("\n--- Invoking LangGraph Tenant Workflow ---")
            final_state = run_workflow(user_input, extract_uuid(user_input))

            print("\n--- Workflow Output ---")
            print(f"Agent Response:\n{final_state['agent_response']}")
            print("-" * 50)

        except Exception as e:
            print(f"An error occurred: {e}")
