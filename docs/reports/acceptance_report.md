# 验收报告（批次 A–G：M0 最小闭环 + T07 参考评估器 + T09 界面 + T10 查表 + T11–T13 分相结构）

- 生成时间（UTC）：2026-09-11T00:51:16.837757+00:00
- 代码版本：commit=b1378d87fb51886a0929f6ceba87a17609958d96｜工作区有改动=False｜源码清单哈希=fdc54e3f1bf07138…
- 执行环境：Python 3.13.14｜NumPy 2.3.5｜Windows-11-10.0.26100-SP0
- 结果目录：`C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance`
- 汇总：通过 97｜失败 0｜未运行 4

> 公式核查、数值实现验证、实验复现三栏分开记录。本报告不含任何实验复现结论：
> A–C 只做到「公式核查 + 数值实现验证」；D 的 YSZ/SiC 参考量也只做到「公式核查」；
> E 的界面操作检查为「数值实现验证」（提交/回放的求解次数记账）；
> F 的查表为「数值实现验证」（schema/插值/越界/路由），SiC 曲线由材料卡参数按式(6) 重算，
> **不构成对原文曲线的复现**。原图/原表数字化与工况对应完成前不建立实验回归用例。
> G 的分相结构为「数值实现验证」（同相合并/界面不跳过/种子复现/截断命名），
> 相响应为内联合成定义、跨相截断为有损近似，**不含任何实验复现结论**。

## 逐项结果

| 编号 | 算例 | 量 | 预期 | 实测 | 误差 | 容差 | 状态 | 验证类别 |
|---|---|---|---|---|---|---|---|---|
| G01 | examples/analytic_single_pulse.json | 峰值能流 (J/m^2) | 73890.5609893065 | 73890.5609893065 | 0.0 | 解析恒等 | 通过 | 公式核查 |
| G01 | examples/analytic_single_pulse.json | 单脉冲能量 (J) | 1.1606702178681694e-05 | 1.1606702178681694e-05 | 0.0 | 解析恒等 | 通过 | 公式核查 |
| G01 | examples/analytic_single_pulse.json | 中心去除深度 (m) | 2e-07 | 1.9999999999999996e-07 | 1.3234889800848443e-16 | rel <= 1e-12 | 通过 | 数值实现验证 |
| G01 | examples/analytic_single_pulse.json | 去除体积 (m^3) | 3.1415926535897935e-17 | 3.1411e-17 | 0.00015681650809518674 | rel <= 1% | 通过 | 数值实现验证 |
| G01 | examples/analytic_single_pulse.json | 去除半径 (m) | 1e-05 | 1e-05 | 0.0 | 解析恒等 | 通过 | 公式核查 |
| G01 | 网格 dx=dy=w/20 | 去除体积相对误差 | <= 1% | 1.568e-04 | 0.00015681650809518674 | rel <= 1% | 通过 | 数值收敛记录 |
| G01 | 网格 dx=dy=w/40 | 去除体积相对误差 | <= 1% | 1.557e-05 | 1.5566496101057905e-05 | rel <= 1% | 通过 | 数值收敛记录 |
| G01 | 网格 dx=dy=w/80 | 去除体积相对误差 | <= 1% | 6.365e-06 | 6.365350953644903e-06 | rel <= 1% | 通过 | 数值收敛记录 |
| G02 | examples/ten_pulses.json | 事件数 | 10 | 10 | 0 | == 10 | 通过 | 数值实现验证 |
| G02 | examples/ten_pulses.json | 中心深度 (m) | 2e-06 | 1.9999999999999995e-06 | 2.1175823681357508e-16 | rel <= 1e-12 | 通过 | 数值实现验证 |
| G02 | examples/ten_pulses.json | 去除体积 (m^3) | 3.1415926535897933e-16 | 3.1410999999999994e-16 | 0.00015681650809534368 | rel <= 1% | 通过 | 数值实现验证 |
| G03 | 零事件 | 去除体积 (m^3) | 0.0 | 0.0 | 0.0 | == 0（事件数为 0） | 通过 | 数值实现验证 |
| G03 | 等阈值 | 最大去除深度 (m) | 0.0 | 0.0 | 0.0 | == 0（F <= Fth） | 通过 | 数值实现验证 |
| G03 | 无效能量（负） | 是否拒绝 | 拒绝（CONFIG_INVALID） | 已拒绝 |  |  | 通过 | 数值实现验证 |
| G03 | 无效半径（负） | 是否拒绝 | 拒绝（CONFIG_INVALID） | 已拒绝 |  |  | 通过 | 数值实现验证 |
| G03 | NaN 参数 | 是否拒绝 | 拒绝（CONFIG_INVALID） | 已拒绝 |  |  | 通过 | 数值实现验证 |
| G03 | 域外照射 | 发射/截获能量 (J) | 发射 1.160670e-05，截获 0 | 发射 1.160670e-05，截获 0.000000e+00 |  | 截获 == 0 且不去除 | 通过 | 数值实现验证 |
| G03 | 缺 δ 请求深度 | 是否拒绝 | 拒绝（MATERIAL_CAPABILITY_MISSING） | 已拒绝 |  |  | 通过 | 数值实现验证 |
| G04 | examples/line_scan.json | 事件数 | 7 | 7 | 0 | == 7 | 通过 | 数值实现验证 |
| G04 | examples/line_scan.json | 位置间距 v/f (m) | 1e-05 | 9.999999999999999e-06 | 1.6940658945086004e-16 | rel <= 1e-9 | 通过 | 数值实现验证 |
| G04 | examples/line_scan.json | ROI 可用 | True | True |  | == True | 通过 | 数值实现验证 |
| G04 | examples/line_scan.json | 全域平均深度 (m) | V/A | 2.361380496720783e-08 | 0.0 | 与 V/A 一致 | 通过 | 数值实现验证 |
| G04 | 正反向扫描 | 深度场最大绝对差 | 0.0 | 2.9116757561866574e-22 | 2.9116757561866574e-22 | rel <= 1e-12（对称工况） | 通过 | 数值实现验证 |
| G04 | 统一时钟 | 时间戳重复数 | 0 | 0 | 0 | == 0 | 通过 | 数值实现验证 |
| C-raster | examples/raster_multipass.json | 事件数 | 105 | 105 | 0 | == 105（5 行 × 7 点 × 3 遍） | 通过 | 数值实现验证 |
| C-raster | examples/raster_multipass.json | 遍数快照 | 4 | 4 |  | >= 3 遍 + 最终 | 通过 | 数值实现验证 |
| C-raster | examples/raster_multipass.json | 多 ROI 分别统计 | 2 | 2 | 0 | == 2 | 通过 | 数值实现验证 |
| M0-synthetic | examples/synthetic_demo_point.json | 单位系统 | dimensionless | dimensionless |  | == dimensionless | 通过 | 数值实现验证 |
| M0-synthetic | examples/synthetic_demo_point.json | 中心深度 (L_ref) | 10.0 | 10.0 | 0.0 | rel <= 1e-12 | 通过 | 数值实现验证 |
| G05 | examples/ysz_reference_case.json | YSZ 扫描速度 (mm/s) | 278.9734276388 | 278.97342763877356 | 9.474805537572104e-14 | rel <= 1e-12 | 通过 | 公式核查 |
| G05 | examples/ysz_reference_case.json | 错误换算 2*w0*f/N (mm/s) | 355.2 | 355.19999999999993 | 1.6003214769371628e-16 | rel <= 1e-12 | 通过 | 公式核查 |
| G05 | examples/ysz_reference_case.json | YSZ 脉冲能量 (uJ) | 201.464053689406 | 201.4640536894062 | 9.875308392213193e-16 | rel <= 1e-12 | 通过 | 公式核查 |
| G05 | examples/sic_reference_case.json | SiC Fth(1) (J/cm^2) | 2.35 | 2.35 | 0.0 | rel <= 1e-15 | 通过 | 公式核查 |
| G05 | examples/sic_reference_case.json | SiC Fth(N->inf) (J/cm^2) | 0.7 | 0.7 | 0.0 | rel <= 1e-9 | 通过 | 公式核查 |
| G05 | examples/sic_reference_case.json | SiC k (1/pulse) | 0.0199 | 0.0199 | 0.0 | == 0.0199（卡内值） | 通过 | 公式核查 |
| G05 | examples/sic_reference_case.json | 平均率 / 协议累计深度 独立输出 | 两项分别给出，累计=平均×N_eff | 平均 59.5674 nm/有效脉冲；N=720 累计 42.8885 um |  | rel <= 1e-12 | 通过 | 数值实现验证 |
| G05 | （语义闸门） | 平均率/累计量能否进入事件核 | 拒绝（RESPONSE_SEMANTICS_INVALID） | 已拒绝 |  |  | 通过 | 数值实现验证 |
| G05 | （参考运行目录） | 是否写出形貌表面文件 | 不写（参考评估器不求解网格） | 文件：config.json, diagnostics.json, material_snapshot.json, metadata.json, profiles.csv, snapshots, statistics.csv |  |  | 通过 | 数值实现验证 |
| G06 | examples/alsic_particle_composite.json | 结构实例求解状态 | completed（合成结构，内部单位） | 颗粒增强铝基（合成）｜结构类型 particle_composite｜种子 20260910｜事件 21 |  |  | 通过 | 数值实现验证 |
| G06 | examples/cfrp_laminated_ply.json | 结构实例求解状态 | completed（合成结构，内部单位） | CFRP 铺层 [0/90/0/90]（合成）｜结构类型 laminated_fiber_composite｜种子 20260911｜事件 16 |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 同相合并等价 | 合并前后几何逐位一致 | 合并 3→2 层，几何逐位一致 |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 同相层界不假截断/不同相不跳过 | 同相列 inf；异相列 = 层厚 | 同相列 inf；异相列 1（= 层厚） |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 颗粒界面不跳过 | 进入/离开距离解析命中 | 进入 0.3｜离开 0.5｜无颗粒列 inf |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 不拆伪脉冲 | 单次应用 = 单一相核候选量 | 中心列一次应用 0.1887093983 = 单一纤维核 0.1887093983 |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 固定种子重现 | 同种子逐位一致，异种子不同 | 同种子 7 个颗粒逐位一致；异种子几何不同 |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 目标/实际体积分数分列 | 两者分别给出且方法可追溯 | 目标 0.32｜实际 {"Al_matrix": 0.72815, "SiC_particle": 0.27185}（grid_sample, n=20000） |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 跨相截断命名与诊断 | 「未应用候选去除体积」+ 四项统计 | 截断事件 14｜相切换事件 14｜未应用占比 0.02175 |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 红线：旧 δ 字段 | 拒绝（RESPONSE_SEMANTICS_INVALID） | RESPONSE_SEMANTICS_INVALID |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 红线：相借用其它材料卡 | 拒绝（MATERIAL_CAPABILITY_MISSING） | MATERIAL_CAPABILITY_MISSING |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 红线：相沿用整体阈值来源 | 拒绝（MATERIAL_CAPABILITY_MISSING） | MATERIAL_CAPABILITY_MISSING |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 红线：整体阈值拆给分相 | 拒绝（MATERIAL_CAPABILITY_MISSING） | MATERIAL_CAPABILITY_MISSING |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 红线：结构×动态角度互斥 | 拒绝（CONFIG_INVALID） | CONFIG_INVALID |  |  | 通过 | 数值实现验证 |
| G06 | examples/（两个合成结构算例） | 红线：结构类型与卡一致 | 拒绝（CONDITION_MISMATCH） | CONDITION_MISMATCH |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 初始渲染无异常 | 无异常 | 无异常 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 初始 solve_count | 0 | 0 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 初始无冻结结果 | frozen=None | frozen=None |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 提交计算：solve_count 恰好 +1 | 1 | 1 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 改参数未提交：旧结果标为「上一次运行」且不求解 | is_stale=True｜solve_count=1｜界面出现「上一次运行」 | is_stale=True｜solve_count=1｜label=上一次运行（2026-09-11T00:51:10.011001+00:00） |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 改参数后 solve_count 不变 | 1 | 1 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 快照回放 / 切图层 / 旋转视图 / 切截面：solve_count 不变 | 1（不变） | 1 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 读取历史运行：read_count +1 且 solve_count 不变 | read_count=1｜solve_count=0 | read_count=1｜solve_count=0｜run_id=ui_run_20260911T005110_3r11 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 参考评估器「评估」：不进入逐事件求解、不产生形貌 | solve_count=0｜产出参考结果｜frozen=None | solve_count=0｜有参考结果=True｜frozen=None |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 查表「查值」：不触发求解、不读取历史 | solve_count=0｜产出查表结果｜read_count=0 | solve_count=0｜read_count=0｜有查表结果=True |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 切换曲线卡 / 插值方法：solve_count 始终为 0 | 0（不变） | 曲线数=3｜异常组合=无 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 图层标签不含被禁措辞（严格子串：热影响区/HAZ/温度…） | 全部通过 | 全部通过 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 缺能力模式显示准确不可用原因（不自动降级、不静默填值） | 拒绝并给出原因 | 允许=False｜原因=材料卡未开放 reference_case 模式；允许：['threshold_only', 'synthetic_demo'] |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 合成模式禁止导出物理深度 | 以 depth_um 命名时拒绝；depth 标签非 um | 导出标签=depth/L_ref｜拒绝 depth_um=True |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | threshold_only/参考结果：去除量显示「不提供」而非 0 | removal_available=False | removal_available=False｜run_id=g05_ysz_reference |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 探针运行输出位置 | 隔离到 C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\_ui_probe | 已写入 65 个运行目录 |  |  | 通过 | 数值实现验证 |
| G09-UI | app.py（Streamlit） | 提交运行目录 | 存在 metadata.json | 存在 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 示例发现 | cfrp_laminated_ply.json 出现在界面示例列表 | 在列表中 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 示例发现 | alsic_particle_composite.json 出现在界面示例列表 | 在列表中 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 逻辑层提交 | cfrp_laminated_ply.json 提交后 status=completed | status=completed｜solve_count=1｜快照=4 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 逻辑层提交 | alsic_particle_composite.json 提交后 status=completed | status=completed｜solve_count=1｜快照=2 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 只读不重算 | 渲染/切图层/取快照后 solve_count 不变 | 1 -> 1 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 只读不重算 | 渲染/切图层/取快照后 solve_count 不变 | 1 -> 1 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 措辞守卫 | 全部图层标签不含被禁词（7 项） | 被禁词表 ('热影响区', 'HAZ', '温度场', '温度分布', '热输入', '温度')；命中 0 项 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 措辞守卫（不可用原因） | 不可用原因文案不含被禁词 | 命中 0 项 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 界面全链路（AppTest） | 首屏无异常 | 异常=0 错误=0 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 界面全链路（AppTest） | 点「提交计算」后无异常且出结果 | 异常=0 错误=0｜solve_count=1｜status=completed｜快照=4 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 界面结果区渲染 | 结果页重跑无异常 | 异常=0 错误=0 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 分相诊断面板 | 结果区出现「相结构诊断」面板 | 相结构诊断｜laminated_fiber_composite｜算法 structure_v1｜种子 20260911｜相数 2 |  |  | 通过 | 数值实现验证 |
| G09-demo | app.py（端到端链路） | 历史读取同源 | 读回历史带出同源结构诊断且不求解 | solve_count=0 read_count=1｜结构类型=laminated_fiber_composite｜截断诊断行=6｜phase_id=有 |  |  | 通过 | 数值实现验证 |
| G09-table | curves/sic_threshold_vs_effective_n.curve.json | 曲线去向（语义路由） | evaluator（event_kernel 仅限 event_depth_increment） | evaluator｜可进事件核=False｜派生=threshold_fluence |  |  | 通过 | 数值实现验证 |
| G09-table | curves/synthetic_volume_per_energy.curve.json | 曲线去向（语义路由） | evaluator（event_kernel 仅限 event_depth_increment） | evaluator｜可进事件核=False｜派生=removal_volume,removal_volume_per_energy |  |  | 通过 | 数值实现验证 |
| G09-table | curves/ysz_analytic_depth_vs_fluence.curve.json | 曲线去向（语义路由） | event_kernel（event_kernel 仅限 event_depth_increment） | event_kernel｜可进事件核=True｜派生=removal_depth_per_pulse,local_depth |  |  | 通过 | 数值实现验证 |
| G09-table | tests/fixtures/curves_invalid/* | 无效曲线卡是否按预期码拒收 | 14 组分别触发预期错误码（CONFIG_INVALID / NUMERIC_NONFINITE / …） | 通过 14/14 |  | 全通过（14 项） | 通过 | 数值实现验证 |
| G09-table | lookup_below_range | 行为级拒收错误码 | `TABLE_OUT_OF_RANGE` | `TABLE_OUT_OF_RANGE` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | lookup_above_range | 行为级拒收错误码 | `TABLE_OUT_OF_RANGE` | `TABLE_OUT_OF_RANGE` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | pchip_without_scipy | 行为级拒收错误码 | `NOT_IMPLEMENTED` | `NOT_IMPLEMENTED` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | local_depth_from_volume | 行为级拒收错误码 | `CONFIG_INVALID` | `CONFIG_INVALID` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | event_kernel_from_threshold | 行为级拒收错误码 | `RESPONSE_SEMANTICS_INVALID` | `RESPONSE_SEMANTICS_INVALID` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | condition_mismatch_on_event_curve | 行为级拒收错误码 | `CONDITION_MISMATCH` | `CONDITION_MISMATCH` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | unknown_interpolation_method | 行为级拒收错误码 | `CONFIG_INVALID` | `CONFIG_INVALID` |  | 码一致 | 通过 | 数值实现验证 |
| G09-table | ysz_analytic_depth_vs_fluence | 越界查询返回值 | None（不是 0，也不外推） | 越界=[None]｜状态=below_range |  | 值必须为 None | 通过 | 数值实现验证 |
| G09-table | ysz_analytic_depth_vs_fluence | 端点包含性 | 含端点（闭区间） | [1.0, 40.0] 查值状态=ok |  | status==ok | 通过 | 数值实现验证 |
| G09-table | curves/* | 查表是否产生形貌表面文件 | 不产生（查表不是求解） | 无 final_surface.npz（查表只读曲线） |  |  | 通过 | 数值实现验证 |
| G07 | （斜入射基准） | 0° 退化；60° 足迹比 2、中心能流减半；可见性 | 见执行细则 10 节 | 未运行 |  |  | 未运行 | 数值实现验证 |
| G08 | （分组求解） | 分组与逐脉冲偏差 <= 1%；回退正确 | 见执行细则 10 节 | 未运行 |  |  | 未运行 | 数值实现验证 |
| G09 | （逐事件核查表接入） | 越界拒绝；标签；重读一致；逐事件核接入 | 见执行细则 10 节 | 未运行 |  |  | 未运行 | 数值实现验证 |
| B01-B04 | （性能基准） | CPU/内存/事件数/耗时 | 见任务书 12 节 | 未运行 |  |  | 未运行 | 不适用 |

## 未运行项与原因

- **G07**（数值实现验证）：批次 J（T18）未实施：斜入射在配置层拦截（GEOMETRY_UNSUPPORTED）
- **G08**（数值实现验证）：批次 I（T17）未实施：accelerators.py 仍为占位
- **G09**（数值实现验证）：查表（T10，批次 F）已交付：曲线卡/越界/路由检查见上方 G09-table 行；但**逐事件主循环接入**属批次 H（T14）未实施；界面（T09）操作检查见上方 G09-UI 行
- **B01-B04**（不适用）：批次 I（T16）未实施：本批只记录单次运行的 elapsed_s

## 复现命令

```bash
python -m ufdemo validate examples/analytic_single_pulse.json
python -m ufdemo run examples/analytic_single_pulse.json --out runs/g01_single_pulse --force-new-suffix
python -m ufdemo run examples/ten_pulses.json --out runs/g02_ten_pulses --force-new-suffix
python -m ufdemo run examples/line_scan.json --out runs/g04_line_scan --force-new-suffix
python -m ufdemo run examples/raster_multipass.json --out runs/c_raster --force-new-suffix
python -m ufdemo run examples/synthetic_demo_point.json --out runs/m0_synth --force-new-suffix
python -m ufdemo reference examples/ysz_reference_case.json --out runs/g05_ysz_reference
python -m ufdemo reference examples/sic_reference_case.json --out runs/g05_sic_reference
python -m ufdemo run examples/alsic_particle_composite.json --out runs/g06_alsic_particles --force-new-suffix
python -m ufdemo run examples/cfrp_laminated_ply.json --out runs/g06_cfrp_plies --force-new-suffix
python -m ufdemo table data/curves/ysz_analytic_depth_vs_fluence.curve.json --x 5
python tools/make_curves.py
python tools/table_report.py
python tools/structure_report.py
python tools/migrate_materials.py
python tools/ui_probe.py
python tools/ui_demo_probe.py
python tools/run_acceptance.py
python -m pytest -q
streamlit run app.py
```
