# 氧化锆 pass 标量闭环标定（首个真实数据子集）

本报告对应真实的光机所 pass 数据，但只对**标量终态深度**做监督。标签来自已经审核过的
`docs/experiments/laser_proc_2026-09-18/depth_vs_passes.csv`，工艺参数来自
`docs/experiments/design_tables_pulse_fs/氧化锆/氧化锆_pass_design.csv`。原始实验来源为
`E:\博士课题资料\光机所实验原始数据\氧化锆\60Pass组.cag`；仓库内保存的是从该原始
高度场审计得到的归纳表。工具 `tools/calibrate_zirconia_pass.py` 按 `(脉宽_fs, 频率, 线间距, 速度)` 连接两张表，每个工艺保留
`N=1..4` 四个终态点；默认剔除 `monotonic=False` 的五条浅槽曲线。它不读取文件编号猜配对，
也不把不同遍数终态相减伪造逐事件标签。

## 生成的真实长表

运行：

```powershell
$env:PYTHONPATH='src'
python tools/calibrate_zirconia_pass.py --write-long --max-groups 0
```

结果为 10 个单调工艺、40 个终态点：

- `runs/calibration/zirconia_pass_scalar_long.csv`
- `runs/calibration/zirconia_pass_scalar_long.audit.json`

## 已执行的分组真实数据标定

为了让数值成本可控并保留留出意义，本次先选取按排序得到的 3 个单调工艺条件：

```text
(223 fs, 20 kHz, 4 μm, 25 mm/s)
(223 fs, 40 kHz, 4 μm, 20 mm/s)
(500 fs, 10 kHz, 8 μm, 5 mm/s)
```

其中一整条工艺曲线作为留出组，训练组和留出组均按工艺条件分组，避免把同一工艺的不同遍数拆到两边。求解域为 `80×80 μm`、步长 `1 μm`；实际蛇形加工区为 `60×60 μm`；监督量为中央 `60×60 μm` 窗口的中位深度。材料卡不改写，因真实批次为 `223–4000 fs` 而原卡文献协议为 `208 fs`，运行模式显式设为 `synthetic_demo`。

结果文件：

- `runs/calibration/zirconia_pass_first_pass_gain.json`
- `runs/calibration/zirconia_pass_first_pass_report.json`

结果如下：

| 项目 | 标定前 | 标定后 |
|---|---:|---:|
| 训练组，8 个终态点 MAE | 142.56 μm | 14.09 μm |
| 训练组，8 个终态点 RMSE | 178.13 μm | 15.99 μm |
| 留出工艺组，4 个终态点 MAE | 243.82 μm | 133.39 μm |
| 留出工艺组，4 个终态点 RMSE | 244.28 μm | 160.18 μm |

得到的单一递推增益为 `0.2916927713`，没有触及 `[0.05, 2.0]` 边界。增益放在每次事件表面提交之前，后续脉冲会按更新后的表面重新计算离焦，因此这里不是终态深度乘系数。

可复现命令：

```powershell
$env:PYTHONPATH='src'
python tools/calibrate_zirconia_pass.py --run --max-groups 3 --holdout-groups 1 `
  --window-um 80 --dx-um 1 --machining-region-um 60 `
  --observation-kind center_window_mean --observation-window-um 60 `
  --observation-statistic median `
  --out-result runs/calibration/zirconia_pass_first_pass_gain.json
```

这次留出误差仍然很大，结论是：现有材料卡加一个全局标量增益还不能解释脉宽、频率、搭接和遍数之间的工艺差异；下一步应引入按工况受限的效率闭合或补充同脉宽单脉冲锚点，而不是把 `0.2917` 当作材料常数。

## 已执行的闭环 smoke 标定

为确认标定参数确实进入递推几何更新，另选取一个真实终态点做可运行性 smoke：

| 脉宽 | 频率 | 线间距 | 速度 | 遍数 | 实测深度 |
|---:|---:|---:|---:|---:|---:|
| 1000 fs | 40 kHz | 6 μm | 25 mm/s | 1 | 18.25 μm |

计算域为 `200×200 μm`，网格步长 `4 μm`，使用同一弓字形路径和固定原始焦面。基线预测为
`65.677188 μm`；将正增益放到逐事件表面更新前后，求得：

```text
gain = 0.40945650779757986
after MAE = 3.37e-9 μm
```

完整 JSON 记录在 `runs/calibration/zirconia_pass_single_point_gain.json`。这是一个**单点闭环
可运行性验证**，没有留出集，不能当作材料泛化精度或最终标定结果。

## 适用域说明

材料卡 `zirconia_ysz_machining_effective_n3.json` 的文献协议是 208 fs，而本 pass 批次为
223–4000 fs；因此脚本显式使用 `run_mode=synthetic_demo` 绕过不匹配的文献协议门禁。卡内
孵化响应仍由求解器按其声明自动启用。这个结果只能称为**工程有效标量闭合**，不升级原卡的
证据状态，也不修改原始材料卡。要得到可报告的多工艺标定，应补充同一材料/本机脉宽下的
单脉冲锚点，并在 10 个单调工艺上运行分组留出（按工艺条件分组，不能按 N 随机拆分）。
