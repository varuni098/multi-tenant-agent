import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "multi_tenant_agent_db")
}

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def get_tenant_page_theme(tenant_uuid: str, page_name: str) -> str:
    """Gets current theme for a specific tenant UUID and page name."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    query = """
        SELECT event_id, theme 
        FROM tenant_page_events 
        WHERE tenant_uuid = %s AND LOWER(page_name) = LOWER(%s)
    """
    cursor.execute(query, (tenant_uuid, page_name))
    result = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if result:
        return f"Tenant {tenant_uuid} page '{page_name}' (Event ID: {result['event_id']}) is currently in {result['theme']} mode."
    return f"No page entry found for page '{page_name}' under tenant {tenant_uuid}."

def toggle_tenant_page_theme(tenant_uuid: str, page_name: str) -> str:
    """Toggles theme state between light and dark for a specific tenant page event."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # 1. Check current state and get event_id
    select_query = """
        SELECT event_id, theme 
        FROM tenant_page_events 
        WHERE tenant_uuid = %s AND LOWER(page_name) = LOWER(%s)
    """
    cursor.execute(select_query, (tenant_uuid, page_name))
    row = cursor.fetchone()
    
    if not row:
        cursor.close()
        conn.close()
        return f"Error: Could not find page '{page_name}' for tenant {tenant_uuid}."
    
    current_theme = row['theme']
    new_theme = "dark" if current_theme == "light" else "light"
    event_id = row['event_id']
    
    # 2. Update to new theme in MySQL
    update_query = "UPDATE tenant_page_events SET theme = %s WHERE event_id = %s"
    cursor.execute(update_query, (new_theme, event_id))
    conn.commit()
    
    cursor.close()
    conn.close()
    
    return f"Success: Page '{page_name}' (Event ID: {event_id}) for tenant {tenant_uuid} switched from {current_theme} to {new_theme} mode."

def get_all_tenant_pages() -> list[dict]:
    """Fetch all tenant page records from MySQL for validation."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        query = "SELECT tenant_uuid, page_name, theme FROM tenant_page_events"
        cursor.execute(query)
        result = cursor.fetchall()
        return result
    finally:
        cursor.close()
        conn.close()

# --- Quick Test Run ---
if __name__ == "__main__":
    test_uuid = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
    test_page = "login"
    
    print("Testing Get Theme:")
    print(get_tenant_page_theme(test_uuid, test_page))
    
    print("\nTesting Toggle Theme:")
    print(toggle_tenant_page_theme(test_uuid, test_page))
    
    print("\nChecking Theme After Toggle:")
    print(get_tenant_page_theme(test_uuid, test_page))