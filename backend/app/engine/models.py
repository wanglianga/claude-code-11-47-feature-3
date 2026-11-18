"""引擎侧不可变数据模型。

路网版本在资料截止点被物化为不可变快照（GraphSnapshot），引擎只面对快照，
不直接读实时表，从而保证“证据截止时间”语义与缓存可重复性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    x: float
    y: float
    allow_wait: bool


@dataclass(frozen=True)
class RoadEvent:
    """封路或分时限速。effective_to 为 None 表示尚未恢复/长期有效。

    direction 仅对封路有意义：forward 只阻断该有向边的通行方向，reverse 记录
    反向车道结论（引擎沿有向边行进时不受其阻断），both 表示来源未给方向。
    origin 区分人工登记事件与证据融合派生事件，便于审计回溯。
    """

    event_id: str
    event_type: str  # closure | speed_limit
    edge_id: str
    effective_from: datetime
    effective_to: Optional[datetime]
    speed_limit_kmh: Optional[float]
    revision_no: int
    direction: str = "both"  # forward | reverse | both
    origin: str = "manual"  # manual | derived


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_node: str
    to_node: str
    length_m: float
    base_speed_kmh: float
    events: tuple[RoadEvent, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OpenWindow:
    open_from: datetime
    open_to: Optional[datetime]  # None 表示长期开放


@dataclass(frozen=True)
class Shelter:
    shelter_id: str
    name: str
    node_id: str
    x: float
    y: float
    windows: tuple[OpenWindow, ...]


@dataclass(frozen=True)
class GraphSnapshot:
    version_id: int
    cutoff_at: datetime
    nodes: dict[str, Node]
    edges: tuple[Edge, ...]
    shelters: tuple[Shelter, ...]
    adjacency: dict[str, tuple[Edge, ...]]
    edge_fingerprint: str
    event_fingerprint: str


@dataclass(frozen=True)
class WaitSegment:
    node_id: str
    waited_from: datetime
    waited_until: datetime
    reason: str  # closure | shelter_closed

    @property
    def wait_seconds(self) -> float:
        return (self.waited_until - self.waited_from).total_seconds()


@dataclass(frozen=True)
class EdgeWindow:
    """一条有向边的实际通行时间窗 [t_entry, t_exit)，供证据时间窗相交判定。"""

    edge_id: str
    t_entry: datetime
    t_exit: datetime


@dataclass(frozen=True)
class NodeLabel:
    node_id: str
    arrival_time: datetime
    total_wait_seconds: float
    node_path: tuple[str, ...]
    edge_path: tuple[str, ...]
    waits: tuple[WaitSegment, ...]
    edge_windows: tuple[EdgeWindow, ...] = field(default_factory=tuple)
