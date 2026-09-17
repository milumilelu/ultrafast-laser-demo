# ultrafast-demo —— 七种材料超快激光加工 Demo（M0 + 批次 D–K）

> 版本：`0.9.1`｜日期：2026-09-13｜状态：**批次 A–L 已实施并通过验收；M0 已放行，M1/M2/M3 条件齐备待审批**
>
> 版本口径：批次**不写进版本号**（`0.9.0-k1` 不是合法 PEP 440，会让 `pip install` / 构建直接失败）。
> 批次由独立元数据 `[tool.ufdemo] build_batch` 承载，运行时读 `ufdemo.BUILD_BATCH`
> 与 `io.code_version()["build_batch"]`。
>
> 依据：上层目录 `ultrafast_laser_demo_execution_spec.md`（执行细则）与
> `ultrafast_laser_demo_task_plan.md`（任务书）。
>
> 已交付：M0 最小 CLI 闭环（A–C）+ YSZ/SiC 文献参考评估器（D）+ Streamlit 界面（E）
> + 查表（F）+ 分相结构（G）+ 受限阈值协议 / 七材料能力入口 / 水印同源（H）
> + 冻结几何批量加速与性能基准（I）+ 斜入射 / 动态角度 / 可见性（J）
> + 本地 Web 界面（K，替代 Streamlit）+ 发布与接口加固（L：统一错误序列化 / 合法版本号 /
> wheel 资源定位 / 绑定提交的发布证据 / 浏览器级回归）。
> **A–L 全部批次已交付**，详见 `docs/reports/progress.md`。

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
| **分相结构**（`phase_at` / `next_different_interface`、颗粒 / 铺层、跨相界面截断） | ✅ 批次 G（T11–T13），`examples/alsic_particle_composite.json`、`examples/cfrp_laminated_ply.json` |
| **受限阈值协议 + 七材料能力入口 + 水印同源**（只按本事件入射能流判超阈、红线/缺口逐条实跑核验、导出与回放共用一个水印） | ✅ 批次 H（T14），`python -m ufdemo materials` / `tools/material_report.py` |
| **冻结几何批量加速**（块内逐事件各自算响应后相加、局部误差估计与自动回退、可选 Numba 后端） | ✅ 批次 I（T16/T17），`solver.mode=grouped`；基准见 `docs/reports/performance_baseline.md` |
| 逐事件核查表**接入**（查表曲线进入逐事件主循环） | ❌ 本批保留不开放（细则第 7 节：只有 `event_depth_increment` 且协议适用时才可进） |
| **斜入射 / 动态角度 / 可见性**（F_s=μF_⊥、首次交点遮挡、Δh=-a_n/n_z） | ✅ 批次 J（T18/T19），`examples/oblique_plane_60deg.json`、`examples/tilted_plane_dynamic_angle.json` |

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
python -m ufdemo table data/curves/analytic_fixture_depth_vs_fluence.curve.json --x 5
python -m ufdemo table data/curves/analytic_fixture_depth_vs_fluence.curve.json --x 2.5 --method pchip
python -m ufdemo table data/curves/analytic_fixture_depth_vs_fluence.curve.json --x 1e3 --allow-out-of-range --json
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

界面分六个标签页：

| 标签页 | 内容 |
|---|---|
| 参数与运行 | 模板选择、网格/光束/路径/输出表单、「提交计算」 |
| 结果 | 形貌（热图/三维曲面）、截面、时间轴快照回放 |
| 参考评估器 | 批次 D 的 YSZ/SiC 算例（只做公式核查，**不求解网格**） |
| 查表 | 批次 F 的曲线卡：原始点、查值（线性/PCHIP、可勾选允许越界）、原始点与插值图（**不触发求解**） |
| 七材料能力入口 | 批次 H：七族的开放内容 / 红线 / 缺口，逐条实跑探针并显示核验结论 |
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
python tools/ui_probe.py                 # 20 项界面操作检查，输出到 runs/_ui_probe
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
├── analytic_fixture_depth_vs_fluence.curve.json   # event_depth_increment → 事件核
├── analytic_fixture_depth_vs_fluence.points.csv
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

> 查表**不接入逐事件主循环**：批次 H 已交付受限阈值协议与七材料能力入口，但查表曲线
> 进主循环仍**保留不开放**（细则第 7 节：只有 `event_depth_increment` 且协议适用才可进）；
> 查表本身不产生任何形貌。
> SiC 曲线由材料卡拟合参数按式(6) 重算，**不构成对原文曲线的复现**
> （边界声明见 `docs/reports/table_lookup.md`）。

---

## 3d. 分相结构（批次 G / T11–T13）

结构配置声明 `structure_type`，相由 `phases[]` 内联给出响应（阈值按 `F_ref`，
去除尺度按 `L_ref`）。接口统一为 `phase_at(x,y,z)` 与 `next_different_interface(x,y,z)`。

```bash
python -m ufdemo run examples/alsic_particle_composite.json --out runs/g06_alsic_particles --force-new-suffix
python -m ufdemo run examples/cfrp_laminated_ply.json      --out runs/g06_cfrp_plies      --force-new-suffix
python tools/structure_report.py      # 生成结构实例 CSV、G06 检查 CSV 与报告
python -m pytest -q -m g06
```

```text
examples/
├── alsic_particle_composite.json   # 颗粒增强铝基（合成，seed 固定）
└── cfrp_laminated_ply.json         # CFRP 铺层 [0/90/0/90]（合成）
docs/reports/
├── structure_instances.csv         # 每个结构实例一行（含目标/实际体积分数、截断诊断）
├── g06_phase_interfaces.csv        # 13 项检查：7 检查 + 6 红线
└── g06_phase_interfaces.md         # G06 报告（含 §8 规则对应与边界声明）
```

分相结构的五条硬规矩：

1. **一个真实脉冲只调用当前暴露相的一个核。** `_per_phase_candidate` 按 `phase_id`
   分派到**单一**相核；**不拆伪脉冲**（不把同一脉冲能量摊到两相叠加）。
2. **跨相截断 = 有损近似。** `applied = min(候选去除量, next_different_interface)`；
   到界面后更新相标签，**下一个真实脉冲**才对新相响应。
3. **截断量命名固定为「未应用候选去除体积」。** 诊断字段
   `unapplied_candidate_removal_volume_internal`；**不是剩余能量**，
   也不在相之间重新分配。
4. **同相相邻区间预先合并，接触界面用统一浮点容差**（`CONTACT_TOL_REL`）；
   单测覆盖「恰好到达界面的下一脉冲」，防止卡在零厚层。
5. **δ 写作 `delta_over_L_ref`（δ/L_ref）。** 与层厚、光斑、离焦同一长度尺度；
   旧字段 `delta_over_delta_ref` 一律拒绝（会让深度整体放大 100 倍）。

三条**红线**（配置层拦截，不靠按钮禁用）：

* 相**不得借用其它材料卡**做标定（如借单晶 SiC 当颗粒相）→ `MATERIAL_CAPABILITY_MISSING`；
* **整体阈值不得拆给分相**（`assert_phase_threshold_not_split`）→ `MATERIAL_CAPABILITY_MISSING`；
* **「分相截断 × 动态角度」互斥** → `CONFIG_INVALID`。

> 结构几何是**合成**的：颗粒顺序放置（`seed` 固定）、铺层为解析条纹；
> 目标体积分数与实际体积分数**分别报告**，不强制相等。
> 本批**不含任何实验复现结论**。界面新增 `phase_id` 图层与结构诊断面板；
> 未启用分相结构的运行时**如实报不可用**，不返回全 0 假数组。

**演示路径**（前端 ↔ 后端已打通）：侧边栏选材料 `cfrp_t700_yb01_800nm` →
运行模式 `synthetic_demo` → 模板页选 `cfrp_laminated_ply.json` → 点「提交计算」→
结果页可切 `phase_id` 图层、展开「相结构诊断」看各相摘要与跨相截断诊断。
整条链路由 `tools/ui_demo_probe.py` 用 `AppTest` 真实驱动并留证
（`docs/reports/ui_demo_probe.md`，13/0/0）。

---

## 3e. 受限阈值协议与七材料能力入口（批次 H / T14）

### 受限阈值协议

逐事件超阈值掩膜按**受限口径**记录：**只按本事件入射能流**判超阈，**不产生深度**。

```bash
python -m pytest -q -m g09
python tools/material_report.py      # 生成 G09 阈值协议 CSV/报告 + 七材料能力表
```

协议的五条硬规矩：

1. **唯一注册基准 = 本事件入射能流**（`per_event_incident`）。累计/平均能流基准
   在配置层被拒（`CONFIG_INVALID`）——累计入射剂量与单脉冲能流量纲虽同、物理含义不同。
2. **禁用不造假。** 未开启协议时 `threshold_mask` 报不可用（`available=False`），
   **不返回全 0 假数组**；界面显示原因，不拿无关量凑数。
3. **多候选须显式索引。** 材料卡给多个阈值候选时，未给 `candidate_index` 即不可用。
4. **多脉冲口径不得当单脉冲阈值。** `multi_response` 口径下的材料（如高温合金）
   在多脉冲运行中不放行；单脉冲口径（如 CFRP `Fth(1)`）可用。
5. **可判别构造**：两发 `0.6 Fth`（累计 `1.2 Fth`）掩膜为空、高度逐位不变；
   单发 `1.2 Fth` 才点亮超阈单元——用于证明协议确实只按本事件判、不累加。

### 七材料能力入口

`python -m ufdemo materials` 与界面「七材料能力入口」页展示七族的
**开放内容 / 红线 / 缺口**，每条都由探针**实跑**核验，而非只写文档：

| 类别 | 含义 |
|---|---|
| 开放 `opened` | 绑定真实能力/运行模式，且该能力查得可用 |
| 红线 `blocked` | 绑定 enforcement + 探针键 + **期望错误码**，实跑须抛出该码 |
| 缺口 `deferred` | 规格要求开放、但实现未支持 → 附探针**证明现在确实打不开** |

> 目前七族合计 **27 条**（开放 15 / 红线 11 / 缺口 1），探针 27/27 成立。
> 唯一的缺口是金刚石「合成形貌」：卡内 `structure_type =
> net_removal_with_optional_modification_mask` 既不在能力白名单、也未被结构构建器支持，
> **如实记为缺口**（探针证明当前打不开），**不冒充已开放**；待人工决策是否补实现。

### 水印同源

`materials.build_watermark(material, unit, run_mode)` 是水印的**唯一权威来源**：
solver 写入、`io` 落盘（`watermark.json` + `statistics.csv` 的 `watermark.*` 自描述行）、
界面展示与历史回放都读它，避免「导出说一个模式、界面显示另一个模式」。

```bash
python -m pytest -q -m g09 -k watermark
```

---

## 3f. 冻结几何批量加速（批次 I / T16、T17）

分组模式把一小段路径内的几何**冻结**，先逐事件算出各自去除量再求和，最后一次性提交：

```bash
python -m pytest -q -m g08                 # G08 分组与逐脉冲对照（26 项）
python tools/perf_report.py                # B01–B04 性能基准（含环境与内存口径）
python tools/perf_report.py --quick        # 缩减规模自检
python tools/perf_report.py --from-csv     # 按已有 CSV 重写报告（免重跑）
```

配置方式（也可在界面「求解模式」一栏选择）：

```json
"solver": {
  "mode": "grouped",            // reference（逐脉冲参考）| grouped（冻结几何分组）
  "batch_size": 64,             // 块的脉冲数上限（事件块限额）
  "local_rel_tol": 1e-3,        // 局部步长误差的相对容差
  "geometry_drift_limit": 0.25, // 几何漂移上限（仅 axial_defocus 生效）
  "acceleration": "off"         // off（NumPy）| numba（可选，实测无收益）
}
```

分组的六条硬规矩：

1. **每个脉冲单独算非线性响应后相加**（`Δh = Σ_j a(F_j)`）。
   **禁止**先累加能流成 `ΣF_j` 再取一次对数——那会改变非线性响应。
   G08 用可判别构造验证：`F₀=e²Fth` 的两脉冲结果是 `4δ`，错误路径是 `≈2.69δ`。
2. **局部误差估计只用于控制批大小**：比较「整块一步」与「两个半步」的更新场，
   超限则**不提交状态**、缩小 `batch_size` 重试，最终回到 `B=1`（参考更新）。
   界面与诊断都会写明：**局部误差估计不是全局误差证明**。
3. **几何与当前高度无关时跳过半步试算**（`fixed_geometry` 下能流只用初始面，
   一步与两步数学恒等）——这是严格等价的优化，不是降低校验强度。
4. **计数语义与逐脉冲逐位对齐**：块级 `touch_counts` 是每单元**被去除的次数**，
   直接累加即等价于逐脉冲的 `exposure_count += 1`。
5. **回退而非硬凑**：分组 × 分相、分组 × 历史耦合、分组 × 动态角度三条组合在**配置层**
   拦截（`CONFIG_INVALID`）；求解器侧另留兜底并写明原因。
6. **快照落在块边界**（粒度近似）：索引标注为块内最后一个命中事件，
   只要发生就写入 `metadata.approximations` 与诊断，**不把近似当精确**。

> **性能口径**：定点/小段扫描有明显收益（本机实测 B01 4.00×、B02 3.90×），
> 大范围扫描收益有限（B04 1.32×）；**Numba 实测无收益**（0.999× 且内存更高），
> 故默认用 NumPy。**不承诺任何固定加速倍数**，无收益时保留参考模式。
> 数值只对 `performance_baseline.md` 第 1 节记录的本机环境成立。

---

## 3g. 斜入射、动态角度与可见性（批次 J / T18、T19）

```bash
python -m pytest -q -m g07                    # G07 斜入射与表面几何（23 项）
python -m ufdemo run examples/oblique_plane_60deg.json --out runs/oblique_001
python -m ufdemo run examples/tilted_plane_dynamic_angle.json --out runs/tilted_001
```

三种几何模式（界面「几何修正」栏可切换）：

| 模式 | 触发 | 法向来源 |
|---|---|---|
| 正入射 | `direction_unit=(0,0,1)` 且 `dynamic_angle=false` | 不启用修正，**走既有正入射核（逐位不变）** |
| 固定角度 | `direction_unit` 倾斜，`dynamic_angle=false` | **解析平面法向**（`flat`→(0,0,1)；`tilted_plane`→由斜率给出） |
| 动态角度 | `solver.dynamic_angle=true` | 当前窗口高度场的**逐点梯度** |

**符号约定（务必先读，否则会把入射角算反）**

本工程取 ``k = direction_unit`` 指向**光照侧**，故入射余弦为

```
mu = max(0, k·n),      n = (-h_x, -h_y, 1)/sqrt(1+h_x^2+h_y^2)
```

任务书写的是 ``max(0, -k·n)``，两者只差 ``k`` 的整体符号（令 ``k'=-k`` 即等价）。
采用本形式可保证 **0° 严格退化到既有正入射核**（G07 已逐位验证）。
因此 `direction_unit` 的 ``k_z`` 必须为正，否则配置层拒绝。

**核心公式**

* ``s = (q-q_f)·k``，``r² = |q-q_f|² - s²``（只把**浮点舍入**的极小负值截零；
  明显负值视为实现错误并报错）；
* 表面能流 ``F_s = mu · F_perp``，**只乘一次**余弦——椭圆投影由 ``r²`` 的定义自然得到；
* 法向厚度 → 高度：``Δh = -a_n/n_z``（**不是** ``-a_n·n_z``）；
* 仅作用于**可见的首次交点**（射线沿 ``+k`` 步进，落到表面之下即判遮挡）。

**四条硬规矩**

1. **超范围即停**：``n_z < 0.5`` 或入射角 > 60° 时停止该模式并报**首个越界单元位置与原因**，
   **不裁剪角度继续运行**。这两个数是**软件数值/展示范围**，不是材料物理边界。
   背向（``μ≤0``）不算越界——它天然零直接照射。
2. **只做几何修正**：未提供材料与波长的 ``A(θ)`` 依据时**不预测吸收差异**；
   界面不提供"材料偏振吸收预测"，也不给透明介质无条件设 ``A=1-R``。
3. **遮挡/背向零直接照射**，但**不代表材料内部无响应**；诊断与 ``metadata.approximations``
   会如实标注。
4. **批量 × 斜入射回退参考**：窗口内可见性会随烧蚀形貌变化而块内几何被冻结，
   故第一版回退逐脉冲并写明原因。斜入射本身仍可在 ``mode=reference`` 下使用。

**配置示例**

```json
"grid": { "initial_surface": "tilted_plane", "initial_slope_x": 0.6, "initial_slope_y": 0.0 },
"laser": { "direction_unit": [0.342, 0.0, 0.940] },
"solver": { "dynamic_angle": true }
```

> **实测**：0° 与参考逐位一致；60° 足迹长短轴比 1.9917（理论 2）、中心能流比
> 0.500000000000；``Δh=-a_n/n_z`` 与解析预测误差 1.5e-16；截获能量相对误差 1.7e-9。

---

## 3h. 本地 Web 界面（批次 K，主界面）

```bash
PYTHONPATH=src python -m ufdemo.webapp              # 默认 127.0.0.1:8787
PYTHONPATH=src python -m ufdemo.webapp --port 9000
python -m pytest -q tests/test_webcontract.py       # 契约层与端到端（23 项）
node webui/test/contract_test.mjs                   # 前端契约（27 项，需后端在跑）
```

**静态前端 + 真实求解 API**：浏览器端是纯 HTML/CSS/JS（无构建步骤、无框架依赖），
后端是 `ufdemo/webapp.py` —— 只用 Python 标准库的 `http.server`，**不引入
FastAPI/Flask**。`POST /api/solve` 是唯一会调用求解器的端点。

```bash
PYTHONPATH=src python -m ufdemo.webapp --runs-dir runs/_web   # 隔离输出
```

六条界面规矩（与 Streamlit 侧同源，因为共用 `ui_service` 的纯逻辑层）：

1. **只有「提交计算」会求解**，且只有**真实调用成功**才递增「求解次数」
   —— 准入失败时求解器没跑，因此不计数；计数可被外部核对。
2. 「读取结果」只递增「读取次数」，读的是**盘上已有**结果，不重算。
3. 切图层 / 旋转视图 / 切截面 / 时间轴回放 / 查表 **都不改变任何计数**。
4. `threshold_only` 的深度是 `null`（不是数值 `0`），界面显示「不提供」。
5. 图层不可用时给出**原因**，且不返回全 0 假数组。
6. 越界查表唯一错误码 `TABLE_OUT_OF_RANGE`；允许越界时返回 `null`，不外推、不钳端点。

> **不要用 `file://` 直接打开 `webui/index.html`**：后端不可用时页面会明确报错并禁用
> 提交，不会给出「看起来能用」的假象。
> 详见 `webui/README.md` 与 `docs/decisions/ADR-0016-web-ui-replaces-streamlit.md`。

---

## 3i. 参考协议外置：三层参数归属（ADR-0021）

> 起因：评审指出「**光斑半径是设备参数，不是材料卡的参数**」。核查后确认这是对的。

**三层归属（不得串位）**：

| 层 | 内容 | 在哪 |
|---|---|---|
| 材料 | δ、F_th、相结构、证据状态 | `data/materials/*.json` |
| **源文献装置** | λ / τ / f / w0、有效 N、峰值能流 | `data/protocols/<protocol_id>.json` |
| **本机设备** | NA、M²、名义 w0 与 z_R、功率 | `data/config/shared_experiment_background.json` |

材料卡只保留**引用**：

```json
"reference_protocol": {
  "protocol_id": "ysz_crown_machining_effective_n3",
  "protocol_file": "data/protocols/ysz_crown_machining_effective_n3.json"
}
```

加载时由 `materials.resolve_reference_protocol()` 装配回 `MaterialSpec.reference_protocol`，
**键集是分离前的超集** ⇒ `config.py` / `references.py` / `webcontract.py` 等消费方零改动。

**为什么必须分开**：核函数 `a = δ·ln(F/F_th)` 是**局域能流**定律，与光斑无关 ——
`solver.py` 与 `response.py` 里**零** w0 引用，材料卡里的 w0 从不进入物理计算，
只作「条件门禁」。留在卡里会被误读成材料参数，而且同一台设备的条件要在多张卡里各抄一遍。

**一个必须知道的后果**：文献装置与本机设备的光斑可以差很多。氧化锆加工卡要求
w0 = 16 µm（JMPT 论文装置），本机名义光学是 0.874 µm（NA 0.45 / M² 1.2），
**相差 18 倍**，两者一起过 `validate_run` 会被 `CONDITION_MISMATCH` 如实拒绝。
所以「借用文献装置复现文献」与「用本机名义光学预测」是**两种不同的运行**，
不能混为一谈 —— 演示/汇报时要说清用的是哪一种。

**坏引用一律报错**（`CONFIG_INVALID` + 精确 `field_path`）：文件缺失、`protocol_id` 不一致、
只有 `protocol_id` 没有 `protocol_file` —— 绝不返回空协议、绝不降级执行。

**协议库与生成器必须同步**：协议由 `tools/migrate_materials.py` 产出，
手改 `data/protocols/` 而不改生成器会被 `tests/test_reference_protocol_files.py` 抓到。

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
* **不提供的图层如实报不可用**：`threshold_mask` 仅在**受限阈值协议开启**且该卡
  提供可用阈值时给出，否则显示原因、**不返回全 0 假数组**；协议**只对**本事件入射能流
  判超阈，不用「累计剂量 vs 单脉冲阈值」比较伪造标记；`threshold_only` 结果不提供深度时
  显示「不提供」而非数值 `0`。
* **阈值基准唯一**：`thresholds.py` 只注册 `per_event_incident` 一个基准；累计/平均
  能流基准在配置层被拒（`CONFIG_INVALID`）；多候选须显式 `candidate_index`；
  `multi_response` 口径不得当单脉冲阈值使用。
* **水印单一权威来源**：`materials.build_watermark(material, unit, run_mode)` 一处生成；
  solver 写入、`io` 落盘（`watermark.json` + `statistics.csv` 的 `watermark.*` 行）、
  界面与历史回放都读它——**导出与回放逐字段一致**。
* **七材料入口不冒充**：`opened` 绑真实能力、`blocked` 绑 enforcement + 期望错误码、
  `deferred` 是「规格要求开放、实现未支持」的**缺口**且附探针证明当前打不开；
  `verify_entry_enforcements` 实跑探针，拦截失效或缺口消失即**如实报失败**
  （`docs/reports/material_capability_table.csv`）。
* **分组不改变物理**：`Δh = Σ_j a(F_j)`，**禁止**先累加能流再取一次对数；
  逐脉冲 NumPy 路径始终是**参考实现**，分组必须与它逐位/浮点级一致
  （`tests/test_grouped_solver.py`）。
* **局部误差 ≠ 全局误差**：`B` 与两个 `B/2` 的试算只用于**控制步长**；
  最终验收以**完整逐脉冲对照**为准。界面与诊断文案必须写明这条边界。
* **批量不支持的组合在配置层拦截**：分组 × 分相、分组 × 历史耦合、
  分组 × 动态角度 → `CONFIG_INVALID`；不靠运行期静默降级。
* **Numba 是可选后端，默认关闭**：实测该逐元素对数核无收益；
  缺失时**回退 NumPy 并给出警告**（结果同式、逐位一致）。
* **不承诺加速倍数**：无收益即保留参考模式，如实记录实测值
  （`docs/reports/performance_baseline.md`）。
* **几何修正只做几何**：斜入射/动态角度按 ``F_s=μ·F_⊥``、首次交点可见性与
  ``Δh=-a_n/n_z`` 修正；未提供 ``A(θ)`` 依据时**不预测吸收差异**。
* **符号约定必须显式**：``μ=max(0,k·n)``（``k`` 指向光照侧；``k_z>0`` 强制），
  与任务书 ``max(0,-k·n)`` 等价但差一个整体符号（ADR-0015）。
* **支持范围是软件范围**：``n_z≥0.5``、入射角≤60°；超出**停止并报位置与原因**，
  不裁剪角度继续运行；背向不算越界。
* **查表不是任意外推**：默认分段线性；可选 PCHIP 显式 `extrapolate=False`；
  越界是显式状态（`TABLE_OUT_OF_RANGE`），**不返回 0、不外推、不钳端点**；
  重复 x 默认拒绝，需合并时须附规则并保留原始点（见
  `docs/decisions/ADR-0011-table-lookup.md`）。
* **曲线去向按语义硬分流**：只有 `event_depth_increment` 可进逐事件核
  （`assert_curve_can_enter_event_kernel`，另含固定条件比对）；体积/平均率/累计
  曲线只进评估器，**不得反推局部深度**（`assert_no_local_depth_from_volume`）。
* **相不是材料卡**：分相结构的相用**内联合成定义**（阈值 `F_ref`、去除尺度
  `L_ref`），**不得**引用别的材料卡做标定，也**不得**沿用父卡整体阈值来源
  （`assert_phase_threshold_not_split`）；整体阈值不得拆给分相。
* **δ 用 `L_ref` 尺度**：相去除尺度写作 `delta_over_L_ref`（δ/L_ref），与层厚、
  光斑、离焦同一尺度；旧字段 `delta_over_delta_ref` 一律拒绝（见
  `docs/decisions/ADR-0012-phase-structure-and-truncation.md`）。
* **跨相截断是有损近似且命名固定**：`applied = min(候选, next_different_interface)`；
  被截断量记为「未应用候选去除体积」（`unapplied_candidate_removal_volume_internal`），
  **不是剩余能量**，不回流、不重分配。
* **分相结构只在配置层放行**：`phase_at` / `next_different_interface` 是唯一接口；
  「分相截断 × 动态角度」在 `validate_run` 互斥；未启用分相时界面 `phase_id`
  图层如实报不可用。

---

## 5. 已知限制（本批）

1. **M0 只开放正入射**。斜入射 / 法向厚度转换 / 遮挡未实现，非正入射
   配置会被 `GEOMETRY_UNSUPPORTED` 拦截（不是靠按钮禁用）。
2. **几何**：正入射与**斜入射**（≤60°，含投影与首次交点可见性）均已支持；
   表面为**2.5D 高度场**，支持均质单相、**解析分相结构**（铺层条纹 / 随机颗粒，批次 G）
   与**解析斜平面**（批次 J），但**不支持任意三维几何**（悬垂/多值表面）。
   遮挡只做**首次交点**射线检查，不做多次反射/衍射。
   「分相截断 × 动态角度」与「斜入射 × 分相」在配置层互斥；
   「批量 × 斜入射」「批量 × 动态角度」回退或拦截（见 ADR-0014/0015）。
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
   历史结果通过运行目录读取，不建数据库。超阈值掩膜图层（`threshold_mask`）
   由批次 H 的**受限阈值协议**给出，但**仅在协议开启且该卡提供可用阈值时**可用，
   否则如实报不可用（协议只按本事件入射能流判超阈，不读作热学损伤标记）。
8. **无性能承诺**。批次 I 已交付 B01–B04 实测基准（`performance_baseline.md`）与
   冻结几何分组；但**定点/小段扫描有收益、大范围扫描收益有限**，
   **Numba 实测无收益**（故默认 NumPy），且**不承诺任何固定加速倍数**。
   数值只对报告记录的本机环境成立。
9. **F03 / F04 文件在当前工作区未找到**（`paper5.0.tex`、
   `ttm_carrier_drilling_q4_axisymmetric.py`）。已按细则记录为缺失，
   不阻塞人工解析主线；哈希核对结果见
   `docs/reports/input_hash_check.csv`。
10. **查表曲线只覆盖固定协议下的一维响应，且不接入逐事件主循环**。
    跨工况需另附条件匹配的曲线。批次 H 已交付受限阈值协议与七材料能力入口，
    但**查表曲线接入主循环仍不开放**（细则第 7 节：只有 `event_depth_increment`
    且协议适用时才可进，本批不启用该通道）。
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
│   ├── structure.py      # 分相结构：相/结构接口、铺层与颗粒、种子与体积分数（批次 G）
│   ├── thresholds.py     # 受限阈值协议：只按本事件入射能流判超阈（批次 H）
│   ├── geometry.py       # 法向/投影/首次交点可见性/法向厚度转换（批次 J）
│   ├── accelerators.py   # 冻结几何分组批量、局部误差估计与回退（批次 I）
│   └── __main__.py       # CLI
├── tools/
│   ├── migrate_materials.py   # F01/F02 → 执行卡 + 迁移/准入报告
│   ├── make_examples.py       # 生成 examples/*.json
│   ├── make_curves.py         # 生成 data/curves 示例曲线与无效夹具（批次 F）
│   ├── table_report.py        # 查表报告：错误 CSV + 原始点/插值图（批次 F）
│   ├── structure_report.py    # 分相结构报告：实例 CSV + G06 检查 CSV/报告（批次 G）
│   ├── material_report.py     # 受限阈值协议报告 + 七材料能力表（批次 H）
│   ├── ui_probe.py            # 界面操作检查（AppTest；批次 E，批次 F/G/H 增补检查）
│   ├── ui_demo_probe.py       # 端到端演示可用性检查（批次 G：前后端对接全链路）
│   └── run_acceptance.py      # 实际执行并把实测值写入验收报告
├── data/materials/       # 执行卡（真实材料 + _synthetic_demo_isotropic）
├── data/protocols/       # 参考协议库：源文献的实验装置条件（ADR-0021）
├── data/curves/          # 响应曲线卡（*.curve.json + *.points.csv，批次 F）
├── data/config/          # 共享实验背景：本机设备层（NA/M²/名义 w0 与 zR，ADR-0020 冻结）
├── data/references/      # 原始来源快照与输入指纹
├── examples/             # 可运行配置（含合成结构实例、查表算例与斜入射/斜平面示例）
├── tests/                # pytest（含 fixtures 人工解析卡、界面逻辑与冒烟测试、无效曲线夹具）
├── docs/decisions/       # 设计决定记录（ADR-0001 … ADR-0021）
├── docs/reports/         # 迁移、准入、验收、界面检查、进度报告
└── runs/                 # 每次运行的独立目录（默认不删不覆盖）
```

原始输入 `seven_materials_parameter_register.xlsx` 与
`material_card_templates.json` **未被修改**，哈希与任务书第 16.3 节指纹一致。

---

## 7. 测试与报告

```bash
python -m pytest -q                  # 737 passed, 1 xfailed（截至 ADR-0021，2026-09-17；实测数见 docs/reports/pytest_output.txt，本行不再逐批维护）
python -m pytest -q -m g05           # 只跑文献语义回归
python -m pytest -q -m g06           # 只跑分相结构检查（批次 G）
python -m pytest -q -m g07           # 斜入射与表面几何（批次 J）
python -m pytest -q -m g08           # 分组模式与逐脉冲对照（批次 I）
python -m pytest -q -m g09           # 界面、查表、受限阈值协议的 G09 相关测试（含批次 H）
python tools/migrate_materials.py    # 迁移 + 7 项准入探针
python tools/make_curves.py          # 生成示例曲线与无效夹具（批次 F）
python tools/table_report.py         # 查表报告：错误 CSV + 原始点/插值图
python tools/structure_report.py     # 分相结构报告：实例 CSV + G06 检查 CSV/报告（批次 G）
python tools/material_report.py      # 受限阈值协议报告 + 七材料能力表（批次 H）
python tools/perf_report.py           # B01–B04 性能基准（批次 I）
python tools/ui_probe.py             # 界面操作检查（AppTest 驱动 app.py）
python tools/ui_demo_probe.py        # 端到端演示可用性（前后端对接全链路）
python tools/run_acceptance.py       # 实际执行并生成验收报告（A–J）
```

产物：

* `docs/reports/acceptance_report.md` —— G01–G09 实测值与容差对照；
* `docs/reports/acceptance_g01_g05.csv` —— 逐项机器可读结果；
* `docs/reports/g05_reference_semantics.md` / `.csv` —— G05 专项报告；
* `docs/reports/reference_equations.md` —— **源公式 ↔ 实现 ↔ 验收 对应表**；
* `docs/reports/ui_operation_check.md` / `.csv` —— **界面操作检查（G09-UI）**；
* `docs/reports/ui_demo_probe.md` / `.csv` —— **端到端演示可用性（前后端对接全链路）**；
* `docs/reports/table_lookup.md` —— **查表汇总报告（G09-table）**；
* `docs/reports/table_errors.csv` —— **错误 CSV**：每条违规输入与错误码；
* `docs/reports/curve_interpolation.html` / `.csv` —— **原始点与插值图**；
* `docs/reports/structure_instances.csv` —— **分相结构实例统计（批次 G）**；
* `docs/reports/g06_phase_interfaces.md` / `.csv` —— **G06 分相报告与检查明细（批次 G）**；
* `docs/reports/material_capability_table.csv` —— **七材料能力入口表（批次 H）**：
  开放 / 红线 / 缺口逐条探针结论；
* `docs/reports/g09_threshold_protocol.md` / `.csv` —— **受限阈值协议报告与检查明细（批次 H）**；
* `docs/reports/performance_baseline.md` / `.csv` —— **B01–B04 性能基准与环境/内存口径（批次 I）**；
* `docs/reports/material_migration.csv` —— 字段级迁移记录；
* `docs/reports/material_admission.csv` —— 四种必查拒绝情况；
* `docs/reports/input_hash_check.csv` —— 输入文件哈希核对；
* `docs/reports/progress.md` —— 批次状态与下一步依赖。

报告把**公式核查**、**数值实现验证**、**实验复现**分栏记录。A–K 只做到前两项；
“软件跑通”不等于“材料物理验证”。
