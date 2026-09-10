# ultrafast-demo —— 七种材料超快激光加工 Demo（M0 + 批次 D/E/F）

> 版本：`0.4.0-f1`｜日期：2026-09-10｜状态：**批次 A–F 已实施并通过验收；M0 已放行，M1 条件齐备待审批**
>
> 依据：上层目录 `ultrafast_laser_demo_execution_spec.md`（执行细则）与
> `ultrafast_laser_demo_task_plan.md`（任务书）。
>
> 已交付：M0 最小 CLI 闭环（A–C）+ YSZ/SiC 文献参考评估器（D）+ Streamlit 界面（E）
> + 查表（F，曲线插值与越界处理）。分相结构、逐事件核查表接入、加速尚未开始，
> 详见 `docs/reports/progress.md`。

---

## 1. 已实现能力

| 能力 | 状态 |
|---|---|
| 单坑 / 定点多脉冲 / 直线扫描 / 多遍蛇形栅格 | ✅ 可运行（`examples/*.json`） |
| 高斯光束 `F=2E/(πw²)·exp(-2r²/w²)`、`w(s)` 离焦、局部裁剪窗口 | ✅ |
| 统一时钟 `t_j = t0 + j/f` 的真实事件流（左闭右开、出光开关、无相位重置） | ✅ |
| 固定阈值对数去除核 `a = δ[ln(F/Fth)]₊`（先掩膜后取对数） | ✅ |
| 2.5D 高度场逐事件更新、去除体积 / ROI / 截面统计 | ✅ |
| 运行目录导出与重读（config / 卡快照 / 统计 / 截面 / 快照 / 诊断 / 事件） | ✅ |
| 材料卡迁移（F01/F02 → 执行卡）与能力推导 | ✅ |
| 取消 / 失败 / 完成三态与部分结果 | ✅ |
| **YSZ/SiC 文献参考评估器**（有效 N、阈值函数、平均率、协议累计深度） | ✅ 批次 D（T07），`python -m ufdemo reference` |
| **Streamlit 界面**（参数表单、形貌/截面/时间轴回放、参考评估器、查表、历史运行） | ✅ 批次 E（T09）+ F，`streamlit run app.py` |
| **查表**（曲线 schema、分段线性 / 保形 PCHIP、越界处理、语义路由） | ✅ 批次 F（T10），`python -m ufdemo table` |
| 分相颗粒 / 铺层 / 界面截断 | ❌ 批次 G（T11–T13） |
| 七材料能力入口与展示 / 逐事件核查表接入 | ❌ 批次 H（T14） |
| 冻结几何批量加速 / 动态角度 | ❌ 批次 I、J（T17、T18） |

**明确不做**：热场、TTM 耦合、裂纹、分层、再沉积、深孔多次反射、机器学习、
完整五轴 CAM。旧 TTM 工作区保持独立、未被修改。

---

## 2. 安装（干净环境）

需要 Python ≥ 3.10。实测环境：Python 3.13.14 / Windows 11。

```bash
cd ultrafast-demo

# 建议使用独立虚拟环境
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 方式一：可编辑安装（推荐，之后可直接用 python -m ufdemo）
pip install -e ".[dev]"

# 含界面依赖（批次 E：streamlit + plotly）
pip install -e ".[dev,ui]"

# 含查表依赖（批次 F：scipy，用于可选 PCHIP；缺它时 PCHIP 报 NOT_IMPLEMENTED）
pip install -e ".[dev,table]"

# 方式二：不安装，靠 PYTHONPATH 运行
pip install -r requirements.lock
export PYTHONPATH=src           # Windows: set PYTHONPATH=src
```

`requirements.lock` 是本机实际安装并验证通过的版本组合（numpy 2.3.5、
pytest 9.1.1、openpyxl 3.1.5；批次 E 追加 streamlit 1.63.0、plotly 7.0.0
及传递依赖；批次 F 追加 scipy 1.18.1），不是“全部最新版”。

可选依赖：`scipy`（批次 F 查表，**已安装**）、`streamlit` + `plotly`（批次 E 界面）、
`numba`（批次 I 可选加速，缺失时回退 NumPy）。

---

## 3. 一个小例子（完整可跑）

```bash
# 1) 准入校验
python -m ufdemo validate examples/analytic_single_pulse.json

# 2) 执行并把结果写到新目录（目录已存在会被拒绝，不覆盖）
python -m ufdemo run examples/analytic_single_pulse.json --out runs/analytic_001

# 3) 查看输出
ls runs/analytic_001
# config.json  material_snapshot.json  metadata.json  final_surface.npz
# statistics.csv  profiles.csv  events.csv  diagnostics.json
# snapshots/index.json  snapshots/snap_0*.npz

# 4) 重新读取结果核对
python -m ufdemo inspect runs/analytic_001

# 5) 打印七材料能力表
python -m ufdemo materials

# 6) 文献参考评估（批次 D）：只做公式复现，不求解网格
python -m ufdemo reference examples/ysz_reference_case.json --out runs/g05_ysz_reference
python -m ufdemo reference examples/sic_reference_case.json --out runs/g05_sic_reference

# 7) 查表（批次 F）：读曲线卡、插值、越界处理；不求解网格
python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 5
python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 2.5 --method pchip
python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 1e3 --allow-out-of-range --json
python -m ufdemo table --all-curves

# 8) 全部测试
python -m pytest -q
```

---

## 3b. 界面（批次 E / T09）

```bash
streamlit run app.py
```

（未安装可编辑包时用 `PYTHONPATH=src streamlit run app.py`。界面依赖见
`pyproject.toml` 的 `ui` extra。）

界面分五个标签页：

| 标签页 | 内容 |
|---|---|
| 参数与运行 | 模板选择、网格/光束/路径/输出表单、「提交计算」 |
| 结果 | 形貌（热图/三维曲面）、截面、时间轴快照回放 |
| 参考评估器 | 批次 D 的 YSZ/SiC 算例（只做公式核查，**不求解网格**） |
| 查表 | 批次 F 的曲线卡：原始点、查值（线性/PCHIP、可勾选允许越界）、原始点与插值图（**不触发求解**） |
| 历史运行 | 列出 `runs/` 下可读目录并读取（**不重新求解**） |

### 界面的三条硬规矩

1. **只有「提交计算」会求解。** 侧边栏实时显示「提交求解次数」；换模板、拖时间轴、
   切图层、旋转/翻转视图、切截面、读历史都只读已有数组，计数不变。
   `ui_service.layer_view()` / `snapshot_arrays()` 内部有 `assert` 硬断言，
   一旦被破坏立刻抛错而不是静默多算。
2. **参数编辑态与结果态分离。** 提交后只要再改参数，结果区立刻把旧结果标为
   「上一次运行（时间戳）」并给出警告；图表永远读冻结快照，不读当前表单值。
3. **缺能力就说缺能力。** 材料卡未开放的运行模式在配置层被拦截，侧边栏显示
   准确原因；合成示例必须用户**显式选择**，不自动降级、不静默填值。

措辞上：阈值类图层不写「热影响区 / HAZ」，照射剂量不写「温度」——
`assert_safe_wording()` 按**严格子串**口径复查（否定式同样拒绝），
图层标签只描述「该图实际是什么量」。

### 界面的验收记录

```bash
python tools/ui_probe.py                 # 17 项界面操作检查，输出到 runs/_ui_probe
python -m pytest tests/test_ui_service.py tests/test_app_smoke.py -q
```

`tools/ui_probe.py` 用 `streamlit.testing.v1.AppTest` 真实执行 `app.py`，
并把「提交/回放求解次数」等检查写入 `docs/reports/ui_operation_check.md` / `.csv`。
探针通过环境变量 `UFDEMO_RUNS_DIR` 把产生的运行写到隔离目录，不污染工作区 `runs/`。

结果含义（G01 为例）：单脉冲、`Fth=1 J/cm²`、`δ=100 nm`、`w=10 μm`、
`F0=e²Fth` → 中心去除 200 nm，去除体积 3.1411e-17 m³（解析值
3.1415926535897935e-17 m³，相对误差 1.57e-4，容差 1%）。

其余六个可运行示例：

| 配置 | 内容 |
|---|---|
| `examples/ten_pulses.json` | 定点 10 脉冲，中心 2 μm，体积 3.1411e-16 m³ |
| `examples/line_scan.json` | 直线扫描，`v/f = 10 μm` 间距，7 个事件 |
| `examples/raster_multipass.json` | 5 行 × 蛇形 × 3 遍，105 个事件，逐遍快照，2 个 ROI |
| `examples/synthetic_demo_point.json` | 无量纲合成演示（`L_ref`/`F_ref`/`δ_ref`），禁止导出物理 μm 深度 |
| `examples/ysz_reference_case.json` | YSZ 式(9) 有效 N 换算 → `278.9734276388 mm/s`；脉冲能量 201.464053689406 μJ |
| `examples/sic_reference_case.json` | SiC 式(6) 阈值（`Fth(1)=2.35`、`F_∞=0.70 J/cm²`）+ 式(7) 平均率 |

> `reference` 只复现文献公式与协议量，**不产生** `final_surface.npz`：
> 参考评估器不求解网格，避免把参考量误当形貌结果。

---

## 3c. 查表（批次 F / T10）

一条曲线 = 一个 JSON 元数据卡 + 一个 CSV 原始点文件（`data/curves/`）：

```text
data/curves/
├── ysz_analytic_depth_vs_fluence.curve.json   # event_depth_increment → 事件核
├── ysz_analytic_depth_vs_fluence.points.csv
├── sic_threshold_vs_effective_n.curve.json    # threshold_only → 评估器
├── sic_threshold_vs_effective_n.points.csv
├── synthetic_volume_per_energy.curve.json     # volume_per_energy → 评估器
└── synthetic_volume_per_energy.points.csv
```

```bash
python tools/make_curves.py      # 可重生成示例曲线与 14 组无效夹具
python tools/table_report.py     # 生成错误 CSV 与原始点/插值图
```

查表的四条硬规矩：

1. **默认分段线性，可选保形 PCHIP。** PCHIP 显式 `extrapolate=False`；
   环境缺 SciPy 时报 `NOT_IMPLEMENTED`，**绝不静默退化为线性**。
2. **越界返回状态，不返回 0、不外推、不钳端点。** 越界抛 `TABLE_OUT_OF_RANGE`；
   允许越界时对应项为 `None`（不是 0）——「低于量测区间」不等于「无去除」。
3. **横坐标严格升序且无重复。** 重复 x 默认拒绝；需要合并必须声明
   `duplicate_policy` + `duplicate_rule_note`，且原始点全部保留在 `raw_points`。
4. **只有 `event_depth_increment` 曲线能进事件核**（且固定条件需匹配）；
   体积/平均率/累计曲线只进评估器，**不得反推局部深度剖面**。

> 本批**不接入逐事件主循环**（属批次 H / T14）；查表本身不产生任何形貌。
> SiC 曲线由材料卡拟合参数按式(6) 重算，**不构成对原文曲线的复现**
> （边界声明见 `docs/reports/table_lookup.md`）。

---

## 4. 关键约定（改动前先看 `docs/decisions/`）

* **单位**：物理模式内部 SI；合成模式内部无量纲（`x/L_ref`、`h/L_ref`、
  `w/L_ref`、`zR/L_ref`、`F/F_ref`、`E/(F_ref·L_ref²)`）。
* **数组方向**：单元中心规则网格，形状固定 `(ny, nx)`；第 0 维 y，第 1 维 x。
* **深度方向**：`h` 为物理高度，z 轴向外；`d = h0 - h ≥ 0`；正入射按 `h ← h - a`。
* **时间**：`t_j = t0 + j/f`，段区间 `[start, end)`，关闭出光期间时钟继续。
* **证据状态**：执行层只用 `unverified` / `literature_reported` /
  `formula_checked` / `experiment_reproduced` / `independently_validated`
  （**不使用** `synthetic`，任务书示例中的写法已在执行层统一）。
* **响应语义**：只有 `event_depth_increment` 能进逐事件主循环；平均率、
  累计深度、轨道深度、体积效率一律拒绝（即使数组形状吻合）。
* **两道方向相反的语义闸门**：`response.assert_increment_semantics()` 只放行
  逐事件增量；`references.assert_reference_only_semantics()` 只放行平均率/累计/
  阈值，防止逐事件增量冒充参考曲线。
* **两套有效脉冲数不共用公式**：`N_eff,YSZ = (π/4)(2w0f)/v`（式 9，面积等效）
  与 `N_eff,SiC = K(2w0f)/v`（式 5，K 为扫描遍数）分别实现，结果里写入
  `definition` 字段；`effective_count`（等效次数）与 `event_count`（真实脉冲数）
  命名强制区分。错误换算 `2w0f/N` 仅保留为回归对照，不进入任何输出。
* **不补数据**：`null` 就是缺失，`unknown` 就是未确认；不用相近材料、
  不同脉宽或纳秒数据“补齐”。
* **不静默降级**：超预算、条件不匹配、模式不支持都在配置层拦截并报错，
  不做自动切换模式或变粗网格。
* **界面分层**：`src/ufdemo/ui_service.py` 是纯逻辑且**不导入 Streamlit/Plotly**；
  `app.py` 是唯一导入 Streamlit 的文件。求解只经 `ui_service.submit()`，
  它同时是 `solve_count` 的唯一递增点（见 `docs/decisions/ADR-0010-ui-layer.md`）。
* **界面措辞**：`FORBIDDEN_TERMS` 按严格子串口径复查界面与导出文案；
  `LAYER_LABELS` 只描述「该图实际是什么量」，澄清信息放在不可用原因文本里。
* **不提供的图层如实报不可用**：`threshold_mask` 本批次未记录，界面显示原因，
  不用「累计剂量 vs 单脉冲阈值」比较伪造标记；`threshold_only` 结果不提供深度时
  显示「不提供」而非数值 `0`。
* **查表不是任意外推**：默认分段线性；可选 PCHIP 显式 `extrapolate=False`；
  越界是显式状态（`TABLE_OUT_OF_RANGE`），**不返回 0、不外推、不钳端点**；
  重复 x 默认拒绝，需合并时须附规则并保留原始点（见
  `docs/decisions/ADR-0011-table-lookup.md`）。
* **曲线去向按语义硬分流**：只有 `event_depth_increment` 可进逐事件核
  （`assert_curve_can_enter_event_kernel`，另含固定条件比对）；体积/平均率/累计
  曲线只进评估器，**不得反推局部深度**（`assert_no_local_depth_from_volume`）。

---

## 5. 已知限制（本批）

1. **M0 只开放正入射**。斜入射 / 法向厚度转换 / 遮挡未实现，非正入射
   配置会被 `GEOMETRY_UNSUPPORTED` 拦截（不是靠按钮禁用）。
2. **只有单相均质表面**。结构化相界面、颗粒、铺层未实现，配置层拦截。
3. **历史与孵化未实现**。`solver.history_enabled=true` 会报错；
   局部受照计数已记录，但不用作孵化输入。
4. **不能从阈值反推绝对深度**。CFRP / Inconel 718 / 金刚石 / 铝基 SiC /
   微晶玻璃缺 δ，能力表里深度能力为不可用，只开放阈值展示或合成演示。
5. **SiC 的平均去除率是评估器语义**，不能逐事件累加。批次 D 已实现
   `references.py`：平均率（`mean_depth_per_effective_pulse`）与协议累计深度
   （`cumulative_depth = 平均率 × N_eff`）分别输出；把平均率转成逐事件模型属
   工程外推，需另记 `engineering_extension` 并单独验证，本批**不做**该转换。
6. **参考评估器不建实验回归用例**。YSZ 图 14、SiC 图 6 的原图/原表尚未数字化，
   G05 只做到公式核查；SiC 扫描次数列表存在重复项，本批只接受直接输入论文有效 N。
7. **界面为单机 Streamlit，无并发会话与持久化状态**。会话状态在进程内；
   历史结果通过运行目录读取，不建数据库。界面**不提供**超阈值掩膜图层
   （本批次未记录），阈值观测量属批次 H 的受限阈值协议。
8. **无性能基准**。本批只记录单次运行 `elapsed_s`；B01–B04 属批次 I。
9. **F03 / F04 文件在当前工作区未找到**（`paper5.0.tex`、
   `ttm_carrier_drilling_q4_axisymmetric.py`）。已按细则记录为缺失，
   不阻塞人工解析主线；哈希核对结果见
   `docs/reports/input_hash_check.csv`。
10. **查表曲线只覆盖固定协议下的一维响应，且不接入逐事件主循环**。
    跨工况需另附条件匹配的曲线；接入主循环属批次 H（T14）。
    PCHIP 在有效区间外不作为（不画虚线外推）。
11. **查表的 SiC 曲线由材料卡拟合参数按式(6) 重算**，不是原图逐点数字化，
    因此**不构成对原文曲线的复现**（见 `docs/reports/table_lookup.md` 边界声明）。

---

## 6. 目录结构

```text
ultrafast-demo/
├── pyproject.toml
├── requirements.lock
├── README.md
├── app.py                # Streamlit 界面（唯一导入 Streamlit 的文件，批次 E）
├── src/ufdemo/
│   ├── errors.py         # 机器可读错误（细则 11.2）
│   ├── config.py         # 配置、单位、跨字段校验、执行准入
│   ├── materials.py      # 材料目录、能力推导、卡版本
│   ├── beam.py           # 高斯能流、离焦、局部窗口
│   ├── paths.py          # 轨迹段、统一时钟、事件迭代器
│   ├── response.py       # 逐事件核、阈值与响应语义
│   ├── surface.py        # 高度场、相界面、局部历史
│   ├── solver.py         # 逐事件编排、取消、快照
│   ├── metrics.py        # 体积、ROI、截面
│   ├── io.py             # 导出、重读、哈希和状态
│   ├── references.py     # 文献参考评估器（批次 D：YSZ/SiC 有效N、阈值、平均率）
│   ├── ui_service.py     # 界面逻辑层（批次 E；不导入 Streamlit/Plotly）
│   ├── tables.py         # 查表：曲线 schema、插值核、越界与语义路由（批次 F）
│   ├── geometry.py       # 批次 J 占位（斜入射/可见性）
│   ├── accelerators.py   # 批次 I 占位（批量加速）
│   └── __main__.py       # CLI
├── tools/
│   ├── migrate_materials.py   # F01/F02 → 执行卡 + 迁移/准入报告
│   ├── make_examples.py       # 生成 examples/*.json
│   ├── make_curves.py         # 生成 data/curves 示例曲线与无效夹具（批次 F）
│   ├── table_report.py        # 查表报告：错误 CSV + 原始点/插值图（批次 F）
│   ├── ui_probe.py            # 界面操作检查（AppTest；批次 E，批次 F 增补查表检查）
│   └── run_acceptance.py      # 实际执行并把实测值写入验收报告
├── data/materials/       # 执行卡（真实材料 + _synthetic_demo_isotropic）
├── data/curves/          # 响应曲线卡（*.curve.json + *.points.csv，批次 F）
├── data/references/      # 原始来源快照与输入指纹
├── examples/             # 可运行配置
├── tests/                # pytest（含 fixtures 人工解析卡、界面逻辑与冒烟测试、无效曲线夹具）
├── docs/decisions/       # 设计决定记录（ADR-0001 … ADR-0011）
├── docs/reports/         # 迁移、准入、验收、界面检查、进度报告
└── runs/                 # 每次运行的独立目录（默认不删不覆盖）
```

原始输入 `seven_materials_parameter_register.xlsx` 与
`material_card_templates.json` **未被修改**，哈希与任务书第 16.3 节指纹一致。

---

## 7. 测试与报告

```bash
python -m pytest -q                  # 251 项，全部通过（A–C 63 + D 18 + E 逻辑 61 + 界面冒烟 18 + F 查表 91）
python -m pytest -q -m g05           # 只跑文献语义回归
python -m pytest -q -m g09           # 界面与查表的 G09 相关测试
python tools/migrate_materials.py    # 迁移 + 7 项准入探针
python tools/make_curves.py          # 生成示例曲线与无效夹具（批次 F）
python tools/table_report.py         # 查表报告：错误 CSV + 原始点/插值图
python tools/ui_probe.py             # 界面操作检查（AppTest 驱动 app.py）
python tools/run_acceptance.py       # 实际执行并生成验收报告（A–F）
```

产物：

* `docs/reports/acceptance_report.md` —— G01–G09 实测值与容差对照；
* `docs/reports/acceptance_g01_g05.csv` —— 逐项机器可读结果；
* `docs/reports/g05_reference_semantics.md` / `.csv` —— G05 专项报告；
* `docs/reports/reference_equations.md` —— **源公式 ↔ 实现 ↔ 验收 对应表**；
* `docs/reports/ui_operation_check.md` / `.csv` —— **界面操作检查（G09-UI）**；
* `docs/reports/table_lookup.md` —— **查表汇总报告（G09-table）**；
* `docs/reports/table_errors.csv` —— **错误 CSV**：每条违规输入与错误码；
* `docs/reports/curve_interpolation.html` / `.csv` —— **原始点与插值图**；
* `docs/reports/material_migration.csv` —— 字段级迁移记录；
* `docs/reports/material_admission.csv` —— 四种必查拒绝情况；
* `docs/reports/input_hash_check.csv` —— 输入文件哈希核对；
* `docs/reports/progress.md` —— 批次状态与下一步依赖。

报告把**公式核查**、**数值实现验证**、**实验复现**分栏记录。A–F 只做到前两项；
“软件跑通”不等于“材料物理验证”。
