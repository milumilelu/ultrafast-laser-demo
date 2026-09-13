# G05：文献语义回归报告（批次 D / T07）

- 生成时间（UTC）：2026-09-13T10:56:22.393266+00:00
- 代码版本：commit=35819a3374bb31839ab8e297fded80e8c6c4bb45｜工作区有改动=True
- 汇总：通过 9｜失败 0

源公式与实现的逐条对应见 `docs/reports/reference_equations.md`。

| 算例 | 量 | 预期 | 实测 | 误差 | 容差 | 状态 | 验证类别 | 结果目录 |
|---|---|---|---|---|---|---|---|---|
| examples/ysz_reference_case.json | YSZ 扫描速度 (mm/s) | 278.9734276388 | 278.97342763877356 | 9.474805537572104e-14 | rel <= 1e-12 | 通过 | 公式核查 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_ysz_reference` |
| examples/ysz_reference_case.json | 错误换算 2*w0*f/N (mm/s) | 355.2 | 355.19999999999993 | 1.6003214769371628e-16 | rel <= 1e-12 | 通过 | 公式核查 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_ysz_reference` |
| examples/ysz_reference_case.json | YSZ 脉冲能量 (uJ) | 201.464053689406 | 201.4640536894062 | 9.875308392213193e-16 | rel <= 1e-12 | 通过 | 公式核查 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_ysz_reference` |
| examples/sic_reference_case.json | SiC Fth(1) (J/cm^2) | 2.35 | 2.35 | 0.0 | rel <= 1e-15 | 通过 | 公式核查 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_sic_reference` |
| examples/sic_reference_case.json | SiC Fth(N->inf) (J/cm^2) | 0.7 | 0.7 | 0.0 | rel <= 1e-9 | 通过 | 公式核查 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_sic_reference` |
| examples/sic_reference_case.json | SiC k (1/pulse) | 0.0199 | 0.0199 | 0.0 | == 0.0199（卡内值） | 通过 | 公式核查 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_sic_reference` |
| examples/sic_reference_case.json | 平均率 / 协议累计深度 独立输出 | 两项分别给出，累计=平均×N_eff | 平均 59.5674 nm/有效脉冲；N=720 累计 42.8885 um |  | rel <= 1e-12 | 通过 | 数值实现验证 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_sic_reference` |
| （语义闸门） | 平均率/累计量能否进入事件核 | 拒绝（RESPONSE_SEMANTICS_INVALID） | 已拒绝 |  |  | 通过 | 数值实现验证 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_sic_reference` |
| （参考运行目录） | 是否写出形貌表面文件 | 不写（参考评估器不求解网格） | 文件：config.json, diagnostics.json, material_snapshot.json, metadata.json, profiles.csv, snapshots, statistics.csv, watermark.json |  |  | 通过 | 数值实现验证 | `C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\acceptance\g05_ysz_reference` |

## 边界声明

- 本报告只做**公式核查**与**数值实现验证**，不含实验图复现；
  原图/原表数字化与工况对应完成前不建立实验回归用例（任务书 10 节 G05 末段）。
- YSZ 式(9) 与 SiC 式(5) 是两套不同的有效脉冲数定义，分别由 `ysz_effective_count` / `sic_effective_count` 实现，并在结果中写入 `definition` 字段。
- 平均去除率（`mean_depth_per_effective_pulse`）与协议累计深度（`cumulative_depth`）分别输出，**不进入**逐事件增量主循环；语义闸门拒绝测试在报告中单列。

## 复现命令

```bash
python -m ufdemo reference examples/ysz_reference_case.json --out runs/g05_ysz_reference
python -m ufdemo reference examples/sic_reference_case.json --out runs/g05_sic_reference
python -m pytest -q -m g05
```
