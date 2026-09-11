# 开发进度与阶段状态

> 更新：2026-09-11｜版本：`0.8.0-j1`（M0 放行版 `0.1.0-m0`；D `0.2.0-d`；E `0.3.0-e1`；F `0.4.0-f1`；G `0.5.0-g1`；H `0.6.0-h1`；I `0.7.0-i1`）
> 状态取值仅限：待开发｜进行中｜待验收｜通过｜阻塞
>
> 说明（2026-09-11）：E/F 两个历史提交因对象缺失**已按 B 方案重建**，哈希见
> 「历史重建记录（B 方案）」。上表批次状态对应的工作内容未变，重建只影响 E/F 的提交哈希。
>
> ⚠️ **仓库完整性告警（2026-09-11 发现）**：提交 E（`4748cb2`）与 F（`ccf0717`）
> 的对象库**部分缺失**（E 缺 `docs/`、`tools/` 两棵顶层 tree 及若干 `runs/` 对象；
> F 缺 `docs/reports/curve_interpolation.html` 的 blob）。**当前 HEAD（`68a7858`）
> 与工作区完整**（`git archive HEAD` 成功、`git status` 干净），A–C 与 D 提交亦可完整导出；
> 但**全历史无法 `git push` / 传输式克隆**（`git pack-objects` 报
> `fatal: missing blob object`）。本仓库**未配置远端**，故目前无法通过推送恢复历史。
> 已采取：`git config gc.auto 0`（避免自动 gc 继续裁剪）；HEAD 快照另存于
> 仓库外 `../_repo_backup/`。修复方案待定，详见「已知限制与缺口」第 16 条。
>
> ✅ **已于 2026-09-11 按 B 方案重建解决**：重建后 `main` 曾指向 `66237a7`
> （末端树与旧 `d798a41` 逐位一致，均 `dfe031d5…`），此后批次 H 的提交叠在其上；
> `git fsck --full` 无 broken link / 无 missing，`git bundle --all` 与全量克隆均成功，
> 克隆内 pytest 通过。**以上告警原文原样保留**，处置细节见「历史重建记录（B 方案，2026-09-11 已执行）」。

## 批次状态

| 批次 | 任务 | 阶段 | 状态 | 备注 |
|---|---|---|---|---|
| A | T01、T02 基础 | M0 | **通过** | 包与 CLI 壳、单位与配置、人工 fixture、设计决定；安装说明、锁文件、配置错误测试、原文件哈希记录齐备 |
| B | T03–T05 | M0 | **通过** | 高斯与局部裁剪、真实事件流、固定阈值核；光束积分测试、事件 CSV、标量解析报告齐备 |
| C | T06、T08 | M0 | **通过** | 高度循环、统计、快照、新目录导出与重读；单坑/10 脉冲/直线/多遍栅格配置 + G01–G04 报告 |
| D | T07 | M1 | **通过** | YSZ/SiC 独立参考评估器；源公式对应表、G05 报告、平均率与累计量独立输出齐备 |
| E | T09 | M1 | **通过（待人工验收）** | `app.py` + `ui_service.py`；提交/回放求解次数记账、参考评估器与历史运行入口；界面操作检查 17/0/0 |
| F | T10 | V0.1 | **通过（待人工验收）** | 曲线 schema、分段线性 + 可选 PCHIP、越界处理、语义路由；示例 CSV、错误 CSV、原始点与插值图齐备 |
| G | T11–T13 | V0.1 | **通过（待人工验收）** | `structure.py` + 配置层接入；两个合成结构实例、种子与结构统计、G06 报告齐备；G06 检查 13/0 + 结构实例 2 |
| H | T14、可选 T15 | V0.1 | **通过（待人工验收）** | 受限阈值协议 + 七材料能力入口 + 水印同源；阈值检查 9/0、入口探针 27/27、水印 3/0；金刚石「合成形貌」记为缺口待决策 |
| I | T16、T17 | V0.2 | **通过（待人工验收）** | 冻结几何分组 + 局部误差估计与回退；G08 检查 26/0；B01–B04 性能实测（定点 4.0×、扫描 1.32×）；Numba 实测**无收益**故保留为可选后端 |
| J | T18、T19 | V0.2 | **通过（待人工验收）** | 斜入射投影 + 动态角度 + 首次交点可见性 + 法向厚度转换；G07 检查 8/0（验收）与 23 项单测；示例包 2 个 |
| — | T20 | 各阶段 | 进行中 | 回归与文档随批次推进；A–J 的回归与记录已完成 |

**M0 放行结论：通过。M1 放行条件已齐备，待人工审批。**
标准要求「M0 + YSZ/SiC 参考评估器（D）+ 基础界面 + 截面与快照回放（E）」：

| M1 放行条件 | 证据 | 状态 |
|---|---|---|
| G05 文献语义回归 | `g05_reference_semantics.md`（9 通过 / 0 失败） | ✅ |
| 界面操作检查记录 | `ui_operation_check.md` / `.csv`（20 通过 / 0 失败） | ✅ |
| 提交与回放求解次数检查 | 上表同源；`G09-UI` 行；`tests/test_app_smoke.py` 硬断言 | ✅ |

**M2 放行条件（批次 H 交付后）**：

| M2 放行条件 | 证据 | 状态 |
|---|---|---|
| V0.1 查表（F）：schema/插值/越界/路由 | `table_lookup.md`（21 通过 / 0 失败） | ✅ |
| 分相结构（G）：同相合并/界面不跳过/种子复现/截断命名 | `g06_phase_interfaces.md`（13 通过 / 0 失败） | ✅ |
| 受限阈值协议（H）：只按本事件入射能流判超阈、多脉冲口径拦截、不产生深度 | `g09_threshold_protocol.md`（9 通过 / 0 失败） | ✅ |
| 七材料能力入口（H）：开放/红线/缺口逐条实跑探针 | `material_capability_table.csv`（27/27 成立） | ✅ |
| 水印贯穿导出与回放：导出与回放逐字段一致 | `tests/test_watermark_export.py`（7 通过）；界面检查 `回放水印一致` 项 | ✅ |
| 逐事件核查表**接入**（查表进主循环） | **未开放**（细则第 7 节口径，本批不启用） | ⏸ 待决策 |

**M3 放行条件（批次 I + J 交付后）**：

放行条件为「G07、G08 通过；有实测误差和性能报告」（细则第 2 节 M3）。

| M3 前置 | 证据 | 状态 |
|---|---|---|
| G08 分组与逐脉冲对照 | `tests/test_grouped_solver.py`（26 通过）；验收 `G08` 段 7 行 | ✅ |
| 实测误差报告（含最大绝对差与归一化 L2） | `acceptance_report.md` G08 行 + `performance_baseline.md` | ✅ |
| 实测性能报告（B01–B04 + 环境/内存口径） | `performance_baseline.csv` / `.md` | ✅ |
| **G07 斜入射与表面几何** | `tests/test_incidence_geometry.py`（23 通过）；验收 `G07` 段 8 行 | ✅ |
| 完整示例包与演示说明（T19） | `examples/oblique_plane_60deg.json`、`examples/tilted_plane_dynamic_angle.json` | ✅ |
| 界面：支持范围与误差标识可见（T19） | 界面探针 24/0；几何面板含支持范围与近似标注 | ✅ |

> 结论：**M3 的四项前置（G07、G08、误差报告、性能报告）均已具备，可申请人工放行。**

**V0.1 查表（F）交付完成，待人工验收。批次 G–J 均已交付，待人工验收。**

## 本批（J）实际完成的交付物

| 交付物 | 位置 | 核对方式 |
|---|---|---|
| 表面法向（内部中心差分/边界单边差分）+ 解析平面法向 | `src/ufdemo/geometry.py` | 对线性平面精确（1e-15 级） |
| 投影 `F_s=μ·F_⊥`（只乘一次余弦） | 同上（`project_fluence`） | G07：60° 中心能流 = 1/2 |
| 首次交点可见性（向量化 + 平坦快速路径） | 同上（`first_intersection_visibility`） | G07：迎光侧遮挡；全网格 132 ms（优化前 7150 ms） |
| 法向厚度转换 `Δh=-a_n/n_z` | 同上（`normal_thickness_to_*`） | G07：解析一致（误差 1.5e-16） |
| 角度/`n_z` 范围检查（超范围即停并报位置） | 同上（`check_geometry_range`） | G07：70° 被拒且说明 60° 上限 |
| 束流斜入射接入（`s`/`r²`/负值判定/窗口） | `src/ufdemo/beam.py` | 正入射分支逐位不变（345 项既有测试全绿） |
| 倾斜初始平面（`tilted_plane` + 斜率） | `src/ufdemo/config.py`、`surface.py` | G07：解析法向与斜率一致 |
| 配置层准入与红线 | `config.py`（`validate_run`） | 斜入射 × 分相 → `CONFIG_INVALID`；`k_z≤0` → 拒 |
| 批量 × 斜入射回退 | `accelerators.check_fallback_conditions` | 回退参考并写明原因 |
| 几何诊断（正入射不留假数据） | `solver.py`（`diagnostics["geometry"]`） | 正入射时 `enabled=False`、各项 0 |
| 界面几何面板（支持范围/近似标识 + 两增强对照） | `app.py`、`ui_service.build_geometry_diagnostics` | 界面探针 24/0 |
| 示例包（T19 必交） | `examples/oblique_plane_60deg.json`、`examples/tilted_plane_dynamic_angle.json` | 均可 `validate + run` |
| 设计决定记录（含**符号约定**） | `docs/decisions/ADR-0015-oblique-incidence-and-visibility.md` | 七项决定可追溯 |

## 上一批（I）实际完成的交付物

| 交付物 | 位置 | 核对方式 |
|---|---|---|
| 冻结几何分组批量（块级累计、逐事件各自算响应后相加） | `src/ufdemo/accelerators.py` | `tests/test_grouped_solver.py`（26 项） |
| 局部误差估计（B 与两个 B/2）+ 几何漂移判据 + 拒绝缩小 B | 同上（`estimate_local_error` / `solve_block`） | G08 断言 + `zR=5μm` 拒绝用例 |
| 回退判据（分相 / 历史 / 动态角度）配置层拦截 | `src/ufdemo/config.py`（`validate_run`）、`accelerators.check_fallback_conditions` | 三条红线逐条断言 `CONFIG_INVALID` |
| 块级状态提交（计数语义与逐脉冲逐位对齐） | `src/ufdemo/surface.py`（`apply_block_increment`） | G08：曝光/照射计数逐位相等 |
| 可选 Numba 局部核（缺失自动回退并警告） | `accelerators.LocalKernel` / `_numba_log_kernel` | 同配置对照（B02 vs B02-numba） |
| 求解器分派与诊断 | `src/ufdemo/solver.py`（`_grouped_event_loop`） | `diagnostics["acceleration"]` + `metadata.acceleration` |
| B01–B04 性能记录（必交产物） | `docs/reports/performance_baseline.csv` / `.md` | 实跑；含环境、耗时、峰值内存、加速比、误差 |
| 性能报告生成器 | `tools/perf_report.py` | `python tools/perf_report.py`（`--quick` 自检、`--from-csv` 免重跑） |
| G08 验收接入 | `tools/run_acceptance.py`（G08 段 7 行 + B01–B04 5 行） | 通过 126｜失败 0｜未运行 2 |
| 界面求解模式开关与批量面板 | `app.py`（求解模式/批大小/后端）、`ui_service.build_acceleration_diagnostics` | 界面探针 22/0；参考模式如实说明未加速 |
| 设计决定记录 | `docs/decisions/ADR-0014-grouped-solver-and-fallback.md` | 八项决定可追溯 |

## 上一批（H）实际完成的交付物

| 交付物 | 位置 | 核对方式 |
|---|---|---|
| 受限阈值协议（只按本事件入射能流判超阈、不产生深度） | `src/ufdemo/thresholds.py` | `tests/test_threshold_protocol.py`（23 项） |
| 七材料能力入口（开放/红线/缺口 + 探针实跑核验） | `src/ufdemo/materials.py`（`MATERIAL_ENTRIES`、`verify_entry_enforcements`） | `tests/test_material_entries.py`（12 项） |
| 水印同源（导出与回放逐字段一致） | `materials.build_watermark` → `solver` / `io` / `ui_service` | `tests/test_watermark_export.py`（7 项） |
| 七材料能力表（必交产物） | `docs/reports/material_capability_table.csv` | 27 条：开放 15 + 红线 11 + 缺口 1，探针 27/27 |
| G09 受限阈值协议检查 CSV | `docs/reports/g09_threshold_protocol.csv` | 9 项：全部通过、0 失败 |
| G09 受限阈值协议报告 | `docs/reports/g09_threshold_protocol.md` | 检查明细 + 七材料入口汇总 + 边界声明 |
| 报告生成器 | `tools/material_report.py` | `python tools/material_report.py`（0 失败） |
| 阈值图层（条件可用）与能力入口标签页 | `app.py`（第 5 页）、`ui_service.py`（`threshold_mask`、`_material_entries_panel`） | 界面探针 20/0；未开启协议时不返回全 0 假数组 |
| 水印落盘 | `watermark.json` + `statistics.csv` 的 `watermark.*` 行 | 每个运行目录齐备；`load_run` 回读同源 |
| 验收接入 | `tools/run_acceptance.py`（H 段：G09-threshold/entries/watermark 共 14 行） | 通过 126｜失败 0｜未运行 2（含 I 段） |
| 设计决定记录 | `docs/decisions/ADR-0013-threshold-protocol-and-capability-entries.md` | 六项决定 + 三条红线 + 金刚石缺口口径可追溯 |

## 更早批次（G）实际完成的交付物

| 交付物 | 位置 | 核对方式 |
|---|---|---|
| 相/结构接口（`phase_at` / `next_different_interface`）、铺层与颗粒、种子与体积分数 | `src/ufdemo/structure.py` | `tests/test_phase_interfaces.py`（21 项） |
| 配置层接入与拦截（结构 schema、红线、互斥） | `src/ufdemo/config.py` | 无效组合逐条断言期望错误码 |
| 求解/表面/导出接入跨相截断与相标签更新及诊断 | `src/ufdemo/solver.py`、`surface.py`、`io.py` | `removal` 诊断字段 + `metadata["structure"]` |
| 两个合成结构实例 | `examples/alsic_particle_composite.json`、`examples/cfrp_laminated_ply.json` | 固定种子可复现；`python -m ufdemo validate/run` |
| 结构实例统计 CSV | `docs/reports/structure_instances.csv` | 逐实例一行，含目标/实际体积分数与截断诊断 |
| G06 检查明细 CSV | `docs/reports/g06_phase_interfaces.csv` | 13 项：7 检查 + 6 红线，逐条期望/实测/状态 |
| G06 报告 | `docs/reports/g06_phase_interfaces.md` | 结构实例 + 检查明细 + §8 规则对应 + 边界声明 |
| G06 报告生成器 | `tools/structure_report.py` | `python tools/structure_report.py`（0 失败） |
| 界面材料相图层与结构诊断面板 | `app.py`、`ui_service.py`（`phase_id` 图层、`build_structure_diagnostics`） | 界面探针 17/0；未启用分相时如实报不可用 |
| 端到端演示可用性检查 | `tools/ui_demo_probe.py` → `docs/reports/ui_demo_probe.md` | 13/0/0：选材料→选模板→提交→出结果→面板渲染→历史读回全链路 |
| 设计决定记录 | `docs/decisions/ADR-0012-phase-structure-and-truncation.md` | 八项决定 + 三条红线可追溯 |

## 更早批次（F）实际完成的交付物

| 交付物 | 位置 | 核对方式 |
|---|---|---|
| 曲线 schema + 加载校验 + 插值核（纯逻辑） | `src/ufdemo/tables.py` | `tests/test_tables.py`（91 项） |
| 示例曲线卡与原始点 CSV（示例 CSV） | `data/curves/*.curve.json`、`*.points.csv` | `tools/make_curves.py` 可重生成 |
| 无效输入夹具（14 组） | `tests/fixtures/curves_invalid/*` | 逐组断言期望错误码 |
| 错误 CSV | `docs/reports/table_errors.csv` | 21 项拒收检查：曲线卡 14 + 行为 7 |
| 原始点与插值图 | `docs/reports/curve_interpolation.html`、`curve_interpolation.csv` | 原始点散点 + 线性 + PCHIP 同图 |
| 查表汇总报告 | `docs/reports/table_lookup.md` | 清单 / 错误 / 插图 / 边界声明 |
| 查表报告生成器 | `tools/table_report.py` | `python tools/table_report.py` |
| 曲线生成器 | `tools/make_curves.py` | `python tools/make_curves.py` |
| CLI 查表子命令 | `python -m ufdemo table <card> --x …` | `--method {linear,pchip}`、`--allow-out-of-range`、`--all-curves`、`--json` |
| 界面「查表」标签页 | `app.py`（第 4 页）+ `ui_service.table_lookup/table_grid` | 查表不触发求解（`solve_count` 恒为 0） |
| 设计决定记录 | `docs/decisions/ADR-0011-table-lookup.md` | 七项边界决定可追溯 |
| 依赖（增量） | `pyproject.toml` 的 `table` extra 声明 scipy；`requirements.lock` 已含 scipy 1.18.1 | `pip install -e ".[table]"` |

> 批次 A–C 的交付物清单见 `29cf6bb`；批次 D 见 `61a2e4e`；批次 E 见 `4748cb2`（⚠️ 对象部分缺失）；
> 批次 F 见 `ccf0717`（⚠️ 对象部分缺失）；批次 G 见 `68a7858`。

## 实测结果摘要

### G07 / 斜入射与表面几何（批次 J，本批实测）

必交产物：G07 报告、完整示例包。清单检查 8 行（验收）+ 23 项单元测试。

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| **0° 退化**（符号约定等价性） | 与既有正入射核**逐位一致** | `np.array_equal` = True | 通过 |
| **60° 足迹长短轴比** | `1/cos60° = 2` | **1.9917**（离散容差内） | 通过 |
| **60° 中心表面能流** | 法向对应值的 `1/2`（μ=0.5） | 比值 **0.500000000000**（误差 1.1e-16） | 通过 |
| **完整平面截获能量** | 仍为 `E_p` | 相对误差 **1.69e-9** | 通过 |
| **斜平面 `Δh=-a_n/n_z`** | 与解析预测一致（**不是** `-a_n·n_z`） | 误差 **1.48e-16**（解析级） | 通过 |
| 法向厚度转换被记录 | 次数 ≥1 且写入近似说明 | 转换=1；`metadata.approximations` 含该条 | 通过 |
| **超范围即停**（不裁剪角度） | 70° 入射 → 拒并说明 60° 上限 | `GEOMETRY_UNSUPPORTED` | 通过 |
| **遮挡在迎光侧** | 被遮挡列全部在墙的迎光侧 | 列 64–79（墙在 x=80） | 通过 |
| 背向（μ≤0）零直接照射 | mask 全 False、F≡0 | 整面背向构型全为 0；**且不算越界** | 通过 |
| 能量-面积口径不混用 | `Σd·dA_proj = Σa_n·A_surf` | 严格一致（rel 1e-12） | 通过 |
| 正入射不留假数据 | `geometry.enabled=False`、各项 0 | 一致；界面不显示该面板 | 通过 |
| 可见性性能 | 不成为瓶颈 | 全网格 161² 从 7150 ms → **132 ms（54×）** | 通过 |

> 明细见 `docs/reports/acceptance_g01_g05.csv` 的 `G07` 行与 `tests/test_incidence_geometry.py`。

### G08 / 分组模式与逐脉冲对照 + B01–B04（批次 I，历史实测）

必交产物：B01–B04 性能记录、G08 逐脉冲对照。

**一致性（G08）**

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 冻结几何下与参考一致 | 接近浮点精度 | 深度场最大绝对差 **4.235e-22**（尺度 2e-6，相对 2.1e-16） | 通过 |
| 归一化 L2 差 | ≤ 1e-12 | 同量级 | 通过 |
| 计数逐位对齐 | 曝光/照射次数逐位相等 | 两者皆 True | 通过 |
| **不先累加能流**（可判别构造） | 结果 = 2δ·ln(F/Fth)，≠ δ·ln(2F/Fth) | 命中正确式，且与错误路径分离 | 通过 |
| 批大小不变性 B=1/2/5/10 | 各档均与参考一致 | 最大差 ≤ 4.2e-22（B=1 逐位相等） | 通过 |
| **弱几何变化 ≤ 1%**（axial_defocus, zR=10μm） | 深度场逐点满足 `0.01|d_ref|+0.01δ` | 全部单元通过；最大相对差 0.56%｜体积 0.19% | 通过 |
| 几何漂移过大 → 拒绝并缩小 B | 拒绝该块、不提交状态 | zR=5μm（漂移 0.4>0.25）被拒；试算前后表面逐位不变 | 通过 |
| 回退：分组 × 分相 / 历史 / 动态角度 | 三条红线配置层拦截 | 全部 `CONFIG_INVALID` | 通过 |
| 奇数块两等分 | 前半取较大的一半 | B=5 → 前半 3 / 后半 2 | 通过 |

**性能（B01–B04，本机实测，非承诺值）**

| 用例 | 规模 | 分组 (s) | 参考 (s) | 加速比 | 峰值内存 | 备注 |
|---|---|---|---|---|---|---|
| B01 | 256×256、1000 脉冲（定点） | 0.362 | 1.451 | **4.00×** | 7.2 MB | 光束补丁复用 984/1000 |
| B02 | 512×512、10000 脉冲（定点） | 17.92 | 69.96 | **3.90×** | 27.8 MB | — |
| B02-numba | 同配置，仅换 Numba 后端 | 17.94 | — | **0.999×** | 50.7 MB | **无收益**，内存翻倍；JIT 0.887 s |
| B03 | 512×512、100000 脉冲、有限快照 | 180.26 | 未测（规模过大） | — | **29.3 MB** | 内存不随事件增长 |
| B04 | 5 行 × 40 点 × 3 遍蛇形扫描 | 0.235 | 0.310 | **1.32×** | 12.2 MB | 焦点移动，补丁不可复用 |

> 内存口径：`tracemalloc` 统计的 Python 分配峰值，不含部分 C 层分配，也不是 OS 的 RSS。
> 结论：**定点/小段工况批量有明显收益；扫描工况收益有限；Numba 实测无收益，故不默认启用**。

明细见 `docs/reports/performance_baseline.csv` / `.md`。

### G09 / 受限阈值协议 + 七材料能力入口 + 水印（批次 H，历史实测）

三件必交产物：受限阈值协议、七材料能力入口表、G09 报告。

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 配置层拒绝累计/平均能流基准 | `CONFIG_INVALID` | 4 个累计/平均基准 + 整份 `RunConfig` 均被拒 | 通过 |
| 唯一注册基准 = 本事件入射能流 | `per_event_incident` | 唯一注册基准即它 | 通过 |
| 禁用时报不可用、不返回全 0 假数组 | `available=False`；分类为 `None` | 未开启 → `available=False`；分类 `None`（非全 0 数组） | 通过 |
| 多候选须显式 `candidate_index` | 未给索引 → 不可用 | 2 个候选：未给索引不可用｜idx0=2.35e+04｜idx1=4.97e+04 | 通过 |
| 多脉冲口径不得当单脉冲阈值 | N=10 拦｜N=1 放行 | 高温合金 N=10 → 不可用｜CFRP `Fth1` → 可用 | 通过 |
| 缺阈值如实报不可用 | `available=False`；`threshold=None` | 微晶玻璃 → `available=False`；`threshold=None` | 通过 |
| **只按本事件能流判超阈（可判别构造）** | 累计超阈但掩膜为空 | 两发 0.6 Fth（累计 1.2 Fth）掩膜=空｜单发 1.2 Fth 点亮 25 单元｜高度逐位不变 | 通过 |
| 求解器启用/禁用两态一致 | 启用记数落盘｜禁用不造假 | 启用：超阈单元·事件=185｜末态单元=37；禁用：掩膜 `None` 且快照不写数组 | 通过 |
| 七材料能力入口：逐条探针实跑 | 全部成立 | 探针 27/27 成立｜开放 15｜红线 11｜缺口 1 | 通过 |

七材料能力入口（探针实跑，非文档声称）：

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 七族全覆盖、条目无重复 | 7 族、每条唯一 | 7 族齐备、无重复 | 通过 |
| 每条 `blocked` 绑探针 + 期望错误码 | 实跑抛出该码 | 红线 11 条全部命中期望码 | 通过 |
| 每条 `opened` 绑真实能力 | 该能力查得可用 | 开放 15 条全部可用 | 通过 |
| 金刚石「合成形貌」记为缺口（非开放） | `opened` 不含该项｜缺口探针证明打不开 | 缺口 1 项：`('金刚石','合成形貌')`；卡内 `structure_type` 调 `load_structure` 抛 `CONFIG_INVALID` | 通过 |
| SiC 无 `synthetic_structure` 声明 | 卡不声明该合成能力 | 确认未声明 | 通过 |

水印同源（导出与回放一致）：

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| `metadata` 带 `run_mode`/`unit_mode`/完整水印 | 三者齐备 | `run_mode=reference_case`｜`unit_mode=SI`｜`watermark_keys=19` | 通过 |
| `watermark.json` 与 `metadata` 逐字段一致 | 全键一致 | 一致 | 通过 |
| `statistics.csv` 含 `watermark.*` 自描述行 | `material_id`/`run_mode`/`unit_mode` 存在 | `watermark.*` 行数=12 | 通过 |
| `load_run` 暴露水印、回放与导出一致 | 逐字段一致 | 一致 | 通过 |
| 合成模式标非物理（禁导出 `depth_um`） | `physical_depth_export_allowed=False` | 一致 | 通过 |

明细见 `docs/reports/g09_threshold_protocol.csv` / `.md` 与
`docs/reports/material_capability_table.csv`。

### G06（批次 G，历史实测，未改动）

三件必交产物：两个合成结构实例、种子与结构统计、G06 报告。

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 同相合并等价 | 合并前后几何逐位一致 | 合并 3→2 层，`phase_at` / `next_different_interface` 逐位一致 | 通过 |
| 同相层界不假截断 / 不同相不跳过 | 同相列 `inf`；异相列 = 层厚 | 同相列 `inf`；异相列 = 层厚 | 通过 |
| 颗粒界面不跳过 | 进入/离开距离解析命中 | 进入/离开射线-球求交命中，无颗粒列 `inf` | 通过 |
| 不拆伪脉冲 | 单次应用 = 单一相核候选量 | 中心列一次应用 = 单一纤维核候选量（rel ≤ 1e-12） | 通过 |
| 固定种子重现 | 同种子逐位一致，异种子不同 | 一致 / 不同均成立 | 通过 |
| 目标/实际体积分数分列 | 两者分别给出且方法可追溯 | 目标 0.32｜实际 0.27185（`grid_sample`, n=20000） | 通过 |
| 跨相截断命名与诊断 | 「未应用候选去除体积」+ 四项统计 | 命名与四项统计齐备，键名不含 residual/energy | 通过 |
| 红线：旧 δ 字段 | 拒绝（`RESPONSE_SEMANTICS_INVALID`） | 一致 | 通过 |
| 红线：相借用其它材料卡 | 拒绝（`MATERIAL_CAPABILITY_MISSING`） | 一致 | 通过 |
| 红线：相沿用整体阈值来源 | 拒绝（`MATERIAL_CAPABILITY_MISSING`） | 一致 | 通过 |
| 红线：整体阈值拆给分相 | 拒绝（`MATERIAL_CAPABILITY_MISSING`） | 一致 | 通过 |
| 红线：结构×动态角度互斥 | 拒绝（`CONFIG_INVALID`） | 一致 | 通过 |
| 红线：结构类型与卡一致 | 拒绝（`CONDITION_MISMATCH`） | 一致 | 通过 |

明细见 `docs/reports/g06_phase_interfaces.csv` / `.md`；结构实例统计见
`docs/reports/structure_instances.csv`。

### G09 / 查表（批次 F，历史实测）

三件必交产物：示例 CSV、错误 CSV、原始点与插值图。

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 曲线卡最少字段校验 | 缺字段即拒（`CONFIG_INVALID`） | `invalid_missing_field` 被拒 | 通过 |
| 横坐标严格升序 | 未升序即拒 | `invalid_unsorted` 被拒 | 通过 |
| 重复 x 默认拒绝 | 无规则说明即拒 | `invalid_duplicate` / `invalid_duplicate_no_rule` 被拒 | 通过 |
| 非有限 / 非数值点 | `NUMERIC_NONFINITE` / `CONFIG_INVALID` | 与预期码一致 | 通过 |
| 有效区间覆盖点 | 点越出 `valid_range` 即拒 | `invalid_range_narrow` 被拒 | 通过 |
| 体积曲线声明深度方向 | 拒绝（语义不符） | `invalid_volume_depth_dir` 被拒 | 通过 |
| 事件增量曲线缺深度方向 | 拒绝 | `invalid_no_depth_direction` 被拒 | 通过 |
| 越界（低于下界） | `TABLE_OUT_OF_RANGE`，**不返回 0** | 抛 `TABLE_OUT_OF_RANGE`；`allow_out_of_range` 时值为 `None` | 通过 |
| 越界（高于上界） | `TABLE_OUT_OF_RANGE` | 一致 | 通过 |
| 端点包含性 | 闭区间内状态 `ok` | `[1.0, 40.0]` 两端点 `ok` | 通过 |
| PCHIP 缺 SciPy | `NOT_IMPLEMENTED`，不静默退化 | 与预期码一致 | 通过 |
| 未登记插值方法 | `CONFIG_INVALID` | `cubic_spline` 被拒 | 通过 |
| 体积曲线反推局部深度 | `CONFIG_INVALID` | 被拒 | 通过 |
| 阈值曲线进事件核 | `RESPONSE_SEMANTICS_INVALID` | 被拒 | 通过 |
| 固定条件不匹配进事件核 | `CONDITION_MISMATCH` | 被拒 | 通过 |
| 曲线语义路由 | 仅 `event_depth_increment` → 事件核 | YSZ 曲线 → `event_kernel`；SiC/体积 → `evaluator` | 通过 |
| 查表不产生形貌 | 无 `final_surface.npz` | 确认无 | 通过 |
| 界面「查表」不求解 | `solve_count` 恒为 0 | 提交/换曲线/换算法均不变 | 通过 |

> 拒收检查合计 **21 项，全部通过、0 失败**（曲线卡 14 + 行为 7），明细见 `table_errors.csv`。

### G09 / 界面操作检查（批次 E，批次 F/G/H/I/J 增补检查）

驱动方式：`streamlit.testing.v1.AppTest` 真实执行 `app.py`；输出隔离到 `runs/acceptance/_ui_probe`。

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 初始渲染 | 无异常、`solve_count=0`、无冻结结果 | 一致 | 通过 |
| 提交计算 | `solve_count` 恰好 `+1` | `0 → 1`，`status=completed`，落盘 `metadata.json` | 通过 |
| 改参数未提交 | 旧结果标为「上一次运行」且不求解 | `is_stale=True`，`solve_count=1` 不变，界面出现该警告 | 通过 |
| 快照回放 / 切图层 / 旋转视图 / 切截面 | `solve_count` 不变 | `1 → 1` | 通过 |
| 读取历史运行 | `read_count +1`、`solve_count` 不变 | `read_count=1`、`solve_count=0` | 通过 |
| 参考评估器「评估」 | 不进入逐事件求解、不产生形貌 | `solve_count=0`，有参考结果，`frozen=None` | 通过 |
| 查表「查值」（批次 F 新增） | 不触发求解、不读历史 | `solve_count=0`，产出查表结果 | 通过 |
| 换曲线卡 / 换插值方法（批次 F 新增） | `solve_count` 恒为 0 | 3 条曲线 × 2 方法均不变 | 通过 |
| 图层标签措辞 | 不含「热影响区 / HAZ / 温度」等被禁词 | 全部干净 | 通过 |
| 缺能力模式 | 配置层拦截并给准确原因 | CFRP + `reference_case` → 拒绝，列出允许模式 | 通过 |
| 合成模式导出 | 禁止以 `depth_um` 命名物理深度 | 导出标签 `depth/delta_ref`，`depth_um` 被拒 | 通过 |
| `threshold_only` 结果 | 去除量显示「不提供」而非数值零 | `removal_available=False` | 通过 |
| **界面为六标签页（批次 H 新增）** | 含「七材料能力入口」页 | 六页齐备 | 通过 |
| **七材料入口面板（批次 H 新增）** | 探针实跑全部成立 | 27/27 成立｜开放 15｜红线 11｜缺口 1 | 通过 |
| **金刚石合成形貌记为缺口（批次 H 新增）** | 缺口行存在、非记为开放 | 缺口 1 项，如实显示 | 通过 |
| **回放水印与导出 `watermark.json` 逐字段一致（批次 H 新增）** | `material_id`/`run_mode`/`unit_mode` 全一致 | 一致 | 通过 |

### G05（批次 D，历史实测，未改动）

| 编号 | 关键断言 | 预期 | 实测 | 结论 |
|---|---|---|---|---|
| G05 | YSZ 扫描速度 | 278.9734276388 mm/s | 278.97342763877356 mm/s | 相对误差 9.47e-14 ≤ 1e-12 |
| G05 | 错误换算 `2w0f/N` | 355.2 mm/s | 355.19999999999993 mm/s | 与式(9) 相差 27.3%（仅作对照） |
| G05 | YSZ 脉冲能量 | 201.464053689406 μJ | 201.4640536894062 μJ | 相对误差 9.88e-16 ≤ 1e-12 |
| G05 | SiC `Fth(1)` | 2.35 J/cm² | 2.35 J/cm² | 精确相等 |
| G05 | SiC `Fth(N→∞)` | 0.70 J/cm² | 0.70 J/cm²（N=1e9） | 精确相等 |
| G05 | SiC `k` | 0.0199 /脉冲 | 0.0199 | 取自材料卡 `multi_response` |
| G05 | 平均率 / 协议累计深度 | 两项分别输出，累计 = 平均 × N_eff | 平均 59.5674 nm/有效脉冲；N=720 累计 42.8885 μm | 相对误差 ≤ 1e-12 |
| G05 | 语义闸门 | 平均/累计拒绝进入事件核 | 拒绝 `RESPONSE_SEMANTICS_INVALID` | 通过 |
| G05 | 参考运行目录 | 不写形貌表面文件 | 目录中无 `*surface*.npz` | 通过 |

### G01–G04（批次 A–C，历史实测，未改动）

| 编号 | 关键断言 | 预期 | 实测 | 结论 |
|---|---|---|---|---|
| G01 | 峰值能流 | 73890.5609893065 J/m² | 73890.5609893065 J/m² | 通过（解析恒等） |
| G01 | 单脉冲能量 | 11.6067021786817 μJ | 11.6067021786817 μJ | 通过（解析恒等） |
| G01 | 中心去除深度 | 200 nm | 199.99999999999996 nm | 相对误差 1.3e-16 ≤ 1e-12 |
| G01 | 去除体积 | 3.1415926535897935e-17 m³ | 3.1411e-17 m³ | 相对误差 1.57e-4 ≤ 1% |
| G01 | 网格收敛 | w/20 → w/40 → w/80 | 1.568e-4 → 1.557e-5 → 6.365e-6 | 单调收敛，均 ≤ 1% |
| G02 | 10 脉冲中心深度 | 2 μm | 1.9999999999999995 μm | 相对误差 2.1e-16 ≤ 1e-12 |
| G02 | 10 脉冲体积 | 3.1415926535897933e-16 m³ | 3.1411e-16 m³ | 相对误差 1.57e-4 ≤ 1% |
| G03 | 零事件 / 等阈值 | 0 去除 | 0 | 通过 |
| G03 | 无效能量 / 负半径 / NaN | 拒绝 `CONFIG_INVALID` | 全部拒绝 | 通过 |
| G03 | 域外照射 | 截获能量 0、不重分配 | 截获 0，发射能量完整记录 | 通过 |
| G03 | 缺 δ 请求深度 | 拒绝 `MATERIAL_CAPABILITY_MISSING` | 已拒绝 | 通过 |
| G04 | 首个事件测试例 | t=0/0.001/0.002 s，增量 1 mm | 一致 | 通过 |
| G04 | `v/f` 间距 | 10 μm | 10 μm | 相对误差 < 1e-9 |
| G04 | 正反向镜像等价 | 深度场一致 | 最大绝对差 2.9e-22 | 相对 9.7e-16 ≤ 1e-12 |
| G04 | ROI 与全域统计 | 分别正确 | 一致；空 ROI 返回不可用 | 通过 |

### 汇总

- `python -m pytest -q`：**368 passed**（A–C 63 + D 18 + E 61 + 界面冒烟 18 + F 查表 91 + G 分相 21 + H 阈值/入口/水印 42 + I 分组 26 + J 斜入射 23 + 回归 5）。
- `docs/reports/acceptance_report.md`：**通过 138｜失败 0｜未运行 1**（含 J 段 8 行、I 段 12 行）。
- 界面操作检查：**通过 24｜失败 0｜未运行 0**（含批量模式与几何修正面板）。
- 端到端演示可用性：**通过 13｜失败 0｜未运行 0**（前后端对接全链路）。
- 查表拒收检查：**通过 21｜失败 0**。
- G06 分相检查：**通过 13｜失败 0**（结构实例 2 个）。
- G09 受限阈值协议：**通过 9｜失败 0**；七材料能力入口：**探针 27/27 成立**。
- G08 分组对照：**通过 7｜失败 0**；B01–B04 性能：**5 行实测记录**。
- G07 斜入射与表面几何：**通过 8｜失败 0**（另 23 项单元测试）。

## 未运行项（不得视为通过）

| 编号 | 原因 |
|---|---|
| G09（查表接入逐事件核） | 批次 H（T14）已交付受限阈值协议与七材料能力入口（见上方 G09-threshold / G09-entries / G09-watermark 行）；**查表曲线接入逐事件主循环仍不开放**（细则第 7 节：只有 `event_depth_increment` 且协议适用时才可进，本批不启用该通道） |

> 批次 D 的 `M1-UI` 未运行项已随批次 E 交付撤销；批次 G 的 G06 未运行项已随批次 G 交付撤销；
> 批次 H 的「受限阈值协议/七材料能力入口」未运行项已随批次 H 交付撤销；
> **批次 I 的 G08 与 B01–B04 未运行项已随批次 I 交付撤销**（唯 B03 的逐脉冲参考因规模过大未测，
> 已在 `performance_baseline.md` 的「未测项」中如实标注）；
> **批次 J 的 G07 未运行项已随批次 J 交付撤销**。至此 A–J 只剩「查表接入逐事件核」一项不开放。

## 阶段收尾检查（执行细则第 12 节）

- [x] 实际执行的小例子可安装、运行、导出并重新读取。
      （`examples/*.json` + `python -m ufdemo run|reference|table` + `python -m ufdemo inspect`）
- [x] 本阶段适用 G 测试有实测记录，失败与未运行项目未标为通过。
      （`acceptance_report.md`：通过 138｜失败 0｜未运行 1，逐一列出原因；
      G06 已由「未运行」转为实测；批次 H 的 G09-threshold/entries/watermark 已转入实测）
- [x] 原始资料和旧程序未被覆盖；迁移与代码变更可追溯。
      （F01/F02 哈希与任务书指纹一致；`input_hash_check.csv`；`material_migration.csv`；
      `微观仿真/` 未被触碰）
- [x] 合成、参考、数值通过和实验验证的标签未混用。
      （合成卡 `synthetic_definition`；fixture `analytic_test_definition`；
      SiC 查表曲线由材料卡参数按式(6) 重算，标注为「非原图复现」；
      参考结果 `run_mode=reference_case` 且 `verified_by_experiment=false`；
      报告分三栏，实验复现栏为空）
- [x] 未开放的功能组合在配置层拦截，不能仅依赖按钮禁用。
      （斜入射、跨层孵化仍在 `validate_run` 拦截；**分组批量的三条红线**——分组 × 分相、
      分组 × 历史耦合、分组 × 动态角度——同样在 `validate_run` 拦截为 `CONFIG_INVALID`；
      分相结构的红线——相借用其它
      材料卡、整体阈值拆给分相、相沿用整体阈值来源——在 `build_phase` / `validate_run`
      拦截；「分相截断 × 动态角度」组合在配置层互斥；
      参考语义由 `assert_reference_only_semantics` 反向闸门拦住；
      查表语义由 `assert_curve_can_enter_event_kernel` / `assert_no_local_depth_from_volume` 拦住；
      受限阈值协议的累计/平均能流基准在配置层被拒（`CONFIG_INVALID`）、多候选须显式
      `candidate_index`、多脉冲口径不得当单脉冲阈值（`thresholds.py`）；
      **斜入射 × 分相结构**在配置层拦截（`CONFIG_INVALID`）、方向约定 `k_z>0` 与
      「入射角 ≤60°」也在配置层校验（`config.py`）；分组 × 斜入射按细则 9.1 回退参考
      并写明原因（`accelerators.check_fallback_conditions`）；
      界面另加 `capability_gate` / `UNAVAILABLE_LAYERS`，但拦截责任仍在配置层）
- [x] 已知限制、尚缺资料和下一批依赖写入阶段报告。（见下节）

## 已知限制与缺口

1. **F03 / F04 文件未找到**：`paper5.0.tex`、
   `ttm_carrier_drilling_q4_axisymmetric.py` 在当前工作区不存在，
   已记录为缺失。细则第 3 节明确其不阻塞人工解析主线。
2. **只支持正入射**；**表面几何**现支持均质单相与解析分相结构（铺层条纹 / 随机颗粒），
   但**不支持任意三维几何**（见 README 第 5 节）。
3. **七种材料的定量能力受限**：目前只有 YSZ 加工分支具备
   `event_depth_increment`（且为条件限定）；其余材料缺 δ 或完整响应，
   只能做阈值展示或合成演示，**不输出物理深度**。
4. **超阈值掩膜图层仅在受限阈值协议开启且该卡提供可用阈值时可用**：批次 H 已实现
   逐事件超阈值掩膜（**只按本事件入射能流**判超阈、**不产生深度**）；未开启或卡无阈值时
   界面**如实显示不可用原因**，**不返回全 0 假数组**，也不用「累计剂量 vs 单脉冲阈值」
   比较来伪造该标记；该标记**不得**读作热学损伤标记。
5. **界面为单机 Streamlit，无并发与持久化会话**：会话状态在进程内；
   历史结果通过运行目录读取，不建数据库。
6. **无性能数据**：B01–B04 未运行，未测得加速倍数，也未引入 Numba。
7. **SiC 平均去除率的逐事件化**未实现，属工程扩展，需单独记录
   `engineering_extension` 并另做验证（本批仍不做该转换）。
8. **实验回归用例未建立**：YSZ 图 14、SiC 图 6 的原图/原表尚未数字化，
   因此 G05 只做到公式核查，不含实验复现。
9. **SiC 扫描次数列表存在重复项、工况映射不清**：本批只接受直接输入论文
   有效 N，不推测扫描次数列表（任务书 3.1）。
10. **查表曲线只覆盖固定协议下的一维响应**，且**不接入逐事件主循环**：
    跨工况需另附条件匹配的曲线。批次 H 已交付受限阈值协议与七材料能力入口，
    但**查表曲线接入主循环仍不开放**（细则第 7 节：只有 `event_depth_increment`
    且协议适用时才可进，本批不启用该通道）。
11. **查表的 SiC 曲线由材料卡拟合参数按式(6) 重算**：不是原图逐点数字化，
    因此**不构成对原文曲线的复现**（`table_lookup.md` 边界声明已写明）。
12. **PCHIP 在有效区间外不作为**：越界不画虚线外推，不预测、不填补。
13. **分相结构仅是合成结构**：颗粒为顺序放置（固定 `seed`）、铺层为解析条纹；
    有限样本的目标体积分数与实际体积分数**分别报告**，不强制相等。
    相响应为**内联合成定义**，**不含任何实验复现结论**。
14. **跨相截断是有损近似**：被截断的候选量记为「未应用候选去除体积」，
    **不是**剩余能量，也不在相之间重新分配（见 ADR-0012）。
15. **「分相截断 × 动态角度」组合未开放**：在配置层互斥（`CONFIG_INVALID`）；
    几何意义未单独定义前不得混用。
16. **⚠️ 仓库对象缺失（E/F 提交，2026-09-11 发现）**：
    - 现象：`git fsck` 报 10 处 `broken link`；`git archive 4748cb2`（E）与
      `git archive ccf0717`（F）失败；`git pack-objects` 全历史失败。
    - 缺失对象：`4748cb2` 的顶层 tree `docs`(`a23c38e9`)、`tools`(`a33b02fa`)
      及若干 `runs/` 子对象；`ccf0717` 的 `docs/reports/curve_interpolation.html`
      之 blob(`fbca9592`)。经 `git cat-file --batch-all-objects` 确认**确实不在库中**
      （非 multi-pack-index 过期所致）；两个 pack 自身 `verify-pack` 均为 `ok`。
    - 影响：**当前 HEAD 与工作区不受影响**（`git archive HEAD` 成功、`git status` 干净），
      A–C(`29cf6bb`)、D(`61a2e4e`)、G(`68a7858`) 均可完整导出；但 E/F 这两个历史快照
      无法完整检出，且**无法 push / 传输式克隆**（即无法用远端备份）。
    - 推断原因：疑似**并发 git 进程**（另一会话）在 23:20–23:26 间做了 repack/gc，
      与本地提交交错，导致被后续提交替换掉的旧对象在某些 pack 写入中丢失
      （`pack-9ef0e07` 的 `.idx`(21:45)/`.rev`(23:20)/`.pack`(23:26) 时间戳互不一致）。
    - 已采取：`git config gc.auto 0`；HEAD 快照 `git archive` 另存仓库外
      `../_repo_backup/ufdemo_HEAD_68a7858_*.tar`（2.75 MB，263 个文件）。
    - **待定**：修复方案需人工决策（见下方「建议的修复选项」，未擅自执行任何历史改写）。
    - **已决策并执行（2026-09-11）**：人工选定 **B**（否 A：仍不能 push/克隆；
      否 C：会丢弃 A–G 提交脉络；否 D：本仓库无远端，无执行依据）。
      本轮已完成重建与全部复验，执行细节见下节；**本条第 16 项的文字原样保留**。

17. **金刚石「合成形貌」是能力缺口（deferred），非已开放**（批次 H 新增）：
    规格（执行细则第 7 节材料表）要求金刚石开放「合成形貌」，但金刚石三张卡的
    `structure_type = net_removal_with_optional_modification_mask` 既**不在能力白名单**
    （`compute_capabilities` 只认 `particle_composite` / `laminated_fiber_composite` /
    `homogeneous_effective` / `homogeneous`），也**未被结构构建器支持**
    （`structure.STRUCTURE_TYPES` 仅 `homogeneous` / `particle_composite` /
    `laminated_fiber_composite`）——该 profile 系从源快照 `material_card_templates_source_snapshot.json`
    的 `materials[6].profile` 原样搬来。
    处置：按「不得冒充已开放」约定记为 `DeferredItem` **缺口**，并附探针
    （`diamond_synthetic_not_supported`）**证明当前确实打不开**（用卡自身 `structure_type`
    调 `load_structure` 抛 `CONFIG_INVALID`）；`verify_entry_enforcements` 实跑该探针，
    缺口消失即报失败。是否补实现（新增结构类型或改卡）**待人工决策**，
    未擅自改卡以免与迁移脚本冲突。
18. **分组批量第一版只支持「固定阈值 + 同相 + 无历史」（批次 I 新增）**：
    分组 × 分相结构、分组 × 历史耦合、分组 × 动态角度三条组合在**配置层**拦截
    （`CONFIG_INVALID`），不靠运行期静默降级。红线见 ADR-0014。
19. **分组模式下快照落在块边界（有损粒度）**：索引标注为该块内最后一个命中事件，
    实际对应块**结束**时的表面状态，与逐脉冲的瞬时快照有差别；只要发生过就在
    `metadata.approximations` 与诊断 `snapshot_on_block_boundary` 中如实标注。
20. **Numba 局部核实测无收益，故不默认启用**（批次 I 实测）：同配置对照
    `numba` 相对 NumPy 为 0.999×，且峰值内存由 27.8 MB 升至 50.7 MB（JIT 开销）。
    该核是逐元素对数运算，NumPy 的 `np.log` 已是 SIMD 向量化实现。
    `numba` 保留为可选后端，缺失时**自动回退并给出警告**（结果同式、逐位一致）。
21. **分组收益依赖工况**：定点/小段扫描有明显收益（B01 4.00×、B02 3.90×），
    大范围扫描收益有限（B04 1.32×，焦点逐点变化使光束补丁不可复用）。
    **不承诺任何固定加速倍数**；若某配置无收益，应保留参考模式（求解器不强制）。
22. **B03 的逐脉冲参考未测**：100000 脉冲规模的参考求解超出合理耗时，
    该项只报告分组模式的耗时与峰值内存；已在 `performance_baseline.md` 的
    「未测项」中如实标注，不以预期值冒充。
23. **性能数值只对本次测试环境成立**：CPU/内存/依赖版本见
    `performance_baseline.md` 第 1 节；峰值内存是 `tracemalloc` 的 Python 分配峰值，
    不含部分 C 层分配，也不是操作系统 RSS。
24. **斜入射/动态角度只做几何修正，不预测吸收差异**（批次 J 新增）：
    未提供材料与波长的 `A(θ)` 依据时**只做投影 + 可见性 + 法向厚度换算**；
    界面不提供"材料偏振吸收预测"开关，也不给透明介质无条件设 `A=1-R`（任务书 6.6）。
25. **`n_z≥0.5`、入射角≤60° 是软件数值/展示范围，不是材料物理边界**（批次 J）：
    超出即**停止**该模式并报首个越界单元位置与原因，**不裁剪角度继续运行**。
    背向（μ≤0）不算越界——它天然零直接照射。
26. **可见性只做首次交点，且限于 2.5D 高度场**（批次 J）：不做多次反射/衍射，
    不支持悬垂或多值表面；射线离开计算域即停止，**不做域外假设**。
27. **「批量 + 斜入射」不在第一版支持范围内**（批次 J）：窗口内可见性会随烧蚀形貌
    变化而块内几何被冻结，故**回退逐脉冲参考**并写明原因；斜入射本身仍可在
    `mode=reference` 下使用。
28. **符号约定必须显式**（批次 J）：本工程取 `μ=max(0,k·n)`（`k` 指向光照侧），
    与任务书 `max(0,-k·n)` 等价但差一个整体符号；`direction_unit` 的 `k_z>0` 由此强制。
    详见 ADR-0015。

## 建议的修复选项（待人工决策，未执行）

| 选项 | 做法 | 保留 | 代价 |
|---|---|---|---|
| A 保持现状 | 不改历史；仅靠 HEAD 快照备份 | 全历史哈希、E/F 提交信息 | 不能 push/克隆；E/F 快照不可读 |
| B 重建 E/F | 用可再生成内容重建 E/F 的 tree（丢弃其不可读快照） | A–D 与 G，提交数与信息 | E/F 哈希变更；E/F 内容退化为近似 |
| C 重建裸历史 | 以 HEAD 工作树重新 `git init` 提交一条干净历史 | 当前全部文件内容 | 丢失 A–G 全部历史哈希（叙述仍在 ADR/报告里） |
| D 恢复远端 | 若他处有完整副本（另一机器/云）→ 取回后 `git fetch` 补齐 | 全部 | 需确认确实存在完整副本 |

> 已排除：`git gc` / `git prune` **无法**恢复缺失对象（它们不是「游离」而是「不存在」）。

## 历史重建记录（B 方案，2026-09-11 已执行）

> 上两节告警与选项表均为事故时的**原始记录，原样保留**；本节记录后续处置。

**决策**：B —— 保留 A–D，重建 E/F，再把 G、告警、演示接回。A 被否（仍无法 push/克隆）、
C 被否（会丢弃 A–G 提交脉络）、D 无执行依据（仓库无远端）。

**方法（可复现，脚本在 `../_repo_backup/rebuild_20260911/`）**

1. `locate_missing.py` 逐一枚举 E/F/G 的树，把每个缺失对象定位到「提交 + 路径」。
2. 规则只有一条：**保留原提交中仍存在的每一个条目；仅把丢失的条目按同路径取自最近的完整树**
   （E 取自 F，F 取自 G）。E 的 `docs/`、`tools/` 两棵**整体缺失**的顶层子树取自 F，
   但**剔除批次 F 自己新增的 7 个产物**，使 E' 不含 F 的工作。
3. 用 `git mktree` 自底向上重建树；先用 G 的树做**自校验**（重建结果与原始 `b2d00484…`
   逐位一致）后才落盘。
4. G / 告警 / 演示三个提交**沿用原树逐位不变**，只改父指针；作者与提交者身份、时间戳
   全部保留（实测 `author+date`、`committer+date`、`subject` 三项均一致）。

**补回清单**（完整明细见 `restore_plan.json`、`OLD_HISTORY.txt`）

- **E'**：文件条目 153；补回 **39** 项（全部取自 F），剔除 **7** 项批次 F 产物
  （`ADR-0011`、`table_lookup.md`、`table_errors.csv`、`curve_interpolation.csv/.html`、
  `tools/make_curves.py`、`tools/table_report.py`）。
- **F'**：文件条目 195；补回 **1** 项
  （`docs/reports/curve_interpolation.html`，取自 G）。
- **已知近似**：E' 中 `docs/`、`tools/` 下若干文件的**中间版本无法恢复**，按「最近完整版本」
  落地（如 `progress.md`、`acceptance_report.md`、`ui_operation_check.*`、
  `tools/run_acceptance.py`、`tools/ui_probe.py`、`ADR-0002/0005`）。此为 B 方案的既定代价。

**新旧哈希**

| 提交 | 旧 | 新 |
|---|---|---|
| E | `4748cb2` | `925cf9b` |
| F | `ccf0717` | `06a5f12` |
| G | `68a7858` | `f7f9d64` |
| 告警 | `7e36d67` | `b7c5c0f` |
| 演示 | `d798a41` | `66237a7` |

A–C（`29cf6bb`）与 D（`61a2e4e`）**未改动**。

**验证（均为实测，非预期值）**

- 末端一致：`git diff d798a41 66237a7` 为空（两树同为 `dfe031d5…`）。
- 对象完整：`git rev-list --objects 66237a7` 共 406 个可达对象，逐一
  `git cat-file --batch-check` → **缺失 0**。
- 逐提交可归档：`git archive` 对 5 个新提交全部成功（归档条目含目录：181 / 225 / 263 / 263 / 266）。
- `git fsck --full`：清 reflog 后**无 broken link、无 missing**，仅余 10 个无害 dangling 对象。
- 可传输性：`git bundle create --all` 自校验报 *records a complete history*（等价于可 push）；
  `git clone --no-hardlinks`（含全部分支）成功，克隆内 `fsck` 干净。
- 克隆内复跑：`pytest` **277 passed**；`run_acceptance` 通过 97｜失败 0｜未运行 4；
  界面检查 17/0/0；端到端演示 13/0/0；G06 13/0（结构实例 2）。
  （注：克隆首次运行有 1 项环境性失败 `test_original_inputs_are_untouched`，
  因原始输入在该测试预期的**仓库父目录**；补上后 9 passed，与本仓库一致。）

**残留与注意**

- 仓外备份**尚未删除**：`_repo_backup/` 下 3 个 tar（G 快照、演示快照、`.git` 全量备份）
  与重建工作目录 `rebuild_20260911/`；待人工确认后再自行清理。
- 若日后需要压缩对象库，可自行执行 `git gc --prune=now`（会清掉 dangling 与旧坏对象）；
  本轮**未执行**，以免与其它会话并发冲突。`gc.auto` 仍为 `0`。
- 本机文件系统上 git **无法直接创建嵌套 ref 目录**（`refs/heads/<a>/<b>` 创建失败、
  `update-ref` 静默无效），需使用平级分支名。

## 下一批依赖（后续 / T20 收尾）

批次 J 已交付：斜入射投影、动态角度、首次交点可见性、法向厚度转换、示例包与界面几何面板。
**A–J 全部批次已交付**，仅剩下列保留项与后续可选扩展：

1. **逐事件核查表接入（查表进主循环）**：仍**保留不开放**。只有
   `event_depth_increment` 曲线可进事件核，且固定条件需匹配；相结构与查表都不得绕过
   `assert_curve_can_enter_event_kernel`。接入时需与「跨相截断后相标签更新」的诊断对齐。
2. **金刚石「合成形貌」缺口**：需人工决策补实现（新增结构类型
   `net_removal_with_optional_modification_mask` 或改卡），完成后缺口转为开放，
   探针须同步更新（见「已知限制与缺口」第 17 条）。
3. **批量能力的边界扩展**：分批相（需表达跨相界面截断）、分历史耦合（需保持事件顺序依赖）、
   分斜入射（需块内保持遮挡判定一致）、以及「批量 + 动态角度」组合——
   均属后续工程扩展，扩展时须先补组合测试再开放。
4. **可见性算法扩展**：多次反射/衍射、悬垂与多值表面；当前限于 2.5D 首次交点。
5. **光学吸收修正**：需要材料与波长的 `A(θ)` 依据后才可启用；当前明确只做几何。
6. **实验回归用例**：YSZ 图 14、SiC 图 6 原图/原表数字化后建立；当前只做到公式核查。
