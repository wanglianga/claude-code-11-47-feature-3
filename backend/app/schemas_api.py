"""请求/响应模型。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScenarioCreate(BaseModel):
    name: str = "临时情景"
    population_as_of: date
    departure_time: datetime
    max_trip_minutes: int = Field(gt=0, le=600)
    road_cutoff: datetime
    boundary_id: Optional[str] = None
    viewer_id: str = "analyst-ui"
    note: Optional[str] = None


class ViewerRelease(BaseModel):
    viewer_id: str


class CompareRequest(BaseModel):
    task_a_id: str
    task_b_id: str


class PublishRequest(BaseModel):
    task_a_id: str
    task_b_id: str
    title: str
    created_by: str = "analyst"


class PrecomputeRequest(BaseModel):
    population_as_of: date
    base_day: datetime
    cutoff: datetime
    deadline_minutes: int = 60
    offsets_minutes: list[int] = [0, 30, 60, 90, 120]
    boundary_id: Optional[str] = None


class InvalidateRequest(BaseModel):
    edge_id: str
    reason: str = "道路事件修订"


class RebuildRequest(BaseModel):
    new_cutoff: datetime


class EventCreate(BaseModel):
    """临时封路/限速：立即参与后续即时计算。"""

    event_id: str
    event_type: str  # closure | speed_limit
    edge_id: str
    effective_from: datetime
    effective_to: Optional[datetime] = None
    speed_limit_kmh: Optional[float] = None
    source_title: str = "临时上报事件"
    raw_text: str = ""


class EvidenceIngest(BaseModel):
    """道路通行证据接入：保留来源坐标、观测/上传时间、方向、阈值、定位误差。"""

    evidence_id: str
    source_type: str  # water_sensor | inspection_photo | crowd_report
    source_ref: str
    source_org: Optional[str] = None
    geom_x: float
    geom_y: float
    location_error_m: float = Field(ge=0)
    location_text: Optional[str] = None
    observed_at: datetime
    uploaded_at: datetime
    bearing_deg: Optional[float] = Field(default=None, ge=0, lt=360)
    threshold_value: Optional[float] = None
    observed_value: Optional[float] = None
    observed_recovery_at: Optional[datetime] = None
    offline: bool = False
    threshold_unit: Optional[str] = "cm"
    raw_text: str = ""
    change_note: Optional[str] = None


class FusionRunRequest(BaseModel):
    strategy: Optional[str] = None  # 缺省取当前活动策略
    cutoff_at: datetime


class FusionStrategyRequest(BaseModel):
    strategy: str  # official_only | multi_source
