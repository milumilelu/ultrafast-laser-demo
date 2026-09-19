"""从 KEYENCE `.cag` / 高度 CSV 提取加工槽的深度。

为什么需要它
------------
KEYENCE VK-X3000 导出的高度场**不能直接读来用**：

* `.cag` 是 ZIP 封装的专有格式（内部 3140 个 UUID 命名的二进制块），
  需要 `CagHeightReader`（见下）才能解出高度图；
* 导出的 `*_高度.csv` 里 **0.000 是"无数据"哨兵**，不是高度；
* 样品/工件台**是倾斜且弯曲的**（实测倾斜 0.5–0.8°，减掉平面后梯度仍在），
  不处理会让"深度"里混进几个 µm 的假信号；
* 一个视场里通常有**多个槽**（20× 物镜视场 705×529 µm，设计间距 400–500 µm），
  需要连通域分离而不是整图取 min；
* 还有灰尘/测量伪影造成的 ±30–40 µm 离群点。

本脚本把这几步固化下来，输出**逐槽**的深度表。

数据来源（二选一）
------------------
1. `.cag` —— 走 `CagHeightReader`，它的输出与 KEYENCE 官方 CSV **字节级一致**
   （`raw_to_micrometres` 在整数域做 half-up 取整，绕开 banker's rounding）。
   解析器位于用户的 `physics-guided Mamba-2` 仓库：`src/io_cag.py`。
   **本脚本不复制该解析器**，而是 import——保持单一事实来源。
2. `*_高度.csv` —— 自解析（含编码容错：utf-8-sig / utf-8 / gbk / cp932 / latin-1）。

用法
----
    python cag_to_depth.py --input <文件或目录> --out depth.csv \
        [--mamba-repo <Mamba-2 仓库路径>] \
        [--thresh -3.0] [--min-area-um2 200] [--detrend linear|quad|none]

    # 直接读 .cag
    python cag_to_depth.py --input "氧化锆/pass实验数据/60Pass组.cag" \
        --mamba-repo "C:/Users/RZF/Desktop/博士课题资料/physics-guided Mamba-2" \
        --out runs/depth_60pass.csv

输出列
------
    source, group, name, center_x_um, center_y_um, area_um2,
    median_depth_um, p10_depth_um, max_depth_um, valid_fraction

设计取舍（都留了退路）
----------------------
* **去倾斜默认只用线性项**（`--detrend linear`）。二次项容易把"样品的真实弯曲"
  和"大面积加工的宏观轮廓"一起吃进去；需要时用 `--detrend quad`，但要清楚代价。
* 深度取**相对**值（减去拟合面），因此**绝对值依赖 detrend 的选择**；
  跨组比较时保持同一 detrend 即可，不要混用。
* 阈值与最小面积可调——太小的连通域是毛刺，不是槽。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp932", "latin-1")

#: 腐蚀后至少保留的像素数；低于它就**放弃腐蚀**（否则会把小槽整个蚀掉，
#: 反而得不到深度）。放弃时 erode_px 记 0，输出里可追溯。
MIN_INNER_PX = 200


# --------------------------------------------------------------------------- #
# 读取层
# --------------------------------------------------------------------------- #
def load_csv_height(path: Path) -> tuple[np.ndarray, float, dict]:
    """读 KEYENCE 导出的 `*_高度.csv` → (z_um, px_um, meta)。

    * 0.000 视为**无数据哨兵** → NaN（KEYENCE 用它表示测量失败点）；
    * 编码逐个尝试，遇到坏行跳过（实测同一批文件里有非 UTF-8 的）。
    """
    raw = path.read_bytes()
    txt = None
    for enc in ENCODINGS:
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if txt is None:
        raise ValueError(f"无法解码：{path}")

    lines = txt.splitlines()
    meta: dict[str, str] = {}
    start = 0
    for i, ln in enumerate(lines[:40]):
        if ln.strip().startswith('"高度"'):
            start = i + 1
            break
        parts = [x.strip('"') for x in ln.split(",")]
        if len(parts) >= 2:
            meta[parts[0]] = parts[1]

    rows: list[list[float]] = []
    for ln in lines[start:]:
        if not ln.strip():
            continue
        try:
            vals = [float(x.strip('"')) for x in ln.split(",") if x.strip()]
        except ValueError:
            continue                      # 坏行直接跳过，不猜
        if len(vals) >= 1000:             # 只收真正的数据行
            rows.append(vals)
    if not rows:
        raise ValueError(f"没读到高度数据行：{path}")

    z = np.array(rows, dtype=np.float64)
    z[z <= 0.0] = np.nan                  # 哨兵 → NaN，绝不填值
    px_um = float(meta.get("XY校准", "0") or 0) / 1000.0
    if px_um <= 0:
        raise ValueError(f"缺少 XY 校准：{path}")
    return z, px_um, meta


def load_cag_heights(path: Path, mamba_repo: Path):
    """用 `CagHeightReader` 逐组读出高度图。

    **import 而非复制**：解析器（含 LUT 776 的推导与验证）在用户仓库里，
    复制一份会立刻产生两个"事实来源"。
    """
    repo = str(mamba_repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    try:
        from src.io_cag import CagHeightReader
    except ImportError as exc:            # 给出可操作的提示，而不是裸 traceback
        raise SystemExit(
            f"无法从 {mamba_repo} 导入 src.io_cag：{exc}\n"
            f"用 --mamba-repo 指向含 src/io_cag.py 的仓库根目录。"
        ) from exc

    with CagHeightReader(path) as reader:
        for g in reader.groups:
            hm = reader.read_height_map(g)
            name = reader.names.get(g) or reader.data_names.get(g) or str(g)
            yield g, str(name), hm.z, hm.dx_um, dict(hm.metadata)


# --------------------------------------------------------------------------- #
# 处理层
# --------------------------------------------------------------------------- #
def drop_outliers(z: np.ndarray, k: float = 4.0) -> np.ndarray:
    """按中位数 ± k·σ 剔除离群点（灰尘/伪影实测可达 ±40 µm）。"""
    v = z[np.isfinite(z)]
    if v.size == 0:
        return z
    med, sd = np.median(v), v.std()
    if sd <= 0:
        return z
    return np.where(np.abs(z - med) > k * sd, np.nan, z)


def detrend(z: np.ndarray, mode: str) -> np.ndarray:
    """去掉样品/台面的倾斜（默认只去线性项）。"""
    if mode == "none":
        return z
    ny, nx = z.shape
    Y, X = np.mgrid[0:ny, 0:nx]
    m = np.isfinite(z)
    if m.sum() < 100:
        return z
    cols = [np.ones(m.sum()), X[m], Y[m]]
    if mode == "quad":
        cols += [X[m] ** 2, Y[m] ** 2, X[m] * Y[m]]
    coef, *_ = np.linalg.lstsq(np.c_[tuple(cols)], z[m], rcond=None)
    P = coef[0] + coef[1] * X + coef[2] * Y
    if mode == "quad":
        P = P + coef[3] * X**2 + coef[4] * Y**2 + coef[5] * X * Y
    return z - P


def find_grooves(z: np.ndarray, px_um: float, thresh: float,
                 min_area_um2: float, erode_px: float = 20.0) -> list[dict]:
    """连通域找槽：z < thresh 的区域，按物理面积过滤。

    ⚠️ **深度只在"中间平缓区"测，不含边界**（用户 2026-09-18 明确指示）：
    槽的**边缘存在过深现象**（边缘效应 / 重铸 / 毛刺），
    把边界算进去会系统性高估深度。做法是对掩膜做**形态学腐蚀**，只在内部区域统计；
    腐蚀后剩余像素太少时**回退**（不腐蚀），并在输出里如实记录 `erode_px`。
    """
    from scipy import ndimage

    mask = np.nan_to_num(z, nan=0.0) < thresh
    lab, n = ndimage.label(mask)
    out: list[dict] = []
    for gid in range(1, n + 1):
        sel = lab == gid
        area_um2 = float(sel.sum()) * px_um * px_um
        if area_um2 < min_area_um2:
            continue
        cy, cx = ndimage.center_of_mass(sel)

        inner, eroded = sel, 0.0
        if erode_px and erode_px > 0:
            trial = ndimage.binary_erosion(sel, iterations=max(1, int(round(erode_px))))
            if trial.sum() >= MIN_INNER_PX:
                inner, eroded = trial, float(round(erode_px, 1))

        zz = z[inner]
        zz = zz[np.isfinite(zz)]
        if zz.size == 0:
            continue
        out.append({
            "center_x_um": float(cx) * px_um,
            "center_y_um": float(cy) * px_um,
            "area_um2": area_um2,
            "median_depth_um": float(np.median(zz)),   # 中位，抗残余毛刺
            "p10_depth_um": float(np.percentile(zz, 10)),
            "max_depth_um": float(np.min(zz)),
            "erode_px": eroded,
            "inner_px": int(inner.sum()),
        })
    return sorted(out, key=lambda d: -d["area_um2"])


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def process_one(source: str, name: str, z_raw: np.ndarray, px_um: float,
                args) -> list[dict]:
    valid_frac = float(np.isfinite(z_raw).mean())
    z = drop_outliers(z_raw)
    z = detrend(z, args.detrend)
    rows = find_grooves(z, px_um, args.thresh, args.min_area_um2,
                        erode_px=getattr(args, "erode_px", 20.0))
    for r in rows:
        r.update({"source": source, "group": name, "valid_fraction": round(valid_frac, 4)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="KEYENCE .cag / 高度CSV → 逐槽深度表")
    ap.add_argument("--input", required=True, help=".cag 文件，或含 *_高度.csv 的目录")
    ap.add_argument("--out", required=True, help="输出 CSV")
    ap.add_argument("--mamba-repo", default=None,
                    help="含 src/io_cag.py 的仓库根目录（读 .cag 时必需）")
    ap.add_argument("--thresh", type=float, default=-3.0,
                    help="判定为槽的深度阈值（µm，负值），默认 -3")
    ap.add_argument("--min-area-um2", type=float, default=200.0,
                    help="最小槽面积（µm²），默认 200")
    ap.add_argument("--detrend", choices=("linear", "quad", "none"), default="linear",
                    help="去倾斜方式，默认 linear（quad 会吃掉宏观弯曲，慎用）")
    ap.add_argument("--erode-px", type=float, default=20.0,
                    help="腐蚀边界像素数：深度只在**中间平缓区**测（槽边缘有过深假象）。"
                         "0 = 不腐蚀（含边界）")
    args = ap.parse_args()

    src = Path(args.input)
    rows: list[dict] = []

    if src.suffix.lower() == ".cag":
        if not args.mamba_repo:
            raise SystemExit("读 .cag 需要 --mamba-repo 指向含 src/io_cag.py 的仓库")
        for g, name, z, px_um, _meta in load_cag_heights(src, Path(args.mamba_repo)):
            rows += process_one(src.name, name, z, px_um, args)
            print(f"  组 {g} ({name}): {len(rows)} 行累计", file=sys.stderr)
    else:
        files = sorted(src.glob("*_高度.csv")) if src.is_dir() else [src]
        if not files:
            raise SystemExit(f"在 {src} 下没找到 *_高度.csv")
        for f in files:
            z, px_um, _meta = load_csv_height(f)
            got = process_one(f.name, f.stem, z, px_um, args)
            rows += got
            print(f"  {f.name}: {len(got)} 个槽", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["source", "group", "center_x_um", "center_y_um", "area_um2",
            "median_depth_um", "p10_depth_um", "max_depth_um",
            "erode_px", "inner_px", "valid_fraction"]
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(",".join(cols) + "\n")
        for r in rows:
            fh.write(",".join(
                f"{r[c]:.4f}" if isinstance(r.get(c), float) else str(r.get(c, ""))
                for c in cols) + "\n")
    print(f"\n写入 {out}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
