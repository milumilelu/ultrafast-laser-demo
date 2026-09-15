# ADR-0019：路径连续性与加工区口径（用户审查 2026-09-15 的第 3、4 条）

* 状态：已采纳｜日期：2026-09-15｜批次：L

## 背景

外部审查（针对 `e4c1106`）指出路径与规划口径的几处缺陷。逐条在代码里**复现确认**后：

| 审查条目 | 复现结论 |
|---|---|
| 弓字形遍间断点 | **成立**。200×200、h=8、N=2（26 条偶数线）段间跳 **200 µm** |
| 关光策略与文案矛盾 | **成立**。策略写 `shutter_off_capability="not_assumed"`，代码却把换向段标 `emitting=False` |
| 非正方形被静默压方 | **成立**。`machining_region_um=float(region_um[0])` 丢第二维；统计按第一维双向裁 |

## 决定一：遍间换向必须回到**本遍起点侧**，并加不变式检查

每一遍都从 **x0** 重新开始（`left_to_right` 按 li 奇偶、**每遍重置**），
所以遍间回程**必须**回到 `x0`。原实现按"继续蛇形"写成
`x0 if n_lines % 2 == 1 else x1` —— **偶数条扫描线时回到 x1**，与下一遍起点（x0）
相差 200 µm。奇数条线（h=10 → 21 条）恰好正确，所以这个 bug 只在一半情形出现。

**同时加一条硬不变式**（审查建议的验收方式）：在 `serpentine_plan` 返回前逐段检查

```
上一段终点 == 下一段起点（含换向段与遍间段）
```

不等即 `CONFIG_INVALID`（报哪一段、实际值、要求值）。理由：缺的那段位移**不会**让结果看起来
异常，却会改变下一遍的**激光时钟相位** —— 这类静默偏差必须直接失败。

实测：3 种区域 × 6 种间距 × 4 种层数 = **72 个组合，0 处不连续**。

## 决定二：关光策略显式声明，不再自相矛盾

`DEVICE_PATH_POLICY` 增加 `shutter_off_during_turns: True` 与
`shutter_state_basis: "assumed_ideal_shutter_not_confirmed_against_device"`，
文案改为明说：换向/遍间段按**理想关光**处理是**模型假设**，
设备是否支持关光空走**尚未确认**；若不能关光，需把那些段改成出光并重算。
删掉原来那句"导出不依赖设备不存在的关光能力"（与实现相反）。

**不做**：不擅自把换向段改成出光 —— 那会改变所有既有算例的剂量，且设备能力未知。
先把它变成**可审计的假设**，等确认后再切。

## 决定三：加工区支持非正方形，两个方向都必须用上

* `PredictionSpec.machining_region_um` 接受标量（正方形）或 `(wx, wy)`；
  新增 `region_size_um()` 统一解析，**长度必须恰好 2**（`(1,2,3)` 不再被静默截断）。
* `build_row_config` 把 `(wx, wy)` **同时**用于蛇形路径与域校验（两维都 ≤ 域）。
* `evaluate_candidate` 的统计裁切改为**双向** `n_x × n_y`
  （原来只按第一维双向裁 ⇒ `(120, 60)` 的指标实际算在 `120×120` 上）。
* 结果侧新增 `CandidateResult.machining_region_size_um` 与
  `machiningRegionSizeUm`（`machiningRegionUm` 保留为第一维，兼容既有调用方）。

## 决定四：细核之后**重新判可行、重新排序**

原实现只检查"细核有没有生成形貌"（`if rec_with_surface.surface is not None:`），
**从不重判可行** ⇒ 会出现

```
粗网格：可行 → 细网格：不可行 → 仍然把它当推荐
```

反向同理：粗筛全否、细核通过时，也继续沿用"无可行方案"。

改为：按粗筛排序取前 `fine_top_k`（默认 3）个用**最终精度**复核，
**复核后重新判可行**，第一个可行者为推荐；粗筛全否时则按"违规最小"取前几个复核
（允许由否转可）。若无可行者，`infeasible_reason` 明说
"**细网格复核后不满足约束**（已复核 N 个）"，并写进 notes（`nFineRechecked`）。

不变量：`recommended is feasible_candidates[0]`（界面推荐 = 表格第一行）仍成立 ——
复核结果**替换回候选列表**而不是留游离对象。

## 验证

pytest **717 passed + 1 xfailed**（`634 + 新增 tests/test_path_and_region_integrity.py 83 条`）。
新测试直接采用审查给的验收断言：

* `test_every_segment_starts_where_the_previous_ended`：
  **6 种间距 × 4 种层数 × 3 种区域 = 72 组合逐一比对段端点**；
* `test_the_reported_break_case_is_now_continuous`：审查点名的算例（26 条偶数线）；
* `test_non_square_region_is_scanned_in_both_dimensions`、`region_size_um` 的两种形状与越界拒绝；
* `test_shutter_assumption_is_declared_not_contradicted`：
  策略必须显式声明假设，且**不得**再出现原来那句自相矛盾的文案；
* `test_recommendation_is_always_from_the_final_precision` /
  `test_two_level_grid_rechecks_and_reports`：推荐必须可行、与 `feasible_candidates[0]` 同实例、
  且如实报告复核个数。

## 本 ADR 不覆盖的审查条目（单独决策）

审查的第 1 条（**光学参数随拟合参数漂移**）、第 2 条（标定用 20 µm 小方块 +
统计区域口径）、第 5 条（覆盖率 ≠ 达标面积）、第 6 条（上游对照工况未匹配）、
第 7 条（增益未与材料/基线绑定、train/holdout 分组泄漏）均**未在本轮改动**：
它们要么改变物理/标定口径（需先定方向），要么是独立的重构。
