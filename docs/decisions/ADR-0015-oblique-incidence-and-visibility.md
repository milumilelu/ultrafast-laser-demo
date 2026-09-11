# ADR-0015：批次 J 的斜入射、投影、可见性与法向厚度转换决定

* 状态：已采纳｜日期：2026-09-11｜批次：J（T18、T19）

## 背景

执行细则第 9.2 节与任务书第 6.6 节把批次 J 定义为「斜平面；方向转换；限定曲面与可见性；
对照展示」，必交「G07 报告、完整示例包」，并附带 M3 放行（细则第 2 节：M3 = V0.2 冻结几何批量
与动态角度两个增强，要求 G07、G08 通过且有实测误差与性能报告）。

原文关键约束：

* 「传播向量 ``k`` 指向传播方向；``mu=max(0,-k·n)``；轴向距离与横向距离从当前表面点和焦点
  求得；``F_s=mu*F_perp``。」
* 「上式自然包含平面斜入射的椭圆投影，**不再额外重复缩放光斑/乘同一余弦**。」
* 「若核明确输出法向后退厚度 ``a_n``，固定 ``(x,y)`` 的一阶高度更新为
  ``dh=-a_n/n_z``，**不是** ``-a_n*n_z``。」
* 「**仅对浮点舍入导致的极小负 ``r²`` 截为零，明显负值视为实现错误**。」
* 「初始支持条件设为 ``n_z>=0.5``、入射角不超过 60°，作为**软件数值/展示范围，不作为
  七类材料的物理边界**。……超过时**停止该模式并给出位置与原因，不裁剪角度继续运行**。」
* 「曲面必须具备受验证的无自遮挡约束或**首次交点可见性**算法；不允许任意曲面静默忽略遮挡。」
* 「没有依据就只做几何修正；**不能给透明介质无条件设 ``A=1-R``**。」

## 决定一：显式固定符号约定，并给出与原文的等价关系（**最重要**）

**本工程取 ``mu = max(0, k·n)``**，而不是原文的 ``max(0, -k·n)``。理由与等价性：

* 既有正入射核（批次 A–I，345 项测试锁定）取 ``direction_unit=(0,0,1)`` 且
  ``s = (q-q_f)·k = h - z_f``，即把 ``k`` 当作**光轴正向**、指向**光源侧**；
* 要使正入射被照射，必须 ``k·n = +1 > 0``，故入射余弦取 ``+k·n``；
* 与原文只差 ``k`` 的整体符号：令 ``k' = -k``，则 ``-k'·n = k·n``。两者**数值等价**。

**落地约束**：`laser.direction_unit` 的 ``z`` 分量必须为正（``k_z>0``），
否则在 `validate_run` 报 `GEOMETRY_UNSUPPORTED` 并指出该约定。

等价性由 G07 的两条断言锁定：0° 与既有正入射核**逐位一致**；60° 时 ``mu=cos60°=0.5``。

## 决定二：``F_s = mu * F_perp``，且**只乘一次**余弦

* ``s = (q-q_f)·k``；``r² = |q-q_f|² - s²``；``F_perp = 2E/(π w(s)²)·exp(-2r²/w(s)²)``；
  ``F_s = mu·F_perp``。
* 横向距离用**光轴到点的距离**（不是 x/y 平面距离），因此斜入射时等 ``r`` 线在水平面上
  自动成为椭圆，长短轴比恰为 ``1/cosθ``（60° → 2）——**不需要**再缩放光斑或再乘一次余弦。
* 能量账本用**投影面积元** ``dx·dy``：``ΣF_s·dx·dy = E_p``（完整平面），
  因 ``∫μF_perp dA_surf = ∫F_perp dA_perp = E_p``。

## 决定三：可见性用**首次交点射线检查**，向量化实现 + 平坦快速路径

* 从每个表面点沿 **``+k``**（朝光源侧）步进，射线落到表面之下即判被遮挡；
  射线离开计算域即停止，**不做域外假设**。
* **平坦快速路径**：窗口内起伏 ≤ 一个步长时直接返回全可见（解析结论：平面无自遮挡）。
  这是绝大多数算例的路径，代价接近 0。
* **向量化**：按步批量推进所有射线（每步一次数组运算），而非逐单元 Python 循环。
  实测全网格 161² 从 7150 ms 降至 **132 ms（54×）**。
* 可见性只在**局部照射窗口**内计算，不对全网格无条件启用。

## 决定四：法向厚度 → 高度用 ``dh = -a_n/n_z``（一阶），并给出推导

任务书原文为 ``dh=-a_n/n_z``。推导（一阶）：

表面 ``z=h(x,y)``，外法向 ``n=(-h_x,-h_y,1)/W``，``W=√(1+h_x²+h_y²)``，``n_z=1/W``。
沿 ``-n`` 平移 ``a_n`` 后，对固定 ``(x,y)`` 有

```
h' - h = a_n·(n_x h_x + n_y h_y) - a_n n_z
       = a_n·[-(h_x²+h_y²)/W - 1/W]
       = -a_n·W = -a_n / n_z
```

正入射 ``n_z=1`` 时退化为 ``dh=-a_n``，与批次 A–I 完全一致。

**代码里的两个函数必须分清**（这是易错点）：

| 函数 | 返回 | 用途 |
|---|---|---|
| `normal_thickness_to_vertical_depth(a_n, n_z)` | ``+a_n/n_z`` | 高度场主循环的 ``cand_arr`` 是**去除量**（正值） |
| `normal_thickness_to_height_drop(a_n, n_z)` | ``-a_n/n_z`` | 语义上的 Δh（负的高度变化） |

触发条件（**不自动转换**，任务书 6.6）：仅当响应核**明确声明**
``depth_direction == "surface_normal"`` 且窗口内 ``n_z`` 确实偏离 1 时才转换；
每次转换计入诊断 ``normal_thickness_conversions`` 并写入 ``metadata.approximations``。

## 决定五：角度/``n_z`` 支持范围是**软件范围**，超范围即停并报位置

* 常量单一权威来源：`geometry.MIN_NZ = 0.5`、`geometry.MAX_INCIDENCE_DEG = 60.0`；
  `config` 只做转出（`MIN_SUPPORTED_NZ` / `MAX_SUPPORTED_INCIDENCE_DEG`），不另写一份。
* 超出时**停止**并给出**首个越界单元下标**、``n_z``、入射角与可读原因；
  **不裁剪角度继续运行**。
* **背向（``μ ≤ 0``）不算越界**：它几何上就不受直接照射（零照射），是合法情形。
  只有"确实被照到但角度过陡（``0 < μ < cos60°``）"才属于超范围。
  这条区分由 G07 的背向用例锁定。

## 决定六：两条新红线在配置层拦截

| 组合 | 错误码 | 理由 |
|---|---|---|
| 斜入射 × `structured_interface` | `CONFIG_INVALID` | 法向去除路径与垂直相列路径不得混用（与既有「动态角度 × 分相」同源，细则 8 节末） |
| 分组批量 × 斜入射 | **回退**（不报错） | 窗口内可见性会随烧蚀形貌变化，而块内几何被冻结 → 块级无法保持遮挡判定一致；按细则 9.1「只允许受支持路径」回退参考模式并写明原因 |

既有红线继续生效：动态角度 × 分相（`CONFIG_INVALID`）、分组 × 动态角度（`CONFIG_INVALID`）。

## 决定七：**只做几何修正**，不预测吸收差异

* 未提供材料/波长的 ``A(θ)`` 依据时**只做几何**（投影 + 可见性 + 法向厚度换算）。
* 界面**不提供**"材料偏振吸收预测"开关；也不给透明介质无条件设 ``A=1-R``。
* 诊断与界面文案必须写明这一边界，避免读者把几何修正读成吸收/热学预测。

## 后果

* **好消息**：0° 严格退化（逐位一致）；60° 的椭圆比、中心能流减半、能量守恒、
  ``Δh=-a_n/n_z`` 四项都有独立断言（`tests/test_incidence_geometry.py`，23 项）；
  可见性有解析可判别构型（迎光侧遮挡）。
* **代价**：批量第一版不支持斜入射（回退参考）；可见性只做首次交点（不做多次反射/衍射）；
  曲面限于 2.5D 高度场（无悬垂/多值表面）。
* **不承诺**：不声称对角度的物理验证；``n_z ≥ 0.5``、``≤60°`` 只是软件展示范围。

## 相关修订（本批一并处理）

* `geometry.py`：从占位改为完整实现（`surface_normal` / `analytic_plane_normal` /
  `incidence_cosine` / `check_geometry_range` / `project_fluence` /
  `first_intersection_visibility` / `visibility_summary` /
  `normal_thickness_to_vertical_depth` / `normal_thickness_to_height_drop`）。
* `beam.py`：放开斜入射准入；新增 `axial_and_lateral`（``s``/``r²``，含负值判定）、
  `is_axial_direction`、`oblique_window_radius`；`FluencePatch` 增
  `oblique/dynamic_angle/mu/nz/visibility/intercepted_energy_plane_J`。
  **纯正入射走既有分支，逐位不变**（`axial and not dynamic`）。
* `config.py`：`GridConfig` 支持 `initial_surface="tilted_plane"` + `initial_slope_x/y`
  （无量纲斜率，含范围校验）；`validate_run` 放开斜入射与 `dynamic_angle`，
  新增「斜入射 × 分相」红线与方向约定校验。
* `surface.py`：`SurfaceState.initialize` 支持解析斜平面初值。
* `solver.py`：传 `dynamic_angle` 到光束；法向厚度转换；新增
  `diagnostics["geometry"]` 与两条 ``metadata.approximations``。
* `accelerators.py`：`check_fallback_conditions` 增 `oblique_incidence` 触发回退。
* `tests/test_validation_edges.py`：旧断言「斜入射一律拒绝」（M0 行为）已过时，
  改写为「范围内放行 / 超范围拒绝 / ``k_z≤0`` 拒绝」。
* 新增示例：`examples/oblique_plane_60deg.json`、`examples/tilted_plane_dynamic_angle.json`。
