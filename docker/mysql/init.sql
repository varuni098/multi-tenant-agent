-- Runs once, on first start of an empty mysql_data volume
CREATE DATABASE IF NOT EXISTS multi_tenant_agent_db;
USE multi_tenant_agent_db;

CREATE TABLE IF NOT EXISTS tenant_page_events (
    event_id    INT AUTO_INCREMENT PRIMARY KEY,
    tenant_uuid CHAR(36)               NOT NULL,
    page_name   VARCHAR(100)           NOT NULL,
    theme       ENUM('light', 'dark')  NOT NULL DEFAULT 'light',
    UNIQUE KEY uq_tenant_page (tenant_uuid, page_name)
);

-- Demo seed data
INSERT IGNORE INTO tenant_page_events (tenant_uuid, page_name, theme) VALUES
    ('a1b2c3d4-e5f6-7890-abcd-ef0123456789', 'login',     'light'),
    ('a1b2c3d4-e5f6-7890-abcd-ef0123456789', 'dashboard', 'dark'),
    ('b2c3d4e5-f6a7-8901-bcde-f01234567890', 'login',     'dark');
