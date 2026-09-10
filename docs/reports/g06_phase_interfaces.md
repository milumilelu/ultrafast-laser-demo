# G06：分相查询、颗粒/铺层与跨相界面截断报告（批次 G / T11–T13）

- 汇总：检查通过 13｜失败 0
- 机读明细：`docs/reports/g06_phase_interfaces.csv`
- 结构实例统计：`docs/reports/structure_instances.csv`

> 本报告只做**数值实现验证**：合成结构不含任何实验复现结论；
> 相响应为内联合成定义，跨相截断是有损近似。

## 1. 两个合成结构实例

| 算例 | 结构类型 | 材料 | 种子 | 算法 | 相 | 目标 Vf | 实际 Vf（方法） | 事件 | 截断事件 | 相切换事件 |
|---|---|---|---|---|---|---|---|---|---|---|
| `alsic_particle_composite.json` | particle_composite | `alsic_sicp_aa2024_1030nm` | 20260910 | structure_v1 | Al_matrix,SiC_particle | 0.32 | {"Al_matrix": 0.72815, "SiC_particle": 0.27185}（grid_sample） | 21 | 21 | 18 |
| `cfrp_laminated_ply.json` | laminated_fiber_composite | `cfrp_t700_yb01_800nm` | 20260911 | structure_v1 | resin,fiber | - | {"fiber": 0.55}（analytic_from_ply_pattern） | 16 | 14 | 14 |

### 结构与几何明细

| 算例 | 相阈值（F_ref） | 相去除尺度（L_ref） | 铺层 输入/合并 | 颗粒 目标/实际 | 初末相单元 | 未应用候选占比 |
|---|---|---|---|---|---|---|
| `alsic_particle_composite.json` | Al_matrix=1.0,SiC_particle=2.35 | Al_matrix=0.5,SiC_particle=0.2 | - | 29/29 | {"Al_matrix": 16287, "SiC_particle": 9634} → {"Al_matrix": 16143, "SiC_particle": 9778} | 0.2434 |
| `cfrp_laminated_ply.json` | resin=0.7,fiber=2.1 | resin=0.35,fiber=0.15 | 4/4 | - | {"resin": 8883, "fiber": 10998} → {"resin": 8825, "fiber": 11056} | 0.02175 |

> 相阈值与去除尺度都是**内部单位**（`F_ref` / `L_ref`）。`delta` 必须写作
> `delta_over_L_ref`（δ/L_ref）：与层厚、光斑、离焦同一长度尺度；
> 若误用 `delta_over_delta_ref`（δ/delta_ref），深度会整体放大 `L_ref/delta_ref`（本工程 100）倍。

## 2. G06 检查明细

| 检查 | 期望 | 实测 | 状态 | 说明 |
|---|---|---|---|---|
| 同相合并等价 | 合并前后几何逐位一致 | 合并 3→2 层，几何逐位一致 | 通过 | 同相相邻铺层合并后 phase_at / next_different_interface 与预合并单层等价 |
| 同相层界不假截断/不同相不跳过 | 同相列 inf；异相列 = 层厚 | 同相列 inf；异相列 1（= 层厚） | 通过 | 同相层界不假截断，真实相界面距离解析给出 |
| 颗粒界面不跳过 | 进入/离开距离解析命中 | 进入 0.3｜离开 0.5｜无颗粒列 inf | 通过 | 解析射线-球求交：进入/离开界面均命中，不越过 |
| 不拆伪脉冲 | 单次应用 = 单一相核候选量 | 中心列一次应用 0.1887093983 = 单一纤维核 0.1887093983 | 通过 | 一个真实脉冲只调用当前暴露相的一个核，未拆成两相叠加 |
| 固定种子重现 | 同种子逐位一致，异种子不同 | 同种子 7 个颗粒逐位一致；异种子几何不同 | 通过 | 固定种子可复现，且换种子确有影响 |
| 目标/实际体积分数分列 | 两者分别给出且方法可追溯 | 目标 0.32｜实际 {"Al_matrix": 0.72815, "SiC_particle": 0.27185}（grid_sample, n=20000） | 通过 | 目标与实际体积分数分别报告，不强制相等 |
| 跨相截断命名与诊断 | 「未应用候选去除体积」+ 四项统计 | 截断事件 14｜相切换事件 14｜未应用占比 0.02175 | 通过 | 截断量命名为「未应用候选去除体积」，且不以剩余能量表述 |
| 红线：旧 δ 字段 | 拒绝（RESPONSE_SEMANTICS_INVALID） | RESPONSE_SEMANTICS_INVALID | 通过 | 相 'x' 使用了旧字段 delta_over_delta_ref |
| 红线：相借用其它材料卡 | 拒绝（MATERIAL_CAPABILITY_MISSING） | MATERIAL_CAPABILITY_MISSING | 通过 | 相 'SiC_particle' 不能引用其它材料卡（material_id='sic_4h_cface_1035nm_multishot'） |
| 红线：相沿用整体阈值来源 | 拒绝（MATERIAL_CAPABILITY_MISSING） | MATERIAL_CAPABILITY_MISSING | 通过 | 相 'resin' 的 response_source='parent_card_threshold' 不被允许 |
| 红线：整体阈值拆给分相 | 拒绝（MATERIAL_CAPABILITY_MISSING） | MATERIAL_CAPABILITY_MISSING | 通过 | 相 'resin' 的阈值与父卡整体阈值相同（0.84） |
| 红线：结构×动态角度互斥 | 拒绝（CONFIG_INVALID） | CONFIG_INVALID | 通过 | 配置层拦截：分相截断与动态角度不得同时启用 |
| 红线：结构类型与卡一致 | 拒绝（CONDITION_MISMATCH） | CONDITION_MISMATCH | 通过 | 结构类型与材料卡声明不一致时拒绝 |

## 3. 界面更新规则（执行细则 8 节）与本实现的对应

| 规则 | 实现 | 位置 |
|---|---|---|
| 1 当前脉冲只调用当前暴露相 | `_per_phase_candidate` 每单元只命中一个相核，按 `phase_id` 分派 | `solver.py` |
| 2 深度与到界面距离比较 | `applied = min(candidate, next_different_interface)` | `surface.apply_increment` |
| 3 达到界面后更新相标签，下一真实脉冲才响应新相 | 更新 `phase_id`；本事件不重复施加完整能量 | `surface.apply_increment` |
| 4 记录截断事件数/单元次数/候选减实际体积 | `clipped_events`、`n_clipped_cells`、`unapplied_candidate_removal_volume_internal` | `surface.py` / `solver.py` |
| 5 同相相邻区间预先合并、统一浮点容差 | 构造时按指纹合并；`CONTACT_TOL_REL` 统一容差 | `structure.py` |

## 4. 边界声明

- 结构几何是**合成**的：颗粒顺序放置（`seed` 固定）、铺层条纹解析；
  有限样本的目标体积分数与实际体积分数**分别报告**，不强制相等（如 0.45 ≠ 随机样本必然 0.45）。
- 跨相截断是**有损近似**：被截断的候选量记为「未应用候选去除体积」，
  **不是**剩余热量，也不是界面能量传输结果。
- 分相截断与动态角度在配置层互斥（M0/M2 不开放该组合）；
  几何意义未单独定义前不得混用。

## 5. 复现命令

```bash
python -m ufdemo run examples/alsic_particle_composite.json --out runs/g06_alsic_particles --force-new-suffix
python -m ufdemo run examples/cfrp_laminated_ply.json --out runs/g06_cfrp_plies --force-new-suffix
python tools/structure_report.py
python -m pytest -q -m g06
```
