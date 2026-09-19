# 显式路径递推与受限效率闭合

当前仓库的 `src/ufdemo/solver.py` 已经是显式事件驱动求解器：`paths.iter_events`
按照统一脉冲时钟生成真实出光事件，`beam.beam_patch` 读取事件开始时的表面，
响应核生成一次事件的局部去除增量，最后由 `SurfaceState.apply_increment` 一次提交。

本版本增加了一个可选的净去除效率闭合入口：

```python
from ufdemo.closures import ConstantEfficiencyClosure, TorchEfficiencyMLP
from ufdemo.solver import solve

result = solve(config, material, efficiency_closure=ConstantEfficiencyClosure(1.0))
```

当前这是研究用 Python 接口，尚未接入 YAML/CLI/WebUI 的模型权重加载；因此不会把一个未训练
网络悄悄加入现有演示流程。

闭合模型位于 `src/ufdemo/closures.py`。它接收当前事件开始状态构成的无量纲特征，
输出

\[
\eta_\phi=\exp(r_{\max}\tanh f_\phi),
\qquad
\Delta d=\eta_\phi\,\Delta d_{\rm phys}.
\]

因此有四条约束：

1. 机理对数核在阈值以下给出的零增量仍为零，网络不能凭空产生槽；
2. 新网络的最后一层零初始化，初始状态严格退化为 `eta=1` 的机理基线；
3. `eta` 始终为有限正值，并由 `r_max` 限定上下界；
4. 修正发生在几何方向转换之后、`apply_increment` 之前，后续事件会读取修正后的表面。

特征只使用当前事件和事件开始状态：局部能流/阈值比、深度/瑞利长度、径向位置/光斑半径、
实际去除事件计数、照射窗口计数、脉宽、当前加工遍数和相邻出光事件时间间隔代理。当前
`FluencePatch.spot_radius` 是窗口尺度（动态离焦时为窗口内最大局部半径），所以径向特征是
窗口归一化代理，不是每个网格点的局部束腰。这个时间
间隔是事件级代理，不是每个网格点自上次受照以来的局部记忆时间；如果将来加入热或颗粒状态，
应在 `SurfaceState` 中增加逐点最后受照时间。没有使用计划的
最终遍数、终态深度或未来事件。

当 `solver.mode=grouped` 但传入 `efficiency_closure` 时，求解器会明确回退到
`reference`，因为块级冻结状态不能保持逐事件闭环语义。回退原因和闭合元数据写入运行结果。

## 训练边界

`TorchEfficiencyMLP.fit` 只接受显式的逐事件效率目标。仓库当前没有同一槽的逐脉冲前后高度场
配对，因此不能把不同遍数的终态高度图相减后伪造成逐事件标签。氧化锆的
`depth_vs_passes.csv` 可以用于组内终态闭环损失，但需要单独实现观测算子和按工况分组交叉验证；
它是同条件独立端点序列，不应自动解释为同一槽的逐遍增量。SiC、CFRP 和 AlSiC 的高度场在
配对、像素标定或材料响应卡方面仍存在阻塞：SiC 索引中的像素标定与说明冲突，AlSiC 的
006–008 使用不同网格，CFRP 的实验遍数为 1–6 而不是 1–4。不能据此宣称材料逐事件参数已经完成验证。

热积累、颗粒遮蔽和再沉积状态尚未加入。它们应在有能够区分时间效应的实验后作为独立的状态
扩展；当前闭合只表示“净去除效率修正”，不应解释为等离子体透过率。

## 标量终态的折内闭环训练

`src/ufdemo/closure_training.py` 提供一个不依赖 pandas 的最小训练接口。调用方必须提供
已经测得的标量表，每行包含 `condition_key`、`RunConfig`、`MaterialSpec` 和目标值（可选
`metric` 与正权重）。同一个 `condition_key` 的所有行会进入同一折：

```python
from ufdemo.closure_training import ScalarObservationTable, fit_fold_aware

table = ScalarObservationTable.from_rows(measured_rows)
cv = fit_fold_aware(table, n_splits=5, seed=0, bounds=(0.1, 10.0))
print(cv.oof_rmse)
```

当前训练器只拟合一个有界的常数净效率因子。每个候选因子都会重新调用
`solve(..., efficiency_closure=ConstantEfficiencyClosure(eta))`，所以因子参与递推并影响后续
离焦；它不会在终态深度上事后相乘。搜索是在 `log(eta)` 上进行的确定性有限网格细化，而不是
对求解器假装可微。训练器拒绝缺失/非有限标签、空表和混合标量指标，也不会从高度场差分或
终态序列伪造中间事件标签。`FoldAwareTrainingResult.metadata` 会显式记录这些边界。

## 高度场观测适配器

`src/ufdemo/observations.py` 的 `load_height_field` 只负责读取原始 `z`、NaN 掩膜、物理像素
尺寸和索引元数据。它使用 `allow_pickle=False`，严格检查数组形状，不转置、不填补无效点，且
不从文件名推断材料或遍数。`derive_observation` 才显式选择参考面、线性/二次去趋势、深度符号、
阈值、ROI 和腐蚀像素，并返回可复核的处理数组与均值/中位数/P10/最大深度/体积统计。

高度场记录仍属于端点观测层；只有经过人工确认的配对、参考面和统计口径，才可以把摘要中的
标量放进 `ScalarObservationTable`。原始相对高度和未确认的跨材料配对不会进入事件响应核。
