# 氧化锆脉宽条件化真实数据标定

- 数据行：60（15 个工艺条件 × 4 遍）
- 脉宽：[223.0, 500.0, 1000.0, 2000.0, 4000.0] fs
- 非单调工艺条件：5 条（保留并标记）
- 观测口径：audited depth_vs_passes mean depth, endpoint pass N=1..4; design joined by pulse duration/frequency/hatch/speed/pass
- 留出：4000.0 fs 整档留出，其余脉宽训练

## 校准模型

四参数幂律闭合：参考阈值倍率、参考去除尺度倍率、阈值脉宽指数、去除尺度脉宽指数。每个目标函数评估均调用真实递推求解器。

- 模型：`ysz_pulsewidth_effective_v1`
- 可选卡：`data/calibrations/zirconia_ysz_pulsewidth_calibrated_effective.json`（原始 `data/materials/zirconia_ysz_machining_effective_n3.json` 保持不变）
- 参考脉宽：208.0 fs
- 阈值指数：0.905748
- δ 指数：-0.216002
- 训练 MAE：13.4125 μm
- 留出 MAE：11.2886 μm

## 证据边界

该模型是同批终态实验数据拟合出的工程有效闭合，不升级原始 208 fs 文献卡，也不等价于独立单脉冲材料常数。有效域仅为数据覆盖的 223–4000 fs；域外由配置层拒绝。训练/留出误差较大时应回到光学能量、观测口径和材料批次配对继续核查。


## 网格检查

代表工况以 dx=2 μm 与 dx=1 μm 对照，最大相对变化 22.969%，平均 13.579%。这只是数值收敛检查；变化仍偏大时，应在正式标定/预测中继续细化网格，不能把 dx=2 μm 结果解释为最终精度。

