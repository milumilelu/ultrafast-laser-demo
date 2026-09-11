# ADR-0016：以本地 Web 界面替代 Streamlit，并接管全部功能

* 状态：已采纳｜日期：2026-09-11｜批次：K

## 背景

批次 A–J 交付后，界面是 Streamlit（`app.py`，六个页签、371 项测试覆盖）。
用户评价「前端不行」，并提出此前由**并行任务**产出的静态演示页
（`frontend-showcase/`，独立于本仓库、非 git 仓库），要求把它「接过来」，
并明确选择：**替代 Streamlit、连真实求解器**。

被接管资产的状态（接管前实测）：

* `node test/smoke.js` **24/24 通过**；覆盖 YSZ 事件核、Inconel 仅阈值、
  相复合截断、插值越界、门控与措辞守卫；
* 信息架构与 `ui_service` 的不变量**对齐**（`solve_count` 只在提交时 +1、
  越界返回 `null`、不返回全 0 假数组）；
* `mockSolve()` 是**纯函数**（不碰 DOM）——这是接真实求解器的天然接入点；
* **缺口**：批次 H/I/J 完全未覆盖（受限阈值协议、七材料能力入口、
  冻结几何批量、斜入射/动态角度）。

## 决定一：迁入 `webui/`，**不改动来源目录**

把前端迁入 `ultrafast-demo/webui/`，`frontend-showcase/` **原样保留**。
理由：来源目录由并行任务维护且不在本仓库，改动它既不进版本历史，
也可能与那边的工作冲突。迁入时顺带去掉设计工具注入的 `data-page-node-id`
（纯噪声，HTML 从 14.9 KB 降到 7.9 KB）。

## 决定二：零外部依赖的服务端（stdlib，不引入 FastAPI/Flask）

`ufdemo/webapp.py` 用 `http.server.ThreadingHTTPServer` 提供静态文件 + JSON API。

* **不引入 FastAPI/Flask**：本工程的核心约定是科学计算核心不依赖界面对象，
  且少一个依赖就少一处版本漂移。本地单人使用，stdlib 足够。
* **求解加全局锁**：`solve` 是 CPU/内存密集的，串行化避免并发大网格打满内存。
* **不实现 `GET /api/solve`**：提交必须是 POST，避免被浏览器预取意外触发求解。
* 若将来需要多用户并发或鉴权，再单独评估——本 ADR 不预先承诺。

## 决定三：契约层**序列化 `FrozenRun`**，不重写业务逻辑

`ufdemo/webcontract.py` 只做「取数 + 序列化 + JSON 安全转换」：

* 直接复用 `ui_service`：`submit`（准入/求解/落盘/诊断）、
  `available_layers`（图层可用性**带原因**）、`threshold_rows`、
  `material_entry_rows`、`capability_gate`、`table_lookup`、`build_watermark`；
* 因此**不存在**「前端一套规则、Streamlit 一套规则」的漂移风险；
* 契约形状与 `mockSolve` 的返回结构对齐，前端从模拟数据切到真实求解时
  渲染层几乎不用改。

## 决定四：JSON 安全是**硬要求**，不是细节

`jsonable()` 必须处理四类会直接导致前端崩掉的情况：

| 输入 | 错误做法 | 后果 | 本实现 |
|---|---|---|---|
| `NaN` / `±Inf` | 直接写 `NaN` | 前端 `JSON.parse` 失败 | → `None` |
| `np.ndarray` | `str(arr)` | 变成不可解析的文本 | → 嵌套 list |
| dataclass（如 `TableLookup`） | 落到 `str()` 分支 | `"TableLookup(curve_id='...')"` | → 展开成 dict |
| `bool` | 先判 `int` | `True` 变成 `1` | 先判 `bool` |

其中 dataclass 那条是**实测踩到的**：查表结果最初被序列化成 repr 字符串，
前端读不到任何字段。已由 `test_jsonable_expands_dataclass` 钉死。

## 决定五：错误分层——`UFDemoError` 的错误码是**封闭集合**

`ufdemo.errors` 只注册 10 个码（`CONFIG_INVALID`、`TABLE_OUT_OF_RANGE`、
`GEOMETRY_UNSUPPORTED` …），它们表达的是**材料与求解语义**。
构造未注册的码会直接抛 `ValueError`（这是设计，防止随意扩张语义）。

因此新增两个**分层**异常，不复用 `UFDemoError`：

* `webcontract.ContractError` —— 资源定位问题（模板/运行不存在、路径越界），
  自带 HTTP 状态码；
* `webapp.HttpError` —— 协议层问题（请求体过大 413、JSON 不合法 400）。

`UFDemoError` 在 HTTP 层仍然映射为 400（求解/配置语义错误）。

> **实测踩到的 bug**：`_send_error_json(err.code, err.message, **err.to_dict())`
> 中 `to_dict()` 已含 `code`，展开会**重复传参**（`TypeError: got multiple values
> for argument 'code'`），结果是**一个响应都发不出去**，客户端只看到连接被关。
> 已被 `test_http_run_traversal_blocked` 抓到并修复。

## 决定六：前端**不再自行插值/自行判决**

* 查值改调 `/api/lookup`，前端删除本地 `interpLinear` 的使用
  （函数保留但不在查值路径上）：越界语义、插值方法、错误码全部由后端决定；
* 图层标签从 `/api/layers` 取（与 Streamlit 侧同源），不前端硬编码；
* 曲线「能否进入事件核」由后端 `capability_rows` 给出，前端只解析显示。

## 决定七：前端补批次 H/I/J，缺口不得留白

接管时前端缺三个批次的功能，全部补上：

* **参数页**：求解路径（reference/grouped）、批大小、局部核后端；
  入射角 + 动态角度；受限阈值协议开关 + 阈值候选索引；
* **结果页**：几何修正面板（支持范围与**近似标注**）、批量面板
  （块数/拒绝数/局部误差/回退原因）、阈值协议面板、相结构、错误块；
* **新增页签**：七材料能力入口（`opened` / `blocked` / `deferred` 三类**分开显示**，
  `deferred` 是缺口，不得读作已开放）。

未启用的面板**不显示**（空字典），而不是显示全 0 假数据。

## 决定八：测试策略——补「无浏览器时最容易漏」的那一类

`webui/test/contract_test.mjs` 覆盖：

* **DOM 契约**：`main.js` 里 `$("#id")` / `getElementById("id")` 引用的元素
  必须在静态 HTML 或**动态生成**里存在（漏一个就会在浏览器里
  `null.addEventListener` 崩掉，而这类错误在没有浏览器的环境里最容易漏）；
  **反向检查**动态元素清单不留「僵尸项」，避免白名单掩盖真正的缺失；
* **后端契约**：越界 → `null`、`threshold_only` 不给 0、图层不可用带原因、
  查表不触发求解、读历史走磁盘且两次结果一致。

Python 侧 `tests/test_webcontract.py`（23 项，含起真实 HTTP 服务的端到端）。

> 环境限制：本机无 Playwright/Selenium 也无系统浏览器，因此**没有**做真实
> 浏览器渲染验证。这一点如实记录，不冒充已做。

## 后果

* **好消息**：界面现代化且**连真实求解器**；两侧共用同一纯逻辑层；
  394 项 Python 测试 + 27 项前端契约测试全绿；前端的两个真 bug
  （非法错误码、响应重复传参）由测试抓出。
* **代价**：多了一个本地服务进程要管；Streamlit 界面暂时并存（作为过渡备用入口），
  尚未下线——**下线时机待人工决策**（需确认新界面覆盖了全部使用场景）。
* **未做**：真实浏览器渲染验证（无浏览器可用）；移动端深度适配；PWA/离线。
* **不承诺**：不承诺多用户并发、鉴权、远程部署能力。

## 相关文件

* `src/ufdemo/webcontract.py` —— 契约层（序列化 `FrozenRun` + 清单 + 查表）
* `src/ufdemo/webapp.py` —— 零依赖 HTTP 服务
* `webui/` —— 前端（含 `README.md` 与契约测试）
* `tests/test_webcontract.py` —— Python 侧契约与端到端测试
* `webui/test/contract_test.mjs` —— DOM/后端契约测试
