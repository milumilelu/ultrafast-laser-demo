# AlSiC 数据缺口审计（U10）

## 结论

- 数据包内 AlSiC 记录：**0 条**
- manifest A02 状态：`gap_no_retrievable_numeric_values`（`is_gap = True`）

> **本轮未取得 AlSiC 的可用飞秒数值，U10 记为「未完成」。**
> 这是**如实报缺**，而不是「因为牌号不匹配而拒绝数据」——
> 缺的是**数值本身**（全文与图表在订阅墙后），不是牌号一致性。

## 缺口原因

**不是**因牌号不匹配而拒绝数据，而是**目前没有取得可用的飞秒数值**：该文（J. Manuf. Process. 49 (2020) 227-233，doi 10.1016/j.jmapro.2019.08.021）的可访问部分只给出摘要，未公开 ablation depth / surface roughness 的逐点数值表；ScienceDirect 正文与图表需订阅，本轮两次检索（中/英文）均未获得可转录的实验数值。

## 目标条件与排除项

- 目标论文：`10.1016/j.jmapro.2019.08.021`（femtosecond (as reported: 1030 nm, 800 fs)）

- **nanosecond_alsic**：检索中出现的 39 μm / 99 μm 等结果来自**纳秒**论文 —— **不得**混入飞秒数据包（细则明列的排除项）。
- **near_material_substitution**：**不得**用纯 AA2024 或相近材料的数据冒充 SiCp/AA2024 的飞秒响应。
- **grade_relaxation**：牌号**允许**不同（细则说「牌号不成为阻塞」）—— 缺的**不是**牌号一致性，而是**数值本身**。

## 本轮检索证据

- 2026-09-13 中文检索：'SiCp/AA2024 铝基碳化硅 飞秒激光 烧蚀 深度 实验 1030 nm 800 fs 数据' → 5 条结果均为摘要/机构库条目，无逐点数值。
- 2026-09-13 英文检索：'SiCp/AA2024 femtosecond laser ablation depth roughness fluence values table' → 同样只有摘要与书目信息。

## 数据包现状（自动扫描）

| 文件 | 行数 | 族（前几个） |
|---|---:|---|
| `ceramics_dot_line_measured.csv` | 4 | 微晶玻璃 ips e.max cad ht lithium-disilicate glass-ceramic ；氧化锆 katana utml 6y-psz  |
| `ceramics_laser_roughness_measured.csv` | 10 | 微晶玻璃 ips e.max cad ht lithium-disilicate glass-ceramic ；氧化锆 katana utml 6y-psz  |
| `ceramics_nonlaser_controls.csv` | 4 | 微晶玻璃 ips e.max cad ht lithium-disilicate glass-ceramic ；氧化锆 katana utml 6y-psz  |
| `cfrp_efficiency_curves.csv` | 230 |   cfrp + 1 wt% cuhap；  cfrp + 5 wt% (cuhap:cahap 50:50)；  cfrp + 5 wt% (cuhap:cahap 75:25)；  cfrp + 5 wt% cuhap |
| `cfrp_efficiency_reported_maxima.csv` | 4 | cfrp cfrp + 1 wt% cuhap ；cfrp cfrp + 5 wt% cuhap ；cfrp cfrp + 5 wt% total cuhap:cahap 50:50 ；cfrp cfrp + 5 wt% total cuhap:cahap 75:25  |
| `dd6_drilling_measured.csv` | 25 | 高温合金 dd6 single-crystal nickel-based superalloy  |
| `diamond_rsm_measured.csv` | 18 | 金刚石 hthp single-crystal diamond  |
| `sic_single_pulse_measured.csv` | 4 | sic monocrystalline 4h-sic  |

### 核对结果（三条「没有被偷偷填上」的断言）

- **纳秒体制混入飞秒包的记录数：0**（必须为 0）
- **以相近材料冒充复合材料的记录数：0**（必须为 0）
- **AlSiC 记录数：0**（当前为 0，故本项未完成）

## 下一步

定向获取全文（机构订阅 / 作者索取 / 换其他 AlSiC 牌号的飞秒或明确皮秒实验），取得后**分别标记脉冲体制**并逐条登记单位与观测类型。

其他六类数据不因本缺口等待（细则原话：AlSiC 保持明确缺口，不作为阻塞项）。

## 复现

```bash
python tools/alsic_gap_report.py
```

该脚本**只审计、不造数据**；有失败项（如发现纳秒混入）时退出码非 0。
