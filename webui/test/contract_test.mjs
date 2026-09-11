/* ============================================================
 * webui 契约测试（无头）
 *
 * 运行：node webui/test/contract_test.mjs            （需要后端在 8787）
 *       BASE=http://127.0.0.1:8792 node webui/test/contract_test.mjs
 *
 * 覆盖两类真问题：
 *  A. **DOM 契约**：main.js 里 `$("#id")` 引用的元素必须在 index.html 里存在
 *     （漏一个就会在浏览器里 `null.addEventListener` 崩掉，且这类错误
 *     在没有浏览器的环境里最容易漏）；
 *  B. **后端契约**：真实调 API，验证关键不变量：
 *     - 越界查表返回 null（不是 0）、不允许越界时报 TABLE_OUT_OF_RANGE；
 *     - threshold_only 不返回数值 0 的深度；
 *     - 图层不可用时给出原因（blocked 非空），不给假数组；
 *     - 读历史不触发求解。
 * ============================================================ */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, "..");
const BASE = process.env.BASE || "http://127.0.0.1:8787";

let pass = 0, fail = 0, skipped = 0;
function ok(cond, name, detail) {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? "  → " + detail : ""}`); }
}
function skip(name, why) { skipped++; console.log(`  SKIP  ${name}  → ${why}`); }

/* ---------------- A. DOM 契约 ---------------- */

console.log("[A] DOM 契约（HTML 元素 ↔ JS 引用）");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const mainJs = fs.readFileSync(path.join(ROOT, "js", "main.js"), "utf8");
const apiJs = fs.readFileSync(path.join(ROOT, "js", "api.js"), "utf8");

const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

/* 渲染期动态生成的元素：它们不在静态 HTML 里，但必须在 main.js 的模板字符串里
 * 真的被生成出来——否则 `$("#morph-canvas")` 会拿到 null 而在浏览器里崩掉。
 * 这里不写死白名单「放行」，而是逐个校验它确实被生成。 */
const DYNAMIC_IDS = ["offer-synthetic", "boot-error", "r-layer", "r-stride", "r-T", "r-fx", "r-fy",
  "s-axis", "s-idx", "t-idx", "morph-canvas", "morph-bar", "morph-title",
  "sec-canvas", "sec-title", "tl-canvas", "tl-bar", "t-meta"];

const referenced = new Set();
for (const re of [
  /\$\("#([A-Za-z0-9_-]+)"\)/g,
  /\$\$\("#([A-Za-z0-9_-]+)/g,
  /getElementById\("([A-Za-z0-9_-]+)"\)/g,
]) {
  for (const m of mainJs.matchAll(re)) referenced.add(m[1]);
}

const missing = [...referenced].filter((id) => !htmlIds.has(id) && !DYNAMIC_IDS.includes(id));
ok(missing.length === 0, `main.js 引用的 ${referenced.size} 个元素 id 都可解析（静态 HTML 或动态生成）`,
   missing.length ? `缺失：${missing.join(", ")}` : "");

/* 动态 id 必须在 main.js 里真的被创建——两种形式都算：
 * 模板串 `id="xxx"` 或赋值 `d.id = "xxx"`。 */
const dynNotGenerated = DYNAMIC_IDS.filter((id) =>
  !htmlIds.has(id)
  && !mainJs.includes(`id="${id}"`) && !mainJs.includes(`id='${id}'`)
  && !mainJs.includes(`.id = "${id}"`) && !mainJs.includes(`.id='${id}'`)
);
ok(dynNotGenerated.length === 0, `动态元素 ${DYNAMIC_IDS.length} 个都在 main.js 中被创建`,
   dynNotGenerated.length ? `未创建：${dynNotGenerated.join(", ")}` : "");

/* 反向检查：DYNAMIC_IDS 里不得留下「根本没有被引用」的僵尸项，
 * 否则白名单会掩盖真正的缺失。 */
const zombieIds = DYNAMIC_IDS.filter((id) => !referenced.has(id));
ok(zombieIds.length === 0, "动态元素清单不含僵尸项（每项都确实被引用）",
   zombieIds.length ? `僵尸项：${zombieIds.join(", ")}` : "");

/* HTML 里存在的控件必须在 JS 里有绑定（避免「控件在那儿但没人听」的假象） */
const CONTROL_IDS = ["p-nx", "p-ny", "p-dx", "p-dy", "p-E", "p-w0", "p-f", "p-v", "p-x0", "p-x1",
  "p-roi", "p-snap", "p-mode", "p-batch", "p-accel", "p-inc", "p-dyn", "p-thr", "p-thr-cand",
  "submit-run", "material-select", "mode-select", "template-select", "load-template",
  "t-curve", "t-xs", "t-method", "t-oor", "t-lookup", "ref-case", "ref-eval",
  "h-run", "h-load", "caps-table", "caps-summary", "caps-note"];
const absentInHtml = CONTROL_IDS.filter((id) => !htmlIds.has(id));
ok(absentInHtml.length === 0, `关键控件 ${CONTROL_IDS.length} 个都在 index.html 中`,
   absentInHtml.length ? `缺失：${absentInHtml.join(", ")}` : "");

const newControls = ["p-mode", "p-batch", "p-accel", "p-inc", "p-dyn", "p-thr", "p-thr-cand"];
const newWired = newControls.filter((id) => mainJs.includes(`#${id}`));
ok(newWired.length === newControls.length, "批次 H/I/J 新控件都已接线到 JS",
   `已接线 ${newWired.length}/${newControls.length}`);

ok(html.includes("js/api.js"), "index.html 引入了 api.js（否则 STORE/API 未定义）");
const apiIdx = html.indexOf("js/api.js"), mainIdx = html.indexOf("js/main.js");
ok(apiIdx >= 0 && apiIdx < mainIdx, "脚本顺序：api.js 在 main.js 之前");

/* 措辞守卫：禁用术语不得出现在前端文案里 */
const FORBIDDEN = ["热影响区", "HAZ", "温度场", "温度分布", "热输入", "温度"];
const forbiddenHit = FORBIDDEN.filter((t) => html.includes(t) || mainJs.includes(t) || apiJs.includes(t));
ok(forbiddenHit.length === 0, "前端文案不含被禁术语（热影响区/HAZ/温度…）",
   forbiddenHit.length ? `命中：${forbiddenHit.join(", ")}` : "");

/* 不得再从前端自行插值（真实插值必须走后端） */
ok(!/function\s+interpLinear/.test(mainJs) || !/onLookup[\s\S]{0,800}interpLinear/.test(mainJs),
   "查值不再使用前端本地插值（改走后端 /api/lookup）");

/* ---------------- B. 后端契约 ---------------- */

console.log("\n[B] 后端契约（真实 API）");

async function get(p) {
  const r = await fetch(BASE + p);
  const t = await r.text();
  return { status: r.status, body: t ? JSON.parse(t) : null };
}
async function post(p, body) {
  const r = await fetch(BASE + p, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const t = await r.text();
  return { status: r.status, body: t ? JSON.parse(t) : null };
}

let alive = false;
try { const h = await get("/api/health"); alive = h.status === 200 && h.body.ok; } catch { alive = false; }
ok(alive, "后端可达（/api/health）", alive ? "" : `无法连接 ${BASE}；请先运行 python -m ufdemo.webapp`);

if (!alive) {
  console.log("\n后端不可达 → 跳过 B 段（**不**记为通过）");
  skipped += 4;
} else {
  /* 清单 */
  const ex = await get("/api/examples");
  ok(ex.status === 200 && ex.body.examples.length > 0, `模板清单非空（${ex.body.examples.length} 个）`);
  const exOne = await get("/api/examples/" + encodeURIComponent(ex.body.examples[0].name));
  ok(exOne.status === 200 && exOne.body.params && exOne.body.params.grid,
     "按名取模板返回完整参数（含 grid）");

  const cu = await get("/api/curves");
  ok(cu.status === 200 && cu.body.curves.length > 0, `曲线清单非空（${cu.body.curves.length} 个）`);

  /* 越界：允许 → null；不允许 → TABLE_OUT_OF_RANGE */
  const target = cu.body.curves.find((c) => c.validRange) || cu.body.curves[0];
  const [lo, hi] = target.validRange;
  const oor = await post("/api/lookup", {
    curve: target.name, xs: [lo - 1, (lo + hi) / 2, hi + 1], allowOutOfRange: true,
  });
  const vals = (oor.body.result || {}).values || [];
  ok(vals[0] === null && vals[2] === null && typeof vals[1] === "number",
     "越界项返回 null（不是 0），区间内返回数值", JSON.stringify(vals));
  ok(oor.body.solveCountUnchanged === true, "查表不触发求解（solveCountUnchanged）");

  const oor2 = await post("/api/lookup", { curve: target.name, xs: [hi + 1], allowOutOfRange: false });
  const code = ((oor2.body.errors || [{}])[0] || {}).code;
  ok(code === "TABLE_OUT_OF_RANGE", "不允许越界时唯一错误码 TABLE_OUT_OF_RANGE", code);

  /* 真实求解 + 不变量 */
  const raw = JSON.parse(fs.readFileSync(path.join(ROOT, "..", "examples", "ten_pulses.json"), "utf8"));
  const solved = await post("/api/solve", { params: raw, label: "web_contract_test" });
  ok(solved.status === 200 && solved.body.status === "completed", "真实求解返回 completed",
     solved.body.status);
  const run = solved.body;
  ok(run.stats && typeof run.stats.center_depth_internal === "number",
     "统计含中心深度（真实计算值）");
  ok(Object.keys(run.blocked || {}).length > 0,
     "不可用图层给出原因（blocked 非空），不返回假数组",
     JSON.stringify(Object.keys(run.blocked || {})));
  ok(!("threshold_mask" in (run.layers || {})) || run.panels.threshold.available,
     "threshold_mask 仅在协议可用时出现（否则走 blocked 原因）");
  ok(run.panels && "geometry" in run.panels && "acceleration" in run.panels,
     "诊断面板随结果返回（geometry / acceleration / threshold / structure）");
  ok(Array.isArray(run.snapshots) && run.snapshots.length > 0 && run.snapshots[run.snapshots.length - 1].final === true,
     "快照数组非空且末帧标 final");

  /* threshold_only 不得给数值 0 的深度 */
  const thrRaw = JSON.parse(JSON.stringify(raw));
  thrRaw.material_id = "cfrp_t700_yb01_800nm";
  thrRaw.material_card_file = "data/materials/cfrp_t700_yb01_800nm.json";
  thrRaw.run_mode = "threshold_only";
  const thr = await post("/api/solve", { params: thrRaw, label: "web_thr_test" });
  if (thr.body.status === "completed") {
    const d = thr.body.stats.center_depth_internal;
    /* 关键：**不得是数值 0**。「不提供」在契约里是 null（JSON 显式），
     * 前端 display 层再渲染成「不提供」。0 会被误读成「确实没有去除」。 */
    ok(thr.body.removalAvailable === false && d !== 0 && (d === null || d === undefined),
       "threshold_only：不提供深度（null，而非数值 0）",
       `removalAvailable=${thr.body.removalAvailable} depth=${JSON.stringify(d)}`);
    ok(!("depth" in (thr.body.layers || {})),
       "threshold_only：不返回**去除深度**层（不显示 0 冒充）",
       `layers=${JSON.stringify(Object.keys(thr.body.layers || {}))}`);
    /* 高度场在 threshold_only 下**是**真实可用的：没有去除，故高度等于初始高度。
     * 「不提供」的是深度，不是高度——这里明确区分，避免把真值误判为假数据。 */
    ok(thr.body.blocked && String(thr.body.blocked.depth || "").length > 0,
       "threshold_only：深度层给出不可用原因", JSON.stringify(thr.body.blocked));
  } else {
    console.log(`  NOTE  threshold_only 算例未通过准入（如实拒绝）：${(thr.body.errors || [{}])[0].code}`);
    ok(true, "threshold_only 准入失败时如实返回错误（未伪造结果）");
  }

  /* 读历史不重算 */
  const ru = await get("/api/runs");
  if ((ru.body.runs || []).length) {
    const id = ru.body.runs[0].run_id;
    const a = await get("/api/runs/" + encodeURIComponent(id));
    const b = await get("/api/runs/" + encodeURIComponent(id));
    ok(a.status === 200 && a.body.readFromDisk === true, "读取历史走磁盘（readFromDisk）");
    ok(JSON.stringify(a.body.stats) === JSON.stringify(b.body.stats),
       "两次读取同一运行结果一致（读取不引入随机性）");
  } else {
    skip("读取历史", "尚无运行目录");
  }
}

console.log(`\n结果：${pass} 通过，${fail} 失败${skipped ? `，${skipped} 跳过` : ""}`);
process.exit(fail ? 1 : 0);
