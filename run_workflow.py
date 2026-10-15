import os
import re
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from dotenv import load_dotenv
from tenant_workflow import app_graph

load_dotenv()

def extract_uuid(text: str) -> str:
    """Helper to extract a UUID pattern from the user's prompt text."""
    uuid_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    match = re.search(uuid_pattern, text)
    # Fallback default UUID if none is typed in the prompt
    return match.group(0) if match else "a1b2c3d4-e5f6-7890-abcd-ef0123456789"

if __name__ == "__main__":
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
                
            # Extract tenant UUID dynamically from the user input prompt
            tenant_uuid = extract_uuid(user_input)
            
            # Construct the initial state for LangGraph
            initial_state = {
                "prompt": user_input,
                "tenant_uuid": tenant_uuid,
                "is_valid": False,
                "agent_response": ""
            }
            
            print("\n--- Invoking LangGraph Tenant Workflow ---")
            final_state = app_graph.invoke(initial_state)
            
            print("\n--- Workflow Output ---")
            print(f"Agent Response:\n{final_state['agent_response']}")
            print("-" * 50)
            
        except Exception as e:
            print(f"An error occurred: {e}")