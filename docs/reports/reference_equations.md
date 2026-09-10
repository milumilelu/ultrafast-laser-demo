# 源公式 ↔ 实现 ↔ 验收 对应表（批次 D / T07）

本表是批次 D 的必交产物（执行细则第 6 节）。每一行给出：文献原文公式、本项目
实现位置、输入量定义、语义、对应验收断言。**公式核查 ≠ 实验复现**：本表只证明
实现与文献公式一致，不声明已复现论文图/表。

## 1. YSZ —— 面积等效有效脉冲数与加工核

来源 **[R1]** Bogatyrev et al., *Journal of Materials Processing Technology* 335
(2025) 118668，式(9) 与第 5.2 节加工分支。

| 项 | 文献 | 实现 | 备注 |
|---|---|---|---|
| 有效脉冲数 | `N_eff = (π/4)·(2·w0·f)/v` | `references.ysz_effective_count()` | 面积等效；**含 π/4** |
| 速度反解 | `v = (π/4)·(2·w0·f)/N_eff` | `references.ysz_speed_for_effective_count()` | G05 主断言 |
| 错误换算（对照） | `v = 2·w0·f/N` | `references.naive_speed_for_effective_count()` | **不得用于输出**；G05 断言其与上式不同 |
| 加工核 | `a = δ·[ln(F/F_th,eff)]_+` | `response.FixedThresholdLogLaw` | `kind=log_fixed_effective`，`output_semantics=event_depth_increment` |
| 峰值能流 ↔ 能量 | `E = F0·π·w0²/2` | `references.gaussian_pulse_energy()` | 条件一致性检查 |

关键数字（G05）：

| 量 | 取值 | 来源 |
|---|---|---|
| `w0` | 16 µm | 任务书 3.1 |
| `f` | 33.3 kHz | 任务书 3.1 / F01 |
| `N_eff` | 3 | 文献式(9) 定义 |
| → `v` | **278.9734276388 mm/s** | 反解 |
| 错误换算 → `v` | 355.2 mm/s | 对照，差约 27.3% |
| `F0` | 50.1 J/cm² | 任务书 10 节 G05 |
| → `E` | **201.464053689406 µJ** | `F0·π·w0²/2` |

材料卡：`data/materials/zirconia_ysz_machining_effective_n3.json`
（`threshold_J_m2=12890`、`delta_m=3.9e-7`、`history_definition.effective_count=3`）。

## 2. SiC —— 有效脉冲数、阈值函数、平均去除率

来源 **[R2]** Wang et al., *Micromachines* 13(8) (2022) 1291，式(5)(6)(7) 与图 6。

| 项 | 文献 | 实现 | 备注 |
|---|---|---|---|
| 有效脉冲数 | `N_eff = K·(2·w0·f)/v` | `references.sic_effective_count()` | **K=扫描遍数；无 π/4 因子** |
| 阈值函数 | `F_th(N) = F_∞ + (F1 − F_∞)·exp(−k·(N−1))` | `references.sic_threshold_fluence()` | 式(6) |
| 平均去除率 | `A_R = δ_eff·[ln(F0/F_th(N))]_+` | `references.sic_mean_removal_rate()` | 式(7)，语义 `mean_depth_per_effective_pulse` |
| 协议累计深度 | （本工程定义）`H_N = A_R·N_eff` | `ReferenceEvaluator` 的 `protocol_cumulative_depth_*` | 仅在该完整协议下成立；**不得逐脉冲重复累加** |

参数（取自材料卡 `multi_response`，不在算例里硬编码）：

| 参数 | 取值 | 卡字段 |
|---|---|---|
| `F1` | 2.35 J/cm² | `Fth1_fitted_J_m2` |
| `F_∞` | 0.70 J/cm² | `Fth_infinity_J_m2` |
| `k` | 0.0199 /脉冲 | `k_inc_per_pulse` |
| `δ_eff` | 22.4 nm | `delta_eff_mean_m` |

两类阈值观测量**分别登记、不得互换**（任务书 5 节表）：

| 观测量 | 值 | 分支 ID |
|---|---|---|
| 单脉冲改性阈值 | 2.35 J/cm² | `sic4h_n1_modification` |
| 单脉冲结构转变阈值 | 4.97 J/cm² | `sic4h_n1_structural_change` |

## 3. 语义边界（本批的硬约束）

执行细则 4.3 / 任务书 3.2：只有 `event_depth_increment` 可以进入逐事件主循环。

| 语义 | 允许进入事件核 | 本批实现位置 |
|---|---|---|
| `event_depth_increment` | ✅ | `response.FixedThresholdLogLaw` |
| `mean_depth_per_effective_pulse` | ❌ | `references.ReferenceEvaluator`（SiC） |
| `cumulative_depth` | ❌ | `references`（协议累计深度） |
| `track_or_pass_depth` | ❌ | 未实现；能力表登记为不可用 |
| `volume_per_energy` | ❌ | 未实现；能力表登记为不可用 |
| `threshold_only` | ❌（不产生深度） | `response.ThresholdEvaluator`；YSZ 参考算例用它表示“不产生深度” |

两道方向相反的闸门：

* `response.assert_increment_semantics()` —— 只放行 `event_depth_increment`；
* `references.assert_reference_only_semantics()` —— 只放行平均率/累计/阈值，
  拒绝把逐事件增量冒充参考曲线。

## 4. 未复现项（显式缺口）

- 原图/原表数字化（SiC 图 6、YSZ 图 14）未完成 → 不建立独立实验回归用例；
- SiC 扫描次数列表存在重复项、工况映射不清 → 本批只接受**直接输入论文有效 N**，
  不推测扫描次数列表（任务书 3.1）；
- 逐事件扩展（`engineering_extension`）本批不做；由参考量转逐事件模型需另做验证
  （任务书 3.2 末段）。
