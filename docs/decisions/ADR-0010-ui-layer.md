# ADR-0010：批次 E 界面层的分层、状态与措辞决定

* 状态：已采纳｜日期：2026-09-10｜批次：E（T09）

## 背景

执行细则第 3 节要求「科学核心不得导入 Streamlit 或 Plotly，不得通过全局 UI 状态
取得输入」；第 11.3 节要求「参数编辑状态与已完成结果分开保存」「提交时冻结配置
快照并计算输入哈希」「只旋转视图、切换图层/截面/快照时读取已有数据」「阈值图不写
『热影响区』；照射剂量不写『温度』」；任务书第 13 节要求界面显示材料身份、模式、
单位与证据状态，并在缺参数能力时显示不可用原因、可显式选择合成示例。

## 决定一：逻辑层与渲染层硬分离（`ui_service.py` / `app.py`）

* `src/ufdemo/ui_service.py` **不导入 Streamlit/Plotly**，只依赖 `config` /
  `materials` / `io` / `solver`。所有状态机、计数、校验、视图变换都在这里。
* `app.py` 是唯一导入 Streamlit 的文件，只做渲染与事件转发。

理由：G09 的「回放不增加求解次数」必须能在**没有浏览器**时被断言。分离后
`tests/test_ui_service.py` 可在纯 Python 下跑 55 项断言，`tests/test_app_smoke.py`
再用 `AppTest` 做端到端复核。若把逻辑写进 `app.py`，测试只能依赖 UI 运行时。

## 决定二：`submit()` 是唯一求解入口，`solve_count` 只在此处递增

`ui_service.submit()` 是**唯一**调用 `solver.solve()` 的函数。因此：

* 准入校验失败（含配置构造失败）→ 记 `last_error`、**不**递增 `solve_count`、
  保留已有冻结结果；
* 参数写回编辑态用 `set_pending_params()`，它不冻结、不求解；
* 读取历史用 `read_existing_run()`，只递增 `read_count`。

计数对用户可见（侧边栏显示「提交求解次数 / 读取历史次数」），使「界面没有偷偷
重算」成为可核对的**外部可观测量**，而不是口头承诺。

## 决定三：`is_stale()` 用参数哈希比较，**不使用 `st.form`**

`SessionState` 同时持有 `params`（可编辑）与 `submitted_params_hash`（冻结时算
的输入哈希）。`is_stale()` = `hash(params) != submitted_params_hash`。

**踩过的坑**：最初用 `st.form` 包住参数区，这样「改参数但未提交」的 widget 值
被 Streamlit 缓冲，`is_stale()` 永远为假，直接违反 G09。因此参数面板**不用
`st.form`**：每次脚本重跑都把表单当前值写进 `state.params`（`set_pending_params`），
再与已提交哈希比较。

结果数组一律从 `FrozenRun` 读取，当前表单值不参与任何渲染——即使参数已经改了，
图表仍是旧结果，并明确标注「上一次运行」。

## 决定四：视图/回放是纯函数，并带硬断言

`layer_view()` / `snapshot_arrays()` 内部断言 `solve_count` 不变
（`assert state.solve_count == before`）。`orient_view()` / `cross_section()` 是
纯数组变换，不接触任何会话状态。这样「旋转视图 / 切图层 / 切截面 / 拖时间轴
不重算」不仅被测试覆盖，运行时一旦被破坏会立刻抛错，而不是静默多算一次。

## 决定五：历史运行按需读盘，不再要求内存里有 `RunResult`

历史目录只有 NPZ，没有内存对象。`FrozenRun` 因此增加：

* `disk_surface`（从 `final_surface.npz` 读到的数组）；
* `snapshot_dir` + `snapshot_files`（按需读 `snapshots/snap_*.npz`）。

`arrays_from()` / `depth_of()` / `layer()` 先看内存 `result`，再看 `disk_surface`，
最后按需读快照文件；`depth` 允许由 `h0 - h` 现场得到。这样历史运行与本次运行
在渲染路径上完全一致，且**不引入任何求解**。

## 决定六：措辞守卫采用**严格子串**口径，标签只描述「是什么量」

`FORBIDDEN_TERMS = (热影响区, HAZ, 温度场, 温度分布, 热输入, 温度)`，
`assert_safe_wording()` 命中即拒绝，**否定式（“不是热影响区”）同样拒绝**。

理由：执行细则 11.3 是字面要求；一旦允许否定式，最朴素的子串复查就失效，
安全网也就不存在了。因此约定 `LAYER_LABELS` **只描述该图实际是什么量**：

| 图层 | 标签 |
|---|---|
| `height` | 当前表面高度 |
| `depth` | 去除深度 |
| `cumulative_fluence` | 累计入射剂量（能量沉积观测量） |
| `illumination_count` | 有效照射计数 |
| `threshold_mask` | 超阈值/改性标记（受限阈值协议量） |
| `warning_mask` | 域外/截断等警告标记 |

澄清信息（例如「不得读作热学损伤标记」）放在 `UNAVAILABLE_LAYERS` 的原因文本里，
该文本同样避开被禁词，因此整条界面文案可被同一守卫复查。

## 决定七：`threshold_mask` 如实报不可用，不用无关量凑数

本批次未记录逐事件超阈值掩膜，因此该图层列入 `UNAVAILABLE_LAYERS` 并给出原因，
`available_layers()` 返回「不可用」，界面显示原因而非空白或零值。
**禁止**用「累计入射剂量 vs 单脉冲阈值」比较来伪造该标记：两者虽同量纲，
物理含义不同（累计入射剂量 ≠ 单脉冲峰值能流）。

同理，`threshold_only` 结果不提供深度时，界面显示「不提供」而不是数值 `0`，
避免把「没有这个量」误读成「去除量为零」。

## 决定八：`UFDEMO_RUNS_DIR` 环境变量覆写运行根目录

`default_runs_dir()` 支持 `UFDEMO_RUNS_DIR` 覆写。界面探针/验收据此把「提交计算」
产生的运行写到隔离目录，不污染工作区 `runs/`。这是**测试基础设施**，
不改变默认行为（未设该变量时仍为 `project_root()/runs`）。

## 与既有决定的衔接

* 决定五、七 与 **ADR-0009 决定五**（参考评估器不写形貌表面）一致：
  二者都拒绝让「参考量」冒充「形貌结果」。
* 决定三 与 **ADR-0007**（运行目录不覆盖）配合：每次提交都新开目录，
  因此「上一次运行」始终可被 `read_existing_run` 找回。
* 决定六 与 **ADR-0005**（合成模式边界）配合：合成结果的标签同样经过措辞守卫。
