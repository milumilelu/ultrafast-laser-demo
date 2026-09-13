# 陶瓷 / 玻璃文献算例回放（U08）

- 数据：`data/measured/ceramics_dot_line_measured.csv`（来源 D02，doi `10.3390/ma15103614`）
- 条件：1030 nm、290 fs、10 kHz（原文 Table 1 / Table 2）
- 共 **4** 组：两类材料 × （点坑 / 交叉线）

> 本报告是**已知条件回放**：展示论文表格中的**实测条件与实测结果**，**不经过模型**。它不是任意工况的预测。

## 1. 算例总表

| 算例 | 材料 | 图形 | 峰值能流 (J/cm²) | 计数 | 深度 (μm) | 线宽 (μm) | 来源定位 |
|---|---|---|---:|---|---:|---:|---|
| D02-glass_dot | IPS e.max CAD HT lithium-disilicate glass-ceramic | dot | 8 | 10 | 6.3 | — | Table 1 |
| D02-zirconia_dot | KATANA UTML 6Y-PSZ | dot | 4 | 20 | 7.4 | — | Table 1 |
| D02-glass_cross | IPS e.max CAD HT lithium-disilicate glass-ceramic | crossed_line | 8 | 20（等效） | 8.8 | 21.5 | Table 2 |
| D02-zirconia_cross | KATANA UTML 6Y-PSZ | crossed_line | 8 | 20（等效） | 7.1 | 22.7 | Table 2 |

## 2. ⚠️ 三条必须保留的边界声明

1. **等效脉冲数不是真实脉冲数**：交叉线的 `equivalent_pulse_count` 是按协议折算的量（展示标签：`等效脉冲数（非真实事件序列）`），**不是**真实事件序列。**不得**把该工况的累计深度除以 N 后声明成「可用于任意扫描的脉冲核」——那会把一个已知条件的结果伪装成通用响应。
2. **没有实测三维形貌**：本批数据只有端点（坑深、线宽、直径），因此只呈现散点与误差；任何由端点重建的图层**必须**标注「假设截面形状重建」，且**不得**反向用于训练模型或计入实验数据。
3. **回放 ≠ 预测**：算例来自论文表格，不经过本仓库的模型；模型对照若叠加，须与实测点分开呈现。

## 3. 权限（与 U05 注册表一致）

| 算例 | observation_access | increment_access |
|---|---|---|
| D02-glass_dot | True | False |
| D02-zirconia_dot | True | False |
| D02-glass_cross | True | False |
| D02-zirconia_cross | True | False |

全部算例 `increment_access = False`：它们报告的是**端点几何**，不是逐事件增量。要进主循环须走 U07 的 `TabulatedEventLaw` + 增量语义曲线。

## 4. 复现

```bash
python tools/ceramics_cases_report.py
```
