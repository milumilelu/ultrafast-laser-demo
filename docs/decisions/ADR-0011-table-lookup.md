# ADR-0011：批次 F 查表的 schema、插值核与越界红线决定

* 状态：已采纳｜日期：2026-09-10｜批次：F（T10）

## 背景

执行细则第 6 节把批次 F 定义为「曲线 schema；线性插值；可选 PCHIP；越界处理」，
必交「示例 CSV、错误 CSV、原始点与插值图」；第 7 节给出查表最少字段，并给出两条
红线：

1. **查表不是任意外推**（任务书 3.5）：先支持固定材料/波长/脉宽/历史协议下的
   一维响应曲线；默认分段线性，需要平滑时用保形 PCHIP。
2. **越界返回状态和原因；低于量测区间不自动返回零**（细则 7 节末）。

任务书 G09 另要求「曲线横坐标严格排序且无重复；重复 x 不静默删除，须附规则并保留
原始点」，且「只有 `event_depth_increment` 曲线能进事件核；仅有终态体积/去除效率时
没有额外形状假设，不能唯一反推每个位置的去除深度」。

## 决定一：一条曲线 = JSON 元数据卡 + CSV 原始点

* `<curve_id>.curve.json`：元数据，逐项校验 `REQUIRED_CURVE_FIELDS`（细则 7 节的
  最少字段：`schema_version / curve_id / material_id / material_identity / x_quantity /
  y_quantity / output_semantics / fixed_conditions / protocol / source_figure_or_table /
  valid_range / points_file`），缺一即拒。
* `<curve_id>.points.csv`：两列 `x,y`，允许 `#` 注释行。**CSV 是原始点的唯一来源**，
  加载后原样保存在 `ResponseCurve.raw_points`（含重复 x），去重结果在 `points`。

理由：与材料卡「JSON 元数据 + 数据文件」的既有约定一致；报告与界面可以同时展示
「原始点」与「用于插值的点」，避免「数据处理不可见」。

## 决定二：默认分段线性，手写二分实现，**不用 `numpy.interp`**

`numpy.interp` 在区间外会**静默把结果钳到端点**——这正是红线 2 禁止的行为。
因此 `_linear()` 是手写的二分分段线性：节点处精确、区间内线性、**区间外根本不进入
本函数**（越界在 `lookup()` 里先行拦截并返回状态）。

## 决定三：PCHIP 显式关闭外推；缺 SciPy 报 `NOT_IMPLEMENTED`

`PchipInterpolator(..., extrapolate=False)`。SciPy 的 PCHIP **默认允许外推**，
若不显式关闭就会绕过红线 1。

若环境缺 SciPy：`lookup(method="pchip")` 抛 `NOT_IMPLEMENTED`，**绝不静默退化为
线性**（任务书 3.5：越界/不可用要显式返回，不能悄悄换算法）。`pyproject.toml` 的
`table` extra 声明 scipy；本环境已安装 scipy 1.18.1。

## 决定四：越界是显式状态，**绝不返回 0、绝不外推、绝不钳端点**

* `lookup()` 默认在越界时抛 `TABLE_OUT_OF_RANGE`（唯一越界错误码）；
* `allow_out_of_range=True` 时返回 `TableLookup`，越界项 `values[i] = None`
  （**不是 0**），状态为 `below_range` / `above_range` / `mixed_out_of_range`。

「低于量测区间」不等于「无去除」：零点只有在曲线自带独立阈值律时才来自数据本身。
界面据此提示，而不是显示数值 0 冒充「无去除」（与批次 E 的 `threshold_only` 显示
「不提供」同口径）。

## 决定五：重复 x 默认拒绝；需要合并时必须声明规则并保留原始点

`load_curve_points(csv, *, duplicate_policy="reject", duplicate_rule_note=None)`：

* 默认 `reject`：出现重复 x 直接 `CONFIG_INVALID`，**不静默删除、不静默平均**；
* 允许 `mean` / `first` / `last`，但必须同时给 `duplicate_rule_note`，并把处理记录
  写进 `DuplicateReport`（策略、规则、重复 x 列表、合并数、原始点/唯一点计数）。

`ResponseCurve.raw_points` 永远保留 CSV 全部原始点，可被报告与界面复核。

## 决定六：语义路由——只有逐事件增量曲线能进事件核，体积不得反推局部深度

* `CURVE_ROUTES` 把 `event_depth_increment` 映射到 `event_kernel`，其余
  （`mean_depth_per_effective_pulse` / `cumulative_depth` / `track_pass_depth` /
  `volume_per_energy` / `threshold_only`）映射到 `evaluator`。
* `assert_curve_can_enter_event_kernel(curve, laser=None)`：语义不符抛
  `RESPONSE_SEMANTICS_INVALID`；给了 `laser` 时按 `fixed_conditions` 逐项比对，
  不匹配抛 `CONDITION_MISMATCH`（`value: null` 的条件视为「未定义」，跳过）。
* `assert_no_local_depth_from_volume(curve, requested_quantity)`：非逐事件增量曲线
  若被要求「深度」类量，抛 `CONFIG_INVALID`——体积/终态效率没有额外形状假设时
  **不能唯一反推局部深度剖面**。

**本批不接入逐事件主循环**（那是批次 H 的 T14），只提供闸门与拒绝路径；查表本身
不产生任何形貌（不存在 `final_surface.npz`）。

## 决定七：查表是纯读取，界面查表不触发求解

`ui_service.table_lookup()` / `table_grid()` 内部断言 `solve_count` 不变，
与批次 E 的 `layer_view()` / `snapshot_arrays()` 同口径。`app.py` 新增「查表」标签页：
读曲线卡、显示原始点、查值（线性/PCHIP、可勾选允许越界）、画原始点与插值图，
全程 `solve_count` 保持为 0（由 `tools/ui_probe.py` 的查表检查与
`tests/test_app_smoke.py` 端到端复核）。

## 后果

* 好消息：越界、缺依赖、语义不符、条件不符四类问题各有唯一错误码，可被外部复核；
  插值算法与数据处理对用户可见（原始点 + 插值线同图）。
* 代价：查表只覆盖「固定协议下的一维响应」，跨工况需另附条件匹配的曲线；PCHIP 在
  节点区间外不作为（不预测、不填补）。这些都是刻意的。
* 已知局限：`interpolate_grid()` 只在有效区间内等距采样，越界区间**不画虚线外推**。
