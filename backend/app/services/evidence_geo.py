"""证据空间匹配的平面几何工具（地方平面坐标系，单位米）。

只依赖道路节点坐标与有向边的起终点，把“来源点 → 边中心线”的投影过程完全
数值化并留痕：垂距、沿线距离、方向夹角均可解释，避免黑箱选边。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EdgeGeometry:
    edge_id: str
    from_node: str
    to_node: str
    length_m: float
    ax: float
    ay: float
    bx: float
    by: float

    @property
    def bearing_deg(self) -> float:
        """边 from→to 的方位角（度，0=正北，顺时针）。"""
        deg = math.degrees(math.atan2(self.bx - self.ax, self.by - self.ay))
        return deg % 360.0


@dataclass(frozen=True)
class ProjectionGeometry:
    edge_id: str
    perpendicular_m: float
    along_m: float
    edge_length_m: float
    inside_segment: bool


def point_to_edge(x: float, y: float, edge: EdgeGeometry) -> ProjectionGeometry:
    """点到有向边线段的投影：垂距 + 垂足沿线距离（垂足落在线段外时夹到端点）。"""
    dx, dy = edge.bx - edge.ax, edge.by - edge.ay
    len2 = dx * dx + dy * dy
    if len2 <= 0:
        return ProjectionGeometry(edge.edge_id, math.hypot(x - edge.ax, y - edge.ay),
                                  0.0, edge.length_m, False)
    t = ((x - edge.ax) * dx + (y - edge.ay) * dy) / len2
    t_clamped = min(1.0, max(0.0, t))
    px = edge.ax + t_clamped * dx
    py = edge.ay + t_clamped * dy
    perp = math.hypot(x - px, y - py)
    along = t_clamped * edge.length_m
    return ProjectionGeometry(edge.edge_id, perp, along, edge.length_m, 0.0 <= t <= 1.0)


def angular_diff(a: float, b: float) -> float:
    """两个方位角之差，归一化到 [0,180] 度。"""
    d = abs((a - b) % 360.0)
    return d if d <= 180.0 else 360.0 - d


def classify_direction(
    source_bearing: Optional[float], edge_bearing: float
) -> tuple[str, str, float]:
    """返回 (投影方向, 匹配类型, 方向权重)。

    来源未给方向（群众上报只描述地名）时投影为 both，权重折减；方向明确冲突
    时 direction_match=none，投影不被接受，但行保留以便解释“为何不够”。
    """
    if source_bearing is None:
        return "both", "unknown", 0.70
    d_forward = angular_diff(source_bearing, edge_bearing)
    d_reverse = angular_diff(source_bearing, edge_bearing + 180.0)
    if d_forward <= 45.0:
        return "forward", "exact", 1.0
    if d_reverse <= 45.0:
        return "reverse", "exact", 1.0
    # 有方向但与边两个方向都不符：不可用于该边
    return "forward" if d_forward <= d_reverse else "reverse", "none", 0.0


def spatial_weight(perpendicular_m: float, location_error_m: float) -> float:
    """空间误差折减：垂距为 0 时权重 1，垂距达到定位误差时降到 e^-1≈0.37。"""
    denom = max(location_error_m, 1.0)
    return math.exp(-perpendicular_m / denom)


def time_weight(upload_delay_seconds: float) -> float:
    """晚传折减：延迟 0 权重 1；延迟 2 小时 0.5；延迟越久越不可信，但不归零。"""
    delay_hours = max(upload_delay_seconds, 0.0) / 3600.0
    return 1.0 / (1.0 + delay_hours / 2.0)
