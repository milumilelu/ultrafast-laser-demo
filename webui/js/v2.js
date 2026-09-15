/* ============================================================
 * V2（C1–C6）：三个工作区的界面逻辑
 *
 *   数据与工况  →  对照与标定  →  规划与结果
 *
 * 设计约束（任务书 §7）：
 *  - **薄 HTTP 层**：这里只做「取数 → 渲染」，不写物理；
 *  - **不伪造**：实验表/上游/基线找不到时如实说"没有"，不画占位假数据；
 *  - **虚拟输入必须标出来**：上游样例若带 isSynthetic，界面显著提示；
 *  - **两类质量信息分开**：标量深度误差（有实验证据）
 *    vs 覆盖/过切/均匀性（模型预测，未导入实测高度图不算验证）。
 * ============================================================ */

"use strict";

const V2 = {
  background: null,
  tables: null,
  upstream: null,
  baselines: null,
  picked: { table: null, case: null, baseline: null },
};

/* ---------------- 小工具 ---------------- */

function v2Num(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toFixed(digits);
}

function v2Pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (Number(v) * 100).toFixed(digits) + "%";
}

/** 表格式渲染；rows 为 [[左, 右], ...] */
function v2KV(rows) {
  return `<table class="kv">${rows
    .filter((r) => r && r[1] !== undefined)
    .map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`)
    .join("")}</table>`;
}

/* ---------------- 共用实验背景 ---------------- */

async function v2LoadBackground() {
  const box = document.getElementById("bg-summary");
  try {
    V2.background = await API.get("/api/shared-background");
  } catch (err) {
    if (box) box.innerHTML = `<div class="empty">读不到共用背景：${err.message || err}</div>`;
    return;
  }
  const b = V2.background;
  if (!b.ok) {
    if (box) box.innerHTML = `<div class="notice warn">共用背景不可用：${b.error}</div>`;
    return;
  }
  if (!box) return;
  const consist = b.selfConsistent
    ? `<span class="tag yes">公式自洽</span>`
    : `<span class="tag no">不自洽</span>`;
  box.innerHTML =
    v2KV([
      ["物镜后功率", `${v2Num(b.postObjectivePowerW, 4)} W <span class="hint">（脉冲能量用它算）</span>`],
      ["软件设定功率", `${v2Num(b.softwareSetpointW, 1)} W <span class="hint">（仅记录，不参与计算）</span>`],
      ["波长 / NA / M²", `${v2Num(b.wavelengthNm, 0)} nm / ${v2Num(b.numericalAperture, 2)} / ${v2Num(b.m2, 2)}`],
      ["束腰 w0（公式推导）", `${v2Num(b.waistUm, 4)} µm ${consist}`],
      ["瑞利长度 zR（公式推导）", `${v2Num(b.rayleighUm, 4)} µm`],
      ["焦点策略", `${b.focusStrategy} <span class="hint">+ ${b.geometryFeedback}（保留被动离焦）</span>`],
      ["动态入射角", b.dynamicAngle ? "开启" : "停用"],
      ["七材料**不共享**", (b.notShared || []).join("、")],
    ]) +
    `<div class="notice info" style="margin-top:10px">
       来源：${(b.provenance || {}).source || "—"}。
       <strong>光学尺寸是 nominal 值</strong>，不是槽内实测光场。
     </div>`;
}

/** 脉冲能量只读显示：E = P_物镜后 / f（不让用户填两个可冲突的值） */
async function syncPulseEnergyReadout() {
  const fKhz = parseFloat((document.getElementById("p-f-khz") || {}).value || "0");
  const out = document.getElementById("p-E-readout");
  if (!out) return;
  const hiddenF = document.getElementById("p-f");
  const hiddenE = document.getElementById("p-E");
  if (hiddenF) hiddenF.value = fKhz * 1000;
  if (!(fKhz > 0)) {
    out.value = "（填重复频率后自动算出）";
    return;
  }
  try {
    const r = await API.get(`/api/pulse-energy?f=${encodeURIComponent(fKhz * 1000)}`);
    if (r.ok) {
      out.value = `${Number(r.pulseEnergyUJ).toFixed(4)} µJ  =  ${Number(r.postObjectivePowerW).toFixed(4)} W ÷ ${(fKhz * 1000).toFixed(0)} Hz`;
      if (hiddenE) hiddenE.value = r.pulseEnergyUJ;
    } else {
      out.value = `算不出：${r.error}`;
    }
  } catch (err) {
    out.value = `算不出：${err.message || err}`;
  }
}

/* ---------------- ① 实验数据 ---------------- */

async function v2LoadExperimentTables() {
  const sel = document.getElementById("exp-table");
  if (!sel) return;
  try {
    V2.tables = await API.experimentTables();
  } catch (err) {
    sel.innerHTML = `<option value="">读取失败</option>`;
    _expInfo(`<div class="notice warn">读不到实验表：${err.message || err}</div>`);
    return;
  }
  const t = V2.tables;
  if (!t.found) {
    sel.innerHTML = `<option value="">（未找到实验数据目录）</option>`;
    _expInfo(`<div class="notice info">${t.note}<br>
      把实验 CSV 放进该目录后刷新即可。目录里的表<strong>不会被修改</strong>。</div>`);
    return;
  }
  const ok = t.tables.filter((x) => x.ok);
  sel.innerHTML = ok.length
    ? ok.map((x) => `<option value="${x.file}">${x.file}（${x.nRows} 行，${x.encoding}）</option>`).join("")
    : `<option value="">（目录里没有可用的 CSV）</option>`;
  if (ok.length) {
    V2.picked.table = ok[0].file;
    v2RenderExperimentInfo();
  }
}

function _expInfo(html) {
  const box = document.getElementById("exp-info");
  if (box) box.innerHTML = html;
}

function v2RenderExperimentInfo() {
  const t = V2.tables;
  if (!t || !t.found) return;
  const item = t.tables.find((x) => x.file === V2.picked.table);
  if (!item) return;
  if (!item.ok) {
    _expInfo(`<div class="notice warn">这张表读不了：${item.error}</div>`);
    return;
  }
  const warns = [];
  if (item.nNegative > 0) {
    warns.push(
      `<div class="notice warn"><strong>${item.nNegative} 行均值为负</strong>：
       纯去除模型无法解释负去除，标定时会<strong>排除并列出</strong>，不会强行拟合。</div>`
    );
  }
  if (item.nSkipped > 0) {
    warns.push(
      `<div class="notice warn">${item.nSkipped} 行缺必需字段，已<strong>单独列出</strong>（不猜值补齐）。</div>`
    );
  }
  _expInfo(
    v2KV([
      ["编码（自动探测）", item.encoding],
      ["行数", `采用 ${item.nRows} / 原始 ${item.nRaw}`],
      ["脉宽档", (item.pulseDurationsFs || []).map((v) => v + " fs").join("、")],
      ["频率档", (item.frequenciesKHz || []).map((v) => v + " kHz").join("、")],
      ["间距档", (item.spacingsUm || []).map((v) => v + " µm").join("、")],
      ["层数档（原表「重复加工次数」）", (item.passCounts || []).join("、")],
    ]) + warns.join("")
  );
}

/* ---------------- ① 上游轮廓 ---------------- */

async function v2LoadUpstream() {
  const sel = document.getElementById("up-case");
  const box = document.getElementById("up-info");
  if (!sel) return;
  try {
    V2.upstream = await API.upstreamCases();
  } catch (err) {
    sel.innerHTML = `<option value="">读取失败</option>`;
    return;
  }
  const cs = V2.upstream.cases || [];
  if (!cs.length) {
    sel.innerHTML = `<option value="">（没有上游样例）</option>`;
    if (box) {
      box.innerHTML = `<div class="notice info">
        上游轮廓需要在 <code>examples/upstream/&lt;算例名&gt;/</code> 下放
        <code>upstream_case.json</code> + <code>profile.csv</code>。
        真实上游轮廓由上游求解器导出（带坐标、工况、输出步号）。</div>`;
    }
    return;
  }
  sel.innerHTML = cs
    .map((c) => `<option value="${c.name}">${c.name}${c.case && c.case.isSynthetic ? "（虚拟）" : ""}</option>`)
    .join("");
  V2.picked.case = cs[0].name;
  v2RenderUpstreamInfo();
}

function v2RenderUpstreamInfo() {
  const box = document.getElementById("up-info");
  const c = (V2.upstream.cases || []).find((x) => x.name === V2.picked.case);
  if (!box || !c) return;
  if (c.error) {
    box.innerHTML = `<div class="notice warn">这个算例读不了：${c.error}</div>`;
    return;
  }
  const d = c.case;
  const synth = d.isSynthetic
    ? `<div class="notice warn"><strong>虚拟输入</strong>：这份上游轮廓是造的，
       只能用来验证链路。<strong>由它得出的对照结论无效</strong>。</div>`
    : "";
  box.innerHTML =
    synth +
    v2KV([
      ["几何类型", `${d.geometryZh || d.geometry}`],
      ["横轴", `${d.xAxis}（${d.xAxis === "r" ? "径向 —— 不是扫描方向" : "扫描方向"}）`],
      ["中心深度", `${v2Num(d.nCenter ?? d.centerDepth, 3)} µm`],
      ["点数", d.nPoints],
      ["脉冲数 / 层数", `${d.nPulses ?? "—"} / ${d.nPasses ?? "—"}`],
      ["上游版本", d.upstreamVersion || "—"],
    ]) +
    (c.radiusNote ? `<div class="notice info">${c.radiusNote}</div>` : "");
}

/* ---------------- ② 上下游对照 ---------------- */

async function v2RunCompare() {
  const box = document.getElementById("cmp-result");
  if (!box) return;
  const c = (V2.upstream.cases || []).find((x) => x.name === V2.picked.case);
  if (!c) {
    box.innerHTML = `<div class="notice warn">先在上面选一个上游算例。</div>`;
    return;
  }
  const dir = `${V2.upstream.dir}/${c.name}`;
  box.innerHTML = `<div class="notice info">正在跑对照（下游要用真实求解器重放一次）…</div>`;
  try {
    const params = buildRunConfig();
    const r = await API.upstreamCompare({ caseDir: dir, params });
    if (!r.ok) {
      box.innerHTML = `<div class="notice warn">下游重放未完成：${r.status || ""}
        ${(r.errors || []).join("、")}</div>`;
      return;
    }
    const m = r.metrics;
    const line = (label, u, d, rel) =>
      `<tr><th>${label}</th><td>${v2Num(u, 3)}</td><td>${v2Num(d, 3)}</td>
       <td>${rel === null || rel === undefined ? "—" : (rel * 100).toFixed(1) + "%"}</td></tr>`;
    box.innerHTML =
      (r.warning ? `<div class="notice warn">${r.warning}</div>` : "") +
      `<table class="kv"><tr><th>指标</th><th>上游</th><th>下游</th><th>相对差</th></tr>
        ${line("中心深度 (µm)", m.centerDepth.upstreamUm, m.centerDepth.downstreamUm, m.centerDepth.relativeDiff)}
        ${line("最大深度 (µm)", m.maxDepth.upstreamUm, m.maxDepth.downstreamUm, m.maxDepth.relativeDiff)}
        ${line("轮廓宽度 (µm)", m.width.upstreamUm, m.width.downstreamUm, m.width.relativeDiff)}
        ${line("截面积 (µm²)", m.area.upstreamUm2, m.area.downstreamUm2, m.area.relativeDiff)}
      </table>
      <div class="notice info" style="margin-top:10px">
        轮廓 RMSE <strong>${v2Num(m.profileRmseUm, 3)} µm</strong>；
        宽度定义 <code>${m.widthDefinition}</code>（两边用<strong>同一个定义</strong>才算得下去）。
      </div>
      <div class="notice info">
        <strong>两个模型吻合 ≠ 实验正确</strong>：它们可能共享同一套假设。
        最终加工深度仍以<strong>未参与拟合的实验记录</strong>为准。
      </div>`;
  } catch (err) {
    box.innerHTML = `<div class="notice warn">对照失败：${err.message || err}</div>`;
  }
}

/* ---------------- ② 标定 ---------------- */

async function v2LoadBaselines() {
  const sel = document.getElementById("cal-baseline");
  if (!sel) return;
  try {
    V2.baselines = await API.baselines();
  } catch (err) {
    sel.innerHTML = `<option value="">读取失败</option>`;
    return;
  }
  const bs = V2.baselines.baselines || [];
  if (!bs.length) {
    sel.innerHTML = `<option value="">（还没有候选基线）</option>`;
    return;
  }
  sel.innerHTML = bs
    .map((b) => `<option value="${b.file}">${b.materialFamily} / ${b.pulseDurationFs} fs（δ=${v2Num(b.deltaNm, 0)} nm）</option>`)
    .join("");
  V2.picked.baseline = bs[0];
  const tau = document.getElementById("cal-tau");
  if (tau && bs[0].pulseDurationFs) tau.value = bs[0].pulseDurationFs;
}

function v2CurrentBaseline() {
  const sel = document.getElementById("cal-baseline");
  const bs = (V2.baselines && V2.baselines.baselines) || [];
  return bs.find((b) => b.file === (sel && sel.value)) || bs[0] || null;
}

async function v2RunCalibration() {
  const box = document.getElementById("cal-result");
  if (!box) return;
  const bl = v2CurrentBaseline();
  const table = (V2.tables && V2.tables.tables || []).find((x) => x.file === V2.picked.table);
  if (!bl) {
    box.innerHTML = `<div class="notice warn">还没有候选基线，标定无从谈起。</div>`;
    return;
  }
  if (!table) {
    box.innerHTML = `<div class="notice warn">先在「数据与工况」里选一张实验表。</div>`;
    return;
  }
  const tau = parseFloat((document.getElementById("cal-tau") || {}).value || "0");
  box.innerHTML = `<div class="notice info">正在标定（每个候选增益都要真跑一遍求解器）…</div>`;
  try {
    const r = await API.calibrate({
      experimentFile: `${V2.tables.dir}/${table.file}`,
      materialCardFile: "tests/fixtures/analytic_fixture.json",
      pulseDurationFs: tau || bl.pulseDurationFs,
      materialId: `${bl.materialFamily}_${tau || bl.pulseDurationFs}fs`,
      responseOverride: {
        kind: "log_fixed",
        output_semantics: "event_depth_increment",
        fluence_basis: "incident_peak_fluence",
        depth_direction: "surface_normal",
        threshold_J_m2: bl.thresholdJm2,
        delta_m: bl.deltaM,
      },
    });
    if (!r.ok) {
      box.innerHTML = `<div class="notice warn">${(r.errors || []).map((e) => e.message).join("；")}</div>`;
      return;
    }
    const mt = r.metrics.train, mh = r.metrics.holdout;
    const warn = r.overfitWarning
      ? `<div class="notice warn"><strong>⚠️ 过拟合</strong>：${r.overfitMessage}</div>`
      : "";
    box.innerHTML =
      warn +
      `<div class="metrics">
        <div class="metric"><div class="k">标定增益 a</div><div class="v">${v2Num(r.gain, 4)}</div></div>
        <div class="metric"><div class="k">训练行</div><div class="v">${r.nTrain}</div></div>
        <div class="metric"><div class="k">留出行</div><div class="v">${r.nHoldout}</div></div>
       </div>
       <table class="kv"><tr><th></th><th>标定前</th><th>标定后</th></tr>
        <tr><th>训练 MAE (µm)</th><td>${v2Num(mt.mae_before_um, 3)}</td><td>${v2Num(mt.mae_after_um, 3)}</td></tr>
        <tr><th>留出 MAE (µm)</th><td>${v2Num(mh.mae_before_um, 3)}</td><td>${v2Num(mh.mae_after_um, 3)}</td></tr>
       </table>
       <div class="notice info" style="margin-top:10px">
         <strong>只有留出误差说明能不能迁移</strong>；训练误差只是拟合优度。
         增益作用在<strong>每次脉冲的几何更新之前</strong>，所以后续脉冲会按新表面重算离焦 ——
         它不是"给最终深度乘个系数"。
       </div>
       ${(r.notes || []).length ? `<details class="fold"><summary>技术说明（${r.notes.length} 条）</summary>
         <ul>${r.notes.map((n) => `<li>${n}</li>`).join("")}</ul></details>` : ""}`;
    V2.gain = r.gain;
  } catch (err) {
    box.innerHTML = `<div class="notice warn">标定失败：${err.message || err}</div>`;
  }
}

/* ---------------- ③ 规划 ---------------- */

/** 估算枚举耗时。**必须给** —— 域 400 + dx 0.5 时单个候选就要 20 s，
 *  25 个候选 20+ 分钟，不给预估用户会以为界面卡死。
 *
 *  ⚠️ 早期把「域 100/200/400 μm → 1.6 / 14.2 / 22.2 s」解读成
 *  「每事件成本随网格**超线性**增长、存在 per-event 全局开销」—— **那是错的**。
 *  把离焦关掉（fixed_geometry）后，域 100/200/400 μm 的单事件成本是
 *  131 / 132 / 138 µs：**网格涨 16 倍，成本只涨 5%**，不存在 O(网格) 代码路径。
 *
 *  真实关系是 `成本 ≈ 0.06 µs × 窗口格数`，而窗口之所以大，是**深孔离焦**撑出来的：
 *  深度 ≫ zR ⇒ 光斑 1.33→39 µm ⇒ ε(1e-8) 窗口直径 237 µm（吃满整个域）。
 *  实测数据与推导见 docs/reports/window_radius_policy.md。
 *
 *  `above_threshold`（超阈值开窗）按**与深度无关的严格上界**开窗
 *  （半径 = w0·sqrt(A/(2e))·margin，A = 未离焦峰值/阈值），窗口与域大小**无关**，
 *  实测 **0.79 ms/事件**（域 400 / 加工区 200 / dx 0.5，4,080 事件；
 *  对照既有口径 19.0 ms/事件 ⇒ 24.2×），且**高度场逐位一致**。 */
function v2EstimateCost() {
  const num = (id, d) => parseFloat((document.getElementById(id) || {}).value || String(d));
  const dom = num("pl-domain", 400), reg = num("pl-region", 200), dx = num("pl-dx", 1);
  const fK = num("p-f-khz", 20), v = num("p-v", 50);
  const n = dx > 0 ? Math.round(dom / dx) : 0;
  const cells = n * n;
  // 沿扫描方向的脉冲间距 = v / f。**两个输入都可能为 0**（页面刚打开、还没载入算例时
  // 扫描速度就是 0）→ pitch = 0 → 事件数 = Infinity，界面会显示「∞ 分钟」。
  // 这里显式判可用性，让界面如实说「参数未就绪」，而不是印一个假的大数/无穷。
  const pitchUm = (v > 0 && fK > 0) ? v / fK : 0;
  const ready = pitchUm > 0 && reg > 0 && n > 0;
  let events = 0;
  if (ready) {
    for (const h of [2, 4, 6, 8, 10]) {
      for (const N of [1, 2, 3, 4, 5]) {
        events += (Math.floor(reg / h) + 1) * Math.ceil(reg / pitchUm) * N;
      }
    }
  }
  // 每事件耗时（ms）—— 两种口径分开估，**不能混用一个常数**
  const polEl = document.getElementById("pl-window-policy");
  const policy = polEl ? polEl.value : "tail_epsilon";
  let perEventMs, basis;
  if (policy === "above_threshold") {
    // 超阈值开窗：窗口半径是**固定的物理尺寸**（与深度、域大小都无关），
    // 所以窗口内格数按 1/dx² 增长，每事件成本也按 1/dx² 换算。
    // 实测 0.79 ms/事件 @ 域 400 / 加工区 200 / dx 0.5（4,080 事件；既有 19.0 ms）。
    perEventMs = 0.79 * Math.pow(0.5 / Math.max(dx, 1e-6), 2);
    basis = "超阈值开窗实测：0.79 ms/事件 @ dx 0.5（窗口与域大小、深度均无关）";
  } else {
    perEventMs = cells <= 60000 ? 2 : (cells <= 250000 ? 14 : 22);
    basis = "既有 ε 尾部截断实测分档：2 / 14 / 22 ms（按格数）";
  }
  const sec = ready ? (events * perEventMs) / 1000 : null;
  return { n, cells, events, sec, region: reg, domain: dom, dx, policy, perEventMs, basis,
           ready, pulsePitchUm: pitchUm };
}

/** 刷新耗时提示（输入变化时调用） */
function v2RefreshCost() {
  const el = document.getElementById("pl-cost");
  if (!el) return;
  const e = v2EstimateCost();
  if (!e.ready) {
    // 参数没齐就别给数 —— 印「∞ 分钟」等于给了一个错数。
    el.innerHTML =
      `<div class="notice warn" style="margin:0">耗时预估暂不可用：` +
      `需要 <strong>扫描速度 v > 0</strong> 与 <strong>频率 f > 0</strong>` +
      `（当前 v = ${(document.getElementById("p-v") || {}).value || 0} mm/s，` +
      `f = ${(document.getElementById("p-f-khz") || {}).value || 0} kHz）。` +
      `请先在「数据与工况」载入算例或填好这两个参数。</div>`;
    return;
  }
  const mins = e.sec / 60;
  const t = mins < 1 ? `${e.sec.toFixed(0)} 秒` : `${mins.toFixed(1)} 分钟`;
  let cls = "info", warn = "";
  if (mins > 10) { cls = "warn"; warn = " ⚠️ <strong>很久</strong>——建议改用「超阈值开窗」，或把筛选网格 dx 调大。"; }
  else if (mins > 3) { cls = "warn"; warn = " 建议先把 dx 调大做粗筛。"; }
  const on = e.policy === "above_threshold";
  el.innerHTML =
    `<div class="notice ${cls}" style="margin:0">` +
    `窗口半径策略 <strong>${on ? "超阈值开窗" : "既有 ε 尾部截断"}</strong>；` +
    `仿真域 ${e.domain} µm ÷ dx ${e.dx} µm → <strong>${e.n}×${e.n} = ${e.cells.toLocaleString()} 格</strong>；` +
    `加工区 ${e.region} µm；25 个候选预计 <strong>${e.events.toLocaleString()} 个事件</strong>，` +
    `估算耗时<strong>约 ${t}</strong>。${warn}` +
    `<br>（${e.basis}；实际以运行为准。扫描线间距 h 与脉冲间距（沿扫描方向 ${e.pulsePitchUm.toFixed(2)} µm）共同决定事件数。` +
    (on ? " 超阈值开窗下<strong>深度逐位不变</strong>，但剂量观测量口径变小，结果会报出裁掉比例。" : "") +
    `）</div>`;
}

async function v2RunPlan() {
  const box = document.getElementById("pl-result");
  if (!box) return;
  const bl = v2CurrentBaseline();
  if (!bl) {
    box.innerHTML = `<div class="notice warn">还没有基线，先去「对照与标定」。</div>`;
    return;
  }
  const g = (id, dflt) => parseFloat((document.getElementById(id) || {}).value || String(dflt));
  const est = v2EstimateCost();
  box.innerHTML = `<div class="notice info">正在枚举 25 个候选，每个都跑一遍求解器…` +
    `仿真域 ${est.domain} µm / 加工区 ${est.region} µm / dx ${est.dx} µm ` +
    `（${est.n}×${est.n} 格，约 ${est.events.toLocaleString()} 事件，估算 ${(est.sec/60).toFixed(1)} 分钟）</div>`;
  await new Promise((r) => setTimeout(r, 50));   // 让上面这句先画出来
  let r;
  try {
    r = await API.plan({
      materialCardFile: "tests/fixtures/analytic_fixture.json",
      targetDepthUm: g("pl-target", 60),
      toleranceUm: g("pl-tol", 12),
      // 域（材料区域）与加工区（矩形槽）**分开**
      domainUm: [g("pl-domain", 400), g("pl-domain", 400)],
      regionUm: [g("pl-region", 200), g("pl-region", 200)],
      pulseDurationFs: bl.pulseDurationFs,
      repetitionRateKHz: g("p-f-khz", 20),
      scanSpeedMmS: g("p-v", 50),
      dxUm: g("pl-dx", 1),
      windowRadiusPolicy: (document.getElementById("pl-window-policy") || {}).value || "tail_epsilon",
      gain: V2.gain || 1.0,
      responseOverride: {
        kind: "log_fixed",
        output_semantics: "event_depth_increment",
        fluence_basis: "incident_peak_fluence",
        depth_direction: "surface_normal",
        threshold_J_m2: bl.thresholdJm2,
        delta_m: bl.deltaM,
      },
    });
  } catch (err) {
    box.innerHTML = `<div class="notice warn">规划失败：${err.message || err}</div>`;
    return;
  }
  if (!r.ok) { box.innerHTML = `<div class="notice warn">规划失败</div>`; return; }

  const rec = r.recommended;
  const best = r.bestEffort;
  const head = rec
    ? `<div class="notice ok"><strong>推荐：h = ${rec.spacingUm} µm，N = ${rec.layerCount} 层</strong>
         → 平均 ${v2Num(rec.meanDepthUm, 2)} µm，理想时间 ${v2Num(rec.idealTimeS, 4)} s</div>`
    : `<div class="notice warn"><strong>当前范围内无可行方案</strong><br>${r.infeasibleReason || ""}
       ${best ? `<br>下面展示的是<strong>最接近的候选（不满足约束，仅供诊断）</strong>：
       h=${best.spacingUm} µm / N=${best.layerCount} 层 → ${v2Num(best.meanDepthUm, 2)} µm` : ""}</div>`;

  const rows = (r.candidates || [])
    .map((c) => `<tr>
      <td>${c.spacingUm}</td><td>${c.layerCount}</td>
      <td>${v2Num(c.meanDepthUm, 2)}</td>
      <td>${v2Num(c.idealTimeS, 4)}</td>
      <td>${v2Pct(c.coverageFraction)}</td>
      <td>${v2Pct(c.overDepthFraction)}</td>
      <td>${c.feasible ? '<span class="tag yes">可行</span>' : `<span class="tag no">${c.status}</span>`}</td>
    </tr>`)
    .join("");

  const twoStage = r.screeningDxUm && r.finalDxUm && r.screeningDxUm !== r.finalDxUm
    ? `<div class="notice info">**两级网格**：25 个候选用 ${r.screeningDxUm} µm 粗筛，`
      + `推荐/最接近的那个用 ${r.finalDxUm} µm 细核（表里数值来自粗筛，形貌来自细核）。</div>`
    : "";
  box.innerHTML = head + twoStage +
    `<div class="section-title">全部候选（${r.nFeasible}/${r.nCandidates} 可行）</div>
     <div style="max-height:280px;overflow-y:auto">
     <table class="kv"><tr><th>间距 h (µm)</th><th>层数 N</th><th>均值 (µm)</th><th>时间 (s)</th>
       <th>覆盖</th><th>过切</th><th>状态</th></tr>${rows}</table></div>
     <div class="notice info" style="margin-top:10px">
       <strong>覆盖率 / 过切 / 均匀性是模型预测</strong>；
       没有导入实测高度图时<strong>不构成</strong>二维形貌验证。
       时间是不含换向减速的<strong>理想值</strong>。
     </div>` +
    ((r.notes || []).length
      ? `<details class="fold"><summary>技术说明（${r.notes.length} 条）</summary>
         <ul>${r.notes.map((n) => `<li>${n}</li>`).join("")}</ul></details>` : "");

  v2RenderGeometry(r.geometryBasis);
  v2DrawPocket(rec || best, rec ? "推荐方案" : "最接近候选（不满足约束）");
}

/** 几何依据：光学按**名义值冻结**；声明的单线宽度只作对照量（ADR-0020）。 */
function v2RenderGeometry(g) {
  const box = document.getElementById("pl-geom");
  if (!box) return;
  if (!g || !g.spotRadiusUm) { box.innerHTML = ""; return; }
  const parts = [];
  parts.push(`光斑半径 <strong>${v2Num(g.spotRadiusUm, 4)} µm</strong>` +
    `（名义光学 <strong>冻结</strong>，zR ${v2Num(g.rayleighRangeUm || 0, 4)} µm` +
    ` —— 与频率/能量/阈值无关）`);
  if (g.declaredLineWidthUm) {
    parts.push(`｜实测单线宽度 <strong>${v2Num(g.declaredLineWidthUm, 3)} µm</strong>` +
      `（加工结果，只作对照）`);
    if (g.lineWidthModelUm) {
      parts.push(`｜模型首击烧蚀宽度 <strong>${v2Num(g.lineWidthModelUm, 3)} µm</strong>` +
        `，声明/模型 = <strong>${v2Num(g.declaredOverModelWidth, 2)}×</strong>` +
        ` —— 差得多应查 F<sub>th</sub>/δ 或搭接模型，不是改光斑`);
    }
  }
  if (g.pulseEnergyUJ) parts.push(`｜脉冲能量 ${v2Num(g.pulseEnergyUJ, 1)} µJ`);
  box.innerHTML = parts.join(" ");
}

/** 画矩形槽：深度热图 + 垂直扫描方向的截面（最能看出搭接/漏加工）。 */
function v2DrawPocket(cand, label) {
  const titleEl = document.getElementById("pl-shape-title");
  const heatWrap = document.getElementById("pl-heat-wrap");
  const secWrap = document.getElementById("pl-sec-wrap");
  const noteEl = document.getElementById("pl-shape-note");
  const s = cand && cand.surface;
  if (!s) {
    if (titleEl) titleEl.style.display = "none";
    if (heatWrap) heatWrap.style.display = "none";
    if (secWrap) secWrap.style.display = "none";
    if (noteEl) noteEl.textContent = "";
    return;
  }
  if (titleEl) titleEl.style.display = "";
  const heat = document.getElementById("pl-surface");
  const sec = document.getElementById("pl-section-y");

  // 热图：摊平成 row-major 一维数组（与 drawHeatmap 的约定一致）
  const flat = [];
  for (const row of s.depthUm) for (const v of row) flat.push(v);
  if (heatWrap) heatWrap.style.display = "";
  const t1 = document.getElementById("pl-heat-title");
  if (t1) {
    t1.textContent = `${label}：矩形槽深度分布（${s.nx}×${s.ny} 格，${v2Num(s.dxUm, 3)} µm/格）`;
  }
  try {
    drawHeatmap(heat, null, flat, s.nx, s.ny, { unit: "µm" });
  } catch (err) {
    if (heat) heat.dataset.unavailable = String(err.message || err);
  }

  if (secWrap) secWrap.style.display = "";
  try {
    drawLineChart(sec, s.sectionYAxisUm, [{ y: s.sectionAlongYUm, label: "深度 (µm)" }],
      { height: 240 });
  } catch (err) {
    if (sec) sec.dataset.unavailable = String(err.message || err);
  }

  const st = s.stats || {};
  if (noteEl) {
    noteEl.innerHTML =
      `统计：均值 ${v2Num(st.meanUm, 2)} µm，最大 ${v2Num(st.maxUm, 2)} µm，` +
      `起伏 P95−P5 = <strong>${v2Num(st.ripplePvUm, 2)} µm</strong>。` +
      `${s.note || ""}` +
      `　⚠️ 这是<strong>模型预测</strong>；高斯光束<strong>做不到理想平底</strong>，` +
      `起伏是真实的物理结果。`;
  }
}

/* ---------------- 事件绑定 ---------------- */

function v2Bind() {
  const on = (id, ev, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener(ev, fn);
  };
  on("p-f-khz", "input", syncPulseEnergyReadout);
  on("exp-table", "change", (e) => {
    V2.picked.table = e.target.value;
    v2RenderExperimentInfo();
  });
  on("exp-load", "click", async () => {
    await v2LoadExperimentTables();
    const sel = document.getElementById("exp-table");
    if (sel && sel.value) { V2.picked.table = sel.value; v2RenderExperimentInfo(); }
  });
  on("up-case", "change", (e) => {
    V2.picked.case = e.target.value;
    v2RenderUpstreamInfo();
  });
  on("cal-baseline", "change", () => {
    const bl = v2CurrentBaseline();
    const tau = document.getElementById("cal-tau");
    if (bl && tau) tau.value = bl.pulseDurationFs;
  });
  on("cmp-run", "click", v2RunCompare);
  on("cal-run", "click", v2RunCalibration);
  on("pl-run", "click", v2RunPlan);
  ["pl-domain", "pl-region", "pl-dx", "p-f-khz", "p-v"].forEach((id) =>
    on(id, "input", v2RefreshCost));
  on("pl-window-policy", "change", v2RefreshCost);
}

async function v2Init() {
  v2Bind();
  await v2LoadBackground();
  await v2LoadExperimentTables();
  await v2LoadUpstream();
  await v2LoadBaselines();
  syncPulseEnergyReadout();
  v2RefreshCost();
}

document.addEventListener("DOMContentLoaded", v2Init);
