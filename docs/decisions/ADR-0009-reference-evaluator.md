# ADR-0009：批次 D 参考评估器的边界与接口决定

* 状态：已采纳｜日期：2026-09-10｜批次：D（T07）

## 背景

执行细则第 6 节把批次 D 定义为「独立 YSZ/SiC 评估器；条件与语义校验」，必交
「源公式对应表、G05 报告、平均率与累计量独立输出」。任务书 3.1/3.2 节要求两套
有效脉冲数**不共用公式**，且平均率/累计量不得进入逐事件主循环。

## 决定一：两套有效 N 各自成函数，参数名强制区分

* `references.ysz_effective_count(w0, f, v)` —— `N_eff = (π/4)·(2·w0·f)/v`
* `references.sic_effective_count(w0, f, v, passes)` —— `N_eff = K·(2·w0·f)/v`

两者返回 `EffectiveCount`，其中 `definition` 字段写入稳定标识
（`area_equivalent_ysz_eq9` / `scan_pass_weighted_sic_eq5`），随结果与报告
一同导出，防止二次混用。参数统一命名为 `effective_count`；通用路径生成器产出的
真实脉冲数一律叫 `event_count`，二者不可互相代入。

**刻意保留** `naive_speed_for_effective_count()`（`v = 2·w0·f/N`，错误换算），
仅用于回归对照与文档，禁止进入任何物理输出。

## 决定二：方向相反的两道语义闸门

| 闸门 | 位置 | 作用 |
|---|---|---|
| `assert_increment_semantics()` | `response.py` | 只放行 `event_depth_increment` |
| `assert_reference_only_semantics()` | `references.py` | 只放行平均率/累计/阈值 |

新增反向闸门的原因：仅有一道闸门无法阻止「逐事件增量曲线被当成参考曲线」。
`ReferenceResult.event_kernel_allowed` 恒为 `False`，且 `build_reference_run_result`
把该值写进 `statistics.csv`，使导出的运行目录自带拒绝标记。

## 决定三：`output_semantics` 枚举**不扩容**

执行细则 4.3 给定 6 个语义值，本批不新增值。YSZ 的有效 N / 速度 / 脉冲能量
换算既非深度也非阈值，选 `threshold_only` 表示“不产生深度”，并在
`notes` 中显式写明该算例只做运动学/条件换算。宁可语义偏保守，也不私改细则枚举。

## 决定四：SiC 标量字段取「协议代表行」

SiC 算例输入一个 N 扫描（含 `N=1`、`N=1e9` 极限探针）。顶层标量字段取
`primary` 行：优先等于材料卡 `reference_protocol.required_history.effective_count`
的那一行（本卡为 720），其次取首个输入值。**不取扫描末行**，因为 `N=1e9` 只是
用来核对 `Fth(N) → F_∞` 的极限探针，其「协议累计深度」无物理意义。
完整扫描保留在 `values.sweep`。

## 决定五：参考运行目录不写形貌表面文件

参考评估器不求解网格，`build_reference_run_result` 令 `surface=None`，复用
`io.save_run` 的标准目录结构但**不产出** `final_surface.npz`。理由：避免把
参考量误当形貌结果；细则 5.4「斜面能量积分与去除投影面积不可混用」的同类风险。
`diagnostics.json` 中写入 `event_model_touched=false` 作为可机器读取的证据。

## 决定六：删除 `config.iter_reference_cases` 占位

原占位函数位于 `config.py`，职责上更贴近 `references.py`，且从未被引用。
本批将其移除，改在 `references.py` 实现 `iter_reference_cases(paths)`，
避免 `config → references` 的反向依赖（`references` 已依赖 `config`）。

## 决定七：`io._write_atomic` 在目标被占用时退化为就地写入

细则 11.1 要求「最终文件通过临时文件写入后替换」。在 Windows 上，若目标文件
被别的程序以共享写方式打开（实测：验收目录中的 `statistics.csv` 被预览器占用），
`os.replace` 抛 `PermissionError` 而就地写入仍可行。本批改为：**先尝试原子替换，
失败时退化为就地写入并清理临时文件**。先替换、后退化，能在支持的平台上保持
原子性语义，同时避免因外部程序占用而中断整条验收流程。若就地写入也失败，
异常照常向上抛出，不静默吞掉。

## 后果

* `examples/` 新增 `ysz_reference_case.json`、`sic_reference_case.json`；
* CLI 新增可用的 `reference` 子命令（此前返回 `NOT_IMPLEMENTED`）；
* 验收报告改为 `acceptance_g01_g05.csv` 并新增 G05 专项报告；
* 仍**未**建立实验回归用例：原图/原表数字化未完成，属显式缺口。
