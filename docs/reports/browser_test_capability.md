> **现状（2026-09-19）**：本文记录的浏览器探测方法与误诊纠正仍然有效；其中提到的 U03 工具
> （`tools/browser_check.py` / `browser_probe.mjs`）已随旧 V2 前端删除（ADR-0023），
> 需要浏览器级验证时按 `system-browser-e2e-testing` 的方式对当前唯一界面重建。

# 浏览器测试能力：误诊复盘与能力补齐

- 日期：2026-09-11
- 起因：`AUDIT_TASKS_EXECUTION_SPEC.md` 待决策项 (c)——
  「引入 Playwright，还是接受浏览器级长期标记『未运行』？」
- 结论：**该问题的前提是错的**。本机有浏览器，浏览器级测试已跑通，
  两条备选路径**都不需要**。

## 1. 被推翻的原结论

原记录（两处，现已勘误）：

| 文件 | 原文 |
|---|---|
| `AUDIT_TASKS_EXECUTION_SPEC.md`（基线实测表） | 真实浏览器端到端 — ⛔ 未运行，理由「本机无 Playwright / Selenium / 系统浏览器」 |
| `ADR-0016`（决定八） | 「本机无 Playwright/Selenium 也无系统浏览器，因此**没有**做真实浏览器渲染验证」 |

## 2. 实测事实

| 项 | 实测结果 |
|---|---|
| Chrome | `152.0.7977.84`，`C:\Program Files\Google\Chrome\Application\chrome.exe` |
| Edge | `152.0.4191.66`，`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` |
| `which chrome` / `which msedge` | **均找不到**（但文件确实存在） |
| Playwright / Selenium（当时） | 均未安装 |
| `%LOCALAPPDATA%\ms-playwright` | **不存在**（从未下载过 Playwright 自带浏览器） |

## 3. 根因：三个独立事实被合并成一个错误结论

**根因 A —— Windows 上浏览器不进 PATH。**
`chrome.exe` / `msedge.exe` 从不注册进 `PATH`。任何 `which` / `where` 式探测
都必然返回「找不到」，于是被读成「没装浏览器」。
→ 正解：直接查默认安装路径。

**根因 B —— Playwright 默认只认它自己下载的浏览器。**
`chromium.launch()` 去找 `%LOCALAPPDATA%\ms-playwright\...`，该缓存为空时报：

```
BrowserType.launch: Executable doesn't exist at
  C:\Users\RZF\AppData\Local\ms-playwright\chromium_headless_shell-1234\...
Looks like Playwright was just installed or updated.
Please run the following command to download new browsers:
    playwright install
```

这个措辞极易被读成「本机没有浏览器」，实际意思是
「Playwright 没下载它自带的浏览器副本」。

**根因 C —— 自动化库确实没装。**
当时 `playwright` / `selenium` / `pyppeteer` 全都没有。
但「库没装」≠「没有浏览器」。A + B + C 被合并成了一句错误的「本机无系统浏览器」。

### 三种 Playwright 启动方式实测对照

| 写法 | 结果 |
|---|---|
| `chromium.launch()` | ❌ `Executable doesn't exist ... playwright install` |
| `chromium.launch(channel="chrome")` | ✅ `152.0.7977.84`，成功打开页面 |
| `chromium.launch(executable_path=<系统Chrome>)` | ✅ `152.0.7977.84`，成功打开页面 |

## 4. 能力补齐方案（已实施）

**选择：`puppeteer-core` + 系统 Chrome，不引入 Playwright。**

理由：项目前端契约测试本来就是 node 的（`webui/test/contract_test.mjs`），
加一个 node 脚本零新运行时；`puppeteer-core` 不下载 Chromium
（25 个包，清华/npmmirror 源约 2 s）。

- 脚本：`ultrafast-demo/tools/browser_probe.mjs`
- 后端：`PYTHONPATH=src python -m ufdemo.webapp --port 8787`

### 实测结果

| 运行方式 | 结果 |
|---|---|
| `node tools/browser_probe.mjs` | **13 通过｜0 失败｜2 跳过**（可复现，跑两次一致） |
| `node tools/browser_probe.mjs --solve` | **15 通过｜0 失败｜1 跳过** |
| `--browser=edge` | ✅ Edge 回退可用 |

覆盖点：加载与 boot（无 JS 异常 / 无失败请求）、6 个页签可切换并激活、
查表不递增求解次数且未触发 `POST /api/solve`、读历史求解次数不变而读取次数 +1、
`--solve` 时求解次数 +1 且确有 `POST /api/solve`、canvas 真实出像素。

**唯一 SKIP 是正确行为**：读回的是 `threshold_only` 运行，
`#morph-canvas` 为 `0×0` —— 按协议「不提供形貌」，不是缺陷。

## 5. 踩到的坑（已固化进脚本注释）

1. **不要用 `.catch(()=>{})` 吞点击异常。**
   第一版对隐藏面板里的按钮 `page.click().catch(()=>{})`，
   结果「读取次数 0 → 0」被静默误报成通过。改为点前先切页签、
   失败记进 `result.json` 并计为 failure。
2. **点某面板里的控件前必须先 `switchTab`**，否则元素不可见、点不到。
3. **favicon 404 要放行**：浏览器自动请求，不是页面缺陷。
4. **Headless 下同 shell 内启动才好验证**：Chrome 会 detach，
   后台任务的完成通知不代表实例还在（实测后台跑完端口就没了）。
5. **别用 `cmd //c start` 在 Git Bash 里起浏览器**，转义会破坏参数。

## 6. 顺带发现的独立问题（未修）

**Bash 工具运行环境 PATH 中途损坏。**
会话中途 `dirname` / `tail` / `head` / `ls` 突然全部 `command not found`，
报错来自 `cli/vendor/shim/shell-runtime-bash-env.sh`。
临时绕过：显式加
`export PATH="/c/Users/RZF/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:/c/Windows:$PATH"`
。疑似与并发进程动 PortableGit 有关，需单独排查。

## 7. 仍未运行（如实标注）

U03 列出的 8 条浏览器级主路径中，以下两条**未覆盖**：

- 下载（导出）
- 换参数后旧结果标记

**勘误（2026-09-11 深夜，复核追加）**：初版本节曾把「曲线单位」列入**已覆盖**——
**该说法不成立，现已更正**。复核证据：

- `tools/browser_probe.mjs` 的用例清单中，与曲线相关的仅两条：
  「查表曲线已绘制」（判 `#t-chart` canvas 是否出像素）与「查表不递增求解次数」；
  **没有任何一条读取 `#t-identity` / `#t-chart-note` / 坐标轴标签的文本**。
- 实测 F04 缺陷在真实浏览器下**仍然可见**（Chrome `152.0.7977.84`，
  切查表页签 + 真实查值后）：

  | 取证项 | 实测值 |
  |---|---|
  | `STORE.curves[*].x_name` 类型 | `object`（三条曲线全部） |
  | `STORE.curves[*].x_unit` / `y_unit` | `""`（全部为空串） |
  | `#t-identity` 可见文本 | `x：[object Object]（）｜y：[object Object]（）｜…` |
  | `#t-raw` 表头 | `#  X（）  Y（）` |

  即：**浏览器能力跑通了，但曲线文案这一项从未被断言**，所以 F04 在
  「DOM 契约层 + 浏览器层」**双重漏网**。取证产物：`runs/_audit_browser_probe2/f04/`
  （`f04-evidence.json` ＋ `f04-table-tab.png`）。

→ 因此：**「曲线单位」应移入「未覆盖」清单**，并由 U03 补断言后转为已覆盖
（写法见 `AUDIT_TASKS_EXECUTION_SPEC.md` 的 U03 条）。其余五条
（启动、选真实数据、提交、错误提示、历史读取 / 回放不重算）确已由
`browser_probe.mjs` 覆盖，实测 **15 通过 / 0 失败 / 1 跳过**。

### 7.1 追加：空 runs 目录会触发假失败

同一探针、同一代码，仅改变 runs 目录即得不同结果：

| 运行条件 | 结果 |
|---|---|
| 默认 `runs/`（含既有运行） | **15 通过｜0 失败｜1 跳过** |
| `UFDEMO_RUNS_DIR` 指向**空**目录 | **14 通过｜1 失败｜1 跳过** → `读历史后读取次数 +1（0 → 0）` |

**根因**：探针的 C3（读历史）排在 C4（提交计算）**之前**；空目录下无可读运行，
`read_count` 不变。**这是假失败，不是页面缺陷**。
但探针按自身约定（前置不满足 → `SKIP`）应在历史列表为空时 `skip(...)`，
否则 **CI 首次 checkout（无 runs）必然假红**。

**规避（在探针修好前）**：跑 `browser_probe.mjs` 时**不要**把 `UFDEMO_RUNS_DIR`
指向空目录——注意这与工程并发规则推荐的「用 `UFDEMO_RUNS_DIR` 隔离输出」正好冲突。

## 8. 相关文件

- `ultrafast-demo/tools/browser_probe.mjs` —— 探针（新增）
- `ultrafast-demo/webui/README.md` —— 「测试」章节已补真实浏览器一层
- `AUDIT_TASKS_EXECUTION_SPEC.md` —— 第 1 节勘误 + 第 (c) 项消解
- `ultrafast-demo/docs/decisions/ADR-0016-*.md` —— 决定八追加勘误
- 产物：`runs/browser-probe/`（截图 + `result.json`）
