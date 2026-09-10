# ADR-0006：错误分类、新增模块与新增错误码

* 状态：已采纳｜日期：2026-09-10｜批次：A（T01/T02）

## 决定一：错误码

执行细则 11.2 的九个码全部实现于 `src/ufdemo/errors.py`：
`CONFIG_INVALID`、`ENERGY_CONFLICT`、`MATERIAL_CAPABILITY_MISSING`、
`CONDITION_MISMATCH`、`RESPONSE_SEMANTICS_INVALID`、`TABLE_OUT_OF_RANGE`、
`RESOURCE_BUDGET_EXCEEDED`、`GEOMETRY_UNSUPPORTED`、`NUMERIC_NONFINITE`。

**新增一个码：`NOT_IMPLEMENTED`。** 理由是细则要求“未开放的功能组合在配置层
拦截”，但把“尚未实现”归入上述任一码都会误导使用者（例如把批次 D 的
参考评估器报成配置错误）。该码只用于尚未开始的批次，且报告中一律记为“未运行”。

每个错误携带 `field_path`、`actual`、`requirement`、`suggestion`，可序列化为
JSON（CLI `--json`）并给出中文原因。CLI 失败返回非零退出码。

## 决定二：新增模块 `errors.py`

细则第 3 节的建议布局没有列出 `errors.py`。新增原因是配置、材料、响应、
求解、导出各层都需要同一套可归类错误，放在 `config.py` 会造成循环依赖。
其余模块划分与细则一致。

## 决定三：`RunConfig.reference_conditions`

为使“缺 δ / N10 误作 N1 / 协议未知 / 能流基准不匹配”四种拒绝可被机器验证，
配置新增可选块 `reference_conditions`：

```json
"reference_conditions": {
  "protocol_id": "inconel718_n10_threshold_reference",
  "fluence_basis": "incident_peak_fluence",
  "effective_count": 10
}
```

`config.check_reference_conditions()` 在 `reference_case` 下逐项比对材料卡的
`reference_protocol`；关键条件缺失（例如未给 `effective_count`）同样拒绝，
而不是放行。

## 后果

`python tools/migrate_materials.py` 的准入探针覆盖 7 个用例（5 个拒绝、
1 个接受、1 个条件不匹配），结果写 `docs/reports/material_admission.csv`。
