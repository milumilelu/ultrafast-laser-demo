# ADR-0013：批次 H 的受限阈值协议、七材料能力入口与贯穿水印决定

* 状态：已采纳｜日期：2026-09-11｜批次：H（T14、可选 T15）

## 背景

执行细则第 6 节把批次 H 定义为「七材料能力入口；受限阈值协议；标签；可选定点孵化」，
必交「七材料能力表、G09 报告、M2 放行」；第 7 节给出七种材料的初始开放内容与
**必须拦截项**；第 11.3 节给出界面措辞红线。任务书另有两条与阈值观测量直接相关的红线：

| 材料入口 | 必须拦截 |
|---|---|
| 铝基 SiC / CFRP / 金刚石 / 高温合金 / 微晶玻璃 | **把累计或平均能流当作单脉冲阈值判超阈** |
| 多候选阈值材料 | **未显式指定候选就默默取第一个** |

本节把上述约束固化为可执行决定，避免「文档声称已开放、实现其实打不开」这类走样。

## 决定一：受限阈值协议的唯一基准是**本事件入射能流**，且不产生深度

* `src/ufdemo/thresholds.py` 只注册 `per_event_incident`（本事件入射能流）一个基准；
  累计/平均能流基准在**配置层**被拒（`CONFIG_INVALID`）——累计入射剂量与单脉冲
  峰值能流量纲虽同、物理含义不同。
* 观测量为**分类掩膜**（超阈/未超阈），**不产生去除深度**，也不进入逐事件去除核。
* 用于证明「只按本事件判、不累加」的**可判别构造**：两发 `0.6 Fth`（累计 `1.2 Fth`）
  掩膜为空且高度逐位不变；单发 `1.2 Fth` 才点亮超阈单元
  （`tests/test_threshold_protocol.py`、`tools/material_report.py`）。

理由：细则第 7 节与任务书材料表要求「按本脉冲能量密度与阈值比较」，并明令
不得用累计量冒充单脉冲阈值。

## 决定二：协议**禁用即如实报不可用**，不返回全 0 假数组

* 未开启协议或该卡未提供可用阈值时，`available=False`，分类返回 `None`——
  **绝不返回全 0 数组**（「未判超阈」与「没有该观测量」是两回事）。
* 界面沿 `UNAVAILABLE_LAYERS["threshold_mask"]` 同口径显示原因，不用
  「累计剂量 vs 单脉冲阈值」比较来伪造该标记（延续批次 E 的
  `threshold_only`「不提供」而非数值 `0`）。

## 决定三：多候选阈值**必须显式 `candidate_index`**；多脉冲口径不得当单脉冲阈值

* 材料卡给出多个阈值候选时，未给 `candidate_index` 即 `available=False`，
  **不默默取第一个**（如 SiC 的 `idx0`/`idx1`）。
* `multi_response` 口径（多脉冲/累积响应）下的材料在**多脉冲运行**中不放行；
  单脉冲口径（如 CFRP `Fth(1)`）可用。这样「N=10 的高温合金」被拦、
  「N=1 的 CFRP」放行。
* 卡无阈值（如微晶玻璃）如实报 `available=False` 且 `threshold=None`。

## 决定四：七材料能力入口以**探针实跑**核验，缺口 `deferred` 不得冒充开放

`materials.MATERIAL_ENTRIES` 把七族拆为三类条目：

| 类别 | 绑定 | 核验方式 |
|---|---|---|
| `opened` | 真实能力/运行模式 | 该能力须查得可用 |
| `blocked` | enforcement + 探针键 + **期望错误码** | 实跑须抛出该码 |
| `deferred` | 探针键 + 期望（**打不开**） | 实跑须证明**当前确实打不开** |

* 每个族各有 `_ENTRY_PROBES` 中的探针；`verify_entry_enforcements()` 逐条实跑，
  拦截失效或缺口消失即**如实报失败**（不是只写文档）。
* **诚实口径**：规格要求开放、但实现未支持的功能必须记为 `deferred` 缺口并附探针，
  **不得**写进 `opened` 假装已开放。
  当前唯一缺口是**金刚石「合成形貌」**：金刚石三张卡的
  `structure_type = net_removal_with_optional_modification_mask` 既不在能力白名单
  （`compute_capabilities` 只认 `particle_composite` / `laminated_fiber_composite` /
  `homogeneous_effective` / `homogeneous`），也未被结构构建器支持
  （`structure.STRUCTURE_TYPES` 仅三种），且该 profile 系从源快照
  `material_card_templates_materials[6].profile` 原样搬来。
  探针 `diamond_synthetic_not_supported` 用卡自身 `structure_type` 调 `load_structure`，
  断言其抛 `CONFIG_INVALID`——证明「现在确实打不开」。
  是否补实现（新增结构类型或改卡）**留待人工决策**，未擅自改卡以免与迁移脚本冲突。
* 结果落 `docs/reports/material_capability_table.csv`（27 条：开放 15 + 红线 11 + 缺口 1），
  界面「七材料能力入口」页与 `python -m ufdemo materials` 同源展示。

## 决定五：水印有**单一权威来源**，导出与回放逐字段一致

* `materials.build_watermark(material, unit, run_mode)` 是水印的唯一生成点；
  solver 写入 `material_snapshot["watermark"]` 与 `metadata["watermark"]`，
  `io` 落盘 `watermark.json` 与 `statistics.csv` 的 `watermark.*` 自描述行，
  界面与历史回放都读它。
* **禁止**各处内联拼装水印——否则会出现「导出说一个模式、界面显示另一个模式」。
  `ui_service.watermark_of()` 改为直接调用 `build_watermark`；历史重读对缺失键
  `setdefault` 补全，旧运行也能重建同源水印。
* `unit` 为 `None` 时相关字段为 `None`（不编造默认）；合成模式
  `physical_depth_export_allowed=False`，禁止以 `depth_um` 命名物理深度。

## 决定六：阈值图层**条件可用**，标签只描述「该图实际是什么量」

* `LAYER_LABELS` 新增 `threshold_mask`（「超阈值/改性标记（仅按本事件入射能流判定）」）；
  仅在协议开启且掩膜数组存在时可用，否则走 `UNAVAILABLE_LAYERS`。
* 标签措辞受 `FORBIDDEN_TERMS` 严格子串守卫（含「热影响区 / HAZ / 温度」，否定式同样拒绝）；
  该掩膜**不得**读作热学损伤标记。

## 后果

* 好消息：阈值口径、能力入口、水印同源三类不变量各有独立断言与独立报告
  （`tests/test_threshold_protocol.py` 23 项、`tests/test_material_entries.py` 12 项、
  `tests/test_watermark_export.py` 7 项；`tools/material_report.py` → 9 项检查、
  能力入口 27/27），可被外部复核；红线与缺口均在配置层/探针处拦截。
* 代价：查表曲线**接入逐事件主循环仍不开放**（细则第 7 节：只有
  `event_depth_increment` 且协议适用时才可进），本批不启用该通道；
  金刚石合成形貌暂为缺口。
* 已知局限：受限期阈值观测量是**分类**，不含任何实验复现结论；
  「软件跑通」不等于「材料物理验证」的口径继续适用。

## 相关修订（本批一并处理）

* `io.REQUIRED_FILES` 增加 `watermark.json`；`LoadedRun` 增加 `watermark` 字段，
  `load_run` 按「`watermark.json` → `metadata["watermark"]` → `material_snapshot["watermark"]`」
  顺序回读，保证旧运行可重建。
* `solver` 在求解收尾处把 `warnings` 同步进水印（两处水印字典），使导出与界面
  的 `warnings` 逐字段同源。
* `tools/run_acceptance.py` 新增 H 段：`G09-threshold`（9 项）、`G09-entries`、
  `G09-watermark`（3 项），并把 G09 未运行项文案改为「查表接入逐事件核不开放」。
