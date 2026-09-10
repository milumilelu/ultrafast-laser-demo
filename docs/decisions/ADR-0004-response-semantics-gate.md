# ADR-0004：响应语义闸门与“一次提交”规则

* 状态：已采纳｜日期：2026-09-10｜批次：B（T05）

## 决定

1. **语义闸门**：只有 `output_semantics = "event_depth_increment"` 可以进入
   逐事件增量主循环。以下语义一律拒绝，**即使数组形状吻合**：
   `mean_depth_per_effective_pulse`、`cumulative_depth`、
   `track_or_pass_depth`、`volume_per_energy`、`threshold_only`。
   拒绝发生在两处：`FixedThresholdLogLaw.__init__`（快速失败）与
   `IncrementResult.validate()`（数组级复核），错误码 `RESPONSE_SEMANTICS_INVALID`。
2. **阈值展示与逐事件去除分派分离**。`ThresholdEvaluator` 不产生深度，
   也不与深度核共用“零 δ 伪实现”。`threshold_only` 模式下体积与深度统计写
   `null`，界面/CLI 显示“不提供”，**不以数值零暗示已经预测无去除**。
3. **先掩膜后取对数**：`a = delta·max(ln(F/Fth), 0)`，先 `F > Fth` 掩膜再取
   `log`，避免 `ln(0)`。
4. **一次提交**：同一事件的高度、受照计数、暴露相在 `apply_increment` 中
   一次性写入；事件开始时读取状态，事件内不重算几何。
5. **非法输出终止运行**：出现 NaN/Inf 或负去除量时抛
   `NUMERIC_NONFINITE` / `RESPONSE_SEMANTICS_INVALID` 并保留失败状态，
   **禁止**用 `nan_to_num` 隐藏问题。

## 后果

`IncrementResult` 必带 `output_semantics`、`depth_direction`、`unit_mode`；
主循环对 `depth_direction` 缺失同样报错（不允许把法向向量的 z 分量
随手当作高度更新比例）。
