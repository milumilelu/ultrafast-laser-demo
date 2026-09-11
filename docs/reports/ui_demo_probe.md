# 端到端演示可用性检查（前后端对接）

- 汇总：通过 13｜失败 0｜未运行 0
- 探针输出目录：`C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\_ui_demo_probe`（隔离，不污染工作区 `runs/`）

> 本检查回答：**前端（Streamlit）与后端（求解引擎）是否真的对接、能否完整演示一遍**。
> 与 `ui_operation_check.md`（界面记账不变量）互补：那份查「有没有偷偷重算」，
> 这份查「选材料→选模板→提交→出结果→面板渲染→历史读回」整条链路。

## 检查项

| 检查 | 预期 | 实测 | 状态 | 说明 |
|---|---|---|---|---|
| 示例发现 | cfrp_laminated_ply.json 出现在界面示例列表 | 在列表中 | 通过 | 示例由 list_examples 扫目录自动纳入界面下拉 |
| 示例发现 | alsic_particle_composite.json 出现在界面示例列表 | 在列表中 | 通过 | 示例由 list_examples 扫目录自动纳入界面下拉 |
| 逻辑层提交 | cfrp_laminated_ply.json 提交后 status=completed | status=completed｜solve_count=1｜快照=4 | 通过 | 走 ui_service.submit（界面唯一求解入口） |
| 逻辑层提交 | alsic_particle_composite.json 提交后 status=completed | status=completed｜solve_count=1｜快照=2 | 通过 | 走 ui_service.submit（界面唯一求解入口） |
| 只读不重算 | 渲染/切图层/取快照后 solve_count 不变 | 1 -> 1 | 通过 | 图层与诊断均为纯读取 |
| 只读不重算 | 渲染/切图层/取快照后 solve_count 不变 | 1 -> 1 | 通过 | 图层与诊断均为纯读取 |
| 措辞守卫 | 全部图层标签不含被禁词（7 项） | 被禁词表 ('热影响区', 'HAZ', '温度场', '温度分布', '热输入', '温度')；命中 0 项 | 通过 | 标签只描述该图实际是什么量 |
| 措辞守卫（不可用原因） | 不可用原因文案不含被禁词 | 命中 0 项 | 通过 | 如 threshold_mask 如实报不可用，不用无关量凑数 |
| 界面全链路（AppTest） | 首屏无异常 | 异常=0 错误=0 | 通过 | AppTest 真实执行 app.py |
| 界面全链路（AppTest） | 点「提交计算」后无异常且出结果 | 异常=0 错误=0｜solve_count=1｜status=completed｜快照=4 | 通过 | 选 CFRP + synthetic_demo + 铺层模板 → 提交 |
| 界面结果区渲染 | 结果页重跑无异常 | 异常=0 错误=0 | 通过 | 结果区在提交后重新渲染 |
| 分相诊断面板 | 结果区出现「相结构诊断」面板 | 相结构诊断｜laminated_fiber_composite｜算法 structure_v1｜种子 20260911｜相数 2 | 通过 | 面板含目标/实际体积分数、同相合并说明、各相摘要与截断诊断 |
| 历史读取同源 | 读回历史带出同源结构诊断且不求解 | solve_count=0 read_count=1｜结构类型=laminated_fiber_composite｜截断诊断行=6｜phase_id=有 | 通过 | read_existing_run 与实时提交共用 build_structure_diagnostics |

## 判定依据

- **提交是唯一求解入口**：界面「提交计算」→ `ui_service.submit` → `solver.solve`，
  成功一次 `solve_count += 1`；其余操作（切图层/时间轴/截面/结构诊断面板/读历史）只读数组。
- **分相图层与诊断同源**：实时提交与 `read_existing_run` 共用 `build_structure_diagnostics`，
  因此历史读回的结构诊断与实时一致（`solve_count` 保持 0，只增 `read_count`）。
- **跨相截断如实披露**：截断量命名为「未应用候选去除体积」，界面同时给出
  「被截断的事件数 / 单元次数 / 相标签切换次数 / 未应用占比」，并提示属于有损近似。
- **不提供的图层如实报不可用**：`threshold_mask` 显示原因而非数值 0；
  均质运行不显示分相诊断面板，`phase_id` 图层如实报不可用（不返回全 0 假数组）。

## 复现命令

```bash
python tools/ui_demo_probe.py
python tools/ui_probe.py
streamlit run app.py
```
