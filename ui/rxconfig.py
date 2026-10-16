import os

import reflex as rx

# Dev: Vite on 3000 and the Reflex backend on 3001 (the browser talks to both).
# Prod: `reflex run --env prod --single-port` serves everything on 3000.
config = rx.Config(
    app_name="agent_ui",
    api_url=os.environ.get("UI_PUBLIC_API_URL", "http://localhost:3001"),
    frontend_port=3000,
    backend_port=int(os.environ.get("UI_BACKEND_PORT", "3001")),
    telemetry_enabled=False,
    show_built_with_reflex=False,
    disable_plugins=[rx.plugins.SitemapPlugin],
    plugins=[rx.plugins.RadixThemesPlugin(theme=rx.theme(accent_color="indigo"))],
)
