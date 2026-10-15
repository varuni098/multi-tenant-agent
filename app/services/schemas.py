from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

# Where an effective value came from, most specific first
Source = Literal["page", "tenant", "template", "default"]


class SettingValue(BaseModel):
    key: str
    value: Any
    source: Source


class PageInfo(BaseModel):
    name: str
    theme: str


class ChangeResult(BaseModel):
    page_name: str | None  # None = tenant-wide default
    key: str
    before: Any  # effective value before
    after: Any  # effective value after
    changed: bool
    message: str


class AuditEntry(BaseModel):
    id: int
    action: str
    page_name: str | None
    key: str | None
    before: Any
    after: Any
    actor: str
    at: datetime | None
