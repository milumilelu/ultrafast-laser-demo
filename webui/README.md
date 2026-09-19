# webui —— 唯一界面（`demo.html`）

`ultrafast-demo` 的**唯一界面**：单文件静态页（内联 CSS/JS，无构建、无框架、无外部资源）。

历史沿革：Streamlit（批次 E）→ 本地 Web 多工作区前端（批次 K/L）→ **2026-09-19 收敛为
`demo.html` 单文件**（旧前端与 Legacy Streamlit 已删除，见
`docs/decisions/ADR-0023-demo-html-only-ui.md`）。

## 启动

```bash
cd ultrafast-demo
PYTHONPATH=src python -m ufdemo.webapp              # 默认 127.0.0.1:8787
PYTHONPATH=src python -m ufdemo.webapp --port 9000
PYTHONPATH=src python -m ufdemo.webapp --runs-dir runs/_web   # 隔离输出
```

浏览器打开 `http://127.0.0.1:8787/demo.html`（`/` 也返回同一页面）。
**不要**用 `file://` 直接打开 `demo.html`：后端不可用时页面会明确报错，不会给「看起来能用」的假象。

`UFDEMO_RUNS_DIR` / `UFDEMO_CURVES_DIR` 可覆写输出与曲线目录（与后端其它路径同源）。

## 页面构成

材料卡选择 → 工艺参数（区域 / 填充间距 h / 层数 N / 脉宽 τ / 频率 f / 速度 v）→
点「开始仿真」→ 形貌图（canvas）+ 中心剖面 + 四项指标（平均 / 最大 / 中心深度、脉冲事件数）
+ 协议参数回显（从材料卡参考协议自动读取）。

| 端点 | 说明 |
|---|---|
| `POST /api/demo-rect` | **本页唯一求解入口**：材料卡 + 间距/层数/区域 + τ/f/v → 矩形槽形貌 |
| `GET /api/health` | 版本与各目录路径 |

后端全量 API（材料 / 曲线 / 查表 / 历史等）仍在 `src/ufdemo/webapp.py` 中可用，供脚本与
契约测试使用；`demo.html` 只调用上表两个端点。

> **工艺参数自由直传的语义**：材料卡存**单脉冲阈值 Fth1 + 文献累积模型**
> `Fth(N) = Fth1·N^(S-1)`，核逐点累计（ADR-0022）；τ/f/v 无需匹配单一工况点。
> 唯一要守的是 **λ、τ**（Fth1 与 S 在 1030 nm / 208 fs 下标定，τ 超出 ±5% 时页面给出中性提示）。

## 测试

```bash
python -m pytest -q tests/test_demo_laser_overrides.py    # demo 端点与参数覆盖
python -m pytest -q tests/test_webcontract.py             # 契约层与 HTTP 端到端
```

浏览器级验证（可选；本机有 Chrome/Edge，不在 PATH）：旧 U03 探针已随旧前端删除（ADR-0023）；
需要时按 `system-browser-e2e-testing` 的方式临时驱动系统浏览器即可。

> `frontend-showcase/` 是并行任务的来源目录（非本仓库），保持不动。
