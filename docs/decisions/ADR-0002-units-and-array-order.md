# ADR-0002：单位、数组方向与坐标约定

* 状态：已采纳｜日期：2026-09-10｜批次：A（T01）

## 决定

1. **单位**
   * 物理模式内部统一 SI（m、s、J、J/m²）。换算常量：
     `1 J/cm² = 1e4 J/m²`、`1 μm = 1e-6 m`、`1 fs = 1e-15 s`
     （`config.py` 中的 `J_CM2_TO_J_M2`、`UM`、`FS`）。
   * 合成模式内部统一无量纲：`x/L_ref`、`h/L_ref`、`w/L_ref`、`zR/L_ref`、
     `F/F_ref`，能量为 `E/(F_ref·L_ref²)`。由 `UnitContext` 统一换算，
     禁止在业务代码里散落手写换算。
2. **数组方向**：单元中心规则网格，形状固定 `(ny, nx)`；第 0 维对应 y，
   第 1 维对应 x（`GridConfig.AXIS_ORDER`）。x/y 严格递增。
   配置显式记录轴顺序，**不靠绘图转置修补求解数据**。
3. **数据类型**：主场 `float64`；`phase_id` 为 `uint16`；计数为 `uint32`
   并在自增前检查溢出（`surface.py` 的 `UINT32_MAX`）。
4. **坐标原点**：只支持 `origin = "cell_center"`，由 `center_x_m/center_y_m`
   与网格间距确定；外边界 = 中心坐标 ± 半个网格。

## 后果

* `UnitContext.allows_physical_depth_export` 在合成模式下为 `False`，
  导出与截面标签使用 `delta_ref` / `L_ref`，**不会**出现 `depth_um`。
* 求解精度与显示降采样分开记录；所有统计从求解场计算。
* `config.py` 中 `GridConfig.axis()` 是唯一生成坐标轴的入口。
