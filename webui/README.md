# webui —— 本地 Web 界面（静态前端 + 真实求解 API）

`ultrafast-demo` 的**主界面**。替代原先的 Streamlit 界面（见 `docs/decisions/ADR-0016`）：
同样的信息架构与交互不变量，但**连的是真实求解器**，不是模拟数据。

## 启动

```bash
cd ultrafast-demo
PYTHONPATH=src python -m ufdemo.webapp              # 默认 127.0.0.1:8787
PYTHONPATH=src python -m ufdemo.webapp --port 9000
PYTHONPATH=src python -m ufdemo.webapp --runs-dir runs/_web   # 隔离输出
```

然后浏览器打开提示的地址（**不要**用 `file://` 直接打开 `index.html`：
后端不可用时页面会明确报错并禁用「提交计算」，不会给你一个「看起来能用」的假象）。

`UFDEMO_RUNS_DIR` / `UFDEMO_CURVES_DIR` 与 Streamlit 侧同源，可覆写输出与曲线目录。

## 结构

| 文件 | 作用 |
|---|---|
| `index.html` | 页面骨架：顶栏（求解/读取计数）、侧栏（材料卡/能力/计数）、六个页签 |
| `css/styles.css` | 桌面优先响应式；浅色单强调色；`prefers-reduced-motion` |
| `js/api.js` | **API 客户端 + 契约映射**（后端 snake_case → 前端结构）；`bootstrapData()` |
| `js/data.js` | 图层标签等**静态展示常量**（不再是数据源） |
| `js/main.js` | 渲染与交互；`baseParams` + 表单 → 真实运行配置 |
| `test/contract_test.mjs` | 无头契约测试（DOM 契约 + 后端契约） |

> `test/contract_test.mjs` 是**无浏览器**替代品，只能静态检查元素与字段。
> 真实浏览器验证走 `tools/browser_probe.mjs`（见下「测试」）。

## 后端 API

| 端点 | 说明 |
|---|---|
| `GET /api/health` | 版本、各目录路径 |
| `GET /api/materials` | 材料卡目录 + 能力门控 + 七材料能力入口（探针实跑） |
| `GET /api/examples` / `GET /api/examples/<name>` | 模板清单 / 单个模板的**完整参数** |
| `GET /api/curves` | 曲线卡（含原始点，CSV 是唯一来源） |
| `GET /api/references` / `POST /api/reference` | 参考算例清单 / 评估（只做公式核查） |
| `GET /api/runs` / `GET /api/runs/<id>` | 历史清单 / 读取已有运行（**只读盘，不重算**） |
| `GET /api/layers` | 图层标签与不可用口径（与 Streamlit 侧同源） |
| `POST /api/solve` | **真实求解**（唯一会调用求解器的端点） |
| `POST /api/preview` | 只做准入校验，不求解、不落盘 |
| `POST /api/lookup` / `POST /api/curve-grid` | 查表 / 插值图数据（均不触发求解） |
| `POST /api/material-entries` | 七材料能力入口（单独端点，探针较慢） |
| `POST /api/guard-depth-export` | 导出前口径检查（合成模式禁止导出物理 `depth_um`） |

`GET /api/solve` 不存在——提交必须是 POST，避免被浏览器预取意外触发求解。

## 保留的核心不变量

1. **只有「提交计算」会调用求解器**；且只有**真实调用成功**才递增「求解次数」
   （准入校验失败时求解器没跑，因此不计数——计数精确等于求解器被调用的次数）。
2. **「读取结果」只递增「读取次数」**，求解次数不变；读的是**盘上已有**结果。
3. **切图层 / 旋转视图 / 切截面 / 时间轴回放 / 查表** 都不改变任何计数。
4. **`threshold_only` 不提供深度**：契约里是 `null`（不是数值 `0`），界面显示「不提供」。
5. **图层不可用给出原因**（`blocked`），且不进 `layers` —— 不返回全 0 假数组。
6. **越界查表**：唯一错误码 `TABLE_OUT_OF_RANGE`；允许越界时逐项返回 `null`，
   **绝不返回 0、不外推、不钳端点**。
7. **措辞守卫**：不得出现「热影响区/HAZ/温度…」（有测试断言）。
8. **三栏分开**：结果里注明只做**数值实现验证**；参考评估器注明只做**公式核查**，
   `verifiedByExperiment` 恒为 `false`。

## 测试

```bash
# 无头契约测试（需要后端在跑；BASE 可指定）
node webui/test/contract_test.mjs
BASE=http://127.0.0.1:8792 node webui/test/contract_test.mjs

# 真实浏览器验证（需要后端在跑；驱动本机 Chrome，不下载 Chromium）
node tools/browser_probe.mjs                    # 默认 http://127.0.0.1:8787
node tools/browser_probe.mjs --browser=edge     # 用 Edge
node tools/browser_probe.mjs --headed           # 有头，肉眼看
node tools/browser_probe.mjs --solve            # 真跑一次求解（会写 runs/）

# Python 侧（含 HTTP 端到端，起真实服务）
PYTHONPATH=src python -m pytest tests/test_webcontract.py -q
PYTHONPATH=src python -m pytest -q -m "not slow"      # 跳过起服务的用例
```

### 真实浏览器验证（`tools/browser_probe.mjs`）

补的是 `contract_test.mjs` **看不见**的三类问题：页面能否在真引擎里 boot、
渲染是否真的出像素、交互是否真能点动。

**它现在逐条覆盖 U03 的 9 条主路径**（执行顺序与清单一致），
由 `tools/browser_check.py` 包装成验收组（自起隔离服务 + 隔离 `runs/`）：

```bash
python tools/browser_check.py            # 全套（含真实求解）
python tools/browser_check.py --no-solve # 跳过需要真实求解的路径
python tools/browser_check.py --browser edge
```

报告：`docs/reports/browser_check.{csv,md}`；`tools/run_acceptance.py` 已注册为
`U03-browser` 检查组（`--no-browser` 可跳过）。

最近一次实测（Chrome 152）：**28 通过｜2 失败｜0 未运行｜1 未实现**。

| U03 主路径 | 结果 |
|---|---|
| U03-1 启动 / U03-2 选真实数据 / U03-3 提交 / U03-4 错误提示 / U03-5 曲线单位 | ✅ 通过 |
| U03-6 换参数后旧结果标记 / U03-7 读历史 | ✅ 通过 |
| U03-8 回放不重算 | ❌ 失败（计数不变量通过；**读历史后的回放面板画不出来**） |
| U03-9 下载 | ⚠️ 未实现（前端无下载控件） |

U03-8 的失败是**读盘路径的契约漂移**（实测证据）：`GET /api/runs/<id>` 返回的快照只有
`index`/`label`（渲染层读 `eventIndex`/`timeS`/`passId` → 显示 `undefined`），
且 `grid` 为空对象（无 `nx`/`ny`）→ `drawHeatmap` 传给 `createImageData` 的是
`undefined`，抛 `Value is not of type 'long'`，`#tl-canvas` 停在 0×0。
实时提交路径的 payload 带这些字段，所以只有「读历史」这条路径能暴露它。

依赖：本机 Chrome 或 Edge + 受管 node 工作区的 `puppeteer-core`
（`cd C:/Users/RZF/.workbuddy/binaries/node/workspace &&`
`npm install puppeteer-core --registry=https://registry.npmmirror.com`）。

| 覆盖点 | 说明 |
|---|---|
| 加载与 boot | 首页 200、无 `boot-error`、无未捕获 JS 异常、无失败请求 |
| 选真实数据 | 材料卡可选可回读；**载入模板后材料卡与运行模式随之回填** |
| 提交 | 求解次数 +1、确有 `POST /api/solve`、形貌 canvas 真实绘制 |
| 错误提示 | 越界入射角显示结构化错误（错误码/原因/要求），且不计入求解次数 |
| 曲线单位 | 遍历全部曲线的轴名与单位非空、无 `[object Object]` |
| 换参数后旧结果标记 | 过期标记出现并说明「显示的是上一次运行」，本身不触发求解 |
| 读历史 | 求解次数不变、读取次数 +1、未触发求解 |
| 回放不重算 | 拖时间轴/切截面/换图层/转置都不改计数；回放画布须真绘制 |
| 下载 | 未实现 → 如实记「未实现」（与「未运行」分列） |

**坑（已固化）**：① 不要用 `.catch(()=>{})` 吞掉点击异常——元素在 hidden 面板里点不到时，
会被静默误报成「功能正常」；② `page.select()` 成功时返回**数组**，不能拿返回值当错误标记；
③ 界面**有状态**，独立用例前要 `resetState()`（重载页面），否则前一条的状态泄漏
会让后一条失败被错误归因；④ 历史为空时 `#h-run` 有**一个占位 option**，
不能用「选项数 0」判空。

**注意**：本机有浏览器，但**不在 PATH**（Windows 特性）。
不要用 `which chrome` 判断——直接查
`C:\Program Files\Google\Chrome\Application\chrome.exe`。
Playwright 若要用，必须 `channel="chrome"`，**不要** `playwright install`。

契约测试覆盖两类**在没有浏览器时最容易漏**的问题：

* **DOM 契约**：`main.js` 里 `$("#id")` 引用的元素必须在静态 HTML 或动态生成里存在
  （漏一个就会在浏览器里 `null.addEventListener` 崩掉）；
  并反向检查动态元素清单里不留「僵尸项」，避免白名单掩盖真正的缺失。
* **后端契约**：越界返回 `null`、`threshold_only` 不给 0、图层不可用带原因、
  查表不触发求解、读历史走磁盘。

## 与 Streamlit 界面的关系

本目录是**主界面**；`app.py`（Streamlit）暂时保留为过渡期的备用入口，
两者共用 `ui_service` 的纯逻辑层（诊断构建、水印、措辞守卫、图层可用性），
因此不会出现「同一份规则写两遍而漂移」。
`frontend-showcase/` 是**来源目录**（并行任务的产物，非本仓库），
已按要求迁入 `webui/`，原目录保持不动。
