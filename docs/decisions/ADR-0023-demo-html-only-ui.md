# ADR-0023：界面收敛为唯一单文件 `webui/demo.html`，删除 V2 前端与 Legacy Streamlit

* 状态：已采纳｜日期：2026-09-19

## 背景

界面经历过三代：

1. **Streamlit（批次 E / T09）** —— `app.py`，六页签；批次 K 起被 Web 前端替代后即标为
   LEGACY（文件头自述「等主流程稳定后再归档 —— 不同时维护两套 UI」）。
2. **本地 Web 多工作区前端（批次 K/L）** —— `webui/index.html` + `js/{api,data,main,v2}.js`
   + `css/`（ADR-0016 接管 `frontend-showcase/` 迁入），并配有 U03 浏览器级验收
   （`tools/browser_check.py` + `browser_probe.mjs`，逐条覆盖 9 条主路径）。
3. **单文件 `webui/demo.html`** —— 一页完成「材料卡 → 工艺参数 → 求解 → 形貌 + 剖面 + 指标」
   主流程，只调 `POST /api/demo-rect`。

用户决定（2026-09-19）：**`demo.html` 作为唯一界面，其他前端全部删除。**
判据：日常使用只需这一条流程；多工作区前端的维护成本（三个工作区守门测试、U03 九路径探针、
契约测试链）远超其被使用价值；两代旧界面继续留在仓库里只会制造「哪套是在用的」歧义。

## 决定

1. **删除以下 13 个文件**：
   - `webui/index.html`、`webui/css/styles.css`、`webui/js/{api,data,main,v2}.js`、
     `webui/test/contract_test.mjs`
   - `app.py`（Legacy Streamlit）
   - `tools/ui_probe.py`、`tools/ui_demo_probe.py`（AppTest 驱动 app.py）
   - `tools/browser_check.py`、`tools/browser_probe.mjs`（U03 探针）
   - `tests/test_app_smoke.py`
2. **后端静态服务改以 demo.html 为默认文档**：`_serve_static` 的默认文档与 SPA 回退由
   `index.html` 改为 `demo.html`（`/` 与 `/demo.html` 均返回它；启动日志直接打印 `/demo.html`）。
3. **验收报告移除三个界面组**：`run_acceptance.py` 中 G09-UI、G09-demo、U03-browser 三组
   退役；其历史产物（`ui_operation_check.*`、`ui_demo_probe.*`、`browser_check.*`）冻结至
   `docs/reports/history/` 留证，不再随验收重新生成。
4. **测试与工具链同步**：删 5 个前端守门测试（`test_core_v2.py` C6 块）与 2 个探针超时测试
   （`test_cases_replay.py` 第 6 节）；`test_webcontract.py` 的静态断言改为校验 demo.html 内容；
   `package_smoke.py` / `release_evidence.py` / `pyproject.toml` / `MANIFEST.in` 的打包与
   证据清单同步（`[ui]` extra 移除）。
5. **打包只带 `webui/demo.html` + `webui/README.md`**（`tool.setuptools.data-files` 更新）。

## 后果

* **正面**：仓库只留一套在用界面；`pytest` 与验收不再维护三套界面探针；打包产物减小；
  `demo.html` 自包含（内联 CSS/JS），无构建步骤、无外部请求。
* **代价**：失去 U03 的浏览器级九路径覆盖（删除时状态为 **42 通过 / 0 失败 / 1 未实现**，
  属全绿）；需要时按 `system-browser-e2e-testing` 的方式对 demo.html 重建（未纳入本次范围）。
* **明确不做**：不动 `frontend-showcase/`（并行任务维护）；不删后端端点
  （`/api/solve`、`/api/lookup` 等仍由脚本与契约测试使用）。

## 相关文件

* `webui/demo.html`、`webui/README.md` —— 唯一界面与其说明
* `src/ufdemo/webapp.py` —— 静态服务（默认文档 = demo.html）与 API
* `src/ufdemo/webcontract.py` —— `demo_rect_payload`（`POST /api/demo-rect` 的实现）
* `tests/test_demo_laser_overrides.py` —— 该端点的参数覆盖测试
* 被本决定部分替代：ADR-0010（界面分层）、ADR-0016（Web 界面替代 Streamlit）
