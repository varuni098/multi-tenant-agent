import os

# Suppress the Pydantic AI startup banner
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"

from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from agent_backend import get_tenant_page_theme, toggle_tenant_page_theme

# Load environment variables from your .env file
load_dotenv()

# Initialize the Pydantic AI agent with an explicit directive to always use tools
agent = Agent(
    model="groq:openai/gpt-oss-120b", 
    system_prompt=(
        "You are a strict multi-tenant database assistant. "
        "CRITICAL RULE: You MUST always call your available tools to query or update the database. "
        "Never guess, assume, or hardcode theme statuses. Always invoke the tools for any theme check or toggle request."
    )
)

@agent.tool
def tool_get_theme(ctx: RunContext, tenant_uuid: str, page_name: str) -> str:
    """Get the current theme status (light/dark) for a specific tenant UUID and page name."""
    db_result = get_tenant_page_theme(tenant_uuid, page_name)
    return db_result

@agent.tool
def tool_toggle_theme(ctx: RunContext, tenant_uuid: str, page_name: str) -> str:
    """Toggle the theme between light and dark mode for a specific tenant page event."""
    db_result = toggle_tenant_page_theme(tenant_uuid, page_name)
    return db_result

# --- Interactive Terminal Loop ---
"""
if __name__ == "__main__":
    print("==================================================")
    print(" Multi-Tenant Agent Initialized (Groq)!")
    print(" Type your request below (or type 'exit' to quit)")
    print("==================================================")
    
    while True:
        try:
            user_input = input("\nEnter your prompt: ")
            
            if user_input.lower() in ["exit", "quit"]:
                print("Exiting agent. Goodbye!")
                break
                
            if not user_input.strip():
                continue
                
            print("Thinking...")
            result = agent.run_sync(user_input)
            
            print(f"\nAgent Response:\n{result.output}")
            print("-" * 50)
            
        except Exception as e:
            print(f"An error occurred: {e}")
"""