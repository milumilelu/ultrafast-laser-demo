# ADR-0001：证据状态枚举统一为五值

* 状态：已采纳｜日期：2026-09-10｜批次：A（T01/T02）

## 背景

任务书第 4.2 节给出的证据枚举是
`unverified`、`literature_reported`、`formula_checked`、`experiment_reproduced`、
`independently_validated`；而第 7.2 节的示例卡片里写的是
`"evidence_status": "synthetic"`。执行细则第 2.1 节判定二者不一致。

## 决定

执行层**只使用第 4.2 节的五种状态**。产生该示例差异的“人工合成”含义改由
两个正交字段承载：

* `run_mode = "synthetic_demo"`（运行模式）
* `source_type = "synthetic_definition"`（来源类型）

实现位置：`src/ufdemo/config.py` 的 `EVIDENCE_STATUSES`、`SOURCE_TYPES`；
`materials.py` 在载入卡片时对非法取值直接抛 `CONFIG_INVALID`。

## 后果

* 测试 fixture 卡（`tests/fixtures/analytic_fixture.json`）使用
  `evidence_status = "unverified"` + `source_type = "analytic_test_definition"`
  + `fixture_only = true`，不再写 `synthetic`。
* 数值测试通过只写进测试报告的 `numerical_verification` 栏，
  **不会**自动把材料证据状态提升为 `formula_checked`。
* `formula_checked` 需要对应公式核查记录；`calibrated_case` 还需要关联
  独立验证报告与已批准卡版本，否则配置层拒绝（见 `validate_run`）。
