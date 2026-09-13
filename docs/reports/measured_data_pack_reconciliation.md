# 数据包版本核对表（U04 第一交付）

日期：2026-09-13｜用途：确定 `data/measured/` 的**唯一口径**，避免报告与代码用两套来源编号

---

## 1. 结论（一句话）

**采用手上的 9-11 版数据包（65 加工 + 4 对照），并按此口径统一全项目引用。**
9-13 复审描述的是**另一个版本**（77 条），其点名的多个文件在交付包中**并不存在**（见 §3）。
两者不可混用；本表之后，报告与代码一律以本文 §2 的编号为准。

---

## 2. 采用版本：9-11 包（`ultrafast_audit_realdata_20260911.zip`，46 364 B）

**sha256 与行数已逐一核对，7 个 CSV 全部一致**（核对脚本：`tools/measured_data_report.py`，21 项全通过）。

### 2.1 来源编号（**本工程唯一口径**）

| 编号 | 文献 | DOI | 材料 |
|---|---|---|---|
| **D01** | Optimization of the Femtosecond Laser Machining Process for Single Crystal Diamond | `10.3390/machines12090614` | 金刚石（HTHP 单晶） |
| **D02** | Preliminary Study on the Optimization of Femtosecond Laser Treatment… | `10.3390/ma15103614` | 微晶玻璃 + 氧化锆 |
| **D03** | Experimental and Simulation Research on Femtosecond Laser Induced… | `10.3390/mi15050573` | 单晶 4H-SiC |
| **D04** | Ablation of CFRP Modified with Copper and Calcium Hydroxyapatites | `10.3390/polym18111284` | CFRP |
| **D05** | Optimization of Femtosecond Laser Drilling Process for DD6 Single Crystal Alloy | `10.3390/met13020333` | DD6 高温合金 |

> ⚠️ **与复审的编号不同**：复审用 D04=SiC、D05=CFRP、D06=AlSiC，
> 与本包的 D03=SiC、D04=CFRP、D05=DD6 **整体位移一位**。
> 引用时必须标明用的是哪一套，否则会把 SiC 的数据说成 CFRP 的。

### 2.2 逐文件清单

| 文件 | 行数 | 来源 | 观测语义 | 建议接入方式 |
|---|---:|---|---|---|
| `diamond_rsm_measured.csv` | 18 | D01 | `groove_geometry_and_roughness_endpoint` | 多变量工况响应（17 拟合 + 1 留出） |
| `dd6_drilling_measured.csv` | 25 | D05 | `through_hole_geometry_endpoint` | 独立钻孔工况评估；**不得**用于验证浅表 2.5D |
| `sic_single_pulse_measured.csv` | 4 | D03 | `single_pulse_crater_endpoint` | 单坑候选标定 |
| `ceramics_dot_line_measured.csv` | 4 | D02 | `point_and_line_geometry_endpoint` | 真实案例回放 |
| `ceramics_laser_roughness_measured.csv` | 10 | D02 | `surface_roughness_endpoint` | 粗糙度工况展示（**不是**去除深度） |
| `cfrp_efficiency_reported_maxima.csv` | 4 | D04 | `volume_per_energy` | 效率评估 |
| `ceramics_nonlaser_controls.csv` | 4 | D02 | `surface_roughness_endpoint` | **对照，不计入激光观测数** |

**合计：65 条激光观测 + 4 条非激光对照。**

### 2.3 计数口径（必须在所有报告里沿用）

- 65 是**已发表条件/结果记录数**，**不是** 65 个独立数据集、**不是** 65 张材料卡；
- `manifest.json` 与 `all_measured_cases.jsonl` 是**同一数据的两种表示**，**不得重复计数**；
- 4 条非激光对照**不计入** 65；
- 包内**不含**作者仪器原始文件（`raw_author_files_included: false`），
  **不含**模型/公式生成行（`measurements_generated_by_model: false`）。

---

## 3. 与 9-13 复审描述的差异（逐项）

复审点名的下列内容**在交付包中不存在**，已核对 ZIP 全部 20 个条目：

| 复审点名 | 交付包实际 |
|---|---|
| `curves/sic4h_2024_measured_terminal_depth.curve.json` | **无 `curves/` 目录，无任何 `.curve.json`** |
| `curves/cfrp_2025_measured_10pass_groove.curve.json` | 同上 |
| `tools/check_data.py` / `quickstart.py` / `fit_sic_candidate.py` | **无 `tools/`**；仅有 `scripts/{build_data_pack,fit_diamond_process_demo,reproduce_isolated_findings}.py` |
| `data/observation_index.csv` / `source_manifest.json` / `quality_notes.csv` | 无；实际为 `data/sources.json` + `manifest.json` + `data/all_measured_cases.jsonl` |
| `candidate_fits/`、`reports/`、`data_validation.json` | 无 |

**计数与内容差异**：

| | 交付包（9-11） | 复审描述（9-13） |
|---|---|---|
| 记录数 | **65 + 4** | 73 + 4 = **77** |
| CFRP | `cfrp_efficiency_reported_maxima.csv`，**4 行效率峰值** | `cfrp_groove_2025_digitized.csv`，**12 点图 5 数字化沟槽** |
| 陶瓷粗糙度 | 10 行 | 14 行 |
| 来源编号 | **D01–D05** | D01–D06（整体位移） |

**ZIP 自带证据**（`data/measured/manifest.json`）：

```json
{ "schema": "ufdemo.experimental_pack/1", "prepared_on": "2026-09-11",
  "main_record_count": 65, "nonlaser_control_count": 4,
  "material_families": ["CFRP","SiC","微晶玻璃","氧化锆","金刚石","高温合金"],
  "missing_material_family": ["铝基碳化硅"],
  "raw_author_files_included": false, "measurements_generated_by_model": false }
```

---

## 4. 处置与影响

| 项 | 处置 |
|---|---|
| 数据包版本 | **采用 9-11 版**（65+4）。若后续补发 9-13 版（含 `curves/` 与 `tools/`），**另开一次核对**，不覆盖本次导入 |
| 来源编号 | 全项目统一用 **D01–D05**（见 §2.1），并在引用处注明与复审编号的位移关系 |
| CFRP | 按手上版本的**4 条效率峰值**推进；复审所述「12 点数字化沟槽」**待补发**，不以现有数据冒充 |
| 陶瓷粗糙度 | 10 行（非 14）；复审核对的 5+5+2=12≠14 亦说明其自身计数需再核 |
| SiC 曲线卡 | 交付包未提供 → 由本仓库按自身 schema 自行生成（归入 U07） |
| 铝基 SiC | **保持明确缺口**（`missing_material_family`），不得用同名代理或纳秒数据冒充 |

---

## 5. 复核方式

```bash
# sha256 / 行数 / 计数口径 / 单位自洽 / DD6-14 异常保留 / 语义红线，共 21 项
python tools/measured_data_report.py     # → docs/reports/measured_data_qa.{csv,md}
```

**该脚本已做负向测试**：人为把 DD6 第 14 行的复算锥度改成与原文一致（模拟「静默修正」）后，
脚本报 **2 项失败**并退出码 1；恢复后回到 21/0、退出码 0 —— 即这些检查**确实会失败**，
不是恒真的空断言。
