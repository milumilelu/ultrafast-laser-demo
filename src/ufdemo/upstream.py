"""上游仿真轮廓的适配与对照（C2）。

任务书 §3 的定位：**上游微观仿真提供另一套同工况参考**，
下游逐脉冲求解器负责实际加工预测，实验数据决定预测是否可信。

本模块只做三件事：

1. **读上游输出**（``upstream_case.json`` + ``profile.csv``），统一坐标/几何/工况；
2. **算对照指标**（中心深度、轮廓宽度、截面积、轮廓 RMSE）；
3. **拦住混用**：轴对称坑 ≠ 单线横截面 ≠ 完整单线表面，**不得互相当作对方**。

⚠️ 关键约束（任务书 §3.3）：

* 旧 Q4 源码用 ``exp[-(r/w_old)^2]``，本项目标准是 ``exp[-2r²/w²]`` ——
  适配时须 ``w = sqrt(2)·w_old``，**不得**把同名 ``w0`` 直接复制；
* ``target_depth_um``（累计连续去除目标）与 ``actual_mesh_depth_um``
  （删除单元后的网格表面）**分开保留**，不得只挑更接近的一列；
* 上游若为轴对称钻孔且忽略离焦，则**与下游定点多脉冲坑**比较，
  **不得**把它当成直线扫描沟槽。

本文件的 ``synthetic_*`` 函数生成**虚拟样例**，仅用于展示与端到端测试；
生成的输入带 ``is_synthetic=True``，报告里必须显示这一点。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import CONFIG_INVALID
from .errors import UFDemoError

#: 几何类型。**三者不可混用**（任务书 §3.2）。
GEOM_AXISYMMETRIC_PIT = "axisymmetric_pit"    # 轴对称坑（半径轴，非扫描方向）
GEOM_LINE_CROSS_SECTION = "line_cross_section"  # 单线横截面
GEOM_FULL_LINE_SURFACE = "full_line_surface"    # 完整单线表面（二维）

GEOMETRY_KINDS: tuple[str, ...] = (
    GEOM_AXISYMMETRIC_PIT, GEOM_LINE_CROSS_SECTION, GEOM_FULL_LINE_SURFACE,
)

#: 几何类型 → 中文说明（界面与报告直接显示，避免被读错）。
GEOMETRY_ZH: Mapping[str, str] = {
    GEOM_AXISYMMETRIC_PIT: "轴对称坑（径向剖面，**不是**扫描方向）",
    GEOM_LINE_CROSS_SECTION: "单线横截面",
    GEOM_FULL_LINE_SURFACE: "完整单线表面（二维）",
}


@dataclass
class UpstreamProfile:
    """一条上游数值轮廓。

    ``x`` 的单位由 ``x_unit`` 声明；内部统一按 **μm 的横向坐标** 与 **μm 的深度** 处理。
    """

    case_id: str
    geometry: str
    x_um: list[float]
    depth_um: list[float]
    target_depth_um: list[float] | None = None   # 累计连续去除目标（若上游给出）
    x_axis: str = "r"          # r（径向）| x（扫描方向）
    depth_sign: str = "positive_is_removal"
    unit_note: str = ""
    n_pulses: int | None = None
    n_passes: int | None = None
    material_family: str = ""
    material_grade: str | None = None
    laser: Mapping[str, Any] = field(default_factory=dict)
    focus: Mapping[str, Any] = field(default_factory=dict)
    upstream_version: str = ""
    source_file: str = ""
    is_synthetic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "geometry": self.geometry,
            "geometryZh": GEOMETRY_ZH.get(self.geometry, self.geometry),
            "xAxis": self.x_axis,
            "nPoints": len(self.x_um),
            "nPulses": self.n_pulses,
            "nPasses": self.n_passes,
            "materialFamily": self.material_family,
            "materialGrade": self.material_grade,
            "laser": dict(self.laser),
            "focus": dict(self.focus),
            "upstreamVersion": self.upstream_version,
            "sourceFile": self.source_file,
            "isSynthetic": self.is_synthetic,
            "depthSign": self.depth_sign,
            "unitNote": self.unit_note,
        }

    # -- 几何指标 ------------------------------------------------------------
    @property
    def max_depth_um(self) -> float:
        return max(self.depth_um) if self.depth_um else 0.0

    @property
    def center_depth_um(self) -> float:
        """中心/轴上的深度（``x`` 最接近 0 的点）。"""
        if not self.x_um:
            return 0.0
        i = min(range(len(self.x_um)), key=lambda k: abs(self.x_um[k]))
        return self.depth_um[i]

    def width_at_fraction_um(self, fraction: float = 0.5) -> float | None:
        """深度降到峰值 ``fraction`` 处的**全宽**（默认半高全宽 FWHM）。

        宽度定义**固定**（任务书 §3.3 要求），避免两边用不同定义比较。
        """
        if len(self.x_um) < 3:
            return None
        peak = self.max_depth_um
        if peak <= 0:
            return None
        thr = peak * float(fraction)
        idx = [i for i, d in enumerate(self.depth_um) if d >= thr]
        if len(idx) < 2:
            return None
        return abs(self.x_um[idx[-1]] - self.x_um[idx[0]])

    def area_um2(self) -> float | None:
        """轮廓下的面积（梯形积分）。需要完整横截面（含两侧）才有意义。"""
        if len(self.x_um) < 2:
            return None
        a = 0.0
        for i in range(len(self.x_um) - 1):
            dx = self.x_um[i + 1] - self.x_um[i]
            a += 0.5 * (self.depth_um[i] + self.depth_um[i + 1]) * abs(dx)
        return abs(a)


def load_upstream_case(json_path: str | Path, profile_path: str | Path | None = None) -> UpstreamProfile:
    """读 ``upstream_case.json``（+ 可选 ``profile.csv``）。

    ``profile.csv`` 优先按 ``upstream_case.json`` 里的 ``profile_file`` 解析；
    显式传入时以显式路径为准。

    支持两种列名风格：
    * 旧 Q4：``r_um, target_depth_um, actual_mesh_depth_um``
    * 通用：``x_um, depth_um``（+ 可选 ``target_depth_um``）
    """
    jp = Path(json_path)
    if not jp.exists():
        raise UFDemoError(
            CONFIG_INVALID, "上游工况文件不存在",
            field_path="upstream.case_json", actual=str(jp), requirement="文件存在",
        )
    raw = json.loads(jp.read_text(encoding="utf-8"))

    geom = str(raw.get("geometry") or "").strip()
    if geom not in GEOMETRY_KINDS:
        raise UFDemoError(
            CONFIG_INVALID,
            f"上游几何类型非法：{geom!r}",
            field_path="upstream.geometry",
            actual=geom,
            requirement=f"取值属于 {list(GEOMETRY_KINDS)}",
            suggestion="必须显式声明几何类型；坑与单线不可混用（任务书 §3.2）。",
        )

    pf = Path(profile_path) if profile_path else (jp.parent / str(raw.get("profile_file", "profile.csv")))
    if not pf.exists():
        raise UFDemoError(
            CONFIG_INVALID, "上游轮廓文件不存在",
            field_path="upstream.profile_csv", actual=str(pf), requirement="文件存在",
        )
    text = pf.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        raise UFDemoError(
            CONFIG_INVALID, "上游轮廓为空",
            field_path="upstream.profile_csv", actual=0, requirement="≥ 1 行",
        )

    xs: list[float] = []
    ds: list[float] = []
    ts: list[float] = []
    has_target = False
    for r in rows:
        x = _pick(r, ("x_um", "r_um", "x", "r"))
        # 深度优先取**实际网格表面**（任务书 §3.2：两者分开，网格面是真实几何）
        d = _pick(r, ("actual_mesh_depth_um", "depth_um", "depth"))
        if x is None or d is None:
            continue
        xs.append(float(x))
        ds.append(float(d))
        t = _pick(r, ("target_depth_um",))
        if t is not None:
            has_target = True
            ts.append(float(t))

    if not xs:
        raise UFDemoError(
            CONFIG_INVALID, "上游轮廓没有可解析的坐标/深度列",
            field_path="upstream.profile_csv",
            actual=list(rows[0].keys()),
            requirement="含 x_um|r_um 与 depth_um|actual_mesh_depth_um",
        )

    # 按 x 升序（重采样方式固定：只排序，不插值）
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    xs = [xs[i] for i in order]
    ds = [ds[i] for i in order]
    ts = [ts[i] for i in order] if has_target else []

    laser = dict(raw.get("laser") or {})
    w_note = _laser_radius_note(laser)

    return UpstreamProfile(
        case_id=str(raw.get("case_id") or jp.stem),
        geometry=geom,
        x_um=xs,
        depth_um=ds,
        target_depth_um=(ts or None),
        x_axis=str(raw.get("x_axis") or ("r" if geom == GEOM_AXISYMMETRIC_PIT else "x")),
        depth_sign=str(raw.get("depth_sign") or "positive_is_removal"),
        unit_note=str(raw.get("unit_note") or ""),
        n_pulses=raw.get("n_pulses"),
        n_passes=raw.get("n_passes"),
        material_family=str(raw.get("material_family") or ""),
        material_grade=raw.get("material_grade"),
        laser=laser,
        focus=dict(raw.get("focus") or {}),
        upstream_version=str(raw.get("upstream_version") or ""),
        source_file=str(pf.name),
        is_synthetic=bool(raw.get("is_synthetic", False)),
    ), w_note


def _pick(row: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s == "" or s.lower() in ("null", "none", "na", "n/a"):
            continue
        try:
            return float(s)
        except ValueError:
            continue
    return None


def _laser_radius_note(laser: Mapping[str, Any]) -> str:
    """把高斯半径的**定义差异**显式写出来（任务书 §3.3 第 3 条）。

    旧 Q4 用 ``exp[-(r/w_old)^2]``，本项目标准 ``exp[-2r²/w²]``。
    同一能量分布下两者等价当且仅当 ``w = sqrt(2)·w_old``。
    **不得**把同名 w0 直接复制 —— 那会让光斑面积差 2 倍、能流密度差 2 倍。
    """
    w = laser.get("spot_radius_m")
    if w is None:
        return ""
    return (
        f"上游半径定义：若为 exp[-(r/w_old)²]，换算到本项目 exp[-2r²/w²] 须 "
        f"w = sqrt(2)·w_old = {math.sqrt(2) * float(w) * 1e6:.4f} μm"
        f"（上游声明 {float(w) * 1e6:.4f} μm）。**不得直接复制同名 w0。**"
    )


# ---------------------------------------------------------------------------
# 对照指标
# ---------------------------------------------------------------------------


@dataclass
class ComparisonMetrics:
    """上下游同工况对照指标（任务书 §3.3：第一版比这四类量）。"""

    center_depth_upstream_um: float | None = None
    center_depth_downstream_um: float | None = None
    max_depth_upstream_um: float | None = None
    max_depth_downstream_um: float | None = None
    width_upstream_um: float | None = None
    width_downstream_um: float | None = None
    area_upstream_um2: float | None = None
    area_downstream_um2: float | None = None
    profile_rmse_um: float | None = None
    width_definition: str = "fwhm_0.5"

    def to_dict(self) -> dict[str, Any]:
        def rel(a, b):
            if a is None or b is None or b == 0:
                return None
            return abs(a - b) / abs(b)
        return {
            "centerDepth": {
                "upstreamUm": self.center_depth_upstream_um,
                "downstreamUm": self.center_depth_downstream_um,
                "relativeDiff": rel(self.center_depth_downstream_um, self.center_depth_upstream_um),
            },
            "maxDepth": {
                "upstreamUm": self.max_depth_upstream_um,
                "downstreamUm": self.max_depth_downstream_um,
                "relativeDiff": rel(self.max_depth_downstream_um, self.max_depth_upstream_um),
            },
            "width": {
                "upstreamUm": self.width_upstream_um,
                "downstreamUm": self.width_downstream_um,
                "relativeDiff": rel(self.width_downstream_um, self.width_upstream_um),
            },
            "area": {
                "upstreamUm2": self.area_upstream_um2,
                "downstreamUm2": self.area_downstream_um2,
                "relativeDiff": rel(self.area_downstream_um2, self.area_upstream_um2),
            },
            "profileRmseUm": self.profile_rmse_um,
            "widthDefinition": self.width_definition,
        }


def resample_to_common_grid(
    a: UpstreamProfile, b: UpstreamProfile, *, n: int = 200,
) -> tuple[list[float], list[float], list[float]]:
    """把两条轮廓重采样到**同一横向网格**（线性插值，方式固定）。

    只对**同几何类型**的轮廓做这件事 —— 坑与单线混着比没有意义。
    """
    if a.geometry != b.geometry:
        raise UFDemoError(
            CONFIG_INVALID,
            f"几何类型不同，不得直接对照：{a.geometry} vs {b.geometry}",
            field_path="upstream.geometry",
            actual=[a.geometry, b.geometry],
            requirement="两者相同",
            suggestion=(
                "轴对称坑只能与定点多脉冲坑比较；单线横截面只能与单线比较。"
                "把半径轴当扫描方向会得到无意义的对照（任务书 §3.2）。"
            ),
        )
    lo = max(min(a.x_um), min(b.x_um))
    hi = min(max(a.x_um), max(b.x_um))
    if not (hi > lo):
        raise UFDemoError(
            CONFIG_INVALID, "两条轮廓的横向范围没有重叠",
            field_path="upstream.profile_csv", actual=[lo, hi], requirement="hi > lo",
        )
    xs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    def interp(p: UpstreamProfile, x: float) -> float:
        for i in range(len(p.x_um) - 1):
            if p.x_um[i] <= x <= p.x_um[i + 1]:
                x0, x1 = p.x_um[i], p.x_um[i + 1]
                if x1 == x0:
                    return p.depth_um[i]
                t = (x - x0) / (x1 - x0)
                return p.depth_um[i] + t * (p.depth_um[i + 1] - p.depth_um[i])
        return 0.0

    return xs, [interp(a, x) for x in xs], [interp(b, x) for x in xs]


def compare_profiles(up: UpstreamProfile, down: UpstreamProfile, *, n: int = 200) -> ComparisonMetrics:
    """对照两条**同几何**轮廓。"""
    xs, ya, yb = resample_to_common_grid(up, down, n=n)
    rmse = math.sqrt(sum((p - q) ** 2 for p, q in zip(ya, yb)) / len(xs))
    return ComparisonMetrics(
        center_depth_upstream_um=up.center_depth_um,
        center_depth_downstream_um=down.center_depth_um,
        max_depth_upstream_um=up.max_depth_um,
        max_depth_downstream_um=down.max_depth_um,
        width_upstream_um=up.width_at_fraction_um(0.5),
        width_downstream_um=down.width_at_fraction_um(0.5),
        area_upstream_um2=up.area_um2(),
        area_downstream_um2=down.area_um2(),
        profile_rmse_um=rmse,
    )


# ---------------------------------------------------------------------------
# 虚拟样例（仅用于展示与端到端测试）
# ---------------------------------------------------------------------------


def synthetic_upstream_pit_case(
    *,
    n_pulses: int = 2,
    depth_um: float = 9.0,
    radius_um: float = 6.0,
    n_points: int = 121,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """生成**虚拟**上游轴对称坑样例（展示/端到端用）。

    ⚠️ ``is_synthetic=True`` 会写进 case —— 报告与界面**必须**显示这是虚拟输入，
    不得被读成真实上游结果。

    返回 ``(case_dict, profile_rows, meta)``，调用方可直接落盘。
    """
    w = float(radius_um)
    xs = [(-1.0 + 2.0 * i / (n_points - 1)) * (2.0 * w) for i in range(n_points)]
    # 一个"上游风格"的熔蚀剖面：近似高斯，带轻微底部展宽
    depth = []
    for x in xs:
        r2 = (x / w) ** 2
        depth.append(float(depth_um) * math.exp(-r2) * (1.0 + 0.06 * r2))
    rows = [
        {"r_um": f"{x:.6f}", "target_depth_um": f"{d * 1.04:.6f}",
         "actual_mesh_depth_um": f"{d:.6f}"}
        for x, d in zip(xs, depth)
    ]
    case = {
        "schema": "ufdemo.upstream_case/1",
        "case_id": f"synthetic_pit_N{n_pulses}",
        "is_synthetic": True,
        "synthetic_note": (
            "**虚拟输入**（非真实上游结果）：仅用于展示与端到端测试。"
            "任何由它得出的对照结论都不得当作上游验证。"
        ),
        "geometry": GEOM_AXISYMMETRIC_PIT,
        "x_axis": "r",
        "depth_sign": "positive_is_removal",
        "unit_note": "r_um（径向，**不是扫描方向**）；深度 μm",
        "n_pulses": int(n_pulses),
        "n_passes": 1,
        "material_family": "SiC",
        "material_grade": None,
        "laser": {
            "wavelength_m": 1030e-9,
            "pulse_duration_s": 300e-15,
            "spot_radius_m": 1.0e-6,
            "spot_radius_definition": "exp[-2r^2/w^2]",
        },
        "focus": {"z_focus": "original_surface", "defocus": "ignored"},
        "upstream_version": "synthetic",
        "profile_file": "profile.csv",
    }
    meta = {
        "is_synthetic": True,
        "warning": "虚拟输入，仅供展示与端到端测试；不得作为上游验证结论。",
    }
    return case, rows, meta


def write_synthetic_upstream_case(dest_dir: str | Path, **kw) -> tuple[Path, Path]:
    """把虚拟样例落盘，返回 ``(case_json, profile_csv)``。"""
    d = Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    case, rows, _ = synthetic_upstream_pit_case(**kw)
    jp = d / "upstream_case.json"
    pp = d / "profile.csv"
    jp.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return jp, pp
