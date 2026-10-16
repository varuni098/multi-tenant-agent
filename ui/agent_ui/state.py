"""UI state. The bearer token is a backend-only var (leading underscore), so it never reaches the browser."""

import asyncio
import contextlib
import dataclasses
import json
import uuid
from typing import Any

import reflex as rx

from agent_ui import api

TOOL_OUTCOME = {
    "ok": "done",
    "changed": "changed",
    "denied": "denied by policy",
    "needs_confirmation": "needs confirmation",
    "error": "failed",
}
UI_DEFAULT = "(tenant defaults)"


@dataclasses.dataclass
class Message:
    role: str  # user | assistant | notice
    text: str = ""
    trace: list[str] = dataclasses.field(default_factory=list)
    citations: list[str] = dataclasses.field(default_factory=list)
    working: bool = False


@dataclasses.dataclass
class PagePreview:
    name: str
    theme: str
    layout: str
    brand_color: str
    language: str


def _fmt(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={_fmt(v)}" for k, v in args.items())


class State(rx.State):
    _token: str = ""

    # Identity
    email: str = ""
    role: str = ""
    tenant_id: str = ""
    login_error: str = ""
    dev_choice: str = ""
    token_input: str = ""

    # Chat
    session_id: str = ""
    messages: list[Message] = []
    busy: bool = False
    confirm_open: bool = False
    confirm_summary: str = ""
    _confirm_token: str = ""

    # Settings and appearance (the UI wears the settings of the page picked in `appearance_page`)
    setting_rows: list[list[str]] = []
    previews: list[PagePreview] = []
    page_options: list[str] = [UI_DEFAULT]
    appearance_page: str = UI_DEFAULT
    appearance: str = "light"
    brand_color: str = "#2563eb"
    _effective: dict[str, dict[str, Any]] = {}
    live: bool = False
    _watching: bool = False

    # Audit
    audit_kind: str = "changes"
    audit_columns: list[str] = []
    audit_rows: list[list[str]] = []
    _audit_next: int | None = None
    audit_has_more: bool = False
    audit_error: str = ""

    # --- Computed ---

    @rx.var
    def signed_in(self) -> bool:
        return bool(self.email)

    @rx.var
    def is_admin(self) -> bool:
        return self.role == "admin"

    @rx.var
    def dev_login(self) -> bool:
        return api.dev_login_enabled()

    @rx.var
    def dev_options(self) -> list[str]:
        return [u.label for u in api.dev_users()] if api.dev_login_enabled() else []

    # --- Login ---

    @rx.event
    def set_dev_choice(self, value: str) -> None:
        self.dev_choice = value

    @rx.event
    def set_token_input(self, value: str) -> None:
        self.token_input = value

    @rx.event
    async def login_dev(self):
        user = next((u for u in api.dev_users() if u.label == self.dev_choice), None)
        if user is None:
            self.login_error = "Pick a user first."
            return
        return await self._login(api.dev_token(user))

    @rx.event
    async def login_token(self):
        return await self._login(self.token_input.strip())

    async def _login(self, token: str):
        try:
            me = await api.get(token, "/v1/me")
        except api.ApiError as e:
            self.login_error = f"Sign-in failed: {e}"
            return
        self._token, self.token_input, self.login_error = token, "", ""
        self.email, self.role, self.tenant_id = me["email"], me["role"], me["tenant_id"]
        self.page_options = [UI_DEFAULT, *me["pages"]]
        self.appearance_page = "dashboard" if "dashboard" in me["pages"] else UI_DEFAULT
        self.session_id, self.messages = uuid.uuid4().hex, []
        return rx.redirect("/chat")

    @rx.event
    def logout(self):
        self.reset()
        return rx.redirect("/")

    @rx.event
    def require_login(self):
        if not self._token:
            return rx.redirect("/")
        return [State.refresh_settings, State.watch_live]

    # --- Chat ---

    @rx.event
    def new_chat(self) -> None:
        self.session_id, self.messages = uuid.uuid4().hex, []

    @rx.event(background=True)
    async def send(self, form: dict[str, Any]):
        prompt = str(form.get("prompt", "")).strip()
        async with self:
            if not prompt or self.busy or not self._token:
                return
            self.busy = True
            self.messages = [*self.messages, Message("user", prompt), Message("assistant", working=True)]
            token, session_id = self._token, self.session_id
        try:
            async for event, data in api.stream_chat(token, prompt, session_id):
                async with self:
                    self._apply_chat_event(event, data)
        except api.ApiError as e:
            async with self:
                self._update_last(text=f"Request failed: {e}", working=False)
        except Exception:
            async with self:
                self._update_last(text="Lost the connection to the agent. Please try again.", working=False)
        finally:
            async with self:
                self.busy = False

    def _update_last(self, **changes: Any) -> None:
        last = dataclasses.replace(self.messages[-1], **changes)
        self.messages = [*self.messages[:-1], last]

    def _apply_chat_event(self, event: str, data: Any) -> None:
        trace = list(self.messages[-1].trace)
        if event == "intent":
            trace.append(f"intent: {data['intent']}")
        elif event == "tool_start":
            trace.append(f"{data['tool']}({_args(data.get('args', {}))}) …")
        elif event == "tool_end":
            outcome = TOOL_OUTCOME.get(data["outcome"], data["outcome"])
            # Complete the matching "…" line
            for i in range(len(trace) - 1, -1, -1):
                if trace[i].startswith(f"{data['tool']}(") and trace[i].endswith(" …"):
                    trace[i] = f"{trace[i][:-2]} → {outcome}"
                    break
        elif event == "result":
            citations = [f"[{c['rank']}] {c['title']} › {c['heading']} ({c['path']})" for c in data["citations"]]
            trace += [f"blocked by policy: {d['tool']} ({d['rule']})" for d in data["denials"]]
            self._update_last(text=data["answer"], trace=trace, citations=citations, working=False)
            if data["requires_confirmation"]:
                self._confirm_token = data["confirmation"]["token"]
                self.confirm_summary = data["confirmation"]["summary"]
                self.confirm_open = True
            return
        elif event == "error":
            self._update_last(text=data.get("message", "The turn failed."), working=False)
            return
        self._update_last(trace=trace)

    @rx.event
    async def confirm(self):
        token, self._confirm_token, self.confirm_open = self._confirm_token, "", False
        try:
            body = await api.post(self._token, "/v1/chat/confirm", {"token": token})
            text = f"Confirmed: {body['message']}"
        except api.ApiError as e:
            text = f"Not confirmed: {e}"
        self.messages = [*self.messages, Message("notice", text)]

    @rx.event
    def cancel_confirm(self) -> None:
        self._confirm_token, self.confirm_open = "", False
        self.messages = [*self.messages, Message("notice", "Cancelled. Nothing was changed.")]

    # --- Settings, appearance and live updates ---

    @rx.event
    async def refresh_settings(self) -> None:
        if not self._token:
            return
        with contextlib.suppress(api.ApiError):
            self._apply_settings(await api.get(self._token, "/v1/settings"))

    def _apply_settings(self, view: dict[str, Any]) -> None:
        effective = {UI_DEFAULT: {s["key"]: s["value"] for s in view["defaults"]}}
        rows = [[UI_DEFAULT, s["key"], _fmt(s["value"]), s["source"]] for s in view["defaults"]]
        for page in view["pages"]:
            effective[page["name"]] = {s["key"]: s["value"] for s in page["settings"]}
            rows += [[page["name"], s["key"], _fmt(s["value"]), s["source"]] for s in page["settings"]]
        self._effective, self.setting_rows = effective, rows
        self.page_options = list(effective)
        self.previews = [
            PagePreview(name, str(v["theme"]), str(v["layout"]), str(v["brand_color"]), str(v["language"]))
            for name, v in effective.items()
        ]
        self._apply_appearance()

    def _apply_appearance(self) -> None:
        values = self._effective.get(self.appearance_page) or self._effective.get(UI_DEFAULT, {})
        self.appearance = "dark" if values.get("theme") == "dark" else "light"
        self.brand_color = str(values.get("brand_color", "#2563eb"))

    @rx.event
    def set_appearance_page(self, value: str) -> None:
        self.appearance_page = value
        self._apply_appearance()

    @rx.event(background=True)
    async def watch_live(self):
        """Follow /v1/events for as long as the user is signed in; reconnect when the stream ends."""
        async with self:
            if self._watching or not self._token:
                return
            self._watching = True
        try:
            while True:
                async with self:
                    token = self._token
                if not token:
                    return
                try:
                    async for event, data in api.live_events(token):
                        view = await api.get(token, "/v1/settings")  # outside the state lock
                        async with self:
                            if self._token != token:
                                return
                            self.live = True
                            self._apply_settings(view)
                        if event != "ready":
                            yield rx.toast.info(_describe(event, data), duration=4000)
                except api.ApiError as e:
                    if e.status == 401:  # token expired or revoked: sign out instead of retrying forever
                        async with self:
                            if self._token == token:
                                self._token, self.email, self.role = "", "", ""
                        yield rx.toast.warning("Your session expired. Please sign in again.")
                        yield rx.redirect("/")
                        return
                except Exception:
                    pass
                async with self:
                    self.live = False
                await asyncio.sleep(3)
        finally:
            async with self:
                self._watching, self.live = False, False

    # --- Audit ---

    @rx.event
    async def load_audit(self) -> None:
        await self._load_audit(more=False)

    async def _load_audit(self, more: bool) -> None:
        if not self._token:
            return
        params = {"kind": self.audit_kind, "limit": 25, "before_id": self._audit_next if more else None}
        try:
            body = await api.get(self._token, "/v1/audit", params)
        except api.ApiError as e:
            self.audit_error, self.audit_rows = str(e), []
            return
        self.audit_error = ""
        if self.audit_kind == "tool_calls":
            self.audit_columns = ["When", "Actor", "Channel", "Tool", "Args", "Outcome", "ms"]
            rows = [
                [
                    i["at"] or "",
                    i["actor"],
                    i["channel"],
                    i["tool"],
                    _args(i["args"] or {}),
                    i["outcome"],
                    str(i["duration_ms"]),
                ]
                for i in body["items"]
            ]
        else:
            self.audit_columns = ["When", "Actor", "Action", "Page", "Key", "Before", "After"]
            rows = [
                [
                    i["at"] or "",
                    i["actor"],
                    i["action"],
                    i["page_name"] or "(tenant)",
                    i["key"] or "",
                    _fmt(i["before"]) if i["before"] is not None else "inherited",
                    _fmt(i["after"]) if i["after"] is not None else "inherited",
                ]
                for i in body["items"]
            ]
        self.audit_rows = [*self.audit_rows, *rows] if more else rows
        self._audit_next = body["next_before_id"]
        self.audit_has_more = body["next_before_id"] is not None

    @rx.event
    async def set_audit_kind(self, value: str | list[str]) -> None:
        self.audit_kind = value if isinstance(value, str) else value[0]
        await self._load_audit(more=False)

    @rx.event
    async def audit_more(self) -> None:
        await self._load_audit(more=True)

    @rx.event
    def require_admin(self):
        if not self._token:
            return rx.redirect("/")
        if self.role != "admin":
            return rx.redirect("/chat")
        return [State.load_audit, State.watch_live]


def _describe(event: str, data: dict[str, Any]) -> str:
    who = data.get("actor") or "someone"
    if event == "setting.changed":
        where = data.get("page_name") or "tenant defaults"
        value = "inherited" if data.get("after") is None else _fmt(data["after"])
        return f"{who} set {data.get('key')} = {value} on {where}"
    if event == "page.created":
        return f"{who} created page {data.get('page_name')}"
    if event == "page.deleted":
        return f"{who} deleted page {data.get('page_name')}"
    return f"{who}: {event}"
