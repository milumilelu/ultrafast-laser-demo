# ADR-0012：批次 G 的分相结构、跨相界面截断与红线决定

* 状态：已采纳｜日期：2026-09-10｜批次：G（T11–T13）

## 背景

执行细则第 6 节把批次 G 定义为「分相查询；颗粒；铺层；界面截断」，
必交「两个合成结构实例、种子与结构统计、G06 报告」；第 8 节给出结构接口与
界面更新规则；G06 的测试项（细则 10 节）为「同相合并等价；不同相不跳过；
不拆伪脉冲；固定种子重现」。任务书另列两条与该批直接相关的红线：

| 材料入口 | 必须拦截 |
|---|---|
| 铝基 SiC | **借用单晶 SiC 当颗粒相标定** |
| CFRP | **整体阈值拆给树脂/纤维；缺 δ 生成物理深度** |

本节把上述约束固化为可执行决定，避免后续实现走样。

## 决定一：结构接口为 `phase_at(x,y,z)` / `next_different_interface(x,y,z)`，解析几何，不建三维全体素场

* `src/ufdemo/structure.py` 提供 `LayeredStructure`（沿 z 的分段相列 + 纤维条纹）
  与 `ParticleStructure`（射线-球求交），二者都实现：
  * `phase_at(x, y, z)`：逐点返回相编号（内部单位）；`x`、`y` 逐列，
    `z` 为一维数组（本质是「沿 z 的相列」，不对全局网格局部分配三维体素场）。
  * `next_different_interface(x, y, z)`：返回从 `(x,y,z)` 沿 −z 到**下一个不同相**
    界面的距离；同相层界返回 `inf`（即「不在这里截断」）。

理由：细则第 8 节明确「初版可采用解析颗粒/纤维与沿 z 的分段相列，避免默认创建
高分辨率三维全体素场」。解析求交让界面距离有闭式解，可被单测逐位核对。

## 决定二：同相相邻区间构造时**预先合并**，接触界面用统一浮点容差

* 构造 `LayeredStructure` 时按「相指纹」（相编号 + 纤维角 + 纤维体积分数 +
  纤维宽度）合并相邻同相铺层；暴露 `original_layer_count` / `merged_layer_count`
  以便外部核对（G06「同相合并等价」逐位比对 `phase_at` 与 `next_different_interface`）。
* 接触界面比较使用统一相对容差 `CONTACT_TOL_REL`，而非各点自定义 epsilon；
  单测覆盖「恰好到达界面的下一个脉冲」，防止卡在零厚层（细则第 8 节规则 5）。

## 决定三：一个真实脉冲只调用**当前暴露相的一个核**，不拆伪脉冲

* `solver._per_phase_candidate()` 对每个单元只按该单元当前 `phase_id` 分派到
  **一个**相核；`applied = min(候选去除量, next_different_interface)`。
* 不把一次脉冲拆成「先按 A 相算一段、再按 B 相算一段」的两相叠加。
  G06「不拆伪脉冲」用单事件 CFRP 案例断言：中心列一次应用量**精确等于**
  单一纤维核在该能流下的候选量（`rel <= 1e-12`）。

理由：细则规则 1「当前脉冲只调用当前暴露相的响应」；把同一脉冲的能量摊到两相
属于无依据的能量重分配。

## 决定四：跨相截断为**有损近似**，命名固定为「未应用候选去除体积」

* `surface.apply_increment(structure=...)`：`applied = min(candidate, dist)`；
  到界面后**更新相标签**，本事件不对新相重复施加完整能量——**下一个真实脉冲**
  才对新相响应（细则规则 3）。
* 诊断字段（`result.diagnostics["removal"]`）：
  `clipped_events`（发生截断的事件数）、`n_clipped_cells`（单元次数）、
  `phase_switch_cells` / `phase_switch_events`、
  `unapplied_candidate_removal_volume_internal`（候选减实际的体积总和）。
* **命名红线**：该量是「未应用候选去除体积」，**不是剩余能量**，
  也不是界面能量传输结果。报告与界面的键名/文案都据此（报告生成器断言
  `removal` 键名不得含 `residual` / `energy` 字样）。

## 决定五：δ 必须写作 `delta_over_L_ref`，旧字段 `delta_over_delta_ref` 一律拒绝

* 任务书「合成模式的单位规则」与细则第 7 节：参与几何/光学/离焦的长度必须采用
  **同一**尺度 `L_ref`。相去除尺度的 `delta` 也按 `δ/L_ref`（键名
  `delta_over_L_ref`）给出，与层厚、光斑、离焦同尺度。
* 若误用 `delta_over_delta_ref`（`δ/delta_ref`），深度会整体放大
  `L_ref/delta_ref`（本工程 100）倍。`structure.build_phase()` 遇到该键
  直接抛 `RESPONSE_SEMANTICS_INVALID`，不做兼容、不猜意图。
* 派生：`config.UnitSystem.depth_label` 在合成模式返回 `L_ref`（不是 `delta_ref`），
  `delta_ref_m` 仍留在 `reference_scales` 里，需要 `d/delta_ref` 显示时按
  `delta_ref_m / L_ref_m` 换算（见「相关修订」）。

## 决定六：三条**红线**在配置/schema 层拦截，不靠按钮禁用

1. **相不得借用其它材料卡做标定**（细则材料表）：相配置带 `material_id` 指向
   别的材料卡 → `MATERIAL_CAPABILITY_MISSING`。这正是「铝基 SiC 借单晶 SiC
   当颗粒相标定」的拦截。
2. **整体阈值不得拆给分相**：`assert_phase_threshold_not_split(phase, card, unit)`
   先把父卡 SI 整体阈值**归一化到 `F_ref`** 再与相阈值比较；相等即
   `MATERIAL_CAPABILITY_MISSING`。这是 CFRP「整体阈值拆给树脂/纤维」的拦截。
   相响应只允许内联合成定义（`response_source` 必须是内联），沿用父卡整体阈值
   来源同样被拒。
3. **分相截断 × 动态角度互斥**（细则第 8 节末）：`structured_interface=true` 与
   `solver.dynamic_angle=true` 同时在 `validate_run` 报 `CONFIG_INVALID`。
   法向去除路径与垂直相列路径不得混用；几何意义未单独定义前不开放该组合。

> 另有一条类型一致性检查：`structure.structure_type` 必须与材料卡声明一致，
> 否则 `CONDITION_MISMATCH`（防止把铺层卡配成颗粒结构）。

## 决定七：种子固定可复现；目标与实际体积分数**分别报告**，不强制相等

* 生成器保存 `seed` / `algorithm_version` / 生成参数于
  `result.metadata["structure"]`；`ParticleStructure(seed=...)` 顺序放置，
  同种子逐位一致、异种子确有差异（G06「固定种子重现」）。
* 体积分数分两栏：`target_volume_fraction`（名义目标）与
  `volume_fraction.actual_volume_fraction`（有限样本实际，附
  `volume_fraction_method`：`grid_sample` 或 `analytic_from_ply_pattern`，
  以及 `n_samples`）。细则第 8 节：有限样本不强制等于目标比例。

## 决定八：界面只读暴露——`phase_id` 图层与结构诊断面板，且不提供图层如实报不可用

* `ui_service.build_structure_diagnostics()` 把运行诊断 + 结构元数据规范化为
  一个纯逻辑字典（**不导入 Streamlit/Plotly**），均质单相运行返回 `{}`。
  **提交路径与历史重读路径共用同一函数**，保证「重读历史不求解」的不变量
  （G09 记账口径，见 ADR-0010）。
* `LAYER_LABELS` 新增 `phase_id`（「材料相标签（当前暴露相的编号）」）。
  未启用分相结构的运行时，`available_layers()` 如实报不可用、`layer()`
  返回 `None`——**不返回全 0 假数组**（与批次 E 的 `threshold_only`「不提供」
  同口径）。
* 措辞仍受 `FORBIDDEN_TERMS` 严格子串守卫（细则 11.3）：标签只描述「该图实际
  是什么量」，不出现「热影响区 / HAZ / 温度」等被禁词。

## 后果

* 好消息：同相合并、界面截断、伪脉冲、种子复现四类不变量各有独立断言
  （`tests/test_phase_interfaces.py`，21 项）与独立报告
  （`tools/structure_report.py` → `g06_phase_interfaces.md`），可被外部复核；
  三条红线在配置层拦截并给出唯一错误码。
* 代价：跨相截断是**有损近似**——被截断的候选量就此记账，不回流、不重分配；
  分相结构仅覆盖解析颗粒/铺层，不支持任意三维几何；M0/M2 不开放
  「分相截断 + 动态角度」组合。
* 已知局限：结构几何为**合成**，不含任何实验复现结论；相响应为内联合成定义；
  「低于量测区间」不等于「无去除」的口径继续适用（沿 ADR-0011）。

## 相关修订（本批一并更正）

* **ADR-0002** 与 **ADR-0005** 中「截面与统计标签使用 `delta_ref`」的措辞与细则
  第 7 节的合成模式单位规则（`h/L_ref`）及实现
  （`config.UnitSystem.depth_label` 返回 `L_ref`）不一致，本批更正为 `L_ref`：
  深度是直接从 `h/L_ref` 主减得的高度差，与平面几何/光斑/离焦同尺度。
  `delta_ref_m` 仍保留在 `reference_scales`，换算不丢失。
