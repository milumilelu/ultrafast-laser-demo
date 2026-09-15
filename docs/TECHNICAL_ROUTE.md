# 当前技术路线与实现细节

* 版本：`0.9.1`｜批次：L｜日期：2026-09-15
* 适用范围：`ultrafast-demo` 仓库当前主干（`33887a1`）
* 配套：`docs/decisions/ADR-*.md`（逐条决策）、`docs/reports/window_radius_policy.md`（性能实测）、
  `docs/reports/progress.md`（批次台账）

本文是**实现者视角**的走查：当前路线是什么、每一步在哪个文件里怎么实现、
有哪些**不能碰的约定**、以及已知的坑。所有数值都标出来源。

---

## 1. 要算什么

输入：一套激光/材料/路径条件；输出：**烧蚀后的表面高度场**（以及派生的深度、覆盖、体积、截面）。

最小可信链条是「**逐事件**累加」：

```
对每个真实脉冲事件 k：
    读当前表面 → 算该事件在表面上的入射能流 F(q) → 响应核给出去除增量 Δd(q)
    → 截断（相界面等）→ 提交到高度场 h(q) -= Δh(q)
```

**唯一进逐事件主循环的量**是「事件深度增量」`event_depth_increment`（红线）：
平均率、累计深度、轨道深度、体积效率一律**不得**进主循环（它们不是可加的物理量）。

---

## 2. 模块地图

| 模块 | 职责 |
|---|---|
| `config.py` | 配置解析与校验（`RunConfig` / `SolverConfig` / `LaserConfig` / `PathConfig` / `GridConfig`）；所有非法组合在这里就拦下 |
| `paths.py` + `config.PathSegment` | 事件枚举：`iter_events()` 按重复频率与路径段生成 `PulseEvent`（焦点坐标 + 方向 + 能量） |
| `surface.py` | 状态载体：高度场、相标签、累计能流、照射计数；`apply_increment()` 是**唯一的提交点** |
| `beam.py` | **光束**：高斯能流、离焦 `w(s)`、局部窗口、投影 μ、可见性；产出 `FluencePatch` |
| `response.py` | **响应核**：能流 → 深度增量（`log_fixed` 解析律 / 曲线核）；`HistoryState` |
| `geometry.py` | 法向、入射余弦、法向厚度↔垂直深度换算、首次交点可见性 |
| `thresholds.py` | 受限阈值协议：只按**本事件**入射能流判超阈（不产生深度） |
| `solver.py` | 主循环编排、诊断聚合、快照、账本 |
| `metrics.py` | ROI / 截面 / 体积 / 域统计 |
| `calibration.py` | 从实验行装配配置（`build_row_config`）、反推基线、规划的**唯一装配入口** |
| `planning.py` | h/N 枚举规划、两级网格、矩形槽形貌 |
| `webcontract.py` / `ui_service.py` / `webapp.py` | Web 接口层（`ui_service.submit()` 是**唯一求解入口**） |
| `webui/` | 前端三工作区（原生 JS，无框架） |

---

## 3. 求解主循环（`solver.solve`）

逐步对应执行细则 5.2 的第 1–8 步：

| 步 | 做什么 | 关键点 |
|---|---|---|
| 1 | 事件计数与索引 | `n_events` 是**全网格口径**，写入 `result.events_rows` 受 `events_limit` 限制 |
| 2 | **局部光斑窗口** | `beam_patch()`；窗口与域无交集时**仍记发射能量**，不制造去除 |
| 3 | 读本事件**开始时**的状态算能流 | 不得边更新局部像素边重算光束几何 |
| 4 | 累计入射剂量与照射诊断 | `surface.accumulate_illumination(section, fluence, mask)` |
| 5 | 响应核给候选增量 | 单相走 `law.increment()`；分相只调用**当前暴露相**的那一个核 |
| 6 | 语义/方向/非负/有限校验 | 在 `IncrementResult.validate()` 内完成 |
| 7 | 相界面截断（结构化时） | 实际去除 = `min(候选, 到界面距离)`；被截断量单列为「未应用候选去除体积」 |
| 8 | **一次提交** | `surface.apply_increment(...)`；高度只在 `[iy0:iy1, ix0:ix1]` 上落 |
| 9 | 快照与进度 | `_record_snapshot` 按事件节点；`progress_callback` 按 `cancel_check_interval` |

**标定增益 `a`（C4）作用在第 5 步与第 8 步之间**，即「候选增量 → 提交表面之前」。
位置是硬要求：后续脉冲按**新表面**重算被动离焦，所以 `D(a) ≠ a·D(1)`——
若改到结果页去乘系数，就退化成「给深度乘个系数」，与任务书 §4 相违。

---

## 4. 光束与局部窗口（`beam.py`，本仓库最容易出错的地方）

### 4.1 约定（不得自行变更）

* 光斑半径统一为**能流分布的 1/e² 半径**；
* `F_perp(r,s) = 2E_p/(π w(s)²) · exp(-2r²/w(s)²)`，`w(s) = w0·√(1+(s/zR)²)`；
* 方向约定 **k 指向光源侧**，`k_z > 0` 强制；`μ = max(0, k·n)`，**不得改成 `-k·n`**；
* 法向厚度→高度：`Δh = -a_n/n_z`（**不是** `·n_z`）；
* 尾部截断半径 `r_cut = w·√(ln(1/ε)/2)`，默认 `ε = 1e-8` ⇒ `3.034854 w`。
  **它是数值设置，不是物理损伤阈值**。

### 4.2 轴向距离 `s` 与离焦

* 焦点策略（共用背景常数）：`fixed_original_surface` + `axial_defocus`
  ⇒ 焦点 Z **恒为初始表面**，离焦量 = **当前高度**：`s = h(q) - z_f`。
* 后果：孔越深 → 离焦越大 → 光斑越大。本算例一趟烧到 **132 µm** ≫ `zR = 4.465 µm`
  ⇒ 离焦 **30 倍** ⇒ 光斑 `1.33 → 39 µm`。

### 4.3 窗口必须覆盖「全部可能超阈值的格子」

窗口半径取 **全网格**的轴向跨度：

```python
axial_span_true = max(|min(h_ref) - fz|, |max(h_ref) - fz|)
max_s = axial_span_true                     # 斜入射再补 2.5·r_probe·tanθ
r_cut = cut_radius(w_of_s(w0, max_s, zR), ε)
window_r = r_cut if 正入射 else oblique_window_radius(r_cut, k, axial_span_true)
```

⚠️ **不得改成「窗口内」的跨度**（曾经这么改过，见 §4.5 的回归）。

### 4.4 超阈值开窗（性能开关，默认关闭）

阈值型响应核在 `F ≤ F_th` 处**返回恰好 0**，所以「不可能超阈值」的区域不必计算。
记 `A = F_peak(s=0)/F_th = 2E/(π w0² F_th)`，某格自身离焦 `u = (w_cell/w0)² ≥ 1`：

```
F(r)/F_th = (A/u)·exp(-2r²/(u·w0²)) > 1   ⟺   r² < w0²·f(u)/2,   f(u) = u·ln(A/u)
```

`f` 在 **`u* = A/e`** 处取**唯一极大值** `f_max = A/e` ⇒ 对**任意** u 有

```
r_max = w0 · sqrt(A/(2e)) · margin          # 与当前深度无关
```

* `margin` 默认 **1.25**（1.0 已由严格不等式保证，余量吸收网格离散与斜入射平移）；
* `A ≤ 1`（峰值本身不超阈值）⇒ 半径 0 ⇒ 窗口取空，事件只记发射能量；
* `r_max` **与深度无关** ⇒ 窗口尺寸整轮恒定，成本可预测。

打开方式：

```json
{"solver": {"window_radius_policy": "above_threshold", "window_threshold_margin": 1.25}}
```

界面：规划与结果 → **窗口半径策略**。规划形参：`plan_for_target(..., window_radius_policy=...)`。

**代价（必须知道）**：`cumulative_fluence` / `illumination_count` / 估计截获能量三个
**剂量观测量**的统计范围从 ε 尾部半径缩到 `r_max`，`max_domain_truncated_fraction`
会从 ~1e-8 升到 9–14%。**这不是域截断**——收尾警告会**换口径**说明，
明细在 `fluence_ledger.threshold_window`；规划的每个候选带
`windowRadiusPolicy` / `windowCellsSkippedFraction`。
`classify_exceedance`（受限阈值协议）不受影响：被裁格子本来就判「未超阈」。

实效（均**逐位一致**）：域 400 / 加工区 200 / dx 0.5 由 **77.58 s → 3.21 s（24.2×）**。

### 4.5 两个已经踩过的坑（务必不要再犯）

**① 「窗口内跨度」是回归**。光束脚下可能恰好是**未加工的平地**、
而 20 µm 外是 ~95 µm 深的坑；此时窗口内跨度趋近 0 ⇒ 窗口缩到 4 µm，
而 `r(u)` 在 `u* = A/e` 处有**峰值 19.95 µm**（本算例）⇒
把 (4 µm, 19.95 µm) 这段**仍能烧蚀**的环带整段切掉。
事件 40 实测：局部窗口 8×9 只算出 **20** 个非零格，真实 **87** 个。

**② 只测 N=1 是无效验收**。上面那个偏差在 N=1 恰好不触发，
所以 25 个候选里 15 个不一致而单点抽检全绿。**验收必须覆盖 N≥2 的多遍候选**
（回归测试：`test_multi_pass_candidates_stay_bitwise_identical`）。

---

## 5. 响应核（`response.py`）

`log_fixed` 解析律（当前标定用的就是它）：

```python
mask   = F > threshold_internal          # 严格大于
values = δ · ln(F / threshold_internal)  # 仅 mask 内
```

* 语义字段必须齐全：`kind` / `output_semantics=event_depth_increment` /
  `fluence_basis=incident_peak_fluence` / `depth_direction` / `threshold_J_m2` / `delta_m`；
* 缺 `depth_direction`、用体积曲线反推局部深度、缺 δ 请求深度 —— 一律抛**既定错误码**，不降级；
* `HistoryState(enabled=False)`（M0 不支持扫描孵化，显式报错而不是静默忽略）；
* 曲线驱动核 `TabulatedEventLaw`（U07）：查表越界返回唯一码 `TABLE_OUT_OF_RANGE`，
  **绝不返回 0、不外推、不钳端点**；默认分段线性**手写二分**实现。

---

## 6. 路径与事件（`paths.py`）

* 只有落在**出光段**内的事件才产生；计算域外的焦点**照样产生事件**（是否照射到由窗口判）；
* 弓字形（serpentine）填充矩形**加工区**，**间距不拉伸**；`pass_id` 记层数 N；
* 焦点 Z 恒定（不做逐层 Z 调整）——这一点会写进界面提示；
* 事件数 = Σ 各段 `floor(span / pitch) + 1`，`pitch = v / f`（沿扫描方向的脉冲间距）。

---

## 7. 规划（`planning.py`）

* **每个候选都真跑一遍求解器**，且与标定**共用同一条装配路径**（`build_row_config`），
  测试断言「规划算的深度」与「标定算的深度」完全相等；
* **仿真域 vs 加工区**分开：网格按域建，路径只在加工区内走，二者同心；
  加工区 > 域 ⇒ **报错**；
* **统计只算加工区子块**，并单列「外围最大去除深度」（应 ≈ 0，用来验证走刀没出加工区）；
* 两级网格：加工区 ≥ 100 µm 时自动把 `dx` 放宽到 ≥ 1 µm 做**粗筛**，
  只把推荐/最接近那一个用**细网格复核**；两级步长都写进结果并说明
  「表值来自粗筛、形貌来自细核，不一致以细核为准」；
* 可行性：`|平均深度 - 目标| ≤ 容差` **且** 覆盖率 ≥ 98% **且** 过切占比 ≤ 5%；
  无可行方案时给**原因** + 明确标注的 `best_effort`（不满足约束，仅供诊断）；
* 返回 `machined_shape = "rectangular_pocket"` 与矩形槽形貌（下采样热图 + 中心截面）。

---

## 8. 标定（`calibration.py`）

* `ExperimentRow` 是**装配单位**：脉宽/频率/速度/间距/层数/实测平均深度；
* `PredictionSpec` 决定域、dt、加工区与响应覆盖；
* 反推基线（C3）：跨频率才能解耦 `Fth` 与 `δ`（单频率共线，已硬拦）；
  可辨识性 `δ×2 → 1.152`、`Fth×2 → 0.239`（Fth 欠辨识）。
  当前候选值 `data/baselines/alsic_223fs_candidate.json`：
  `Fth = 7.8496e4 J/m²`、`δ = 3.6525 µm`（**「初值级别，非可用模型」**，
  训练中位相对误差 43.7% / 留出 58.7%）；
* 标定增益 `a` 与 `δ` **不同时放开**（两者都控幅度，会互相抵消）。

> ⚠️ **本算例的深度会到 132 µm**，远超实验目标 1 µm 量级 —— 这是 C3/C5/C6
> 记录的未决问题（「覆盖好的候选深度 ≥30 μm、无可行解」）。
> 性能策略与深度量级**正交**，但深度合理后窗口本来就会小很多。

---

## 9. Web 界面（`webui/` + `webapp.py`）

三工作区 + 开发工具：**数据与工况 / 对照与标定 / 规划与结果 / 开发工具**。

* **`ui_service.submit()` 是唯一求解入口**，`solve_count` 只在这里 +1；
  切图层、回放历史、查表**都不动计数**；
* **不得用 `st.form`**；前端为原生 JS（`api.js` / `main.js` / `data.js` / `v2.js`）；
* 未提供图层如实报 `UNAVAILABLE_LAYERS`，不给全 0 假数组；
  `threshold_only` 的深度是「不提供」而**不是** `0`；
* 规划页两张卡片别混：**「目标与规划」**里有 `pl-surface`（矩形槽形貌，
  点「枚举 h / N」后才显示）；**「形貌与结果」**显示的是**上一次提交的求解**——
  单点路径就是圆坑，那不是矩形槽。

---

## 10. 工程纪律（红线）

1. **逐位一致**是优化的验收标准：`np.array_equal(h_before, h_after)`，**不设容差**；
2. **证据状态五种**，不用 `synthetic`；`null` = 缺数据、`unknown` = 条件未确认，
   **不得**用相近材料/不同脉宽补值；
3. 「公式核查 / 数值实现验证 / 实验复现」**三栏分开**报；软件跑通 ≠ 材料验证；
4. 合成模式内部无量纲，**禁止导出 `depth_um`**；未开放组合必须在**配置层**拦，
   不能只靠按钮禁用；
5. **不得静默退化**：取不到阈值、参数非法、策略不支持 —— 要么报错，要么
   **回退 + 写警告**，并在诊断里如实上报口径变化；
6. 原始输入 xlsx/json 哈希须合任务书 16.3，**不得覆盖**；临时产物只放 `runs/`。

---

## 11. 速查：文件 ↔ 函数

| 想改/想看 | 位置 |
|---|---|
| 事件枚举 | `paths.iter_events()`；段定义 `config.PathSegment.position_at()` |
| 能流/窗口 | `beam.beam_patch()`；半径 `cut_radius()` / `max_ablation_radius()` |
| 窗口策略 | `BeamOptions.window_radius_policy`；`SolverConfig.window_radius_policy` |
| 响应律 | `response.FixedThresholdLogLaw.increment()`；曲线核 `TabulatedEventLaw` |
| 提交高度 | `surface.apply_increment()` |
| 累计剂量 | `surface.accumulate_illumination()` |
| 主循环 | `solver.solve()`（第 604 行起的逐事件循环） |
| 账本/口径 | `result.diagnostics["fluence_ledger"]`（含 `threshold_window`） |
| 规划 | `planning.plan_for_target()` / `evaluate_candidate()` |
| 标定装配 | `calibration.build_row_config()` |
| 规划接口 | `webcontract.plan_payload()`；前端 `webui/js/v2.js` |
| 界面开关 | `webui/index.html` 的 `#pl-window-policy` |

---

## 12. 已知问题 / 待办

| # | 问题 | 状态 |
|---|---|---|
| 1 | 深度 132 µm 远超 1 µm 目标（δ 是「初值级别」值） | 未决（C3/C6），物理侧 |
| 2 | h/N 无可行解（覆盖好的候选深度 ≥30 μm） | 未决（C5/C6） |
| 3 | `mode=grouped`（分组批量）不支持窗口半径策略 | 已写警告，未实现 |
| 4 | 默认 `tail_epsilon` 口径的窗口仍是**全网格**跨度 ⇒ 剂量观测量随域大小变化 | 未决（可用 §4.4 上界作默认下限，属独立决策） |
| 5 | 耗时预估的每事件常数是**本机实测**外推（2/14/22 ms 分档；超阈 `0.79×(0.5/dx)²` ms） | 换机器需重测 |
