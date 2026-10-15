from typing import Any


class DomainError(Exception):
    """Base for expected, user-explainable failures."""


class TenantNotFound(DomainError):
    def __init__(self, tenant_id: str):
        super().__init__(f"Tenant {tenant_id} does not exist.")


class PageNotFound(DomainError):
    def __init__(self, page_name: str, available: list[str]):
        super().__init__(f"Page '{page_name}' not found. Available pages: {', '.join(available) or 'none'}.")


class PageAlreadyExists(DomainError):
    def __init__(self, page_name: str):
        super().__init__(f"Page '{page_name}' already exists.")


class UnknownSettingKey(DomainError):
    def __init__(self, key: str, available: list[str]):
        super().__init__(f"Unknown setting '{key}'. Available settings: {', '.join(available)}.")


class InvalidSettingValue(DomainError):
    def __init__(self, key: str, value: Any, expected: str):
        super().__init__(f"Invalid value {value!r} for '{key}'. Expected {expected}.")


class NotEntitled(DomainError):
    def __init__(self, key: str, feature: str, plan: str):
        super().__init__(f"Setting '{key}' requires the '{feature}' feature, which the '{plan}' plan does not include.")


class TemplateNotFound(DomainError):
    def __init__(self, name: str, available: list[str]):
        super().__init__(f"Template '{name}' not found. Available templates: {', '.join(available)}.")


class NothingToUndo(DomainError):
    def __init__(self) -> None:
        super().__init__("There is no change to undo.")
