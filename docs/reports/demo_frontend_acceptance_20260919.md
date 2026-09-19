# 实际递推演示前端验收（2026-09-19）

## 验收范围

本次把 `laser_demo_frontend_candidate.html` 合并为仓库唯一演示入口 `webui/demo.html`，并用本地服务进行真实 API 验收。页面继续调用 `/api/demo-rect`，没有把终态深度写死在前端；每次点击“实际求解”都会提交当前工艺参数并刷新高度场、热图、X/Y 截面、遍数增长曲线、事件快照和 A/B 对比。

基线提交：`67fabd7`。本次前端修改提交后，运行元数据中的 `working_tree_dirty` 仅反映提交前生成的验收记录，求解器代码未被前端替换。

## 浏览器实跑

服务地址：`http://127.0.0.1:8787/demo.html`。材料使用 YSZ 工艺卡，计算模式为逐脉冲参考实现（`acceleration=off`）。两次求解均由真实后端完成，并产生 `config.json`、`metadata.json`、`diagnostics.json`、`statistics.csv`、`profiles.csv`、`events.csv`、`final_surface.npz` 与快照索引。

| Run ID | 主要输入变化 | 事件数 | ROI 平均深度 | ROI 最大深度 | ROI 去除体积 |
| --- | --- | ---: | ---: | ---: | ---: |
| `demo_rect_20260919T114905_m72j` | 20 µm 区域，1 遍，hatch=2 µm，40 mm/s | 220 | 100.725 µm | 151.490 µm | 40,289.9 µm³ |
| `demo_rect_20260919T115011_23hz` | 其他输入保持不变，hatch 改为 4 µm | 120 | 44.284 µm | 103.578 µm | 17,713.7 µm³ |

页面验收同时确认：

- 第一次运行显示实时求解状态、核心求解耗时、事件计数和 3 个快照；
- 点击“固定当前终态为 B”后，第二次运行能够生成 A/B 差异，页面报告 ROI 平均深度差 `-56.441 µm`；
- 编辑参数后页面保留上次已提交结果并提示“点击实际求解后才更新”，避免把未提交输入误当成求解结果；
- 快照包含中间状态和终态，可在时间轴中回放；
- 后端警告原样呈现，包括动态孵化历史、尾部截断和材料卡证据状态；页面没有把这些警告包装成精度承诺。

验收产物位于被 `.gitignore` 忽略的 `runs/demo_ui_acceptance/`，不进入源代码提交。当前页面默认主线已切换为正式 200 µm / 4 遍 / hatch=2 µm / 20 mm·s⁻¹；20 / 60 µm 仍可手动输入用于快速联调。正式大区域运行应按分钟级预算安排。本次浏览器验收使用小区域以验证交互、真实递推和可回放链路。

页面已移除演示预设选择器，直接提交当前工艺输入。新增“历史结果”列表：通过 `/api/runs` 扫描已落盘运行，通过 `/api/runs/<run_id>` 只读载入历史高度场和快照；浏览器已验证历史运行 `demo_rect_20260919T115011_23hz` 能恢复加工区均深 44.284 µm、最大深度 103.578 µm 和体积 17,713.7 µm³。

## 自动化检查

- `node` 语法检查：通过；内联脚本可解析。
- `python -m pytest -q -m slow tests/test_webcontract.py::test_http_health_and_static`：`1 passed`。
- `python -m pytest -q tests/test_demo_laser_overrides.py tests/test_p0_optimization.py::test_actual_demo_uses_shared_background_and_rectangle_roi`：`13 passed`。
- 全套回归：`python -m pytest -q` → `749 passed, 1 skipped, 1 xfailed in 68.24s`。

## 结论与剩余边界

演示目标已经达到：路径事件由后端生成，形貌由逐事件状态递推得到，前端展示真实结果并支持快照回放、剖面核对和 A/B 比较。当前页面不宣称四材料真实分相或神经闭合模型已经完成训练；材料卡的证据等级、尾部截断和动态孵化等限制继续显示在结果中。
