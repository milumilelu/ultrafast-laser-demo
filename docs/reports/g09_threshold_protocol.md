# G09：七材料能力入口与受限阈值协议报告（批次 H / T14）

- 汇总：检查通过 9｜失败 0
- 机读能力表：`docs/reports/material_capability_table.csv`
- 机读检查明细：`docs/reports/g09_threshold_protocol.csv`

> 本报告只做**数值实现验证**：软件跑通 ≠ 材料验证。
> 阈值协议只按**本事件入射能流**判超阈，不产生深度；
> 合成模式内部无量纲，导出不含物理 `depth_um`。

## 1. 七材料能力入口表

| 材料族 | 类别 | 条目 | 依据 / 生效位置 | 错误码 / 缺口 | 已核验 |
|---|---|---|---|---|---|
| 氧化锆 | 开放 | YSZ 特定加工核（逐事件，条件受限） | capability:event_depth_increment | - | 是 |
| 氧化锆 | 开放 | YSZ 参考算例与公式核查 | run_mode:reference_case | - | 是 |
| 氧化锆 | 开放 | 条件对应阈值展示 | capability:threshold_only | - | 是 |
| 氧化锆 | 红线 | 合并静态/加工分支 | materials.resolve_material | MATERIAL_CAPABILITY_MISSING | 是 |
| 氧化锆 | 红线 | 默认额外孵化 | response.FixedThresholdLogLaw.increment | RESPONSE_SEMANTICS_INVALID | 是 |
| 铝基碳化硅 | 开放 | 无量纲合成颗粒 | capability:synthetic_structure | - | 是 |
| 铝基碳化硅 | 红线 | 借用单晶 SiC 当颗粒相标定 | structure.build_phase | MATERIAL_CAPABILITY_MISSING | 是 |
| CFRP | 开放 | 协议限定整体阈值 | capability:threshold_only | - | 是 |
| CFRP | 开放 | 合成铺层 | capability:synthetic_structure | - | 是 |
| CFRP | 红线 | 整体阈值拆给树脂/纤维 | structure.assert_phase_threshold_not_split | MATERIAL_CAPABILITY_MISSING | 是 |
| CFRP | 红线 | 缺 δ 生成物理深度 | response.build_pulse_law | RESPONSE_SEMANTICS_INVALID | 是 |
| 高温合金 | 开放 | In718 的 N=10 阈值参考 | capability:threshold_only | - | 是 |
| 高温合金 | 开放 | 合成形貌 | capability:synthetic_structure | - | 是 |
| 高温合金 | 红线 | 当作单脉冲阈值或任意历史阈值 | thresholds._declare_multipulse | UNAVAILABLE | 是 |
| 微晶玻璃 | 开放 | 合成均质表面 | capability:synthetic_structure | - | 是 |
| 微晶玻璃 | 开放 | 导入入口 | capability:table_import | - | 是 |
| 微晶玻璃 | 红线 | 将不同玻璃陶瓷牌号合并 | materials.resolve_material | MATERIAL_CAPABILITY_MISSING | 是 |
| SiC | 开放 | 两类阈值（改性 / 结构变化） | capability:threshold_only | - | 是 |
| SiC | 开放 | 有效 N 参考评估器 | capability:reference_evaluator | - | 是 |
| SiC | 开放 | 平均去除率（仅评估器） | capability:mean_depth_per_effective_pulse | - | 是 |
| SiC | 红线 | 平均率直接逐事件累加 | tables.assert_no_local_depth_from_volume | CONFIG_INVALID | 是 |
| SiC | 红线 | 改性量当去除量 | thresholds.assert_not_removal | RESPONSE_SEMANTICS_INVALID | 是 |
| 金刚石 | 开放 | 条件对应阈值 | capability:threshold_only | - | 是 |
| 金刚石 | 开放 | 导入入口 | capability:table_import | - | 是 |
| 金刚石 | 缺口 | 合成形貌 | 缺口：卡内 structure_type = net_removal_with_optional_modification_mask 未被结构构建器（structure.STRUCTURE_TYPES）支持，且无逐事件去除核，故当前无法生成合成形貌。细则第 7 节要求开放；缺口需在 M2 放行前决策（扩展结构类型／改经共享无量纲卡 _synthetic_demo_isotropic.json／修订规格）。 | 缺口（规格要求开放、实现未支持） | 是 |
| 金刚石 | 红线 | 混合 400/700 fs | config.check_reference_conditions | CONDITION_MISMATCH | 是 |
| 金刚石 | 红线 | 未确认脉宽候选值默认启用 | config.check_reference_conditions | CONDITION_MISMATCH | 是 |

### 缺口（1）

- **金刚石／合成形貌**：三张金刚石卡均无 synthetic_structure 能力，且该结构类型被 load_structure 拒绝（CONFIG_INVALID）。
  - 原因：缺口：卡内 structure_type = net_removal_with_optional_modification_mask 未被结构构建器（structure.STRUCTURE_TYPES）支持，且无逐事件去除核，故当前无法生成合成形貌。细则第 7 节要求开放；缺口需在 M2 放行前决策（扩展结构类型／改经共享无量纲卡 _synthetic_demo_isotropic.json／修订规格）。

## 2. G09 检查明细

| 检查 | 期望 | 实测 | 状态 | 说明 |
|---|---|---|---|---|
| 配置层拒绝累计/平均能流基准 | CONFIG_INVALID | 4 个累计/平均基准 + 整份 RunConfig 均 CONFIG_INVALID | 通过 | 非法能流基准在配置层被拒，不靠界面禁用 |
| 唯一注册基准 = 本事件入射能流 | ('per_event_incident',) | 唯一注册基准=('per_event_incident',) | 通过 | 只登记「本事件入射能流」一种基准 |
| 禁用时报不可用、不返回全 0 假数组 | available=False｜分类=None | 未开启→available=False｜分类返回 None（非全 0 数组） | 通过 | 禁用时如实报不可用，不返回假数组 |
| 多候选须显式 candidate_index | 未给索引→不可用 | 2 个候选：未给索引→不可用｜idx0=2.35e+04｜idx1=4.97e+04 | 通过 | 多候选必须显式 candidate_index，不静默取默认 |
| 多脉冲口径不得当单脉冲阈值 | N=10 拦｜N=1 放行 | 高温合金 N=10→不可用｜CFRP Fth1→可用 | 通过 | 按口径名机器识别多脉冲：N≥2 定点口径判不可用，N=1 端点放行 |
| 缺阈值如实报不可用 | available=False｜threshold=None | 微晶玻璃（无阈值）→available=False｜threshold=None | 通过 | 缺阈值如实报不可用，不补近似值 |
| 只按本事件能流判超阈（可判别构造） | 累计超阈但掩膜为空 | 两发 0.6Fth（累计 1.2Fth）掩膜=空｜单发 1.2Fth 点亮 25 单元｜高度逐位不变 | 通过 | 只按本事件能流判超阈，且协议不产生去除量 |
| 求解器启用/禁用两态一致 | 启用记数落盘｜禁用不造假 | 启用：超阈单元·事件=185｜末态单元=37｜禁用：掩膜=None 且快照不写数组 | 通过 | 启用时落盘并计数，禁用时不造假数组；threshold_only 深度仍为 null |
| 七材料能力入口：逐条探针实跑 | 全部成立 | 探针 27/27 成立｜开放 15｜红线 11｜缺口 1 | 通过 | 七材料入口的开放/红线/缺口逐条实跑核验 |

## 3. 协议硬约束与本实现的对应

| 约束（细则 11.3 / 任务书 6.3） | 实现位置 | 证据 |
|---|---|---|
| 只对本事件入射能流判超阈，绝不用累计剂量 | `thresholds.classify_exceedance` | 两发 0.6Fth 累计 1.2Fth 掩膜仍为空 |
| 阈值口径必须是单脉冲口径；多脉冲定点（N≥2）判不可用 | `thresholds._declare_multipulse` | 高温合金 N=10 拦；CFRP Fth1 放行 |
| 多候选必须显式 `candidate_index` | `thresholds._resolve_threshold_protocol` | SiC 未给索引→不可用 |
| 不产生深度（`used_for_depth` 恒 False） | `thresholds.assert_not_removal` | 高度场逐位不变 |
| 协议未开启/材料无阈值时如实报不可用，不返回假数组 | `build_threshold_protocol` | 掩膜为 `None`，分类返回 `None` |
| 落盘与回放同源 | `surface.to_snapshot` / `io` / `ui_service` | 快照写入掩膜与计数；回放走同一诊断构造 |

## 4. 边界声明

- 「超阈/改性标记」是**分类标记**，不是去除量，也不是温度/热影响结果；
  界面图层标签已按措辞守卫（细则 11.3）严格限定为「该图实际是什么量」。
- 受限阈值协议**不接逐事件主循环**的深度计算：它只做观测量累计。
- 金刚石族的「合成形貌」在细则第 7 节要求开放，但当前结构构建器不支持其
  `structure_type`，故如实记为**缺口**并附探针证明（见上表），留待 M2 放行前决策。

## 5. 复现命令

```bash
python tools/material_report.py
python -m pytest -q -m g09
```
