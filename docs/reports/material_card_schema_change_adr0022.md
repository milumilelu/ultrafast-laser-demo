# 材料卡变更记录（ADR-0022：有效阈值 → 单脉冲常数 + 文献模型）

**日期**：2026-09-17　**变更**：把「加工有效阈值」还原为**单脉冲常数 Fth1 + 文献累积模型**。

## 为什么改

卡里原先存的是 `threshold_J_m2 = 12890`、`threshold_kind = machining_effective`、
`history_definition.effective_count = 3` —— 那**不是材料参数**，而是文献模型

```
Fth(N) = Fth1 · N^(S-1),   S(f) = 0.969 - 0.0029·f_kHz      （取自静态卡 source_equation）
```

在 N=3、f=33.3 kHz 处的**取值**：`14830 × 3^(0.87243-1) = 12890.65`，与原值差 **0.005%**。
把函数在某点的值当常量存，就把「材料的 N 依赖」伪装成了「参数的适用条件」，
进而把 τ/f/v 这些**工艺参数**焊死在一个工况点上。

## 改了什么

| 项 | 改前 | 改后 |
|---|---|---|
| `threshold_J_m2` | 12890（有效阈值） | **14830（Fth1，材料常数）** |
| `threshold_kind` | `machining_effective` | `single_pulse_with_incubation` |
| `kind` | `log_fixed_effective` | `log_fixed_with_incubation` |
| `incubation` | 无（且 `extra_incubation_prohibited_without_refit: True`） | **模型已声明**（S_f_kHz） |
| `history_definition` | `effective_count_embedded` / N=3 | `per_event_counting` |
| 协议 `required_history` | N_eff=3（门禁条件） | **删除**（不再锁定工况点） |
| `validate_run` 的 N_eff 检查 | 必须 == 卡值 | **删除** |

## 逐卡哈希（本次只有加工卡与其协议变化）

| 材料卡 | 变化 | sha256（后） |
|---|---|---|
| _synthetic_demo_isotropic.json | 否 | `836e7900a34238bbcc80b53f3fd9bcc6a13f18b154a77717be28c924d3f520c6` |
| alsic_sicp_aa2024_1030nm.json | 否 | `0f56b7cdfd390e581855ae8c1e1dde94566fa7d4c5c4a43f42d96d0b6f0fe803` |
| cfrp_t700_yb01_800nm.json | 否 | `9ccb47330930d71548de7cb302960012faee38fdb333c629478e444a2aefab2c` |
| diamond_scd_cvd_1030nm_400fs.json | 否 | `411f858b379d7a76b37163633c454e583d636fc23925cc29431f99531a5f56c7` |
| diamond_scd_cvd_1030nm_700fs.json | 否 | `d7e742d218e9d68be870986aedda1a5472a5b285e34a5e06466726af98ad22e9` |
| diamond_scd_cvd_1030nm_pulsewidth_unconfirmed.json | 否 | `0bf4e57b5ddb58040dbfd1faadff4f5119effad5ca781b125f52f7d41368ddc9` |
| glass_ceramic_unbranded_1030nm.json | 否 | `9f57eaf34f1e3e625ffb9309b87a93f070d8347d8b5a5ee475dd14785693f47f` |
| inconel718_1030nm_n10.json | 否 | `db962a3012cdffed115ff4e458dd2eb8c55c40b4baf879d72c2a2661083ddc4e` |
| sic_4h_cface_1035nm_multishot.json | 否 | `65de5e83b517cd905f50e3bb0ce8e44e4d3e4cabea9e25bede0b977376593ffb` |
| zirconia_ysz_machining_effective_n3.json | 是 | `9eb25eebc0a75168a27f1adacb9747f0543b201a99c589d4dfa38bfe732e1022` |
| zirconia_ysz_static_aps8ysz.json | 否 | `65b2509a3135f0eec2025498bff2d53f97f440bd82aa5f9543ab35c9c2fdca4c` |

变更前基线存档：`runs/_probe/materials_sha_before_adr0022.json`（临时区）。
更早一次（ADR-0021）的记录见 `material_card_schema_change_adr0021.md`。

## 数值影响（如实报告）

默认工况（100 µm / hatch 4 µm / dx 1 µm / 2 遍）：中心深度 **62.63 µm → 73.57 µm（+17.5%）**。

**这不是回归，是模型细化**：过去用「N=3 的有效阈值」去作用到所有格点；
现在各点按**自己的**累积次数取阈值 —— 中心打得多、阈值更低、去除更多，边缘反之。
代价是槽形更「尖」，与文献的有效核拟合结果不再逐位一致（原值本身就是该模型的近似）。

## 仍保留的约束

* **λ、τ** 仍须落在协议容差内（±1% / ±5%）—— 它们是 Fth1 与 S(f) 的**标定条件**；
* 卡声明了 incubation 却没给逐点历史 ⇒ 核**拒绝执行**（不静默退回固定阈值）；
* S 依赖 f ⇒ 缺 `repetition_rate_Hz` 时报错。

## ⚠️ 待确认

**δ 沿用原有效核的拟合值（3.9e-7 m）**，未按新的 Fth(N) 重新拟合。
在 log 律 `a = δ·ln(F/F_th)` 里 δ 是斜率、F_th 是截距，两者耦合 ——
严格说 δ 也该重新标定。此项已写进卡片的 `limitations`，报告中标明。
