# 开发进度与阶段状态

> 更新：2026-09-10｜版本：`0.4.0-f1`（M0 放行版 `0.1.0-m0`；D 为 `0.2.0-d`；E 为 `0.3.0-e1`）
> 状态取值仅限：待开发｜进行中｜待验收｜通过｜阻塞

## 批次状态

| 批次 | 任务 | 阶段 | 状态 | 备注 |
|---|---|---|---|---|
| A | T01、T02 基础 | M0 | **通过** | 包与 CLI 壳、单位与配置、人工 fixture、设计决定；安装说明、锁文件、配置错误测试、原文件哈希记录齐备 |
| B | T03–T05 | M0 | **通过** | 高斯与局部裁剪、真实事件流、固定阈值核；光束积分测试、事件 CSV、标量解析报告齐备 |
| C | T06、T08 | M0 | **通过** | 高度循环、统计、快照、新目录导出与重读；单坑/10 脉冲/直线/多遍栅格配置 + G01–G04 报告 |
| D | T07 | M1 | **通过** | YSZ/SiC 独立参考评估器；源公式对应表、G05 报告、平均率与累计量独立输出齐备 |
| E | T09 | M1 | **通过（待人工验收）** | `app.py` + `ui_service.py`；提交/回放求解次数记账、参考评估器与历史运行入口；界面操作检查 17/0/0 |
| F | T10 | V0.1 | **通过（待人工验收）** | 曲线 schema、分段线性 + 可选 PCHIP、越界处理、语义路由；示例 CSV、错误 CSV、原始点与插值图齐备 |
| G | T11–T13 | V0.1 | 待开发 | 分相查询、颗粒、铺层、界面截断 |
| H | T14、可选 T15 | V0.1 | 待开发 | 七材料能力入口、受限阈值协议、标签、逐事件核查表接入 |
| I | T16、T17 | V0.2 | 待开发 | 性能基线、局部优化、冻结批量 |
| J | T18、T19 | V0.2 | 待开发 | 斜平面、方向转换、可见性 |
| — | T20 | 各阶段 | 进行中 | 回归与文档随批次推进；A–F 的回归与记录已完成 |

**M0 放行结论：通过。M1 放行条件已齐备，待人工审批。**
标准要求「M0 + YSZ/SiC 参考评估器（D）+ 基础界面 + 截面与快照回放（E）」：

| M1 放行条件 | 证据 | 状态 |
|---|---|---|
| G05 文献语义回归 | `g05_reference_semantics.md`（9 通过 / 0 失败） | ✅ |
| 界面操作检查记录 | `ui_operation_check.md` / `.csv`（17 通过 / 0 失败） | ✅ |
| 提交与回放求解次数检查 | 上表同源；`G09-UI` 行；`tests/test_app_smoke.py` 硬断言 | ✅ |

**V0.1 查表（批次 F）交付完成，待人工验收。**

## 本批（F）实际完成的交付物

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

> 批次 A–C 的交付物清单见 `29cf6bb`；批次 D 见 `61a2e4e`；批次 E 见 `4748cb2`。

## 实测结果摘要

### G09 / 查表（批次 F，本批实测）

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

### G09 / 界面操作检查（批次 E，本批增补查表检查）

驱动方式：`streamlit.testing.v1.AppTest` 真实执行 `app.py`；输出隔离到 `runs/acceptance/_ui_probe`。

| 检查项 | 预期 | 实测 | 结论 |
|---|---|---|---|
| 初始渲染 | 无异常、`solve_count=0`、无冻结结果 | 一致 | 通过 |
| 提交计算 | `solve_count` 恰好 `+1` | `0 → 1`，`status=completed`，落盘 `metadata.json` | 通过 |
| 改参数未提交 | 旧结果标为「上一次运行」且不求解 | `is_stale=True`，`solve_count=1` 不变，界面出现该警告 | 通过 |
| 快照回放 / 切图层 / 旋转视图 / 切截面 | `solve_count` 不变 | `1 → 1` | 通过 |
| 读取历史运行 | `read_count +1`、`solve_count` 不变 | `read_count=1`、`solve_count=0` | 通过 |
| 参考评估器「评估」 | 不进入逐事件求解、不产生形貌 | `solve_count=0`，有参考结果，`frozen=None` | 通过 |
| **查表「查值」（本批新增）** | 不触发求解、不读历史 | `solve_count=0`，产出查表结果 | 通过 |
| **换曲线卡 / 换插值方法（本批新增）** | `solve_count` 恒为 0 | 3 条曲线 × 2 方法均不变 | 通过 |
| 图层标签措辞 | 不含「热影响区 / HAZ / 温度」等被禁词 | 全部干净 | 通过 |
| 缺能力模式 | 配置层拦截并给准确原因 | CFRP + `reference_case` → 拒绝，列出允许模式 | 通过 |
| 合成模式导出 | 禁止以 `depth_um` 命名物理深度 | 导出标签 `depth/delta_ref`，`depth_um` 被拒 | 通过 |
| `threshold_only` 结果 | 去除量显示「不提供」而非数值零 | `removal_available=False` | 通过 |

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

- `python -m pytest -q`：**251 passed**（A–C 63 + D 18 + E 61 + 界面冒烟 18 + F 查表 91）。
- `docs/reports/acceptance_report.md`：**通过 69｜失败 0｜未运行 5**。
- 界面操作检查：**通过 17｜失败 0｜未运行 0**。
- 查表拒收检查：**通过 21｜失败 0**。

## 未运行项（不得视为通过）

| 编号 | 原因 |
|---|---|
| G06 | 批次 G（T11–T13）未实施：结构化相界面在配置层拦截 |
| G07 | 批次 J（T18）未实施：斜入射在配置层拦截（`GEOMETRY_UNSUPPORTED`） |
| G08 | 批次 I（T17）未实施：`accelerators.py` 为占位 |
| G09（逐事件核查表接入） | 查表（T10）已交付 schema/插值/越界/路由；**逐事件主循环接入**属批次 H（T14）未实施 |
| B01–B04 | 批次 I（T16）未实施；本批只记录单次运行 `elapsed_s` |

> 批次 D 的 `M1-UI` 未运行项已随批次 E 交付撤销。

## 阶段收尾检查（执行细则第 12 节）

- [x] 实际执行的小例子可安装、运行、导出并重新读取。
      （`examples/*.json` + `python -m ufdemo run|reference|table` + `python -m ufdemo inspect`）
- [x] 本阶段适用 G 测试有实测记录，失败与未运行项目未标为通过。
      （`acceptance_report.md`：通过 69｜失败 0｜未运行 5，逐一列出原因）
- [x] 原始资料和旧程序未被覆盖；迁移与代码变更可追溯。
      （F01/F02 哈希与任务书指纹一致；`input_hash_check.csv`；`material_migration.csv`；
      `微观仿真/` 未被触碰）
- [x] 合成、参考、数值通过和实验验证的标签未混用。
      （合成卡 `synthetic_definition`；fixture `analytic_test_definition`；
      SiC 查表曲线由材料卡参数按式(6) 重算，标注为「非原图复现」；
      参考结果 `run_mode=reference_case` 且 `verified_by_experiment=false`；
      报告分三栏，实验复现栏为空）
- [x] 未开放的功能组合在配置层拦截，不能仅依赖按钮禁用。
      （斜入射、结构化相界面、跨层孵化、批量加速均在 `validate_run` 拦截；
      参考语义由 `assert_reference_only_semantics` 反向闸门拦住；
      查表语义由 `assert_curve_can_enter_event_kernel` / `assert_no_local_depth_from_volume` 拦住；
      界面另加 `capability_gate`，但拦截责任仍在配置层）
- [x] 已知限制、尚缺资料和下一批依赖写入阶段报告。（见下节）

## 已知限制与缺口

1. **F03 / F04 文件未找到**：`paper5.0.tex`、
   `ttm_carrier_drilling_q4_axisymmetric.py` 在当前工作区不存在，
   已记录为缺失。细则第 3 节明确其不阻塞人工解析主线。
2. **M0 只支持正入射与单相均质表面**，见 README 第 5 节。
3. **七种材料的定量能力受限**：目前只有 YSZ 加工分支具备
   `event_depth_increment`（且为条件限定）；其余材料缺 δ 或完整响应，
   只能做阈值展示或合成演示，**不输出物理深度**。
4. **界面不提供超阈值掩膜图层**：本批次未记录逐事件超阈值掩膜；
   阈值观测量属批次 H 的受限阈值协议。界面**如实显示不可用原因**，
   不用「累计剂量 vs 单脉冲阈值」比较来伪造该标记。
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
    跨工况需另附条件匹配的曲线；接入主循环属批次 H（T14）。
11. **查表的 SiC 曲线由材料卡拟合参数按式(6) 重算**：不是原图逐点数字化，
    因此**不构成对原文曲线的复现**（`table_lookup.md` 边界声明已写明）。
12. **PCHIP 在有效区间外不作为**：越界不画虚线外推，不预测、不填补。

## 下一批依赖（批次 G，T11–T13）

1. 分相查询与结构化相界面：`validate_run` 现已在配置层拦截结构化相；
   批次 G 需放开并实现「同相合并等价、不同相不跳过、固定种子复现」。
2. 颗粒与铺层：需在相结构上叠加颗粒/铺层几何，并接入已有的
   `assert_increment_semantics` 闸门（平均/累计量不得进事件核）。
3. 界面截断：批次 E 的「不提供图层如实报不可用」机制可复用；
   新相结构图层若不可用须走 `UNAVAILABLE_LAYERS` 同口径。
4. 查表（批次 F）与相结构的接口：只有 `event_depth_increment` 曲线可进事件核，
   相结构不得绕过 `assert_curve_can_enter_event_kernel`。
