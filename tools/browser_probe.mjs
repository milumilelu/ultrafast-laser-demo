/* ============================================================
 * browser_probe.mjs —— 用**真实浏览器**驱动 ufdemo 本地 Web 界面
 *
 * 落实 `AUDIT_TASKS_EXECUTION_SPEC.md` 的 **U03 浏览器级验收**：
 * 逐条覆盖 U03 要求的 8 条主路径 + 1 条「下载」。**执行顺序与清单一致**
 * （U03-1 → U03-9），每条结果都带 `u03` 字段，供 `tools/browser_check.py`
 * 组装成验收清单。
 *
 * 与 `webui/test/contract_test.mjs` 的分工：
 *   contract_test.mjs = **无浏览器**替代品，只做静态检查（元素是否存在、字段类型）；
 *   本脚本 = 真引擎里跑，能看见静态检查**看不见**的三类问题：
 *     ① 页面能否 boot（JS 异常 / console error / 资源 404）
 *     ② 渲染是否真的出像素（canvas 非空白）
 *     ③ 交互是否真能点动、是否遵守计数不变量
 *
 * 依赖：本机已装 Chrome 或 Edge ＋ 受管 node 的 `puppeteer-core`
 *       （`puppeteer-core` **不下载 Chromium**，直接驱动系统浏览器）
 *
 * 用法：
 *   node tools/browser_probe.mjs                    # 全套 U03（含真实求解）
 *   node tools/browser_probe.mjs --no-solve         # 跳过需要真实求解的路径
 *   node tools/browser_probe.mjs --browser=edge     # 用 Edge
 *   node tools/browser_probe.mjs --headed           # 有头，肉眼看
 *   node tools/browser_probe.mjs --base <url> --out <dir>
 *
 * 产物：`<out>/result.json`（含逐条 u03 结果）+ 截图；退出码 = 有失败则 1。
 *
 * ⚠️ 已踩过的坑（勿回退，全部有实跑依据）
 *   1. **不要**用 `.catch(()=>{})` 吞掉点击异常 —— 元素在 hidden 面板里点不到时，
 *      会被静默误报成「功能正常」。本脚本把点击失败记进 result.json 并计为失败。
 *   2. 点某面板内的控件前**必须先切页签**（`switchTab`），否则元素不可见、点不到。
 *   3. **`page.select()` 成功时返回数组**（truthy），绝不能拿它的返回值当错误标记，
 *      必须显式 try/catch（见 `selectOption`）。曾因此把「成功」误判成「失败」。
 *   4. **界面是有状态的**：前一条用例改过材料/参数会污染后一条，失败被错误归因。
 *      需要独立的用例前先 `resetState()`。曾因「载入模板」把材料卡留在原地，
 *      导致后面的「提交」以 CONFIG_INVALID 失败，看起来像「提交坏了」。
 *   5. **空历史不是「读取成功」**：`#h-run` 在无运行时会有**一个占位 option**
 *      （`value=""`），所以「选项数为 0 才跳过」是错的，要看占位值。
 *   6. favicon 404 是浏览器自动请求，不算页面缺陷，单独放行。
 * ============================================================ */

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");

/* ---------------- 参数 ---------------- */
const argv = process.argv.slice(2);
const flag = (n) => argv.includes(n);
const opt = (n, d) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : d; };

const BASE = opt("--base", process.env.BASE || "http://127.0.0.1:8787");
const WANT = opt("--browser", process.env.BROWSER || "auto"); // auto | chrome | edge
const HEADED = flag("--headed");
const DO_SOLVE = !flag("--no-solve");     // U03 的多数路径需要真实求解，默认开启
const OUT = path.resolve(opt("--out", path.join(ROOT, "runs", "browser-probe")));

/* ---------------- 全局看门狗：任何挂死都必须变成「带原因的失败」 ----------------
 *
 * 为什么必须有（本工程在探针上**踩过三次挂死**，每次都是十几分钟无输出）：
 *   ① 复用被杀留下的 `.profile` → Chrome 拒绝启动；
 *   ② `await browser.close()` 在 Windows 下不 resolve；
 *   ③ `fs.rmSync(profileDir)` 同步阻塞在文件系统层（maxRetries 无法中断）。
 * 三次的共同特征是：**result.json 早已写好或根本不必写，进程却永不退出**，
 * 于是包装器等 900s 超时，把「探针成功」误报成「未运行」，验收凭空少一整组。
 *
 * 已分别修掉上述三处，但「不能无限等待」这件事应该有**结构性保证**，
 * 而不是依赖把每个坑都想到。看门狗到点就写出**带看门狗原因**的 result.json
 * 并强退 —— 这样最坏情况也是「明确失败 + 原因」，而不是静默挂死。
 *
 * 默认 300s（正常全程约 45–60s，留足余量）；`--watchdog=<秒>` 可覆写，0 关闭。
 */
const WATCHDOG_S_DEFAULT = 300;
function watchdogSeconds() {
  const arg = process.argv.find((a) => a.startsWith("--watchdog="));
  if (!arg) return WATCHDOG_S_DEFAULT;
  const v = Number(arg.split("=")[1]);
  return Number.isFinite(v) && v >= 0 ? v : WATCHDOG_S_DEFAULT;
}
let watchdogFired = false;
const WATCHDOG_S = watchdogSeconds();
if (WATCHDOG_S > 0) {
  const t = setTimeout(() => {
    watchdogFired = true;
    const payload = {
      base: BASE, browser: null, exe: null, headed: HEADED, solveRan: DO_SOLVE,
      ranAt: new Date().toISOString(),
      summary: { pass, fail: fail + 1, skipped },
      checks: checks.concat([{
        u03: null, name: `看门狗：探针在 ${WATCHDOG_S}s 内未完成`,
        status: "fail",
        detail: "探针挂死（不是断言失败）。常见原因：Chrome 未启动 / profile 被占用 / " +
                "CDP 连接不返回。进程与 chrome 数可用命令行核对。",
      }]),
      consoleErrors, pageErrors, failedRequests: failedReqs, apiCalls, actionErrors,
      watchdog: { fired: true, seconds: WATCHDOG_S },
    };
    try {
      fs.mkdirSync(OUT, { recursive: true });
      fs.writeFileSync(path.join(OUT, "result.json"), JSON.stringify(payload, null, 2));
    } catch { /* 连结果都写不出就只能强退 */ }
    console.error(`\n[看门狗] 探针超过 ${WATCHDOG_S}s 未完成，已写出失败结果并退出。`);
    process.exit(3);
  }, WATCHDOG_S * 1000);
  t.unref?.();  // 不阻止正常退出
}

/* ---------------- puppeteer-core：从受管 node 工作区解析 ---------------- */
const WS = process.env.WB_NODE_WS || "C:/Users/RZF/.workbuddy/binaries/node/workspace";
const require = createRequire(WS.replace(/\/?$/, "/"));
let puppeteer;
try {
  puppeteer = require("puppeteer-core");
} catch {
  console.error("找不到 puppeteer-core。请先安装：\n" +
    `  cd "${WS}" && npm install puppeteer-core --registry=https://registry.npmmirror.com`);
  process.exit(2);
}

/* ---------------- 浏览器定位（**只查绝对路径**，不用 PATH 探测）----------------
 * Windows 上浏览器从不注册进 PATH，`which chrome` / `where chrome` 必然失败，
 * 据此判断「本机没浏览器」是本工程踩过的误诊（见 docs/reports/browser_test_capability.md）。 */
const CANDIDATES = {
  chrome: [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    (process.env.LOCALAPPDATA || "") + "/Google/Chrome/Application/chrome.exe",
  ],
  edge: [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  ],
};
/* 从版本号目录读版本，避免 `--version` 启动一个真实实例（已在跑时会直接退出） */
function fileVersion(exe) {
  try {
    const v = fs.readdirSync(path.dirname(exe))
      .filter((d) => /^\d+\.\d+\.\d+\.\d+$/.test(d)).sort().pop();
    return v || "unknown";
  } catch { return "unknown"; }
}
function findBrowser() {
  for (const name of (WANT === "auto" ? ["chrome", "edge"] : [WANT])) {
    for (const p of (CANDIDATES[name] || [])) {
      if (p && fs.existsSync(p)) return { name, exe: p, version: fileVersion(p) };
    }
  }
  return null;
}

/* ---------------- 结果收集 ---------------- */
let pass = 0, fail = 0, skipped = 0;
const checks = [];
/* u03 = U03 清单里的路径名（null 表示附加检查）；status 'unimplemented' = 功能本身不存在 */
function ok(cond, u03, name, detail) {
  if (cond) {
    pass++; console.log(`  PASS  ${name}`); checks.push({ u03, name, status: "pass" });
  } else {
    fail++; console.log(`  FAIL  ${name}${detail ? "  → " + detail : ""}`);
    checks.push({ u03, name, status: "fail", detail });
  }
  return !!cond;
}
/* ⚠️ 签名是 `skip(u03, why, name)`：`name` 可省，默认沿用 u03 路径名。
 *    刻意把 `why` 放第 2 位 —— 绝大多数调用都只想说「为什么没跑」，
 *    避免出现 `skip(a, b)` 到底是 (u03, name) 还是 (u03, why) 的元数踩坑。 */
function skip(u03, why, name) {
  const label = name || u03 || "(附加检查)";
  skipped++; console.log(`  SKIP  ${label}  → ${why}`);
  checks.push({ u03, name: label, status: "skip", detail: why });
}

const br = findBrowser();
if (!br) {
  /* 找不到浏览器 → 这是**环境缺失**，不是「本机没浏览器」的同义词：
     记录检查到的绝对路径，便于复核。 */
  console.error("按绝对路径未找到 Chrome / Edge：");
  for (const [k, list] of Object.entries(CANDIDATES))
    for (const p of list) console.error(`  [${k}] ${p}  ${fs.existsSync(p) ? "存在" : "不存在"}`);
  process.exit(2);
}
console.log(`浏览器：${br.name} ${br.version}\n  ${br.exe}`);
console.log(`目标：${BASE}\n无头：${!HEADED}｜真实求解：${DO_SOLVE}\n产物：${OUT}\n`);

fs.mkdirSync(OUT, { recursive: true });

/* ⚠️ **每次都用全新 profile**（启动前清掉上一次的）。
 *
 * 为什么必须清：脚本末尾虽然会删 `.profile`，但那只在**正常跑到结尾**时生效。
 * 探针一旦被中途杀掉（超时、Ctrl-C、外部 kill），`.profile` 就留下来带着
 * `SingletonLock` / `SingletonCookie` 等锁文件 → 下一次 Chrome **拒绝启动这个
 * profile**，`puppeteer.launch` 卡在等 DevTools 端点。
 *
 * 症状极具误导性：探针**进程在跑**、**一个 chrome 都没有**、挂十几分钟不返回
 * （实测挂 10 分钟 vs 正常 45 秒），看起来像「探针坏了 / 环境又坏了」。
 * 删不掉（仍被占用）时退到带 pid+时间戳的唯一目录，保证一定能启动。 */
let profileDir = path.join(OUT, ".profile");
try {
  fs.rmSync(profileDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
} catch {
  profileDir = path.join(OUT, `.profile-${process.pid}-${Date.now()}`);
  fs.mkdirSync(profileDir, { recursive: true });
}

const browser = await puppeteer.launch({
  executablePath: br.exe,
  headless: HEADED ? false : true,
  userDataDir: profileDir,
  // 显式超时：Chrome 起不来时**快速失败**，不要无限等 DevTools 端点。
  // 实测遇到过 Chrome 未启动、探针静默挂着的情况（见文件顶部看门狗说明）。
  timeout: 60000,
  protocolTimeout: 120000,
  args: ["--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--disable-features=Translate", "--window-size=1440,1000"],
});
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1000 });

const consoleErrors = [], pageErrors = [], failedReqs = [], apiCalls = [], actionErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
page.on("pageerror", (e) => pageErrors.push(String(e)));
page.on("requestfailed", (r) => failedReqs.push(`${r.url()} :: ${r.failure()?.errorText || ""}`));
page.on("request", (r) => {
  const u = r.url();
  if (u.includes("/api/")) apiCalls.push({ method: r.method(), path: new URL(u).pathname });
});

const settle = (ms = 700) => new Promise((r) => setTimeout(r, ms));
const chip = (id) => page.$eval(`#${id}`, (e) => {
  const m = e.textContent.match(/(\d+)/); return m ? Number(m[1]) : null;
});
const text = (sel) => page.$eval(sel, (e) => e.textContent.trim()).catch(() => "");

/* 点击**不吞异常**：失败就记下来 */
async function click(sel, label) {
  try {
    await page.waitForSelector(sel, { timeout: 3000 });
    await page.click(sel);
    return null;
  } catch (e) {
    const msg = `${label || sel}: ${e.message.split("\n")[0]}`;
    actionErrors.push(msg);
    return msg;
  }
}
async function switchTab(name) {
  const err = await click(`.tabs button[data-tab="${name}"]`, `切到「${name}」页`);
  if (err) return err;
  try {
    await page.waitForFunction(
      (n) => document.querySelector(`#tab-${n}`)?.classList.contains("active"),
      { timeout: 3000 }, name);
    return null;
  } catch { const m = `「${name}」页未激活`; actionErrors.push(m); return m; }
}
/* 设置 number/range 输入并派发事件，触发前端重渲染 */
async function setInput(sel, value, evt = "input") {
  return page.$eval(sel, (e, v, t) => {
    e.value = String(v);
    e.dispatchEvent(new Event(t, { bubbles: true }));
    return e.value;
  }, value, evt).catch(() => null);
}
/* ⚠️ `page.select` 成功时返回**数组**（truthy），不能拿返回值当错误标记；
 *    必须显式 try/catch，否则会把成功误判成失败。 */
async function selectOption(sel, value) {
  try {
    await page.select(sel, value);
    return null;
  } catch (e) {
    return e.message.split("\n")[0];
  }
}
/* 回到干净状态：重新加载页面走一次 bootstrap。
 * 必要性：界面是**有状态**的，前一条用例改过的材料/参数会污染后一条，
 * 让失败被错误归因。 */
async function resetState() {
  await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 25000 });
  await settle(1200);
}
/* 读某个 canvas 的真实像素：尺寸 0 或全透明都算「没画出来」 */
const canvasState = (id) => page.$eval(`#${id}`, (c) => {
  let nonBlank = "n/a";
  try {
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    nonBlank = false;
    for (let i = 3; i < d.length; i += 400) if (d[i] !== 0) { nonBlank = true; break; }
  } catch { /* 无可读上下文 */ }
  return { w: c.width, h: c.height, visible: c.getBoundingClientRect().height > 0, nonBlank };
}).catch(() => null);

/* 提交并等待：成功则求解次数 +1；失败则 #submit-msg 出现错误提示 */
async function submitAndWait(maxMs = 90000) {
  const before = await chip("chip-solve");
  const err = await click("#submit-run", "点击提交计算");
  if (err) return { err, before, after: before, msg: "" };
  let waited = 0;
  while (waited < maxMs) {
    await settle(1000); waited += 1000;
    const after = await chip("chip-solve");
    if (after !== before) return { before, after, msg: await text("#submit-msg"), waited };
    const msg = await text("#submit-msg");
    /* 失败路径会把 msg 换成「请求失败」或「准入校验未通过」 */
    if (/请求失败|准入校验|失败/.test(msg)) return { before, after, msg, waited };
  }
  return { before, after: await chip("chip-solve"), msg: await text("#submit-msg"), waited, timeout: true };
}
/* 走一次「切参数面板 → 设定入射角与快照策略 → 提交」。
 * 快照设为 events 是为了让 U03-8（回放）有快照可拖。 */
async function doSolve({ snap = "events", inc = "0" } = {}) {
  await switchTab("params");
  if (await page.$("#p-inc")) await setInput("#p-inc", inc, "change");
  if (snap && await page.$("#p-snap")) await selectOption("#p-snap", snap);
  await settle(400);
  if (await page.$eval("#submit-run", (e) => e.disabled)) return { skipped: "#submit-run 被禁用" };
  apiCalls.length = 0;
  return await submitAndWait();
}
/* 历史面板是否为空：无运行时 `#h-run` 会有**一个占位 option（value=""）**，
 * 因此不能用「选项数为 0」判断（踩过：把占位当成可读的运行 → 计为失败）。 */
const historyEmpty = () => page.$eval("#h-run", (e) => {
  const opts = [...e.options];
  return opts.length === 0 || (opts.length === 1 && opts[0].value === "");
}).catch(() => true);

try {
  /* ================= U03-1 启动 ================= */
  console.log("[U03-1] 启动");
  const resp = await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 25000 });
  await settle(1200);
  const boot = await page.evaluate(() => {
    const be = document.querySelector("#boot-error");
    const visible = be && getComputedStyle(be).display !== "none" &&
      be.getBoundingClientRect().height > 0;
    return { title: document.title, bootErrorVisible: !!visible, bootText: be ? be.textContent.trim() : "" };
  });
  ok(resp && resp.status() === 200, "U03-1 启动", `首页返回 200（实际 ${resp && resp.status()}）`);
  ok(!boot.bootErrorVisible, "U03-1 启动", "页面未显示 boot-error 面板", boot.bootText);
  ok(pageErrors.length === 0, "U03-1 启动", "无未捕获 JS 异常", pageErrors.slice(0, 2).join(" | "));
  const realFailed = failedReqs.filter((u) => !/favicon/.test(u));
  ok(realFailed.length === 0, "U03-1 启动", "无失败请求（favicon 除外）", realFailed.slice(0, 2).join(" | "));
  const realConsole = consoleErrors.filter((t) => !/favicon|404/.test(t));
  ok(realConsole.length === 0, "U03-1 启动", "无 console error（favicon 404 除外）", realConsole.slice(0, 2).join(" | "));
  console.log(`        标题：${boot.title}`);

  /* 面板可切换（隐藏面板里的控件点不到，是最常见的假通过来源） */
  const TABS = ["result", "ref", "table", "caps", "history", "params"];
  let tabOk = 0;
  for (const t of TABS) { if (!(await switchTab(t))) tabOk++; }
  ok(tabOk === TABS.length, null, `${tabOk}/${TABS.length} 个面板可切换并激活`,
    actionErrors.slice(-2).join(" | "));

  /* ================= U03-2 选真实数据 ================= */
  console.log("\n[U03-2] 选真实数据");
  await switchTab("params");
  const mats = await page.$$eval("#material-select option", (o) => o.map((x) => x.value));
  /* 真实材料 = 排除合成演示卡 */
  const realMat = mats.find((m) => /zirconia_ysz_machining/.test(m)) ||
                  mats.find((m) => !/synthetic/i.test(m));
  if (!realMat) skip("U03-2 选真实数据", "材料卡清单里没有非合成材料");
  else {
    const err = await selectOption("#material-select", realMat);
    let selOk = false;
    if (!err) {
      await settle(1200);
      selOk = (await page.$eval("#material-select", (e) => e.value)) === realMat;
    }
    ok(selOk, "U03-2 选真实数据", `可选中真实材料卡并回读一致（${realMat}）`, err || "");
    /* 相邻联动：能力门控与材料卡元信息必须随选择更新，而不是停在上一张卡 */
    const meta = await text("#material-meta");
    const gate = await text("#gate-box");
    ok(meta.length > 0, "U03-2 选真实数据", "材料卡元信息随选择更新（#material-meta 非空）",
      `#material-meta 为空；gate=${gate.slice(0, 80)}`);
    console.log(`        选中：${realMat}\n        门控：${gate.slice(0, 110)}`);
  }
  /* 模板载入同样属于「选真实数据」——且必须**整体**生效，而不是只回填数值 */
  const tpls = await page.$$eval("#template-select option", (o) => o.map((x) => x.value)).catch(() => []);
  if (!tpls.length) skip("U03-2 选真实数据", "无可用模板（#template-select 为空）");
  else {
    const err = await selectOption("#template-select", tpls[0]);
    const err2 = err ? err : await click("#load-template", "载入模板");
    await settle(1500);
    const eVal = await page.$eval("#p-E", (e) => e.value).catch(() => "");
    ok(!err2 && eVal !== "", "U03-2 选真实数据", `真实算例模板可载入并写入参数（${tpls[0]}，E=${eVal}）`,
      err2 || "载入后 #p-E 为空");

    /* 「载入/重置为该模板」应当把该模板的**材料卡与运行模式**也带过来。
     * 只回填数值参数、把身份留在上一个选择上，会让随后提交被配置层拒绝
     * （材料与模式不匹配），而用户看到的是「参数都填好了却提交失败」。 */
    const want = await page.evaluate(async (tid) => {
      const r = await fetch("/api/examples/" + encodeURIComponent(tid));
      const j = await r.json();
      const p = j.params || j;
      return { materialId: p.material_id, runMode: p.run_mode };
    }, tpls[0]).catch(() => null);
    const cards = await page.$$eval("#material-select option", (o) => o.map((x) => x.value));
    if (!want || !want.materialId) {
      skip("U03-2 选真实数据", "无法读取模板的 material_id");
    } else if (!cards.includes(want.materialId)) {
      /* 模板用的是测试夹具材料（不在材料卡清单里）→ 该断言不适用 */
      skip("U03-2 选真实数据",
        `模板材料 ${want.materialId} 不在材料卡清单（测试夹具），不适用材料回填断言`);
    } else {
      const gotMat = await page.$eval("#material-select", (e) => e.value);
      ok(gotMat === want.materialId, "U03-2 选真实数据",
        `载入模板后材料卡随之回填（期望 ${want.materialId}，实际 ${gotMat}）`,
        `模板 ${tpls[0]} 的 material_id=${want.materialId}，但材料卡停在 ${gotMat}`);
      const gotMode = await page.$eval("#mode-select", (e) => e.value).catch(() => "");
      ok(gotMode === want.runMode, "U03-2 选真实数据",
        `载入模板后运行模式随之回填（期望 ${want.runMode}，实际 ${gotMode}）`,
        `模板 ${tpls[0]} 的 run_mode=${want.runMode}，但模式停在 ${gotMode}`);
    }
  }

  /* ================= U03-3 提交（真实求解）================= */
  console.log("\n[U03-3] 提交");
  let solved = false;
  await resetState();                 // 切断 U03-2 的状态污染
  if (!DO_SOLVE) skip("U03-3 提交", "--no-solve 已跳过（会真实写 runs/）");
  else {
    const r = await doSolve();
    if (r.skipped) skip("U03-3 提交", r.skipped);
    else {
      solved = r.after === r.before + 1;
      ok(solved, "U03-3 提交", `提交后求解次数 +1（${r.before} → ${r.after}，等待 ${r.waited / 1000}s）`,
        r.msg.slice(0, 160));
      ok(apiCalls.some((c) => c.method === "POST" && c.path === "/api/solve"), "U03-3 提交",
        "确实发生了 POST /api/solve", JSON.stringify(apiCalls.slice(0, 6)));
      if (solved) {
        /* 结果面板必须真的画出东西（不是只有空白 canvas） */
        await switchTab("result");
        await settle(900);
        const m = await canvasState("morph-canvas");
        if (!m || !m.visible || m.w === 0) {
          skip("U03-3 提交", `结果面板无可见形貌 canvas（${m ? `${m.w}×${m.h}` : "读取失败"}）；` +
            `若该运行模式为 threshold_only，则「不提供形貌」符合协议`);
        } else ok(m.nonBlank === true, "U03-3 提交",
          `形貌 canvas 已真实绘制（${m.w}×${m.h}）`, `nonBlank=${m.nonBlank}`);
      }
    }
  }

  /* ================= U03-4 错误提示 ================= */
  console.log("\n[U03-4] 错误提示");
  if (!(await page.$("#p-inc"))) skip("U03-4 错误提示", "页面上没有入射角输入 #p-inc");
  else {
    await switchTab("params");
    /* 越界入射角：后端返回 HTTP 200 + status=failed + 结构化 errors（无 watermark）。
       期望界面显示**结构化**错误（含错误码/要求/建议），而不是原始 JS 异常。 */
    await setInput("#p-inc", "75", "change");
    await settle(500);
    const dis = await page.$eval("#submit-run", (e) => e.disabled);
    if (dis) skip("U03-4 错误提示", "越界后 #submit-run 被禁用（在配置层拦截，属可接受路径）");
    else {
      actionErrors.length = 0;
      const r = await submitAndWait(60000);
      const msg = r.msg || "";
      const isStructured = /GEOMETRY_UNSUPPORTED|入射角|超出软件支持范围/.test(msg);
      const isRawJs = /Cannot read propert|is not a function|undefined \(reading/.test(msg);
      ok(isStructured && !isRawJs, "U03-4 错误提示",
        "越界入射角后显示结构化错误（含错误码/原因/要求）",
        `实际提示 = ${msg.slice(0, 220)}`);
      ok(!r.timeout, "U03-4 错误提示", "错误提示在超时前出现（未无限等待）");
      console.log(`        提示：${msg.slice(0, 180)}`);
      /* 失败不得计入求解次数 */
      ok(r.after === r.before, "U03-4 错误提示",
        `准入失败不计入求解次数（${r.before} → ${r.after}）`);
    }
  }

  /* ================= U03-5 曲线单位（F04 观测口）================= */
  console.log("\n[U03-5] 曲线单位（F04 观测口）");
  if (!(await page.$("#t-identity"))) skip("U03-5 曲线单位", "页面上没有 #t-identity");
  else {
    await switchTab("table");
    const curves = await page.$$eval("#t-curve option", (o) => o.map((x) => x.value)).catch(() => []);
    if (!curves.length) skip("U03-5 曲线单位", "曲线清单为空");
    else {
      const bad = [];
      for (const cid of curves) {
        await selectOption("#t-curve", cid);
        await settle(600);
        const ident = await text("#t-identity");
        const line = ident.split("\n").map((s) => s.trim()).find((s) => /^x：/.test(s)) || "";
        /* 期望形如 x：<名称>（<单位>）；坏值形如 x：[object Object]（） */
        const m = line.match(/^x：(.+?)（(.*?)）/);
        const xName = m ? m[1] : "";
        const xUnit = m ? m[2] : "";
        const good = !!m && xName.length > 0 && xUnit.length > 0 && !xName.includes("[object");
        if (!good) bad.push({ cid, line: line.slice(0, 120) });
      }
      ok(bad.length === 0, "U03-5 曲线单位",
        `${curves.length} 条曲线的轴名/单位都非空且非 "[object Object]"`,
        bad.length ? `坏值 ${bad.length} 条：` + bad.map((b) => `${b.cid} → ${b.line}`).join(" ｜ ") : "");
    }
  }

  /* ================= U03-6 换参数后旧结果标记 ================= */
  console.log("\n[U03-6] 换参数后旧结果标记");
  if (!(await page.$("#stale-warning"))) skip("U03-6 换参数后旧结果标记", "页面上没有 #stale-warning");
  else if (!DO_SOLVE) {
    skip("U03-6 换参数后旧结果标记", "需要先成功提交一次（--no-solve 已跳过）");
  } else {
    await resetState();               // 过期标记需要一个「成功结果」作前提
    const r = await doSolve();
    if (r.skipped || r.after !== r.before + 1) {
      skip("U03-6 换参数后旧结果标记",
        r.skipped || `前置提交未成功（${r.before} → ${r.after}）：${(r.msg || "").slice(0, 120)}`);
    } else {
      await switchTab("params");
      const before = await chip("chip-solve");
      await setInput("#p-E", "999", "input");
      await settle(1000);
      const disp = await page.$eval("#stale-warning", (e) => getComputedStyle(e).display);
      const warn = await text("#stale-warning");
      ok(disp !== "none" && /上一次运行|尚未提交|已修改/.test(warn), "U03-6 换参数后旧结果标记",
        "改参数后出现过期标记并说明「显示的是上一次运行」",
        `display=${disp}｜文案=${warn.slice(0, 140)}`);
      ok((await chip("chip-solve")) === before, "U03-6 换参数后旧结果标记",
        `改参数本身不触发求解（${before} → ${await chip("chip-solve")}）`);
    }
  }

  /* ================= U03-7 读历史 ================= */
  console.log("\n[U03-7] 读历史");
  if (!(await page.$("#h-load"))) skip("U03-7 读历史", "页面上没有 #h-load");
  else {
    await switchTab("history");
    await settle(500);
    const enabled = await page.$eval("#h-load", (e) => !e.disabled);
    if (await historyEmpty()) {
      /* 隔离 runs 目录下若没有历史，说明前面没有成功提交（如 --no-solve）→ 如实记为未运行 */
      skip("U03-7 读历史", "历史为空（#h-run 仅有占位项）：runs/ 下无运行记录可读" +
        (DO_SOLVE ? "，且前置提交未成功" : "（--no-solve 未产生运行）"));
    } else if (!enabled) {
      skip("U03-7 读历史", "#h-load 被禁用");
    } else {
      const before = { solve: await chip("chip-solve"), read: await chip("chip-read") };
      actionErrors.length = 0; apiCalls.length = 0;
      const err = await click("#h-load", "点击读取结果");
      if (err) ok(false, "U03-7 读历史", "点得动「读取结果」", err);
      else {
        await settle(2500);
        const after = { solve: await chip("chip-solve"), read: await chip("chip-read") };
        ok(after.solve === before.solve, "U03-7 读历史",
          `读历史后求解次数不变（${before.solve} → ${after.solve}）`);
        ok(after.read === before.read + 1, "U03-7 读历史",
          `读历史后读取次数 +1（${before.read} → ${after.read}）`);
        ok(!apiCalls.some((c) => c.path === "/api/solve"), "U03-7 读历史",
          "读历史未触发 POST /api/solve");
      }
    }
  }

  /* ================= U03-8 回放不重算 ================= */
  console.log("\n[U03-8] 回放不重算");
  await switchTab("result");
  await settle(900);
  const subs = await page.$$eval("#result-body .subtabs button", (bs) => bs.map((x) => x.dataset.sub))
    .catch(() => []);
  const count0 = await chip("chip-solve");
  const replayTried = [];
  if (!subs.length) {
    skip("U03-8 回放不重算", "结果面板无子页签（当前无可用结果；需先成功提交或读取一次）");
  } else {
    /* 每个可见子页签都点一遍，并拖动其中出现的控件 */
    for (const s of subs) {
      const e = await click(`#result-body .subtabs button[data-sub="${s}"]`, `切到结果子页签「${s}」`);
      await settle(800);
      if (e) continue;
      for (const [sel, val] of [["#t-idx", 0], ["#r-stride", 5], ["#s-idx", 1]]) {
        if (await page.$(sel)) {
          await setInput(sel, val, "input");
          await settle(500);
          replayTried.push(`${s}:${sel}`);
        }
      }
      /* 图层切换与视图变换也必须不重算 */
      if (await page.$("#r-layer")) {
        const opts = await page.$$eval("#r-layer option", (o) => o.map((x) => x.value));
        if (opts.length > 1) {
          await selectOption("#r-layer", opts[opts.length - 1]);
          await settle(600); replayTried.push(`${s}:layer`);
        }
      }
      if (await page.$("#r-T")) { await page.click("#r-T").catch(() => {}); await settle(500); replayTried.push(`${s}:transpose`); }
    }
    const after = await chip("chip-solve");
    if (!replayTried.length) skip("U03-8 回放不重算", "子页签内无可见回放/视图控件（快照或深度不可用）");
    else {
      ok(after === count0, "U03-8 回放不重算",
        `拖动时间轴/切截面/换图层/转置共 ${replayTried.length} 次，求解次数不变（${count0} → ${after}）`,
        replayTried.join(", "));

      /* 回放面板必须真的能回放：控件在 → 画布要按快照尺寸绘制，
       * 且快照元信息不得是 undefined。
       * 实测（2026-09-11）：**读历史**回来的运行这两条都坏——
       *   ① 快照对象键为 index/label，渲染层读 eventIndex/timeS/passId → 显示 undefined；
       *   ② 读回 payload 的 grid 为空对象（无 nx/ny）→ drawHeatmap 传给 createImageData
       *      的是 undefined，抛 `Value is not of type 'long'`，canvas 停在 0×0（空白回放区）。
       * 实时提交路径的 payload 带这些字段，所以只有「读历史」这条路径能暴露它。 */
      const tIdx = await page.$("#t-idx");
      if (!tIdx) {
        skip("U03-8 回放不重算", "该结果无快照（未提供 #t-idx 回放滑块）");
      } else {
        await setInput("#t-idx", "1", "input");   // 拖到末帧触发重绘
        await settle(1200);
        const meta = await text("#t-meta");
        ok(!/undefined/.test(meta), "U03-8 回放不重算",
          "快照元信息完整（事件/时刻/遍次均非 undefined）",
          `#t-meta = ${meta.slice(0, 160)}`);

        const tl = await canvasState("tl-canvas");
        if (!tl) skip("U03-8 回放不重算", "页面上没有 #tl-canvas");
        else {
          const sized = tl.w > 0 && tl.h > 0;
          ok(sized && tl.nonBlank === true, "U03-8 回放不重算",
            `回放画布已按快照尺寸绘制（${tl.w}×${tl.h}）`,
            tl.w === 0
              ? `#tl-canvas 停在 ${tl.w}×${tl.h}（未绘制）；` +
                `相关 JS 异常：${pageErrors.filter((s) => /createImageData|not of type/.test(s)).slice(0, 1).join("")}`
              : `nonBlank=${tl.nonBlank}`);
        }
      }
    }
  }

  /* ================= U03-9 下载 ================= */
  console.log("\n[U03-9] 下载");
  const dl = await page.evaluate(() => {
    const hits = [];
    document.querySelectorAll("a[download], button, a").forEach((el) => {
      const t = (el.textContent || "").trim();
      if (/下载|导出|export|download/i.test(t)) hits.push(t.slice(0, 40));
    });
    return { hits, hasDownloadAttr: !!document.querySelector("a[download]") };
  });
  if (!dl.hits.length && !dl.hasDownloadAttr) {
    /* 这是**功能未实现**，与「测试没跑」是两件事：如实分列。 */
    skip("U03-9 下载", "前端未实现下载/导出（index.html 无下载控件、main.js 无 Blob/createObjectURL）");
    checks[checks.length - 1].status = "unimplemented";
  } else {
    const okDl = await click(dl.hits.length ? `text=${dl.hits[0]}` : "a[download]", "点击下载");
    ok(!okDl, "U03-9 下载", `可点击下载控件（${dl.hits[0] || "a[download]"}）`, okDl || "");
  }

  /* ================= 附加：查表不重算（非 U03 主干，同属界面不变量）================= */
  console.log("\n[X] 附加：查表不递增求解次数");
  if (await page.$("#t-lookup")) {
    await switchTab("table");
    const b4 = await chip("chip-solve");
    actionErrors.length = 0; apiCalls.length = 0;
    const e = await click("#t-lookup", "点击查值");
    await settle(1500);
    if (e) ok(false, null, "点得动「查值」", e);
    else {
      ok((await chip("chip-solve")) === b4, null, `查表后求解次数不变（${b4} → ${await chip("chip-solve")}）`);
      ok(!apiCalls.some((c) => c.path === "/api/solve"), null, "查表未触发 POST /api/solve");
    }
  } else skip(null, "页面上没有 #t-lookup", "查表不递增求解次数");

  /* 所有被点击控件的响应性汇总（防止「点不到」被静默吞掉） */
  ok(actionErrors.length === 0, null, "所有被点击的控件都真实响应（无「点不到」）",
    actionErrors.slice(0, 3).join(" | "));

  await page.screenshot({ path: path.join(OUT, "shot-final.png"), fullPage: true });

} catch (e) {
  fail++;
  pageErrors.push("PROBE CRASH: " + String(e));
  console.error("探针自身异常：" + e.message);
} finally {
  fs.writeFileSync(path.join(OUT, "result.json"), JSON.stringify({
    base: BASE, browser: `${br.name} ${br.version}`, exe: br.exe,
    headed: HEADED, solveRan: DO_SOLVE, ranAt: new Date().toISOString(),
    summary: { pass, fail, skipped }, checks,
    consoleErrors, pageErrors, failedRequests: failedReqs, apiCalls, actionErrors,
  }, null, 2));

  /* ⚠️ `browser.close()` 在 Windows + 本机 Chrome 下**会永久挂住**：
   * Chrome 进程其实已经退出（进程表里一个都没有），但 CDP 连接的关闭应答不返回，
   * 于是这个 await 永不 resolve。
   *
   * 后果极具误导性：`result.json` 已写好、结果完全正常，但**探针进程不退出** →
   *   - 包装器 `tools/browser_check.py` 等到 900s 超时，把「探针成功」误报成「未运行」；
   *   - 验收总数凭空少一整组（实测：168 → 138），看起来像 U03 全坏。
   *
   * 因此这里加**超时兜底**：close 超时就直接强杀浏览器进程并继续往下走。
   * `result.json` 已经先写好，所以结果永远不会因为这个丢。 */
  await Promise.race([
    browser.close().catch(() => {}),
    new Promise((resolve) => setTimeout(resolve, 15000)),
  ]);
  try { browser.process()?.kill("SIGKILL"); } catch { /* 已退出 */ }

  /* ⚠️ **退出时不做 profile 清理** —— 这一步本身就是挂死源。
   *
   * 实测（2026-09-13）：`result.json` 15:09:52 写好，`.profile` 15:09:59 被触碰，
   * 而进程到 15:25 仍活着 —— `fs.rmSync(profileDir)` 在 Windows 上**同步阻塞**：
   * Chrome 尚未释放句柄，删除卡在文件系统层；`maxRetries` 只控制重试次数，
   * **不能中断正在阻塞的那一次删除**，所以「有界清理」实际是无界的，
   * 后面的 `process.exit()` 永远到不了。
   *
   * 残留 profile **不需要在这里处理**：启动时（那时没有 Chrome 持有句柄，
   * 删除是毫秒级）已经会清掉。清理放在启动处既可恢复又绝不阻塞退出。 */
}

/* U03 逐路径汇总，便于人工对照清单 */
const U03 = ["U03-1 启动", "U03-2 选真实数据", "U03-3 提交", "U03-4 错误提示", "U03-5 曲线单位",
  "U03-6 换参数后旧结果标记", "U03-7 读历史", "U03-8 回放不重算", "U03-9 下载"];
console.log("\nU03 逐路径：");
for (const p of U03) {
  const rs = checks.filter((c) => c.u03 === p);
  if (!rs.length) { console.log(`  ${p.padEnd(24)} —  （未产生检查）`); continue; }
  const st = rs.some((r) => r.status === "fail") ? "失败"
    : rs.every((r) => r.status === "unimplemented") ? "未实现"
    : rs.some((r) => r.status === "pass") ? "通过" : "未运行";
  console.log(`  ${p.padEnd(24)} ${st}  (${rs.filter((r) => r.status === "pass").length} 通过 / ` +
    `${rs.filter((r) => r.status === "fail").length} 失败 / ${rs.filter((r) => r.status === "skip").length} 跳过)`);
}
console.log(`\n结果：${pass} 通过 / ${fail} 失败 / ${skipped} 跳过`);
console.log(`产物：${path.join(OUT, "result.json")}`);
process.exit(fail > 0 ? 1 : 0);
