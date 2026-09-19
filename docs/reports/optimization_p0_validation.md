# P0 与 G-01 验收记录

日期：2026-09-19。仓库基线为当前 `main` 工作树；未创建第二个前端或第二套求解器。

## 行为验证

```text
python -m pytest -q
738 passed, 1 skipped, 1 xfailed
```

`tests/test_p0_optimization.py` 覆盖了孵化的 N=1,2,3,4 索引、照射计数与去除计数
分离、本机实际模式的共享背景装配和矩形加工 ROI。原有分组/参考、响应语义、数据
观测和 Web 契约测试继续通过。

## 实际入口

`processMode=actual` 使用共享背景的物镜后功率 5.3333 W，按
`E_p=P_post_objective/f` 计算脉冲能量，使用名义束腰和推导瑞利长度，保持焦点在原始
表面并启用 `axial_defocus`。材料卡的波长/脉宽适用性校验仍然有效；不匹配时不会
自动降级成合成结果。

页面的主平均深度从全计算域切换到 `machining_region` 矩形 ROI。矩形边界使用
单元与 ROI 的实际相交面积权重；全域统计仍保留，两者通过 `stats` 与 `roiStats`
分开返回。

## G-01 网格基线

入口：`python tools/grid_convergence.py --dx-um 0.5 0.25 0.125`。

在 `examples/ten_pulses.json` 的固定物理输入下，输出写入 `runs/grid_convergence/g01.json`
（被忽略，不是源码证据）。三级网格均完成 10 个事件，0.25→0.125 μm 的全域平均深度
相对变化约 0.31%，体积约 0.00092%；最细网格半单元平移的平均深度/体积变化约
0.00078%。这只证明该解析均质算例的基线行为，不能外推到复合材料、孵化或闭合模型。
