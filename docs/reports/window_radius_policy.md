# 窗口半径策略实测报告（超阈值开窗）

* 日期：2026-09-15｜批次：L｜决策：`docs/decisions/ADR-0017-window-radius-policy.md`
* 开关：`solver.window_radius_policy = "above_threshold"`（**默认 `"tail_epsilon"`，关闭**）
* 环境：venv `ufdemo`，本机 CPU，`PYTHONPATH=src`

## 1. 结论摘要

深孔算例（`axial_defocus`）下，窗口里**能流超阈值**（对去除真有贡献）的格子只占
**0.8%**，其余 99% 的计算是**可证明的零贡献**。按超阈值半径开窗后可省掉
**93–95.5%** 的窗口格，实测提速 **5.8×–35×**，而**高度场逐位一致**。

## 2. 成本模型：完全由窗口大小决定

对照实验 —— 固定 1,040 事件、加工区 100 µm、dx 0.5 µm，只换 `geometry_feedback`：

| 域 (µm) | 网格 | `fixed_geometry` µs/事件 | `axial_defocus` µs/事件 | 后者窗口均值 |
|---|---|---|---|---|
| 100 | 40,000 | **131** | 1,445 | 39,037 格 |
| 200 | 160,000 | **132** | 8,551 | 139,281 格 |
| 400 | 640,000 | **138** | 13,624 | 214,575 格 |

* `fixed_geometry`（离焦恒 0）：**网格涨 16 倍，成本只涨 5%** ⇒ 无隐藏 O(网格) 代码路径；
* `axial_defocus`：`成本 ≈ 0.06 µs × 窗口格数`，三个点都吻合；
* ⇒ 提速只能从**缩小窗口**来，不能靠改 numpy 用法。

## 3. 窗口为什么这么大：自增强回路

| 环 | 量 | 值 |
|---|---|---|
| ① | 深度（1 趟，δ=3.6525 µm） | **132 µm**（第一炮中心 `δ·ln(1231)` = 25.99 µm） |
| ② | 离焦（深度 / zR=4.47 µm） | **30 倍** |
| ③ | 光斑（1.33 µm ⇒ ） | **39 µm**（`patch.spot_radius` 实测） |
| ④ | ε 窗口直径（`3.035w`，ε=1e-8） | **237 µm** ≈ 吃满 200 µm 域 |

单事件窗口演化（域 200 / dx 1.0）：

| 事件# | 窗口格数 | 其中超阈 | 中心峰值/阈值 |
|---|---|---|---|
| 1 | 64 | 16 | 697 |
| 5 | 9,216 | 54 | 697 |
| 20 | 12,753 | 82 | 926 |
| 100 | 34,000（满网格） | 363 | 926 |
| 1040 | 28,899 | 342 | 926 |

窗口涨 **530 倍**，「有用」的格子只涨 **21 倍**。
全事件合计：窗口 **36,172,632** 格，其中超阈 **460,866** 格 = **1.274%**。

> 注：中心峰值/阈值 697 与解析值 1231 的差别是**网格采样**造成的
> （焦点落在格心之间，最近格 r²=0.5 µm²，`exp(-2r²/w0²)=0.566`）；
> 事件 2 起采样位置变化，读到 926。

## 4. 超阈值半径

```
F(r) = F_peak · exp(-2 r² / w²),   F_peak = 2E/(π w²)
F(r) = F_th   ⇒   r_above_threshold = w · sqrt( ln(F_peak / F_th) / 2 ) · margin
```

* ``F_peak <= F_th`` ⇒ 半径 0 ⇒ 整个平面增量恒为 0 ⇒ 窗口取空（仍记发射能量）；
* `margin` 默认 **1.25**；`margin = 1.0` 即恰好取到 `F = F_th` 的半径。

**为什么可以不改变结果**：阈值型响应核在 ``F <= F_th`` 处返回**恰好 0**
（`response.FixedThresholdLogLaw.increment` 里 `mask = F > threshold`）。

## 5. 实测收益

固定 1,040 事件（加工区 100 µm）：

| 算例 | 既有 `tail_epsilon` | `above_threshold` | 提速 | 裁掉窗口格 | 高度场最大差 |
|---|---|---|---|---|---|
| 域 200 / dx 1.0 | 1.34 s | 0.23 s | **5.8×** | 93.11% | **0.000000 nm（逐位一致）** |
| 域 400 / dx 0.5 | 19.76 s | 0.58 s | **33.9×** | 95.50% | **0.000000 nm（逐位一致）** |

界面实际参数（域 400 / 加工区 200 / dx 0.5，4,080 事件）：

| | 单候选 | 每事件 | 25 候选量级 |
|---|---|---|---|
| 既有 | 57.5 s | 14.1 ms | ≈ 23.9 min |
| 超阈开窗 | **1.62 s** | **0.40 ms** | ≈ **0.68 min** |
| | | | 裁掉 95.49%，逐位一致 |

按界面那批 **279,600** 个事件线性换算：**约 65 分钟 → 约 1.9 分钟**。

## 6. 口径变化（必须知道）

启用后下列**剂量观测量**的统计范围从 ε 尾部截断半径缩到超阈值半径：

* `surface.cumulative_fluence`（逐格累计入射能流）
* `surface.illumination_count`（照射计数；也是「受照范围」的来源）
* `fluence_ledger.estimated_intercepted_energy_internal`（估计截获能量）

因此 ``max_domain_truncated_fraction`` 会从 ~1e-8 量级升到 **9–14%**
（域 200：13.797%；域 400：8.982%）。**这不是域截断**，是「这部分能流低于阈值、
对去除贡献恰好为 0」。求解器收尾警告在启用时**换口径**说明这一点，
并在 `fluence_ledger.threshold_window` 里给出 `cells_skipped_fraction`。

`classify_exceedance`（受限阈值协议）**不受影响**：它也只看单个事件的
``patch.fluence``，被裁掉的格子本来就判为「未超阈」。

## 7. 复现

```bash
export PYTHONPATH=src
python -m pytest tests/test_window_radius_policy.py -q       # 11 条
```

最小用法（配置层）：

```python
cfg.solver.window_radius_policy = "above_threshold"
cfg.solver.window_threshold_margin = 1.25     # >= 1.0；默认 1.25
res = solve(cfg, material)
res.diagnostics["fluence_ledger"]["threshold_window"]   # 口径变化在这里
```

作业 JSON：`{"solver": {"window_radius_policy": "above_threshold"}}`。

## 8. 待办（本轮未做）

1. **接界面**：前端开关 + 让「耗时预估」按策略给出不同估计
   （`planning.plan_for_target` 现在仍按 ε 窗口估时，会高估 ~30 倍）；
2. **planner 参数**：`evaluate_candidate` / `plan_for_target` 未透出该策略，
   需要加形参才能真正让「25 个候选」跑在超阈开窗下；
3. **`grouped` 路径**：目前不支持，已写警告；
4. **物理侧**：δ 仍是 C3 的「初值级别」值（3652.5 nm），深度 132 µm 远超 1 µm 目标。
   本策略与深度量级正交，但深度合理后窗口本来就会小很多。
