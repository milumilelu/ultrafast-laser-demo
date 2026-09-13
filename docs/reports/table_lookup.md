# 查表报告（批次 F / T10）

> 对照 `docs/reports/progress.md` 与本目录的验收报告阅读。

## 1. 曲线清单（示例 CSV）

| 曲线 | 材料 | 横轴（单位） | 纵轴（单位） | 语义 → 去向 | 有效区间 | 原始点 | 可派生量 |
|---|---|---|---|---|---|---|---|
| `analytic_fixture_depth_vs_fluence` | `analytic_fixture_not_a_material` | peak_fluence (J/cm^2) | removal_depth_per_pulse (m) | event_depth_increment → 逐事件核（需协议适用） | [1.0, 40.0] | 9 | removal_depth_per_pulse,local_depth |
| `sic_threshold_vs_effective_n` | `sic_4h_cface_1035nm_multishot` | effective_count (1) | threshold_fluence (J/cm^2) | threshold_only → 评估器（不得生成局部形貌） | [1.0, 720.0] | 7 | threshold_fluence |
| `synthetic_volume_per_energy` | `synthetic_demo_isotropic` | pulse_energy_over_E_ref (1) | removal_volume_over_L_ref3 (1) | volume_per_energy → 评估器（不得生成局部形貌） | [0.35, 8.0] | 6 | removal_volume,removal_volume_per_energy |

曲线卡与点文件都在 `data/curves/`：卡是 JSON 元数据（执行细则 7 节的查表最少字段），点文件是 CSV（`x,y` 两列，允许 `#` 注释）。**CSV 是原始点的唯一来源**，加载后原样保留在 `raw_points`，去重结果在 `points`。

## 2. 错误 CSV：被拒收的输入与错误码

- 汇总：通过 21｜失败 0
- 机读明细：`docs/reports/table_errors.csv`

| 案例 | 类别 | 期望错误码 | 实际错误码 | 状态 | 说明 |
|---|---|---|---|---|---|
| `invalid_unsorted` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线横坐标必须严格升序 |
| `invalid_duplicate` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线存在重复横坐标（1 组，共 1 个重复点） |
| `invalid_duplicate_no_rule` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 声明了重复 x 处理策略但未给出重复试验处理规则 |
| `invalid_nonfinite` | card_schema | `NUMERIC_NONFINITE` | `NUMERIC_NONFINITE` | 通过 | 曲线点出现非有限数值 |
| `invalid_single_point` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线至少需要 2 个点才能插值 |
| `invalid_bad_header` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线点 CSV 表头必须是 x,y |
| `invalid_nonnumeric` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线点无法解析为数值 |
| `invalid_range_narrow` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 数据点落在声明的有效区间之外 |
| `invalid_volume_depth_dir` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 体积曲线不得声明 depth_direction |
| `invalid_missing_field` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线缺少必填字段 'protocol' |
| `invalid_empty` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线至少需要 2 个点才能插值 |
| `invalid_bad_semantics` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 曲线 output_semantics 未登记 |
| `invalid_no_depth_direction` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 逐事件增量曲线必须声明 depth_direction |
| `invalid_no_unit` | card_schema | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | y_quantity.unit 必须是非空字符串（单位不可省略） |
| `lookup_below_range` | behavior | `TABLE_OUT_OF_RANGE` | `TABLE_OUT_OF_RANGE` | 通过 | 越界：低于有效区间下界（不得返回 0） |
| `lookup_above_range` | behavior | `TABLE_OUT_OF_RANGE` | `TABLE_OUT_OF_RANGE` | 通过 | 越界：高于有效区间上界 |
| `pchip_without_scipy` | behavior | `NOT_IMPLEMENTED` | `NOT_IMPLEMENTED` | 通过 | PCHIP 缺 SciPy 时必须显式报错，不得静默退化为线性 |
| `local_depth_from_volume` | behavior | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 体积曲线不得反推局部去除深度 |
| `event_kernel_from_threshold` | behavior | `RESPONSE_SEMANTICS_INVALID` | `RESPONSE_SEMANTICS_INVALID` | 通过 | 阈值曲线不得进入逐事件核 |
| `condition_mismatch_on_event_curve` | behavior | `CONDITION_MISMATCH` | `CONDITION_MISMATCH` | 通过 | 固定条件不匹配时拒绝逐事件核 |
| `unknown_interpolation_method` | behavior | `CONFIG_INVALID` | `CONFIG_INVALID` | 通过 | 未登记的插值方法拒绝 |

## 3. 原始点与插值图

- 图：`docs/reports/curve_interpolation.html`（原始点散点 + 分段线性 + 保形 PCHIP）
- 数值：`docs/reports/curve_interpolation.csv`（长表：每条曲线的 201 点插值线 + 原始点）

## 4. 具体执行方式

| 要求 | 实现 | 位置 |
|---|---|---|
| 最少字段（细则 7 节） | `REQUIRED_CURVE_FIELDS` 逐项校验，缺一即拒 | `tables.py` |
| 默认分段线性 | 手写二分分段线性（**不用** `numpy.interp`，避免区间外静默钳端点） | `_linear()` |
| 可选 PCHIP 且关闭外推 | `PchipInterpolator(..., extrapolate=False)`；缺 SciPy 报 `NOT_IMPLEMENTED`，**不静默退化** | `_pchip()` |
| 越界返回状态与原因 | `TABLE_OUT_OF_RANGE`；`allow_out_of_range=True` 时返回 `None`（**不是 0**） | `lookup()` |
| 低于量测区间不自动变零 | 越界项一律 `None`；只有曲线自带独立阈值律时零点才来自数据 | `lookup()` / 曲线点 |
| 横坐标严格排序且无重复 | 未升序拒绝；重复 x 默认拒绝 | `load_curve_points()` |
| 重复 x 不静默删除 | 需 `duplicate_rule_note`；原始点保留在 `raw_points` | `load_curve_points()` |
| 只有逐事件增量曲线进事件核 | `assert_curve_can_enter_event_kernel()`（另含条件比对） | `tables.py` |
| 体积不得反推局部深度 | `assert_no_local_depth_from_volume()` | `tables.py` |
| 曲线语义不新增枚举 | `output_semantics` 只允许执行细则 4.3 的 6 个值 | `load_curve()` |

## 5. 边界声明

- 本报告只做**公式核查**与**数值实现验证**：
  YSZ 曲线来自解析 fixture（可与人解析式逐点比对）；
  SiC 曲线由**材料卡内拟合参数按式(6) 重算**，不是原图逐点数字化，因此
  **不构成对原文曲线的复现**；合成体积曲线无任何实验来源。
- 查表本身**不产生**形貌：不存在 `final_surface.npz`。逐事件核接入属批次 H（T14），
  本批只提供闸门与拒绝路径。

## 6. 复现命令

```bash
python tools/make_curves.py
python tools/table_report.py
python -m ufdemo table data/curves/analytic_fixture_depth_vs_fluence.curve.json --x 5
python -m ufdemo table data/curves/analytic_fixture_depth_vs_fluence.curve.json --x 2.5 --method pchip
python -m pytest -q -m g09
```
