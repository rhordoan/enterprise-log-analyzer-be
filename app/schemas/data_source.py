from __future__ import annotations

from typing import Any, Dict, Literal

from pydantic import BaseModel, Field


SourceType = Literal["filetail", "splunk", "datadog", "thousandeyes", "snmp", "dcim_http", "telegraf"]


class DataSourceBase(BaseModel):
    name: str = Field(..., max_length=128)
    type: SourceType
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class DataSourceCreate(DataSourceBase):
    pass


class DataSourceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    config: Dict[str, Any] | None = None


class DataSourceOut(DataSourceBase):
    id: int
    # Returned only on creation for telegraf sources
    one_time_token: str | None = None
    one_time_agent_id: str | None = None

    class Config:
        orm_mode = True


