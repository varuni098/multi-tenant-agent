"""Agent UI: chat with a live tool trace and confirmations, a settings browser, and the audit log."""

import reflex as rx
from reflex.style import set_color_mode

from agent_ui.state import Message, PagePreview, State


def nav() -> rx.Component:
    return rx.hstack(
        rx.box(width="12px", height="12px", border_radius="3px", background=State.brand_color),
        rx.heading("Tenant agent", size="4"),
        rx.link("Chat", href="/chat"),
        rx.link("Settings", href="/settings"),
        rx.cond(State.is_admin, rx.link("Audit", href="/audit")),
        rx.spacer(),
        rx.tooltip(
            rx.badge(rx.cond(State.live, "live", "offline"), color_scheme=rx.cond(State.live, "green", "gray")),
            content="Live updates from /v1/events",
        ),
        rx.text(State.email, " · ", State.role, size="2", color_scheme="gray"),
        rx.button("Sign out", on_click=State.logout, variant="soft", size="1"),
        align="center",
        spacing="4",
        padding="12px 20px",
        border_bottom="1px solid var(--gray-a5)",
        width="100%",
    )


def follow_tenant_theme() -> rx.Component:
    """The UI wears the effective theme of the chosen tenant page, so live changes show instantly.

    Color mode is a browser-side setting in Reflex. Each branch below mounts when the theme
    flips to it, and its on_mount applies that mode.
    """
    return rx.cond(
        State.appearance == "dark",
        rx.box(display="none", on_mount=set_color_mode("dark")),
        rx.fragment(rx.box(display="none", on_mount=set_color_mode("light"))),
    )


def layout(*children: rx.Component) -> rx.Component:
    return rx.box(
        follow_tenant_theme(),
        nav(),
        rx.box(*children, padding="20px", max_width="1100px", margin="0 auto"),
        min_height="100vh",
    )


# --- Login ---


def login_page() -> rx.Component:
    return rx.center(
        rx.card(
            rx.vstack(
                rx.heading("Sign in", size="6"),
                rx.cond(
                    State.dev_login,
                    rx.vstack(
                        rx.text("Development users (seeded data)", size="2", color_scheme="gray"),
                        rx.select(
                            State.dev_options,
                            placeholder="Choose a user",
                            value=State.dev_choice,
                            on_change=State.set_dev_choice,
                            width="100%",
                        ),
                        rx.button("Sign in as this user", on_click=State.login_dev, width="100%"),
                        rx.divider(),
                        width="100%",
                    ),
                ),
                rx.text("Or paste a bearer token", size="2", color_scheme="gray"),
                rx.text_area(value=State.token_input, on_change=State.set_token_input, width="100%", rows="3"),
                rx.button("Sign in with token", on_click=State.login_token, variant="outline", width="100%"),
                rx.cond(State.login_error != "", rx.callout(State.login_error, color_scheme="red", width="100%")),
                width="360px",
                spacing="3",
            )
        ),
        height="100vh",
    )


# --- Chat ---


def trace_line(line: str) -> rx.Component:
    return rx.text(line, size="1", font_family="monospace", color_scheme="gray")


def message_view(m: Message) -> rx.Component:
    return rx.match(
        m.role,
        (
            "user",
            rx.box(
                rx.text(m.text),
                align_self="flex-end",
                background="var(--accent-a3)",
                padding="8px 12px",
                border_radius="10px",
                max_width="75%",
            ),
        ),
        (
            "notice",
            rx.callout(m.text, size="1", color_scheme="gray", width="100%"),
        ),
        rx.card(
            rx.vstack(
                rx.cond(m.trace.length() > 0, rx.vstack(rx.foreach(m.trace, trace_line), spacing="0")),
                rx.cond(m.working, rx.hstack(rx.spinner(size="1"), rx.text("Working…", size="2"))),
                rx.cond(m.text != "", rx.markdown(m.text)),
                rx.cond(
                    m.citations.length() > 0,
                    rx.vstack(
                        rx.text("Sources", size="1", weight="bold"),
                        rx.foreach(m.citations, lambda c: rx.text(c, size="1", color_scheme="gray")),
                        spacing="0",
                    ),
                ),
                spacing="2",
                align="start",
            ),
            max_width="85%",
        ),
    )


def confirm_dialog() -> rx.Component:
    return rx.alert_dialog.root(
        rx.alert_dialog.content(
            rx.alert_dialog.title("Confirm this action"),
            rx.alert_dialog.description(State.confirm_summary),
            rx.hstack(
                rx.alert_dialog.cancel(rx.button("Cancel", variant="soft", on_click=State.cancel_confirm)),
                rx.alert_dialog.action(rx.button("Confirm", color_scheme="red", on_click=State.confirm)),
                justify="end",
                spacing="3",
                margin_top="16px",
            ),
        ),
        open=State.confirm_open,
    )


def chat_page() -> rx.Component:
    return layout(
        rx.vstack(
            rx.hstack(
                rx.heading("Chat", size="5"),
                rx.spacer(),
                rx.button("New chat", on_click=State.new_chat, variant="soft", size="1"),
                width="100%",
            ),
            rx.vstack(rx.foreach(State.messages, message_view), width="100%", spacing="3", min_height="50vh"),
            rx.form(
                rx.hstack(
                    rx.input(
                        name="prompt",
                        placeholder="e.g. make the login page dark, or: what does the pro plan include?",
                        disabled=State.busy,
                        width="100%",
                    ),
                    rx.button("Send", type="submit", loading=State.busy),
                    width="100%",
                ),
                on_submit=State.send,
                reset_on_submit=True,
                width="100%",
            ),
            confirm_dialog(),
            spacing="4",
            width="100%",
        )
    )


# --- Settings ---


def preview_card(p: PagePreview) -> rx.Component:
    dark = p.theme == "dark"
    return rx.box(
        rx.hstack(
            rx.box(width="10px", height="10px", border_radius="50%", background=p.brand_color),
            rx.text(p.name, weight="bold", size="2"),
            align="center",
        ),
        rx.text(p.theme, " · ", p.layout, " · ", p.language, size="1"),
        padding=rx.cond(p.layout == "compact", "8px", "14px"),
        border_radius="8px",
        border="1px solid var(--gray-a6)",
        background=rx.cond(dark, "#111827", "#ffffff"),
        color=rx.cond(dark, "#f9fafb", "#111827"),
        min_width="170px",
    )


def settings_page() -> rx.Component:
    return layout(
        rx.vstack(
            rx.hstack(
                rx.heading("Settings", size="5"),
                rx.spacer(),
                rx.text("UI follows", size="2"),
                rx.select(State.page_options, value=State.appearance_page, on_change=State.set_appearance_page),
                rx.button("Refresh", on_click=State.refresh_settings, variant="soft", size="1"),
                width="100%",
                align="center",
            ),
            rx.text(
                "Changes made anywhere (chat, MCP clients, other users) appear here as they commit.",
                size="2",
                color_scheme="gray",
            ),
            rx.flex(rx.foreach(State.previews, preview_card), wrap="wrap", spacing="3"),
            rx.table.root(
                rx.table.header(
                    rx.table.row(*(rx.table.column_header_cell(h) for h in ("Page", "Key", "Value", "Source")))
                ),
                rx.table.body(
                    rx.foreach(
                        State.setting_rows,
                        lambda r: rx.table.row(
                            rx.table.cell(r[0]), rx.table.cell(r[1]), rx.table.cell(r[2]), rx.table.cell(r[3])
                        ),
                    )
                ),
                width="100%",
                size="1",
            ),
            spacing="4",
            width="100%",
        )
    )


# --- Audit ---


def audit_page() -> rx.Component:
    return layout(
        rx.vstack(
            rx.hstack(
                rx.heading("Audit log", size="5"),
                rx.spacer(),
                rx.segmented_control.root(
                    rx.segmented_control.item("Setting changes", value="changes"),
                    rx.segmented_control.item("Tool calls", value="tool_calls"),
                    value=State.audit_kind,
                    on_change=State.set_audit_kind,
                ),
                rx.button("Refresh", on_click=State.load_audit, variant="soft", size="1"),
                width="100%",
                align="center",
            ),
            rx.cond(State.audit_error != "", rx.callout(State.audit_error, color_scheme="red")),
            rx.scroll_area(
                rx.table.root(
                    rx.table.header(rx.table.row(rx.foreach(State.audit_columns, rx.table.column_header_cell))),
                    rx.table.body(
                        rx.foreach(
                            State.audit_rows,
                            lambda row: rx.table.row(rx.foreach(row, lambda c: rx.table.cell(c))),
                        )
                    ),
                    size="1",
                    width="100%",
                ),
                type="auto",
                scrollbars="horizontal",
            ),
            rx.cond(State.audit_has_more, rx.button("Older", on_click=State.audit_more, variant="soft")),
            spacing="4",
            width="100%",
        )
    )


app = rx.App()
app.add_page(login_page, route="/", title="Sign in")
app.add_page(chat_page, route="/chat", title="Chat", on_load=State.require_login)
app.add_page(settings_page, route="/settings", title="Settings", on_load=State.require_login)
app.add_page(audit_page, route="/audit", title="Audit", on_load=State.require_admin)
