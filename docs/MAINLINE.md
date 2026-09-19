# 唯一主线

仓库只保留一条生产求解主线：

`webui/demo.html` → `webcontract.demo_rect_payload()` → `RunConfig` →
`ufdemo.solver.solve()` → `SurfaceState`。

求解器的参考实现位于 `src/ufdemo/solver.py`；可用的分组加速位于
`src/ufdemo/accelerators.py`，不支持的响应核、历史、分相和闭合组合必须回退
到同一个逐事件参考循环。`src/ufdemo/closures.py` 只提供事件级的受约束净效率
闭合，不另建终态预测器或第二套路径解释器。

当前唯一前端是 `webui/demo.html`。页面默认发送 `processMode=actual`，使用
`data/config/shared_experiment_background.json` 的物镜后功率、名义光学、固定原始
焦面和被动轴向离焦；文献参考演示只能由 API 显式传 `processMode=reference`。

实验数据的唯一入口是观测适配器和标定工具：

- `src/ufdemo/observations.py`：高度场的物理坐标、NaN 掩膜和观测算子；
- `tools/audit_height_field_pairs.py`：四材料高度场清点与保守配对审计；
- `tools/calibrate_zirconia_pass.py`：氧化锆遍数标量基线标定；
- `tools/grid_convergence.py`：固定物理输入的 G-01 网格/半格偏移基线。

`docs/experiments/`、`docs/reports/` 和 `independent_probes/` 是数据或历史证据，
不是第二套实现。运行输出写入 `runs/`，不作为源码或训练标签提交。

## 任务状态（2026-09-19）

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| P0-01 | 已实现 | actual 分支、共享背景装配、实际 `E=P/f`、`test_p0_optimization.py` |
| P0-02 | 已实现 | `roiStats` 与加工矩形观测；矩形 ROI 使用单元相交面积权重，圆形仍按中心掩膜 |
| P0-03 | 已实现 | illumination/exposure 分离、1-based N、effective history 元数据 |
| P0-04 | 已实现 | 孵化响应自动回退 tail window；above-threshold 不再漏掉后续可烧蚀环带 |
| P0-05 | 已实现/受限回退 | 分组几何模式、统一响应准入、非支持核回退、半步估计 |
| P0-06 | 已实现 | accepted-prefix 消费、事件顺序与指定快照切块 |
| P0-07 | 已实现 | 质量记录保留、trajectory_key、轨迹级分组 |
| P0-08 | 已实现 | effective configuration、P0 行为回归和统一 pytest 命令 |
| G-01 | 已实现基线 | `tools/grid_convergence.py` 三级网格与半单元偏移输出 |
| P1-01 | 审计闭环已实现，显式配对待人工确认 | 四材料 149 个 NPZ 全部可读；脚本不猜配对 |
| P1-02 | 分类与证据边界已实现 | 有效模型/显式分相/合成验证分开；真实分相参数仍缺 |
| P1-03 | 氧化锆有效标量基线已实现 | `docs/reports/zirconia_pass_scalar_calibration.md` |
| P1-04 | 事件级闭合已接入 | `closures.py`、solver 前向调用、checkpoint loader；真实训练权重待 P1-05 |
| P1-05 | 有条件可执行 | `closure_training.fit_event_supervised_mlp` 已支持显式事件效率标签；终态深度不能冒充事件标签，真实闭环权重仍需配对事件或有依据的参考标签 |
| P1-06 | 基线评估已实现 | `closure_evaluation.py` 提供同一观测算子下的 M0 与分组 OOF M1 对比；M4 需外部训练权重并保持训练/测试分组隔离 |
| G-02 | 已实现可选参考模式 | `solver.subcell_order=2/4` 对每个脉冲的子单元能流先算非线性响应再平均；分组加速显式回退，默认 `1` 保持历史结果 |
| G-03 | 误差审计框架已实现 | `microstructure_resolution.audit_phase_resolution` 只接受显式相图与相参数，不推断 CFRP/AlSiC 微结构；真实材料结论仍待相尺度与响应参数 |
| G-05 | 粗筛安全带与审计已实现 | `plan_for_target(screening_margin_um=...)` 保留粗筛、细核复核、边界不确定候选和耗时/漏选诊断 |
| G-04/P2-01/P2-02 | 按条件延期 | 需要 G-02/G-03/P1-06 或时序辨识证据 |

因此“唯一主线”是代码组织约束，不把尚未有实验辨识依据的模型写成已完成能力。
