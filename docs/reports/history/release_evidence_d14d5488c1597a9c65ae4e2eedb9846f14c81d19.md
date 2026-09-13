# 发布证据（统一到同一提交）

- **commit_sha**: `d14d5488c1597a9c65ae4e2eedb9846f14c81d19`
- 工作树有未提交改动：**是**（29 项）
- 采集时间：2026-09-13T15:28:43+0800
- Python：3.13.14

> ⚠️ 本报告与 `commit_sha` **绑定**。若 HEAD 已变，报告作废 —— 用 `python tools/release_evidence.py --verify` 检查。

## 汇总

- 已运行：pytest, node, browser, package
- 未运行：dataqa
- 已运行但失败：（无）

## 逐项

| 检查 | 状态 | 结果 | 说明 |
|---|---|---|---|
| pytest（全量） | `run` | 426 passed in 122.42s (0:02:02) | 通过 |
| Node 契约测试 | `run` | passed=32，failed=0 | 通过 |
| 真实浏览器探针 | `run` | passed=29，failed=0 | 通过 |
| wheel 干净环境安装冒烟 | `run` | passed=21，failed=0 | 通过 |
| 实测数据 QA | `not_run` | — | tools/measured_data_report.py 尚未交付（U04 未实施） |

## 明细

### pytest（全量）

- 命令：`pytest -q --junitxml`
- 退出码：0
- JUnit：`{"tests": 426, "failures": 0, "errors": 0, "skipped": 0, "time_s": 122.176}`
- 尾部输出：

```
........................................................................ [ 67%]
........................................................................ [ 84%]
..................................................................       [100%]
426 passed in 122.42s (0:02:02)
```

### Node 契约测试

- 命令：`node webui/test/contract_test.mjs`
- 退出码：0
- 尾部输出：

```
PASS  x_name 取 label||name（sic_threshold_vs_effective_n）

结果：32 通过，0 失败
```

### 真实浏览器探针

- 命令：`node tools/browser_probe.mjs`
- 退出码：0
- 尾部输出：

```
结果：29 通过 / 0 失败 / 1 跳过
产物：C:\Users\RZF\Desktop\博士课题资料\工艺仿真软件\ultrafast-demo\runs\_release_evidence\browser\result.json
```

### wheel 干净环境安装冒烟

- 命令：`python tools/package_smoke.py`
- 退出码：0
- 尾部输出：

```
====================================================================
结果：21 通过｜0 失败｜0 跳过
====================================================================
```

### 实测数据 QA

- 命令：`—`
- **未运行原因**：tools/measured_data_report.py 尚未交付（U04 未实施）
