# ADR-0014：批次 I 的冻结几何分组、局部误差估计与回退决定

* 状态：已采纳｜日期：2026-09-11｜批次：I（T16、T17）

## 背景

执行细则第 9.1 节与任务书第 11.1 节把批次 I 定义为「测量基线；局部优化；冻结批量；误差和回退」，
必交「B01–B04 性能记录、G08 逐脉冲对照」。核心约束（原文口径）：

* 「保留逐脉冲 NumPy 路径为**参考实现**。先测量局部计算开销，**再决定**是否引入 Numba；
  **不得并行化依赖历史的事件轴**。」
* 「每个脉冲**单独**计算非线性响应后相加，**不先累加能流**。」
* 「事件块和空间块**同时限额**，不构建全事件×全网格张量。」
* 「每个候选批次从相同初态试算 B 和两个 B/2，比较更新场；失败则**不提交状态**，缩小 B 重试。」
* 「局部误差估计用于**控制步长**，完整逐脉冲对照用于**最终验收**。」
* 「频繁阈值切换、相界面、未支持历史或遮挡变化触发**禁用/回退**，并保存原因。」
* 「第一版**不开放**『批量+动态角度』组合。」
* 「优化前后使用**相同配置**；若无收益**如实保留参考模式**，不宣称更快。」

本节把上述约束固化为可执行决定。

## 决定一：分组是**独立求解模式**，局部核后端是正交选项

* `solver.mode` ∈ {`reference`, `grouped`} —— 决定**走哪条求解路径**；
* `solver.acceleration` ∈ {`off`, `numba`} —— 决定分组路径里**局部数值核的编译器**；
* 批策略参数（`batch_size` / `min_batch_size` / `local_rel_tol` / `local_abs_tol_internal` /
  `geometry_drift_limit` / `max_cell_block`）随之落入 `SolverConfig`，并在 `from_dict` 处校验
  （如 `min_batch_size <= batch_size`、`0 < local_rel_tol < 1`）。

理由：把「求解路径」与「编译后端」分开，才能做**同配置对照**（B02 与 B02-numba 只换后端）。

## 决定二：块式累计，**逐事件各自算非线性响应后相加**

`accelerators.accumulate_block` 在**块起点几何**下遍历块内事件，对每个事件：

1. 算该事件的局部能流窗口 `patch`（相同焦点的连续脉冲命中**补丁缓存**，只算一次光束）；
2. **单独**调用响应核 `a(F_j)`（或用局部核后端）；
3. 把 `a_j` 累加到块级增量 `Σ_j a_j`。

**红线**：绝不先算 `Σ_j F_j` 再取一次对数——那会改变非线性响应。G08 用可判别构造验证：
解析算例取 `F₀ = e²·Fth`，两脉冲的正确结果是 `2δ·ln(F₀/Fth) = 4δ`，而错误路径
`δ·ln(2F₀/Fth) ≈ 2.69δ`，二者被显式分离断言。

## 决定三：局部误差估计是「B 与两个 B/2」，且 `fixed_geometry` 下**跳过半步**（严格等价）

* 标准情形：先算整块一步（用块起点几何），再算两个半步（前半块用块起点几何、
  后半块用**前半块结束后**的几何），比较两者更新场，给出**最大绝对差**与**归一化 L2 差**。
  奇数块按两个**尽量等长**的子块切分（前半取较大的一半）。
* **优化（严格等价，不是降低校验）**：当几何与当前高度**无关**时（`fixed_geometry` 下
  `beam_patch` 用 `initial_height` 算离焦），半步的中间几何**根本不参与能流计算**，
  一步与两个半步在数学上恒等 → 跳过半步试算，节省 2/3 的试算开销。
  该情形由 `drift_reference_for(config)` 返回 `None` 标识；诊断里如实写
  `local_error_estimated=false` 并说明理由。

实测收益（本机）：B01 2.52×→3.78×，B04 0.744×→**1.389×**（优化前 B04 分组反而比参考慢）。

## 决定四：几何漂移判据只在几何**真正影响能流**时生效

* `fixed_geometry`：能流只用初始面 → 块内去除多少都不改变后续能流 → **不存在漂移**，
  参考长度取 `None`，该判据不参与。
* `axial_defocus`：能流用**当前高度** → 块内高度变化会改变后续脉冲的离焦 →
  以离焦尺度（`zR`，缺省 `w0`）为参考长度，判据为
  `块内最大去除量 / 参考长度 <= geometry_drift_limit`（默认 0.25）。

实测：`zR = 5μm`（漂移 0.4）时该块被**拒绝并缩小 B**；`zR = 10μm`（漂移 0.2）时
深度偏差 0.56%、体积偏差 0.19%，均满足 G08 的 1% 判据。

> 早期版本误把参考长度取成 `delta`（100 nm），导致「10 脉冲累计 2 μm」这类正常算例
> 漂移比恒为 8，批量退化成 B=1 而失去意义。这是必须避免的语义错误。

## 决定五：回退条件在**配置层**拦截；求解器侧另留兜底

红线三条（`validate_run` 内 `CONFIG_INVALID`）：

| 组合 | 理由 |
|---|---|
| `grouped` × `structured_interface` | 相标签会随去除更新，冻结几何无法表达跨相界面截断 |
| `grouped` × `history_enabled` | 批量不得改变事件顺序依赖 |
| `grouped` × `dynamic_angle` | 两个增强第一版不得同时启用（细则 9.1 末） |

`accelerators.check_fallback_conditions` 是同一组判据的**可调用版本**（供测试与求解器复用），
求解器在进入分组前再判一次；不满足则**回退逐脉冲**并写入 `warnings` 与
`metadata.acceleration.fallback_reason`（不静默降级）。

## 决定六：Numba 实测**无收益** → 默认 NumPy，仅保留可选后端

T16 要求「先测量局部计算开销，**再决定**是否引入 Numba」。实测结论是**不作为默认**：

* 固定阈值对数核是**逐元素**运算，NumPy 的 `np.log` 已是 SIMD 向量化实现；
* Numba 版本（`njit`，未并行）需逐元素循环 + 调用/调度开销，在 161²–512² 窗口下**反而更慢**
  （见 `performance_baseline.md` 的 B02 与 B02-numba 行，同配置只换后端）；
* `cache=True` 的 JIT 编译耗时单独记录在 `metadata.acceleration.local_kernel_jit_time_s`；
* 因此 `acceleration` 默认 `off`；请求 `numba` 而环境缺失时，**回退 NumPy 并给出警告**
  （结果同式、逐位一致，只是没有 JIT）——这符合任务书「缺失时回退 NumPy」，
  且因为结果不变，不属于「静默降低精度」。

## 决定七：快照在**块边界**触发，并如实标注为近似

分组模式按块前进，块内多个事件无法逐个记录瞬时形貌。因此快照在**块提交后**触发，
索引标注为**该块内最后一个命中事件**，实际对应块结束时的表面状态。
只要发生过这种情况，就把说明写入 `metadata.approximations`
（`grouped_snapshot_on_block_boundary`），并在诊断里记 `snapshot_on_block_boundary=true`。

理由：细则要求「不把近似当精确」；回放粒度变化必须可被外部看见。

## 决定八：计数语义与逐脉冲**逐位对齐**

`SurfaceState.apply_block_increment` 接收块级数组：

* `delta_h`：累加后的高度增量；
* `touch_counts`：每个单元在块内**被去除的次数**（不是布尔量）——直接累加即可与逐脉冲的
  `exposure_count += 1` 对齐；
* `fluence_sum` / `illum_counts`：累计入射剂量与照射次数；
* `exceed_counts` / `exceed_or`：受限阈值协议观测量（批次 H），逐事件按**本事件入射能流**判定。

**提交窗口必须是"被照射"的包围盒，而不是"被去除"的包围盒**：照射域（含 `F < Fth` 的外围）
严格大于去除域；若只按 `delta_h > 0` 取窗口，外围单元的 `illumination_count` 与
`cumulative_fluence` 会被丢掉。这是 G08 测试实际抓到的一个 bug，已修复。

## 后果

* **好消息**：分组与逐脉冲在 `fixed_geometry` 下深度场最大绝对差为浮点舍入量级
  （实测 4.2e-22），整数计数逐位一致；`axial_defocus` 弱离焦下满足 1% 判据；
  拒绝/回退路径有独立断言（`tests/test_grouped_solver.py`，26 项）。
* **代价**：批量第一版不支持分相、历史与动态角度；快照粒度变为块边界；
  扫描工况下若收益不足，应保留参考模式（求解器不强制）。
* **不承诺性能**：细则明确不得预先承诺「实时」「百万脉冲秒级」或固定加速倍数；
  报告只给本机本环境的实测值与口径说明（含 `tracemalloc` 的内存统计边界）。

## 相关修订（本批一并处理）

* `config.py`：`SolverConfig` 新增批策略六个字段与校验；`validate_run` 从「一律拒绝
  `acceleration != off`」改为「按 mode/acceleration 分别判定 + 三条红线」。
* `accelerators.py`：从占位改为完整实现（`BatchPolicy` / `GeometryView` / `LocalKernel` /
  `accumulate_block` / `estimate_local_error` / `solve_block` / `check_fallback_conditions`）。
* `surface.py`：新增 `apply_block_increment`（批量路径的唯一状态提交入口）。
* `solver.py`：新增 `_event_batches` 与 `_grouped_event_loop`；参考模式也记录
  `metadata.acceleration`，避免界面/导出读到空字段。
* `tests/test_validation_edges.py`：旧断言「`acceleration=numba` 应被拒」已过时
  （那是批量未实现时的行为），改写为断言三条新红线。
* `tools/perf_report.py`：B01–B04 基准 + 分组/参考对照 + 环境与内存口径说明。
