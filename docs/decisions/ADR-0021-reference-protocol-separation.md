# ADR-0021：参考协议外置 —— 材料属性 / 源文献装置条件 / 本机设备三层分离

* 状态：已采纳｜日期：2026-09-17｜批次：L（结构性调整，非任务书条目；来自用户评审）

## 背景

用户评审时指出：**「材料卡的参数是不是有点问题，光斑半径应该不是材料卡的参数，这是设备参数吧」**。

核查后确认**用户是对的**，而且这个工程本来就把它当设备参数 —— 设备层是独立存在的：

```json
// data/config/shared_experiment_background.json
"optics": {"wavelength_nm": 1030, "numerical_aperture": 0.45, "m2": 1.2,
           "nominal_waist_radius_um": 0.874, "nominal_rayleigh_length_um": 1.943}
```

`shared_background_patch(bg, repetition_rate_Hz=…)` 把它装配进 `laser.*`，
`basis.source = "frozen_nominal_optics"`；ADR-0020 已把光学**冻结**。

问题出在另一处：材料卡里也有一份 `reference_protocol.required_laser`，含
**λ / τ / f / w0** —— 全是光束量。

### 关键证据：卡里的 w0 从不进入物理计算

```
grep spot_radius  src/ufdemo/solver.py     → 0 命中（求解器只用 run config 的 laser.spot_radius_m）
grep w0|waist|spot_radius  src/ufdemo/response.py → 0 命中
```

核函数 `a = δ·ln(F/F_th)` 是**局域能流**定律，与光斑无关 ⇒ δ、F_th 本来就是 w0 无关的材料量。
`reference_protocol` 只被这些地方读：`config.py`（`validate_run` 门禁）、`references.py`（比对/报告）、
`materials.py`（解析）、`webcontract.py`（自动配置）。

**⟹ 卡里那份 w0 只作「条件门禁」，是源文献的装置条件，不是材料属性。**

### 后果一：两套 w0 相差 18 倍

设备层冻结光学 + 氧化锆卡一起过 `validate_run`：

```
[CONDITION_MISMATCH] 激光1/e² 光斑半径与材料卡协议不匹配
    actual      = 8.742911540514785e-07 m    ← 本机名义 0.874 µm
    requirement = 1.6e-05 m                  ← 卡要求 16 µm（rel_tol 0.05）
```

**这不是缺陷，是设计后果**：氧化锆这张卡描述的是 **JMPT 论文那台机器**
（1030 nm / 208 fs / 33.3 kHz / w0 = 16 µm），不是本机。

### 后果二：三个概念被塞进两个文件

| 概念 | 应属 | 分离前 |
|---|---|---|
| 材料属性（δ、F_th、相结构） | 材料卡 | ✅ 材料卡 |
| **源文献装置条件**（λ/τ/f/w0） | 协议库 | ❌ 内联在每张卡里 |
| **本机设备**（NA/M²/名义 w0·z_R/功率） | 设备层 | ✅ 独立文件 |

由此产生：同一份光束条件在各卡重复登记（λ/τ/f 抄 7 遍、w0 抄 2 遍）；
字段名 `required_laser` 读起来像「材料要求什么光」，实际是「文献用了什么光」，容易被误读。

## 决定一：三层归属，协议独立成文件

```
材料属性（δ、F_th、相结构）        → data/materials/*.json
源文献装置条件（协议）              → data/protocols/<protocol_id>.json   ← 新增
本机设备（NA/M²/名义 w0 与 zR）     → data/config/shared_experiment_background.json
```

卡片只保留引用：

```json
"reference_protocol": {
  "protocol_id": "ysz_crown_machining_effective_n3",
  "protocol_file": "data/protocols/ysz_crown_machining_effective_n3.json"
}
```

协议文件显式标明 `kind: "source_experiment_beam"` 并写明「这不是本机设备参数」。
`validity_domain.peak_fluence_J_m2` 同为光束量，一并迁入协议
（改名 `reference_peak_fluence_J_m2`）；`validity_domain` 保留 `scope` / `protocol_id` / `note`
—— 那些描述的是**卡片的适用域**，不是光束。

## 决定二：装配在加载器，运行期形状**不变**

`materials.resolve_reference_protocol()` 在解析卡片时把协议读回来拼成
`MaterialSpec.reference_protocol`，**键集是分离前的超集**：

```
protocol_id, required_laser, required_history, protocol_note   ← 分离前就有
+ protocol_file, reference_peak_fluence_J_m2, source_ids        ← 新增可追溯信息
```

⟹ `config.py` / `references.py` / `webcontract.py` **零改动**（仅 `webcontract` 读峰值能流的
两处改为从协议读，输出字段名不变）。

解析基址优先取**卡片所在树的根**（`<root>/data/materials/x.json` → `parents[2]`），
再回退 `resource_root()`。这样生成器把产物写到临时目录、或 wheel 装到别的 prefix 都能自洽。

## 决定三：坏引用**如实报错**，不静默

| 情形 | 行为 |
|---|---|
| 无 `reference_protocol` | 返回 `{}`（不是所有卡都有协议） |
| 内联完整协议（人工 fixture） | 原样返回（向后兼容） |
| 只有 `protocol_id`、没有 `protocol_file` | `CONFIG_INVALID`，`field_path=reference_protocol.protocol_file` |
| 协议文件缺失 | `CONFIG_INVALID`，`actual` 列出**所有**试过的路径 |
| 文件内 `protocol_id` 与卡内不一致 | `CONFIG_INVALID`，`actual` 给出两边的值 |

绝不返回空协议、绝不降级执行 —— 这与「宁可拒绝，不可静默降级」一致。

## 决定四：生成器必须显式写 LF（可复现性）

`tools/migrate_materials.py` 原先用 `Path.write_text(...)`（文本模式）写卡；
Windows 上会把 LF 翻成 CRLF，而 `.gitattributes` 是 `* text=auto eol=lf`。
后果是**git 归一化后看不到任何 diff，但文件 sha256 全变** ——
`test_material_cards_unchanged` 报「材料卡被改写」，而 `git diff` 空空如也。

新增 `_write_text_lf()` 显式写 LF。修正后重跑生成器：
**4 张卡字节未变，仅 7 张（有协议者）变化** —— 改动面与设计意图吻合。

## 不做什么

* **不动曲线卡的 `required_history`**。`data/curves/*.curve.json` 里也有同名字段，但那是
  **曲线自己的**「适用历史/有效 N」声明（供 `tables.py` 语义路由），与材料协议无关。
  名字相同是历史巧合，本轮不统一，以免把曲线 schema 一起牵动。
* **不改本机设备层**。`shared_experiment_background.json` 的 NA/M²/冻结光学保持原样。
* **不为让演示多几项而放宽门禁**。演示端点若借用论文装置，必须在文案上讲清是
  「复现文献条件」而非「本机预测」（本机名义 w0 = 0.874 µm，与 16 µm 差 18 倍，
  结果不会同量级）。
* **不擅自改验收断言**。

## 验收

| 项 | 结果 |
|---|---|
| 生成器 | 7 协议 + 11 卡写出；7 项准入探针 **全 PASS**（含 `unknown_protocol` 必须拒） |
| F01/F02 原始输入哈希 | **不变** ✅ |
| 卡片取值等价性 | 忽略协议键后 **11 张逐值一致** |
| 端到端复算（ZrO₂ demo） | 44.0616 / 63.3547 / 62.6316 µm、621 事件 —— 与迁出前一致 |
| 材料卡字节变化 | **4 张未变，仅 7 张变化**（有意） |
| 新增测试 | `tests/test_reference_protocol_files.py` **13 项**（结构 / 解析 / 如实报错） |
| 断言非恒真 | 三处结构断言对**改动前**的卡均判违规 ✅ |
| 全量 pytest | **737 passed, 1 xfailed**（724 + 13） |

## 代价与后续

* 多一层文件：改协议条件要动 `data/protocols/`，且**必须同步生成器**
  （由 `test_protocols_match_generator_literals` 守）。
* 哈希护栏（`C:/tmp/materials_sha_baseline.json`）**有意刷新过一次**，
  逐卡前后哈希与理由见 `docs/reports/material_card_schema_change_adr0021.md`。
* 仍未解决：**本机设备条件与文献条件如何并存**（演示要么借用文献装置、要么用本机名义光学）。
  这是一个独立的、面向用户的**模式选择**问题，另行决策。
