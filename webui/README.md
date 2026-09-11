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

# Python 侧（含 HTTP 端到端，起真实服务）
PYTHONPATH=src python -m pytest tests/test_webcontract.py -q
PYTHONPATH=src python -m pytest -q -m "not slow"      # 跳过起服务的用例
```

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
