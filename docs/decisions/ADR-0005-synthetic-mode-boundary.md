# ADR-0005：合成模式边界与“沙盒可继续”的落实方式

* 状态：已采纳｜日期：2026-09-10｜批次：A（T02）

## 背景

任务书第 7.3 节写“激光条件不匹配时，严格参考模式拒绝运行；沙盒可继续”。
这句话如果不落到显式字段上，很容易变成“校验失败后自动降级”。

## 决定

1. **禁止自动切换模式**。校验失败就是失败；用户必须**显式选择**
   `run_mode = "synthetic_demo"`，并且必须显式给出 `unit_system = "dimensionless"`
   与 `reference_scales`（`L_ref_m`、`F_ref_J_m2`、`delta_ref_m`）。
2. **单位约束**：`unit_system = dimensionless` 只允许搭配
   `run_mode = synthetic_demo`，否则 `CONFIG_INVALID`。
3. **配置分别保存**：合成配置与参考配置是两份独立 JSON，
   各自写入自己的 `runs/<run_id>/`。
4. **标签贯穿**：合成卡 `data/materials/_synthetic_demo_isotropic.json`
   使用 `source_type = "synthetic_definition"`、`fixture_only = true`、
   `applicability.physical_material_prediction_allowed = false`；
   文件名以 `_` 开头以便目录加载时区分。
5. **禁止物理深度导出**：`UnitContext.allows_physical_depth_export` 为
   `False`，截面与统计标签使用 `L_ref`（不是 `delta_ref`；见 ADR-0012「相关修订」），
   不生成 `depth_um`。

## 后果

`validate_run` 对合成模式额外给出说明性 note；
`unit.mode == "dimensionless"` 时 `synthetic_demo` 之外的所有模式直接报错。
