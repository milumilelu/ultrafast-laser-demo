# 界面操作检查（批次 E / T09，G09；批次 F / T10 增补查表检查）

- 生成时间（UTC）：2026-09-13T11:08:28.470000+00:00
- 代码版本：commit=1a777e4fb717d2b565144d162c947c456653971f｜工作区有改动=True
- 驱动方式：`streamlit.testing.v1.AppTest` 真实执行 `app.py`
- 汇总：通过 24｜失败 0｜未运行 0
- 探针输出目录：`C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\_ui_probe`（由 `UFDEMO_RUNS_DIR` 隔离，不污染工作区 `runs/`）

## 检查项

| 检查 | 预期 | 实测 | 状态 | 说明 |
|---|---|---|---|---|
| 初始渲染无异常 | 无异常 | 无异常 | 通过 | 六标签页：参数与运行 / 结果 / 参考评估器 / 查表 / 七材料能力入口 / 历史运行 |
| 初始 solve_count | 0 | 0 | 通过 | 未提交前不得求解 |
| 初始无冻结结果 | frozen=None | frozen=None | 通过 |  |
| 提交计算：solve_count 恰好 +1 | 1 | 1 | 通过 | run_id=ui_run_20260913T110256_pv3n｜status=completed |
| 改参数未提交：旧结果标为「上一次运行」且不求解 | is_stale=True｜solve_count=1｜界面出现「上一次运行」 | is_stale=True｜solve_count=1｜label=上一次运行（2026-09-13T11:02:56.438504+00:00） | 通过 | 执行细则 11.3 |
| 改参数后 solve_count 不变 | 1 | 1 | 通过 |  |
| 快照回放 / 切图层 / 旋转视图 / 切截面：solve_count 不变 | 1（不变） | 1 | 通过 | 执行细则 11.3「只旋转视图、切换图层/截面/快照时读取已有数据」 |
| 读取历史运行：read_count +1 且 solve_count 不变 | read_count=1｜solve_count=0 | read_count=1｜solve_count=0｜run_id=ui_run_20260913T110256_pv3n | 通过 | 读取不等于用当前参数求解 |
| 回放水印与导出 watermark.json 逐字段一致 | material_id/run_mode/unit_mode 全部一致 | 导出存在=True｜material_id=analytic_fixture_not_a_material｜run_mode=reference_case｜unit_mode=SI | 通过 | 批次 H：导出=回放，标签不再各写一份 |
| 参考评估器「评估」：不进入逐事件求解、不产生形貌 | solve_count=0｜产出参考结果｜frozen=None | solve_count=0｜有参考结果=True｜frozen=None | 通过 | 参考评估器只做公式核查（批次 D 语义） |
| 查表「查值」：不触发求解、不读取历史 | solve_count=0｜产出查表结果｜read_count=0 | solve_count=0｜read_count=0｜有查表结果=True | 通过 | 批次 F：查表是纯读取，不是求解 |
| 切换曲线卡 / 插值方法：solve_count 始终为 0 | 0（不变） | 曲线数=3｜异常组合=无 | 通过 | 批次 F：换曲线/换算法不得触发求解 |
| 七材料能力入口：全部条目探针实跑通过 | 未核验=0｜探针全通过｜开放/红线/缺口均绑定依据 | 开放=15｜红线=11｜缺口=1[('金刚石', '合成形貌')]｜未核验=0 | 通过 | 批次 H：拦截与缺口都必须有可执行证据，不得只在文档中声称 |
| 金刚石「合成形貌」记为缺口而非开放 | opened 中不含「合成形貌」 | opened=['导入入口', '条件对应阈值'] | 通过 | 规格要求开放但实现未支持，必须如实标为缺口并证明当前打不开 |
| 图层标签不含被禁措辞（严格子串：热影响区/HAZ/温度…） | 全部通过 | 全部通过 | 通过 | 执行细则 11.3 措辞守卫 |
| 缺能力模式显示准确不可用原因（不自动降级、不静默填值） | 拒绝并给出原因 | 允许=False｜原因=材料卡未开放 reference_case 模式；允许：['threshold_only', 'synthetic_demo'] | 通过 | 配置层拦截，而非仅按钮禁用 |
| 合成模式禁止导出物理深度 | 以 depth_um 命名时拒绝；depth 标签非 um | 导出标签=depth/L_ref｜拒绝 depth_um=True | 通过 | 执行细则 4.1：合成深度与微米平面坐标不可混算 |
| threshold_only/参考结果：去除量显示「不提供」而非 0 | removal_available=False | removal_available=False｜run_id=g05_ysz_reference | 通过 | 界面不显示数值零冒充「无去除」 |
| 求解模式可切为「冻结几何分组」并产出批量诊断 | solve_count+1｜acceleration_diagnostics.grouped=True｜含块数与批大小 | solve_count=0→1｜grouped=True｜批大小=5｜块=1｜拒绝/回退=0｜补丁复用率=0.0 | 通过 | 批次 I：细则 12 节要求界面显示实际批大小、拒绝/回退次数与运行时间 |
| 参考模式：加速面板如实说明未启用批量（不假装加速过） | grouped=False｜effective_mode=reference｜含「非全局误差证明」边界说明 | grouped=False｜effective_mode=reference｜boundary_note 存在=True | 通过 | 批次 I：局部误差估计不是全局误差证明，边界必须写明 |
| 几何修正：入射角/动态角度可切换，且支持范围与近似标注可见 | solve_count+1｜斜入射与动态角度均生效｜面板含 n_z/入射角支持范围 | solve_count=0→1｜斜入射=True｜动态角度=True｜光轴夹角=59.99999999999999｜法向厚度转换=0｜支持范围 n_z≥0.5、入射角≤60.0 | 通过 | 批次 J / T19：两增强对照可检查、支持范围与误差标识可见 |
| 正入射：不显示几何修正面板（未启用即不显示） | geometry_diagnostics 为空字典 | 空字典=True | 通过 | 与批次 G/H/I 同口径：未启用不返回假数据 |
| 探针运行输出位置 | 隔离到 C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\_ui_probe | 已写入 371 个运行目录 | 通过 | UFDEMO_RUNS_DIR 覆盖，不污染工作区 runs/ |
| 提交运行目录 | 存在 metadata.json | 存在 | 通过 |  |

## 判定依据

- **提交才求解**：只有「提交计算」按钮调用 `ufdemo.solver.solve`；`ui_service.submit` 是唯一入口，成功一次 `solve_count += 1`。
- **参数编辑态与结果态分离**：可编辑参数在 `SessionState.params`，已提交结果在 `FrozenRun`；图表一律读冻结结果，不读当前表单值。
- **改参数即过期**：提交后再改参数 → `is_stale()` 为真，结果区标为「上一次运行」（执行细则 11.3、任务书 13 节）。
- **回放/视图不求解**：拖时间轴、切图层、旋转/翻转视图、取截面只读已有数组，`layer_view` / `snapshot_arrays` 内有 `assert solve_count 不变` 的硬断言。
- **读取历史不求解**：`read_existing_run` 只增加 `read_count`。
- **参考评估器不求解网格**：批次 D 语义，只复现文献公式与协议量。
- **查表不求解（批次 F）**：读曲线卡、线性/PCHIP 插值、换曲线/换算法，`solve_count` 始终为 0（`ui_service.table_lookup` / `table_grid` 内有硬断言）。
- **措辞守卫**：图层标签严格避免「热影响区 / HAZ / 温度」等被禁词（执行细则 11.3）。
- **不自动降级**：缺能力模式在配置层拦截并给出准确原因，合成示例须用户显式选择。

## 复现命令

```bash
python tools/ui_probe.py
python tools/table_report.py
python -m pytest tests/test_ui_service.py tests/test_app_smoke.py -q
streamlit run app.py
```
