# 材料卡结构与哈希变更记录（ADR-0021 参考协议外置）

**日期**：2026-09-17　**变更**：把参考协议的协议体从材料卡迁出到 `data/protocols/`，卡片只保留引用。

## 为什么刷新了哈希护栏

`tests/test_dataset_permissions.py::test_material_cards_unchanged`
的基线（`C:/tmp/materials_sha_baseline.json`，2026-09-13 记录）用于挡住
「**顺手改写**既有材料卡」。本次是**有意**的 schema 变更，故按要求刷新基线并留下本记录。
变更前基线已存档于临时区：`runs/_probe/materials_sha_baseline_pre_adr0021.json`。

## 逐卡哈希

| 材料卡 | 变化 | sha256（前） | sha256（后） |
|---|---|---|---|
| _synthetic_demo_isotropic.json | 否 | `836e7900a34238bbcc80b53f3fd9bcc6a13f18b154a77717be28c924d3f520c6` | `836e7900a34238bbcc80b53f3fd9bcc6a13f18b154a77717be28c924d3f520c6` |
| alsic_sicp_aa2024_1030nm.json | 否 | `0f56b7cdfd390e581855ae8c1e1dde94566fa7d4c5c4a43f42d96d0b6f0fe803` | `0f56b7cdfd390e581855ae8c1e1dde94566fa7d4c5c4a43f42d96d0b6f0fe803` |
| cfrp_t700_yb01_800nm.json | 是 | `fa235f8fab4dc66e85a14a1ac9f76545fe11769d12861526edc0f25635af0a45` | `9ccb47330930d71548de7cb302960012faee38fdb333c629478e444a2aefab2c` |
| diamond_scd_cvd_1030nm_400fs.json | 是 | `e5a32d34d31dc76cb82408e588a6177e7f5f818b38c90525cdac9cce56affa9b` | `411f858b379d7a76b37163633c454e583d636fc23925cc29431f99531a5f56c7` |
| diamond_scd_cvd_1030nm_700fs.json | 是 | `ed2a61e4294942c26b4df0aeffab0290f244abbc331bb7bec79c990e5af46fd7` | `d7e742d218e9d68be870986aedda1a5472a5b285e34a5e06466726af98ad22e9` |
| diamond_scd_cvd_1030nm_pulsewidth_unconfirmed.json | 是 | `3e5e7a7ae95a14b4194d4962519de05fac54848c4203553ed70675cad4ea5e3b` | `0bf4e57b5ddb58040dbfd1faadff4f5119effad5ca781b125f52f7d41368ddc9` |
| glass_ceramic_unbranded_1030nm.json | 否 | `9f57eaf34f1e3e625ffb9309b87a93f070d8347d8b5a5ee475dd14785693f47f` | `9f57eaf34f1e3e625ffb9309b87a93f070d8347d8b5a5ee475dd14785693f47f` |
| inconel718_1030nm_n10.json | 是 | `6c1a6b7113dc46118ddee40a40b2acb22c603f4699ebbd82f04d411324f510e0` | `db962a3012cdffed115ff4e458dd2eb8c55c40b4baf879d72c2a2661083ddc4e` |
| sic_4h_cface_1035nm_multishot.json | 是 | `42dba4c2ffa76510af9a2b0d3626eaec8b49f18c207980343d4ccd8e6608b946` | `65de5e83b517cd905f50e3bb0ce8e44e4d3e4cabea9e25bede0b977376593ffb` |
| zirconia_ysz_machining_effective_n3.json | 是 | `56b00c09771a194ab4e868ef277f3c6d2d0a6134e1e3ab2d6a73a7022d95efff` | `42cd061acfe195b79e99beda9e014666aef222eb8f9df17168bc01842c9f2481` |
| zirconia_ysz_static_aps8ysz.json | 否 | `65b2509a3135f0eec2025498bff2d53f97f440bd82aa5f9543ab35c9c2fdca4c` | `65b2509a3135f0eec2025498bff2d53f97f440bd82aa5f9543ab35c9c2fdca4c` |

## 取值等价性（不是「差不多」，是逐值一致）

解析后逐值比对（忽略 `reference_protocol` 与已迁走的 `validity_domain.peak_fluence_J_m2`）：**11 张卡全部一致**。
端到端复算亦一致（demo 端点，ZrO₂ 加工卡）：

| 量 | 迁出前 | 迁出后 |
|---|---|---|
| 平均深度 | 44.06 µm | **44.0616 µm** |
| 最大深度 | 63.35 µm | **63.3547 µm** |
| 中心深度 | 62.63 µm | **62.6316 µm** |
| 脉冲事件 | 621 | **621** |

## 顺带修掉的生成器缺陷：换行符

`tools/migrate_materials.py` 原先用 `Path.write_text(...)`
（文本模式）写卡，在 Windows 上会把 LF 翻成 CRLF；而仓库 `.gitattributes` 是 `* text=auto eol=lf`。
后果：**git 归一化后看不到任何 diff，但文件 sha256 全变** —— 护栏报「材料卡被改写」，
而 `git diff` 空空如也，极易误判。
已新增 `_write_text_lf()` 显式写 LF；11 张卡与 7 个协议文件复核 CRLF=0。

修正后重跑生成器：**4 张卡字节未变，仅 7 张（有协议者）变化** —— 改动面与设计意图吻合。

