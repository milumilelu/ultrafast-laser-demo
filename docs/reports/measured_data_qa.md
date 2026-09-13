# 实测数据可用性 QA（U04）

- 对象：`data/measured/`（审计包 `ultrafast_audit_realdata_20260911.zip`，2026-09-11 构建）
- 口径：**65 条已发表条件/结果记录 + 4 条非激光对照**；这是**观测记录数**，不是 65 个独立数据集、不是 65 张材料卡。
  数据来源为论文表格/正文的**人工转录**（`manual_transcription_...`），**不含**作者仪器原始文件、**不含**公式采样或模型预测行。
- 本检查是**数据可用性**验收，**不做实验复现**；软件跑通 ≠ 材料验证。

- 汇总：通过 24｜失败 1

## 逐项

| 组 | 检查项 | 期望 | 实测 | 状态 | 说明 |
|---|---|---|---|---|---|
| manifest | 存在 manifest.json | 存在 | 存在 | 通过 | schema=ufdemo.experimental_pack/1 prepared_on=2026-09-11 |
| 完整性 | manifest 列出的文件都存在 | 0 缺失 | 0 缺失 | 通过 |  |
| 完整性 | 逐文件 sha256 与 manifest 相等 | 全部相等 | 全部相等 | 通过 |  |
| 完整性 | 逐文件行数与 manifest 相等 | 全部相等 | 全部相等 | 通过 |  |
| 计数 | 激光观测行数 = manifest.main_record_count | 65 | 65 | 通过 |  |
| 计数 | 非激光对照行数 = manifest.nonlaser_control_count | 4 | 4 | 通过 |  |
| 计数 | 唯一 case_id 数 = 激光行数 | 65 | 65 | 通过 |  |
| 计数 | 材料族数（激光数据） | 6 | 6 | 通过 | CFRP、SiC、微晶玻璃、氧化锆、金刚石、高温合金 |
| 计数 | all_measured_cases.jsonl 行数 = 激光观测数（同一数据的另一表示，不重复计数） | 65 | 65 | 通过 |  |
| 单位 | 成对单位换算自洽（不隐式换算，只核两份记录彼此自洽） | 全部自洽 | 核对 102 对，0 处不符 | 通过 |  |
| 异常保留 | D05-14 原文锥度与复算值并存且不相等 | 两者均非空且不相等 | reported=-1.809 recomputed=-0.3774304880539393 | 通过 | 原文值保留、未静默修正 |
| 异常保留 | D05-14 不一致被显式标记 | taper_consistent_with_diameters=False | False | 通过 |  |
| 异常保留 | D05-14 quality_note 说明排除该原文锥度参与拟合 | 含 FLAG / exclude | FLAG: row14 printed taper inconsistent with reported diamete… | 通过 |  |
| 来源纯净 | manifest.measurements_generated_by_model | False | False | 通过 |  |
| 来源纯净 | manifest.raw_author_files_included（作者原始文件不在包内） | False | False | 通过 |  |
| 来源纯净 | data_kind 中无 model/synthetic/formula 生成行 | 无 | 无 | 通过 | published_experimental_result |
| 字段完整 | 身份列全部非空（case_id/source_id/data_kind/output_semantics/source_locator） | 0 空 | 0 空 | 通过 |  |
| 字段完整 | 来源/方法列全部非空（source_doi/source_url/extraction_method/extraction_date） | 0 空 | 0 空 | 通过 |  |
| 缺失语义 | 金刚石 pulse_duration_fs 保持 null（原文只给设备最小脉宽，不得补值） | 18 行全为 null | 18/18 为 null | 通过 | 配套 equipment_min_pulse_duration_fs=250 fs 另列 |
| 缺失语义 | 金刚石 pulse_duration_fs 无 0 值冒充（null≠0） | 0 行 | 0 行 | 通过 |  |
| 语义红线 | 导入数据不含 event_depth_increment（累计/体积等不得改名混入逐事件主循环） | 不含 | 不含 | 通过 | cumulative_depth、single_pulse_crater_endpoint、surface_roughness_endpoint、through_hole_geometry_endpoint、track_or_pass_depth、volume_per_energy |
| 权限注册表 | 注册表条目数 = 实测记录数（65+4） | 69 | 69 | 通过 |  |
| 权限注册表 | 全部记录均有 observation_access（可浏览/回放） | 全部 | 69/69 | 通过 |  |
| 权限注册表 | 无任何记录取得 increment_access（实测端点语义不是逐事件增量） | 0 | 0 | 通过 |  |
| 权限注册表 | registry.json 与当前数据一致（无漂移） | 一致 | **不一致** | 失败 | 不一致说明数据变了但注册表没重生成；重跑 --write-registry |

## 失败项

- **registry.json 与当前数据一致（无漂移）**：期望 一致，实测 **不一致**（不一致说明数据变了但注册表没重生成；重跑 --write-registry）
