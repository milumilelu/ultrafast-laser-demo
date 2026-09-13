"""导出、重读、哈希与运行状态（执行细则第 11.1 节）。

运行目录结构（细则 11.1）::

    runs/<run_id>/
      config.json
      material_snapshot.json
      metadata.json
      final_surface.npz        # 失败/取消时为 partial_surface.npz
      statistics.csv
      profiles.csv
      events.csv               # 本批新增，用于事件流核查（见 ADR-0007）
      snapshots/index.json
      snapshots/*.npz
      diagnostics.json

* ``run_id`` 使用时间加随机后缀；目录已存在则拒绝，不覆盖。
* 配置和卡哈希采用排序键的稳定 JSON 表示；原始来源文件另外保留原始字节哈希。
* 代码版本记录 git 提交及工作区是否有改动；非 git 环境保存源码清单哈希，
  不伪造提交号。
* NPZ 仅保存数值/字符串数组，不依赖 pickle；重读时关闭 pickle。
* 最终文件通过临时文件写入后替换；所有必要文件成功后状态才置为 completed。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import string
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .errors import CONFIG_INVALID, UFDemoError
from .materials import watermark_rows
from .solver import RunResult

RUN_STATUSES = ("running", "completed", "cancelled", "failed")

REQUIRED_FILES = (
    "config.json",
    "material_snapshot.json",
    "metadata.json",
    "watermark.json",
    "statistics.csv",
    "diagnostics.json",
)


# ---------------------------------------------------------------------------
# 稳定序列化与哈希
# ---------------------------------------------------------------------------


def stable_json(obj: Any) -> str:
    """排序键的稳定 JSON 表示（禁止 NaN/Inf，保证哈希可复现）。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def config_hash(config_snapshot: Mapping[str, Any]) -> str:
    return sha256_text(stable_json(config_snapshot))


def _write_atomic(path: Path, data: str | bytes) -> None:
    """先写临时文件再替换；目标被其它进程占用时退化为就地写入。

    细则 11.1 要求「最终文件通过临时文件写入后替换」。在 Windows 上，如果目标
    文件被别的程序（例如预览器/编辑器）以共享写方式打开，``os.replace`` 会抛
    ``PermissionError``，而就地写入仍然可行。此时退化为就地写入，并在返回后
    删除临时文件；先替换、后退化，能在支持的平台上保持原子性语义。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": ""}
    with open(tmp, mode, **kwargs) as fh:
        fh.write(data)
    try:
        os.replace(tmp, path)
    except PermissionError:
        # 目标被占用：就地写入，随后清理临时文件
        with open(path, mode, **kwargs) as fh:
            fh.write(data)
        try:
            os.unlink(tmp)
        except OSError:  # pragma: no cover - 临时文件已被取消
            pass


def code_version(project_root: str | Path) -> dict[str, Any]:
    """代码版本：优先 git 提交；非 git 环境保存源码清单哈希，不伪造提交号。

    另含：

    * ``ufdemo_version``：即使脱离 git（源码清单哈希也变了），
      也能从运行目录直接读出产生该结果的软件版本；
    * ``build_batch``：交付批次。批次**不写进版本号**（``0.9.0-k1``
      不是合法 PEP 440，会让构建失败），由独立元数据承载。
    """
    from . import __version__, BUILD_BATCH

    root = Path(project_root)
    info: dict[str, Any] = {
        "git_available": False,
        "ufdemo_version": __version__,
        "build_batch": BUILD_BATCH,
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10
        )
        if commit.returncode == 0:
            info["git_available"] = True
            info["commit"] = commit.stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, timeout=10
            )
            info["working_tree_dirty"] = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        info["git_available"] = False

    src = root / "src"
    manifest: list[str] = []
    if src.exists():
        for p in sorted(src.rglob("*.py")):
            manifest.append(f"{p.relative_to(root).as_posix()}:{sha256_file(p)}")
    info["source_manifest_sha256"] = sha256_text("\n".join(manifest))
    info["source_file_count"] = len(manifest)
    info["python"] = sys.version.split()[0]
    return info


def environment_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
    }
    try:
        import numpy as np

        info["numpy"] = np.__version__
    except Exception:  # pragma: no cover
        info["numpy"] = None
    try:
        import pytest  # noqa: F401

        info["pytest"] = pytest.__version__
    except Exception:  # pragma: no cover
        info["pytest"] = None
    return info


# ---------------------------------------------------------------------------
# 运行目录
# ---------------------------------------------------------------------------


def make_run_id(label: str = "", *, now: datetime | None = None, suffix_len: int = 4) -> str:
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=suffix_len))
    base = f"{ts}_{suffix}"
    return f"{label}_{base}" if label else base


def new_run_dir(base_dir: str | Path, run_id: str) -> Path:
    """建立新的运行目录。目录已存在则拒绝，不覆盖。"""
    d = Path(base_dir) / run_id
    if d.exists():
        raise UFDemoError(
            CONFIG_INVALID,
            "运行目录已存在，拒绝覆盖",
            field_path="output_dir",
            actual=str(d),
            requirement="run_id 唯一",
            suggestion="换一个 run_id 或输出目录；历史运行默认不删除、不覆盖。",
        )
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------


@dataclass
class SavedRun:
    run_id: str
    run_dir: str
    status: str
    config_sha256: str
    written_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "status": self.status,
            "config_sha256": self.config_sha256,
            "written_files": list(self.written_files),
        }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    import io as _io

    names = fieldnames or (list(rows[0].keys()) if rows else [])
    buf = _io.StringIO()
    if names:
        writer = csv.DictWriter(buf, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("null" if r.get(k) is None else r.get(k)) for k in names})
    _write_atomic(path, buf.getvalue())


def save_run(result: RunResult, output_dir: str | Path, *, project_root: str | Path | None = None, code_info: Mapping[str, Any] | None = None) -> SavedRun:
    """把一次运行写入 ``output_dir``。

    细则 11.1：最终文件通过临时文件写入后替换；所有必要文件成功后
    才把状态改为 ``completed``。取消或失败只保留 ``partial_surface.npz``
    等部分结果及已完成事件数，不生成代表成功的最终结果。
    """
    import numpy as np

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "snapshots").mkdir(exist_ok=True)

    written: list[str] = []
    cfg_hash = config_hash(result.config_snapshot)

    # config.json
    _write_atomic(out / "config.json", stable_json(result.config_snapshot))
    written.append("config.json")

    # material_snapshot.json
    _write_atomic(out / "material_snapshot.json", stable_json(result.material_snapshot))
    written.append("material_snapshot.json")

    # watermark.json（细则 11.3：标签贯穿导出，单文件即可读出身份与模式）
    watermark = dict((result.metadata or {}).get("watermark") or {})
    if not watermark:
        watermark = dict(
            ((result.material_snapshot or {}).get("watermark") or {})
        )
    _write_atomic(out / "watermark.json", stable_json(watermark))
    written.append("watermark.json")

    # statistics.csv（长表：metric,value,unit）
    stats_rows: list[dict[str, Any]] = []
    stats_rows.extend(watermark_rows(watermark) if watermark else [])
    for k, v in (result.statistics or {}).items():
        stats_rows.append({"metric": k, "value": "null" if v is None else v, "unit": _metric_unit(k, result)})
    for r in result.rois or []:
        for k, v in r.items():
            stats_rows.append({"metric": f"roi[{r.get('name')}].{k}", "value": "null" if v is None else v, "unit": ""})
    _write_csv(out / "statistics.csv", stats_rows, ["metric", "value", "unit"])
    written.append("statistics.csv")

    # profiles.csv
    prof_rows: list[dict[str, Any]] = []
    for p in result.profiles or []:
        for i, c in enumerate(p.get("coord", [])):
            prof_rows.append(
                {
                    "section": p.get("label"),
                    "axis": p.get("axis"),
                    "coord": c,
                    "depth": p.get("depth", [None] * (i + 1))[i],
                    "height": p.get("height", [None] * (i + 1))[i],
                }
            )
    _write_csv(out / "profiles.csv", prof_rows, ["section", "axis", "coord", "depth", "height"])
    written.append("profiles.csv")

    # diagnostics.json
    diagnostics = {
        "run_id": result.run_id,
        "status": result.status,
        "events_processed": result.events_processed,
        "events_total": result.events_total,
        "removal_available": result.removal_available,
        "diagnostics": result.diagnostics,
        "warnings": result.warnings,
        "errors": result.errors,
        "elapsed_s": result.elapsed_s,
    }
    _write_atomic(out / "diagnostics.json", stable_json(diagnostics))
    written.append("diagnostics.json")

    # snapshots
    snap_index: list[dict[str, Any]] = []
    for i, snap in enumerate(result.snapshots or []):
        arrays = {k: v for k, v in snap.items() if hasattr(v, "shape")}
        meta = {k: v for k, v in snap.items() if not hasattr(v, "shape")}
        name = f"snap_{i:04d}.npz"
        tmp = out / "snapshots" / (name + ".tmp.npz")
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, out / "snapshots" / name)
        snap_index.append({"file": name, **meta, "arrays": sorted(arrays)})
        written.append(f"snapshots/{name}")
    _write_atomic(out / "snapshots" / "index.json", stable_json(snap_index))
    written.append("snapshots/index.json")

    # events.csv（事件流核查；上限固定，避免长序列爆盘）
    if result.events_rows:
        _write_csv(out / "events.csv", list(result.events_rows), list(result.events_rows[0].keys()))
        written.append("events.csv")

    # 表面文件：只有成功才写 final_surface.npz
    surface = result.surface
    if surface is not None:
        payload = {
            "height": surface.height,
            "initial_height": surface.initial_height,
            "phase_id": surface.phase_id,
            "cumulative_fluence": surface.cumulative_fluence,
            "illumination_count": surface.illumination_count,
            "exposure_count": surface.exposure_count,
            "warning_mask": surface.warning_mask.astype(np.uint8),
            "x": surface.x,
            "y": surface.y,
        }
        # 受限阈值协议量：仅在协议开启时落盘（未开启不写全 0 假数组）
        thr_count = getattr(surface, "threshold_exceedance_count", None)
        if thr_count is not None:
            payload["threshold_exceedance_count"] = thr_count.astype(np.uint32)
        thr_mask = getattr(surface, "threshold_exceeded_mask", None)
        if thr_mask is not None:
            payload["threshold_exceeded_mask"] = thr_mask.astype(np.uint8)
        fname = "final_surface.npz" if result.status == "completed" else "partial_surface.npz"
        tmp = out / (fname + ".tmp.npz")
        np.savez_compressed(tmp, **payload)
        os.replace(tmp, out / fname)
        written.append(fname)

    # metadata.json 最后写，状态在此刻才置为 completed
    metadata = dict(result.metadata)
    # metadata.json itself is part of the manifest.  Include it before
    # serialising so the on-disk manifest and SavedRun.written_files agree.
    manifest_files = list(written)
    if "metadata.json" not in manifest_files:
        manifest_files.append("metadata.json")
    metadata.update(
        {
            "run_id": result.run_id,
            "status": result.status,
            "config_sha256": cfg_hash,
            "material_card_sha256": (result.material_snapshot or {}).get("card_sha256"),
            "events_processed": result.events_processed,
            "code": dict(code_info) if code_info is not None else code_version(project_root or Path.cwd()),
            "environment": environment_info(),
            "written_files": manifest_files,
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    if result.status != "completed":
        metadata["partial_result_note"] = (
            "本次运行未完成（cancelled/failed）：只保留部分结果，"
            "不代表成功的最终加工输出，不得与成功结果混用。"
        )
    _write_atomic(out / "metadata.json", stable_json(metadata))
    written = manifest_files

    return SavedRun(run_id=result.run_id, run_dir=str(out), status=result.status, config_sha256=cfg_hash, written_files=written)


def _metric_unit(name: str, result: RunResult) -> str:
    u = result.unit
    if u is None:
        return ""
    if "volume" in name:
        return f"{u.length_label}^3"
    if "depth" in name or name.startswith("height"):
        return u.depth_label if "depth" in name else u.length_label
    if "fluence" in name:
        return u.fluence_label
    if "area" in name:
        return f"{u.length_label}^2"
    return ""


# ---------------------------------------------------------------------------
# 重读
# ---------------------------------------------------------------------------


@dataclass
class LoadedRun:
    run_id: str
    run_dir: str
    status: str
    metadata: dict[str, Any]
    config: dict[str, Any]
    material_snapshot: dict[str, Any]
    statistics: dict[str, Any]
    diagnostics: dict[str, Any]
    surface: dict[str, Any]
    snapshots: list[dict[str, Any]]
    profiles: list[dict[str, Any]]
    events: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)
    watermark: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "status": self.status,
            "statistics": self.statistics,
            "n_snapshots": len(self.snapshots),
            "n_events": len(self.events),
            "warnings": self.warnings,
        }


def load_run(output_dir: str | Path) -> LoadedRun:
    """重读一次运行。NPZ 关闭 pickle。"""
    import numpy as np

    d = Path(output_dir)
    if not d.exists():
        raise UFDemoError(CONFIG_INVALID, "运行目录不存在", field_path="output_dir", actual=str(d))

    metadata = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    config = json.loads((d / "config.json").read_text(encoding="utf-8"))
    mat = json.loads((d / "material_snapshot.json").read_text(encoding="utf-8"))
    diagnostics = json.loads((d / "diagnostics.json").read_text(encoding="utf-8"))

    # 水印：优先 watermark.json（导出时的唯一权威来源），再退回 metadata / 卡快照
    watermark: dict[str, Any] = {}
    wp = d / "watermark.json"
    if wp.exists():
        watermark = json.loads(wp.read_text(encoding="utf-8"))
    if not watermark:
        watermark = dict(metadata.get("watermark") or {})
    if not watermark:
        watermark = dict((mat or {}).get("watermark") or {})

    stats: dict[str, Any] = {}
    sp = d / "statistics.csv"
    if sp.exists():
        with open(sp, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                v = row.get("value")
                if v in ("null", "", None):
                    stats[row["metric"]] = None
                else:
                    try:
                        stats[row["metric"]] = float(v)
                    except ValueError:
                        stats[row["metric"]] = v

    profiles: list[dict[str, Any]] = []
    pp = d / "profiles.csv"
    if pp.exists():
        grouped: dict[str, dict[str, Any]] = {}
        with open(pp, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                g = grouped.setdefault(row["section"], {"section": row["section"], "axis": row["axis"], "coord": [], "depth": [], "height": []})
                g["coord"].append(float(row["coord"]))
                g["depth"].append(float(row["depth"]) if row["depth"] not in ("", "None") else None)
                g["height"].append(float(row["height"]) if row["height"] not in ("", "None") else None)
        profiles = list(grouped.values())

    events: list[dict[str, Any]] = []
    ep = d / "events.csv"
    if ep.exists():
        with open(ep, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                events.append(row)

    surface: dict[str, Any] = {}
    for fname in ("final_surface.npz", "partial_surface.npz"):
        fp = d / fname
        if fp.exists():
            with np.load(fp, allow_pickle=False) as npz:
                surface = {k: npz[k] for k in npz.files}
            surface["_source_file"] = fname
            break

    snap_index: list[dict[str, Any]] = []
    si = d / "snapshots" / "index.json"
    if si.exists():
        snap_index = json.loads(si.read_text(encoding="utf-8"))
        for entry in snap_index:
            fp = d / "snapshots" / entry["file"]
            if fp.exists():
                with np.load(fp, allow_pickle=False) as npz:
                    entry["_arrays_loaded"] = sorted(npz.files)

    warnings = list(diagnostics.get("warnings", []))
    if metadata.get("status") != "completed":
        warnings.append("该结果未完成（上一次运行的失败/取消结果），不得当作成功输出。")

    return LoadedRun(
        run_id=metadata.get("run_id", d.name),
        run_dir=str(d),
        status=metadata.get("status", "unknown"),
        metadata=metadata,
        config=config,
        material_snapshot=mat,
        statistics=stats,
        diagnostics=diagnostics,
        surface=surface,
        snapshots=snap_index,
        profiles=profiles,
        events=events,
        warnings=warnings,
        watermark=watermark,
    )


def is_completed(output_dir: str | Path) -> bool:
    md = Path(output_dir) / "metadata.json"
    if not md.exists():
        return False
    try:
        return json.loads(md.read_text(encoding="utf-8")).get("status") == "completed"
    except json.JSONDecodeError:
        return False
