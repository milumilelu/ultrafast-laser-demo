# ADR-0018：焦平面恒为**加工前的原始上表面**（把口头约定做成代码约束）

* 状态：已采纳｜日期：2026-09-15｜批次：L

## 背景

`data/config/shared_experiment_background.json` 一直声明：

```json
"focus": {
  "focus_strategy": "fixed_original_surface",
  "geometry_feedback": "axial_defocus",
  "layer_refocus": false,
  "statement": "焦平面恒为加工前原始表面 z0；不随槽底下降调整 Z。
                但表面下降会产生**被动离焦**（w(d)=w0·sqrt(1+(d/zR)²)），
                这是几何反馈，不是主动调焦。两者是**不同开关**。"
}
```

但它在代码里**只是文档**：

* `focus_strategy` 全仓库只被"回显"两处（`config.py` 的 property、`webcontract` 的一个字段），
  **没有任何校验或强制**；
* `SolverConfig.geometry_feedback` 与 `BeamOptions.geometry_feedback` 的**默认值都是
  `fixed_geometry`** —— 该模式的语义是「离焦恒为 0、光斑跟着表面走」，
  **等价于逐层重新对焦**，与上面这条约定正好相反；
* `calibration.build_row_config` 调 `serpentine_plan(...)` 时**没有传 `focus_z_m`**，
  于是焦平面被**硬编码为 0.0**，而不是"原始表面高度"。

用户 2026-09-15 明确：**"焦点就固定在上表面"**。

## 决定：三重约束，fail closed

### 1. 声明层：只接受 `fixed_original_surface`

新增 `SolverConfig.focus_strategy`（默认 `"fixed_original_surface"`，进 `to_dict`）。
`from_dict` 遇到任何其它值（`layer_refocus` / `follow_surface` / `dynamic_compensation` …）
**直接 `CONFIG_INVALID`**，并在建议里说明本项目不提供逐层 Z 调整（任务书 §6）。
理由：**未实现的能力必须在配置层拦**，不能靠"没人会设"。

### 2. 构造层：焦平面 = 原始表面，**按构造成立**

* `PredictionSpec.initial_height_m`（新，默认 0）**同时**写进 `grid.initial_height_m`
  与路径每一段的 z（`serpentine_plan(focus_z_m=...)`）。
* 于是"焦平面 = 原始表面"不再依赖"它恰好是 0"；工件表面高于/低于基准面时也成立。
* 多层（N≥1）时焦平面**不随层数变化** —— 没有逐层 Z 调整。

### 3. 校验层：`validate_run` 复核，不等即失败关闭

当 `focus_strategy == "fixed_original_surface"` 时，
**每一段端点的 z 必须等于 `grid.initial_height_m`**；否则 `CONFIG_INVALID`，
报错里给出**是哪一段、实际值、要求值**。通过时在 `notes` 里写一条
「焦平面：固定于加工前原始上表面 z0 = …」。

这挡住的是"声明对、路径却把焦点放别处"（例如手写配置把焦点放到槽底）——
那种情形原先会**静默按错的焦平面算**。

### 4. 可审计：每次运行都记录

`result.metadata["focus"] = {strategy, z_m, layer_refocus: false, geometry_feedback,
passive_defocus, note}`。判据不再只存在于背景文件里。

## 与 `geometry_feedback` 的分工（不得混淆）

| 开关 | 决定什么 |
|---|---|
| `focus_strategy` | 焦平面的**位置**（恒为原始上表面 z0） |
| `geometry_feedback` | 有没有**被动离焦**：`axial_defocus` ⇒ `w(d)=w0·sqrt(1+(d/zR)²)`；`fixed_geometry` ⇒ 离焦恒 0 |

注意 `fixed_geometry` 在本仓库的用途是**冻结几何的解析基准**（批次 A–I 的逐位对照），
它**不是**深孔加工的物理模型。所以**默认值保持 `fixed_geometry` 不变**
（避免改动既有算例），而由**装配路径**（共用背景 patch）给出 `axial_defocus`，
并在元数据里如实记录 —— 这样"用了哪个模式"永远可查。

## 验证

* pytest **634 passed + 1 xfailed**（新增 `tests/test_focus_convention.py` **12 条**）：
  * 默认策略与 `to_dict`；5 种非法策略拒绝；
  * 路径 z = 原始表面 ⇒ 通过并留 note；z ≠ 原始表面（焦点下移 20 µm）⇒ **失败关闭**，
    报错含字段路径与实际值；
  * 非零 `initial_height_m`（−3.5 µm）⇒ `grid.initial_height_m` 与**所有**段端点 z 一致；
  * **行为断言**：初始平表面上 `axial_distance ≈ 0`（焦点**就落在**原始表面），
    打深后窗口内最大光斑 `> 1.5·w0`（**存在被动离焦**）——两条合起来就是
    「焦点固定 + 有被动离焦」，正是用户那句话的可执行版本；
  * `res.metadata["focus"]` 如实记录。
* 既有 11 个示例的全部路径段端点 z 本来就是 0、`initial_height_m` 也是 0，
  所以新校验**不改变任何既有算例**。

## 不做

* **不动光斑宽度**：`a00b8d0` 把"实测单线宽度 5 µm"反推成**等效光斑**这件事，
  用户明确本轮先不动。注：5 µm 是**烧蚀宽度**，不是光斑直径；
  用 `r_abl = w·sqrt(ln(F0/Fth)/2)`（≈1.9–2.5·w）换算才不会重复计入这个倍数 —— 待单独决策。
* **不加深度衰减/等离子体屏蔽**：属物理建模，需独立证据链。
