/* ============================================================
 * 演示前端主逻辑
 * 关键不变量（与真实工程 app.py / ui_service.py 对齐）：
 *  - 只有「提交计算」递增 solveCount；视图/图层/截面/时间轴/查表/读历史均不递增
 *  - 「读取历史结果」只递增 readCount
 *  - 参数改了未提交 → 结果区显示「上一次运行」并给出过期警告
 *  - threshold_only 不提供深度时显示「不提供」，不显示数值 0
 *  - 越界查表返回 None / 错误码 TABLE_OUT_OF_RANGE，绝不返回 0、不外推
 * 本页面所有数据均为内置模拟，不接真实求解器。
 * ============================================================ */

"use strict";

/* ---------------- 会话状态 ---------------- */

const state = {
  solveCount: 0,
  readCount: 0,
  materialId: null,        // bootstrap 后由真实材料目录设定
  runMode: null,
  templateId: null,
  baseParams: null,        // 当前模板的**完整**配置（表单只覆盖其中一部分）
  frozen: null,            // 已提交结果（结果区唯一数据源）
  submittedKey: null,      // 已提交参数指纹
  result: { layer: "height", subtab: "morph", snapIdx: -1, orient: { T: false, fx: false, fy: false }, stride: 2 },
  table: { curveId: null, result: null },
  ref: { caseId: null, result: null },
  history: { idx: 0 },
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function materialOf(id) {
  return STORE.materials.find((m) => m.id === id);
}

/* ---------------- Viridis 色带（控制点插值） ---------------- */

const VIRIDIS_STOPS = [
  [0.0, 68, 1, 84], [0.13, 71, 44, 122], [0.25, 59, 87, 139],
  [0.38, 44, 123, 142], [0.5, 33, 145, 140], [0.63, 39, 173, 129],
  [0.75, 92, 200, 99], [0.88, 170, 220, 50], [1.0, 253, 231, 37],
];

function viridis(t) {
  t = Math.min(1, Math.max(0, t));
  for (let i = 1; i < VIRIDIS_STOPS.length; i++) {
    if (t <= VIRIDIS_STOPS[i][0]) {
      const [t0, r0, g0, b0] = VIRIDIS_STOPS[i - 1];
      const [t1, r1, g1, b1] = VIRIDIS_STOPS[i];
      const k = (t - t0) / (t1 - t0);
      return [r0 + (r1 - r0) * k, g0 + (g1 - g0) * k, b0 + (b1 - b0) * k];
    }
  }
  return [253, 231, 37];
}

/* ---------------- 伪随机（固定种子可复现） ---------------- */

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ============================================================
 * 内置模拟求解器（仅演示，非真实物理求解）
 * ============================================================ */

function mockSolve(params, material, runMode, opts = {}) {
  const normalized = !material.physical;
  const nx = Math.min(201, Math.max(3, params.nx | 0));
  const ny = Math.min(201, Math.max(3, params.ny | 0));
  const dx = params.dx, dy = params.dy;
  const xs = new Float64Array(nx), ys = new Float64Array(ny);
  for (let i = 0; i < nx; i++) xs[i] = (i - (nx - 1) / 2) * dx;
  for (let j = 0; j < ny; j++) ys[j] = (j - (ny - 1) / 2) * dy;

  /* 脉冲序列 */
  let centers = [];
  const v = params.v, f = Math.max(1e-9, params.f);
  if (v > 0 && params.x1 !== params.x0) {
    const spacingUm = (v / f) * 1e6;
    const L = Math.abs(params.x1 - params.x0);
    let n = Math.min(400, Math.max(1, Math.floor(L / Math.max(spacingUm, 1e-6)) + 1));
    for (let k = 0; k < n; k++) centers.push(params.x0 + ((params.x1 - params.x0) * k) / Math.max(1, n - 1));
  } else {
    for (let k = 0; k < 100; k++) centers.push(params.x0);
  }
  const nPulses = centers.length;

  /* 峰值能流 */
  let F0, FthBase, deltaEff, fluenceUnit, depthUnit, lengthUnit;
  if (normalized) {
    F0 = (params.E * 1e3) / (2 * Math.PI * params.w0 * params.w0); // F_ref
    fluenceUnit = "F_ref"; depthUnit = "δ_ref"; lengthUnit = "L_ref";
  } else {
    F0 = (2 * params.E * 1e-6) / (Math.PI * Math.pow(params.w0 * 1e-6, 2)); // J/m²
    fluenceUnit = "J/cm²"; depthUnit = "µm"; lengthUnit = "µm";
  }

  const resp = material.response;
  const isThresholdOnly = runMode === "threshold_only" || !resp || resp.kind === "threshold_only";
  const incub = resp && resp.kind === "incubation";
  if (resp) {
    FthBase = incub ? resp.Fth1 : resp.Fth;
    deltaEff = resp.delta_eff_m || 0;
  }

  /* 复合结构：相标签场（种子随机颗粒） */
  let phaseId = null;
  if (opts.composite) {
    const rnd = mulberry32(DEMO.COMPOSITE_DIAGNOSTICS.seed);
    phaseId = new Float64Array(nx * ny);
    const particles = [];
    for (let p = 0; p < 70; p++) {
      particles.push({
        cx: (rnd() - 0.5) * nx * dx * 0.9,
        cy: (rnd() - 0.5) * ny * dy * 0.9,
        r: (0.03 + rnd() * 0.075) * nx * dx,
      });
    }
    for (let j = 0; j < ny; j++)
      for (let i = 0; i < nx; i++) {
        for (const pt of particles) {
          const ddx = xs[i] - pt.cx, ddy = ys[j] - pt.cy;
          if (ddx * ddx + ddy * ddy < pt.r * pt.r) { phaseId[j * nx + i] = 1; break; }
        }
      }
  }

  const depth = new Float64Array(nx * ny);
  const dose = new Float64Array(nx * ny);
  const mask = new Float64Array(nx * ny);
  const ncell = new Float64Array(nx * ny);
  const w0 = params.w0;
  const snapshots = [];
  const snapAt = new Set(
    [0.25, 0.5, 0.75, 1].map((q) => Math.min(nPulses - 1, Math.round(q * nPulses) - 1))
  );

  if (!isThresholdOnly) {
    for (let p = 0; p < nPulses; p++) {
      const cx = centers[p];
      for (let j = 0; j < ny; j++) {
        const ddy = ys[j];
        for (let i = 0; i < nx; i++) {
          const ddx = xs[i] - cx;
          const r2 = ddx * ddx + ddy * ddy;
          if (r2 > 9 * w0 * w0) continue;
          const F = F0 * Math.exp((-2 * r2) / (w0 * w0));
          const idx = j * nx + i;
          let Fth = FthBase, dEff = deltaEff;
          if (opts.composite && phaseId[idx] === 1) { Fth = 2.6; dEff = 0.03; }
          else if (opts.composite) { Fth = 1.0; dEff = 0.08; }
          if (incub) {
            if (F >= resp.Fth_inf) {
              ncell[idx] += 1;
              Fth = resp.Fth_inf + (resp.Fth1 - resp.Fth_inf) * Math.exp(-resp.k_inc * (ncell[idx] - 1));
            }
          }
          dose[idx] += F;
          if (F >= FthBase) mask[idx] = 1;
          if (F > Fth) {
            let inc = dEff * Math.log(F / Fth);
            if (!normalized) inc *= 1e6; // m → µm
            depth[idx] += inc;
          }
        }
      }
      if (snapAt.has(p)) {
        snapshots.push({
          eventIndex: p + 1, timeS: ((p + 1) / f).toExponential(3),
          passId: 1, final: p === nPulses - 1,
          height: Float64Array.from(depth, (d) => -d),
        });
      }
    }
  } else {
    /* 仅阈值判定：只累积剂量与掩膜，不产生深度 */
    for (let p = 0; p < nPulses; p++) {
      const cx = centers[p];
      for (let j = 0; j < ny; j++) {
        const ddy = ys[j];
        for (let i = 0; i < nx; i++) {
          const ddx = xs[i] - cx;
          const r2 = ddx * ddx + ddy * ddy;
          if (r2 > 9 * w0 * w0) continue;
          const F = F0 * Math.exp((-2 * r2) / (w0 * w0));
          const idx = j * nx + i;
          dose[idx] += F;
          if (F >= (FthBase || Infinity)) mask[idx] = 1;
        }
      }
    }
  }

  const height = Float64Array.from(depth, (d) => -d);
  let removal = 0, centerDepth = 0;
  for (let k = 0; k < depth.length; k++) { removal += depth[k]; if (depth[k] > centerDepth) centerDepth = depth[k]; }
  removal *= dx * dy;

  const layers = { height, fluence_dose: dose, threshold_mask: mask };
  const blocked = {};
  if (phaseId) layers.phase_id = phaseId;
  else blocked.phase_id = "未启用分相结构，本运行无相标签场（不返回全 0 假数组）";
  if (isThresholdOnly) {
    blocked.height = "threshold_only 结果不提供高度场（δ 缺失，不输出深度）";
    delete layers.height;
  }

  const wm = {
    material_id: material.id,
    family: material.family,
    grade: material.grade || "（未登记牌号）",
    run_mode: runMode,
    unit_system: normalized ? "归一化（无量纲）" : "物理单位",
    evidence_status: material.evidence_status,
    physical_prediction_allowed: material.physical,
    length_label: lengthUnit, depth_label: depthUnit, fluence_label: fluenceUnit,
    warnings: normalized
      ? ["归一化演示的深度单位为 δ_ref，不能标成真实 µm。", "合成结果不含实验复现结论。"]
      : [],
  };

  return {
    runId: opts.runId || `demo_${new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14)}_${material.id}`,
    status: opts.status || "completed",
    runMode, materialId: material.id,
    watermark: wm,
    eventsProcessed: nPulses, eventsTotal: nPulses,
    removalAvailable: !isThresholdOnly,
    stats: isThresholdOnly ? {} : { removal_volume_internal: removal, center_depth_internal: centerDepth },
    warnings: wm.warnings.slice(),
    errors: opts.status === "failed" ? [{ code: "RUN_CANCELLED", message: "运行被中途取消，仅有部分结果。", field_path: "run" }] : [],
    grid: { nx, ny, xs, ys },
    layers, blocked,
    depth: isThresholdOnly ? null : depth,
    snapshots,
    diagnostics: opts.composite ? DEMO.COMPOSITE_DIAGNOSTICS : null,
    paramsKey: paramsKey(params, material.id, runMode),
  };
}

/* ============================================================
 * 通用绘图
 * ============================================================ */

function drawHeatmap(canvas, barCanvas, data, nx, ny, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 600;
  const cell = Math.max(2, Math.floor(W / nx));
  const cw = cell * nx, ch = cell * ny;
  canvas.width = cw * dpr; canvas.height = ch * dpr;
  canvas.style.height = ch + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const img = ctx.createImageData(nx, ny);

  let lo = Infinity, hi = -Infinity;
  for (let k = 0; k < data.length; k++) {
    if (data[k] < lo) lo = data[k];
    if (data[k] > hi) hi = data[k];
  }
  if (opts.vmin !== undefined) lo = opts.vmin;
  if (opts.vmax !== undefined) hi = opts.vmax;
  const span = hi - lo || 1;

  for (let j = 0; j < ny; j++)
    for (let i = 0; i < nx; i++) {
      const v = data[j * nx + i];
      const [r, g, b] = viridis((v - lo) / span);
      const o = (j * nx + i) * 4;
      img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b; img.data[o + 3] = 255;
    }
  const off = document.createElement("canvas");
  off.width = nx; off.height = ny;
  off.getContext("2d").putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0, cw, ch);

  if (barCanvas) {
    const bw = 22, bh = ch;
    barCanvas.width = bw * dpr; barCanvas.height = bh * dpr;
    barCanvas.style.height = bh + "px";
    const bctx = barCanvas.getContext("2d");
    bctx.scale(dpr, dpr);
    for (let y = 0; y < bh; y++) {
      const [r, g, b] = viridis(1 - y / bh);
      bctx.fillStyle = `rgb(${r | 0},${g | 0},${b | 0})`;
      bctx.fillRect(0, y, bw, 1);
    }
    barCanvas.dataset.lo = fmtNum(lo);
    barCanvas.dataset.hi = fmtNum(hi);
    const holder = barCanvas.parentElement;
    holder.querySelector(".cb-hi").textContent = fmtNum(hi);
    holder.querySelector(".cb-lo").textContent = fmtNum(lo);
  }
}

function drawLineChart(canvas, xs, ysArr, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 640, H = opts.height || 300;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.height = H + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const padL = 64, padR = 18, padT = 16, padB = 42;
  const iw = W - padL - padR, ih = H - padT - padB;

  const allY = ysArr.flatMap((s) => s.y);
  let xLo = Math.min(...xs), xHi = Math.max(...xs);
  let yLo = Math.min(...allY, 0), yHi = Math.max(...allY);
  if (yHi === yLo) yHi = yLo + 1;
  const X = (v) => padL + ((v - xLo) / (xHi - xLo || 1)) * iw;
  const Y = (v) => padT + ih - ((v - yLo) / (yHi - yLo)) * ih;

  ctx.strokeStyle = "#e2e8f0"; ctx.fillStyle = "#64748b";
  ctx.font = "11px " + getComputedStyle(document.body).fontFamily;
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const yv = yLo + ((yHi - yLo) * g) / 4;
    ctx.beginPath(); ctx.moveTo(padL, Y(yv)); ctx.lineTo(W - padR, Y(yv)); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(fmtNum(yv), padL - 8, Y(yv) + 4);
  }
  for (let g = 0; g <= 5; g++) {
    const xv = xLo + ((xHi - xLo) * g) / 5;
    ctx.textAlign = "center"; ctx.fillText(fmtNum(xv), X(xv), H - padB + 16);
  }
  ctx.strokeStyle = "#cbd5e1";
  ctx.strokeRect(padL, padT, iw, ih);

  if (opts.xlabel) { ctx.textAlign = "center"; ctx.fillText(opts.xlabel, padL + iw / 2, H - 8); }
  if (opts.ylabel) {
    ctx.save(); ctx.translate(14, padT + ih / 2); ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center"; ctx.fillText(opts.ylabel, 0, 0); ctx.restore();
  }

  const colors = ["#0d9488", "#b45309", "#1d4ed8"];
  ysArr.forEach((s, si) => {
    ctx.strokeStyle = s.color || colors[si % colors.length];
    ctx.lineWidth = 2;
    ctx.setLineDash(s.dash || []);
    ctx.beginPath();
    s.x.forEach((v, i) => { i ? ctx.lineTo(X(v), Y(s.y[i])) : ctx.moveTo(X(v), Y(s.y[i])); });
    ctx.stroke();
    ctx.setLineDash([]);
    if (s.markers) {
      ctx.fillStyle = "#fff";
      s.x.forEach((v, i) => {
        ctx.beginPath(); ctx.arc(X(v), Y(s.y[i]), 4, 0, 7);
        ctx.fill(); ctx.stroke();
      });
    }
  });

  /* 图例 */
  ctx.textAlign = "left";
  let lx = padL + 10;
  ysArr.forEach((s, si) => {
    if (!s.name) return;
    ctx.fillStyle = s.color || colors[si % colors.length];
    ctx.fillRect(lx, padT + 8, 14, 3);
    ctx.fillStyle = "#475569";
    ctx.fillText(s.name, lx + 20, padT + 13);
    lx += 24 + ctx.measureText(s.name).width + 22;
  });
}

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e5 || a < 1e-3)) return v.toExponential(2);
  return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(4);
}

/* ============================================================
 * 渲染：侧边栏
 * ============================================================ */

function renderSidebar() {
  const m = materialOf(state.materialId);

  /* 材料卡 select */
  const sel = $("#material-select");
  sel.innerHTML = STORE.materials.map((x) => {
    const label = `${x.id}｜${x.family}${x.grade ? "／" + x.grade : ""}｜证据 ${x.evidence_status}`;
    return `<option value="${x.id}" ${x.id === state.materialId ? "selected" : ""}>${label}</option>`;
  }).join("");

  /* 能力表 */
  $("#cap-table").innerHTML =
    "<table class='data'><thead><tr><th>能力</th><th>可用</th><th>说明</th></tr></thead><tbody>" +
    Object.entries(m.capabilities).map(([name, c]) =>
      `<tr><td>${name}</td><td><span class="tag ${c.available ? "yes" : "no"}">${c.available ? "是" : "否"}</span></td>` +
      `<td>${c.reason}${c.missing ? `<br><span style="color:var(--text-3)">缺：${c.missing.join(", ")}</span>` : ""}</td></tr>`
    ).join("") + "</tbody></table>";

  /* 运行模式 */
  const modes = m.allowed_run_modes.length ? m.allowed_run_modes : ["threshold_only"];
  if (!modes.includes(state.runMode)) state.runMode = modes[0];
  $("#mode-select").innerHTML = modes.map((md) =>
    `<option value="${md}" ${md === state.runMode ? "selected" : ""}>${RUN_MODES[md]}</option>`).join("");
  $("#mode-select").disabled = !m.allowed_run_modes.length;

  /* 能力门控 */
  const gate = $("#gate-box");
  if (!m.allowed_run_modes.length) {
    gate.className = "notice error";
    gate.innerHTML = `不可用：材料卡缺响应参数（${m.readiness}），任何模式都无法在配置层通过。` +
      `<div class="btn-row"><button class="btn" id="offer-synthetic">改用合成示例（需显式选择）</button></div>` +
      `<div style="margin-top:6px;font-size:12px">合成示例为无量纲演示，只验证流程与界面，不输出物理结论。</div>`;
    $("#offer-synthetic").addEventListener("click", () => {
      state.materialId = "synthetic_demo_isotropic";
      state.runMode = "synthetic_normalized";
      loadTemplate("synthetic_demo_point.json");
      renderAll();
    });
  } else if (m.physical) {
    gate.className = "notice ok";
    gate.textContent = `能力门控通过：模式 ${RUN_MODES[state.runMode]} 与材料卡 ${m.readiness} 匹配。`;
  } else {
    gate.className = "notice warn";
    gate.textContent = `可用但为非物理预测（证据状态：${m.evidence_status}）；单位一律为归一化量。`;
  }

  /* 激光条件 + 限制 */
  $("#material-meta").innerHTML =
    `<div class="watermark">激光条件：${m.laser}</div>` +
    (m.limitations || []).map((l) => `<div class="notice warn" style="margin:6px 0">⚠ ${l}</div>`).join("");

  renderLedger();
}

function renderLedger() {
  $("#ledger").innerHTML =
    `提交求解次数 : <b>${state.solveCount}</b><br>` +
    `读取历史次数 : <b>${state.readCount}</b><br>` +
    `视图/图层/时间轴/截面：不增加求解次数`;
  $("#chip-solve").innerHTML = `求解 <b>${state.solveCount}</b>`;
  $("#chip-read").innerHTML = `读取 <b>${state.readCount}</b>`;
}

/* ============================================================
 * 渲染：参数面板
 * ============================================================ */

/* 表单值 → **真实运行配置**（与 examples/*.json 同构）。
 *
 * 表单只暴露网格/光束/路径/输出的少数字段，其余（结构参数、阈值协议、
 * 求解模式、几何修正等）取自当前模板，因此这里以 baseParams 为底板做覆盖，
 * 而不是凭空拼一个配置。 */
function buildRunConfig() {
  const base = JSON.parse(JSON.stringify(state.baseParams || {}));
  base.grid = base.grid || {};
  base.laser = base.laser || {};
  base.path = base.path || {};
  base.output = base.output || {};
  base.solver = base.solver || {};

  const um = 1e-6;
  base.grid.nx = Math.round(+$("#p-nx").value);
  base.grid.ny = Math.round(+$("#p-ny").value);
  base.grid.dx_m = +$("#p-dx").value * um;
  base.grid.dy_m = +$("#p-dy").value * um;
  base.laser.pulse_energy_J = +$("#p-E").value * um * 1; // µJ → J
  base.laser.spot_radius_m = +$("#p-w0").value * um;
  base.laser.repetition_rate_Hz = +$("#p-f").value;

  const segs = base.path.segments || (base.path.segments = [{}]);
  const s0 = segs[0];
  const v = +$("#p-v").value;
  const x0 = +$("#p-x0").value * um;
  const x1 = +$("#p-x1").value * um;
  s0.start_xyz_m = [x0, 0, 0];
  s0.end_xyz_m = [x1, 0, 0];
  if (v > 0 && x1 !== x0) {
    s0.speed_m_s = v;
    delete s0.end_s;                       // 有速度时由起止点与速度决定时长
  } else {
    delete s0.speed_m_s;
  }

  if (base.output.roi && base.output.roi.length) {
    base.output.roi[0].radius_m = +$("#p-roi").value * um;
    base.output.roi[0].center_xy_m = [0, 0];
  }
  base.output.snapshot_policy = $("#p-snap").value;

  /* 批次 H/I/J 的新开关 */
  if ($("#p-mode")) base.solver.mode = $("#p-mode").value;
  if ($("#p-batch")) base.solver.batch_size = Math.round(+$("#p-batch").value);
  if ($("#p-accel")) base.solver.acceleration = $("#p-accel").value;
  if ($("#p-inc")) {
    const deg = +$("#p-inc").value;
    const t = (deg * Math.PI) / 180;
    base.laser.direction_unit = [Math.sin(t), 0, Math.cos(t)];
  }
  if ($("#p-dyn")) base.solver.dynamic_angle = $("#p-dyn").checked;
  if ($("#p-thr")) base.solver.threshold_protocol = $("#p-thr").checked;
  if ($("#p-thr-cand")) base.solver.candidate_index = Math.round(+$("#p-thr-cand").value);

  /* 材料与模式 */
  base.material_id = state.materialId;
  base.run_mode = state.runMode;
  return base;
}

function paramsKey(cfg) {
  return JSON.stringify(cfg);
}

function isStale() {
  if (!state.frozen) return false;
  /* 历史读取的结果不对应当前表单 → 一律按「上一次运行」显示 */
  if (!state.submittedKey) return true;
  if (!state.baseParams) return false;
  return paramsKey(buildRunConfig()) !== state.submittedKey;
}

/* 载入模板：按名向后端取**完整参数**（清单里只给元信息）。 */
async function loadTemplate(tid) {
  try {
    const resp = await API.get("/api/examples/" + encodeURIComponent(tid));
    const p = resp.params || {};
    state.templateId = tid;
    state.baseParams = p;
    const g = p.grid || {}, L = p.laser || {}, o = p.output || {};
    const seg0 = ((p.path || {}).segments || [])[0] || {};
    const um = 1e-6;
    $("#p-nx").value = g.nx;
    $("#p-ny").value = g.ny;
    $("#p-dx").value = (g.dx_m / um).toFixed(4);
    $("#p-dy").value = (g.dy_m / um).toFixed(4);
    $("#p-E").value = (L.pulse_energy_J / um).toFixed(6);
    $("#p-w0").value = (L.spot_radius_m / um).toFixed(3);
    $("#p-f").value = L.repetition_rate_Hz;
    $("#p-v").value = seg0.speed_m_s || 0;
    $("#p-x0").value = ((seg0.start_xyz_m || [0, 0, 0])[0] / um).toFixed(2);
    $("#p-x1").value = ((seg0.end_xyz_m || [0, 0, 0])[0] / um).toFixed(2);
    if (o.roi && o.roi.length) $("#p-roi").value = (o.roi[0].radius_m / um).toFixed(2);
    $("#p-snap").value = o.snapshot_policy || "events";
    /* 批次 H/I/J 开关回填 */
    const sol = p.solver || {};
    if ($("#p-mode")) $("#p-mode").value = sol.mode || "reference";
    if ($("#p-batch")) $("#p-batch").value = sol.batch_size || 64;
    if ($("#p-accel")) $("#p-accel").value = sol.acceleration || "off";
    if ($("#p-dyn")) $("#p-dyn").checked = !!sol.dynamic_angle;
    if ($("#p-thr")) $("#p-thr").checked = !!sol.threshold_protocol;
    if ($("#p-thr-cand")) $("#p-thr-cand").value = sol.candidate_index || 0;
    const du = L.direction_unit || [0, 0, 1];
    if ($("#p-inc")) {
      const kz = Math.max(-1, Math.min(1, du[2]));
      $("#p-inc").value = ((Math.acos(kz) * 180) / Math.PI).toFixed(2);
    }
    $("#template-select").value = tid;
    const msg = $("#submit-msg");
    if (msg) { msg.style.display = "none"; }
  } catch (err) {
    const msg = $("#submit-msg");
    if (msg) {
      msg.className = "notice warn";
      msg.style.display = "block";
      msg.textContent = `载入模板失败：${err.message || err}`;
    }
  }
}

function renderParamPanel() {
  $("#template-select").innerHTML = STORE.examples.map((t) =>
    `<option value="${t.name}" ${t.name === state.templateId ? "selected" : ""}>${t.label}（${t.name}）</option>`).join("");

  const stale = isStale();
  $("#stale-warning").style.display = stale ? "block" : "none";
  if (stale) {
    const fr = state.frozen && state.frozen.runId ? state.frozen.runId : "（无）";
    $("#stale-warning").innerHTML =
      `参数已修改但<strong>尚未提交</strong>：结果区显示的是「上一次运行」（${fr}），不是这些新参数的结果。`;
  }
  const m = materialOf(state.materialId);
  $("#submit-run").disabled = !m || !(m.allowed_run_modes || []).length;
}

/* 图层显示名：从后端 /api/layers 取（与 Streamlit 侧同源），不前端硬编码一份。 */
function layerLabel(key) {
  const meta = STORE.layerMeta && STORE.layerMeta.layers;
  return (meta && meta[key] && meta[key].label) || key;
}

/* 唯一求解入口：调用真实求解器（POST /api/solve）。
 *
 * 计数语义：**真正调用了求解器才递增 solveCount**。
 * 准入校验失败（后端返回 status="failed"）时求解器根本没跑，因此不计数——
 * 这样界面上「求解次数」精确等于「求解器被调用的次数」，可被外部核对。 */
async function onSubmit() {
  const m = materialOf(state.materialId);
  if (!m) return;
  const btn = $("#submit-run");
  const msg = $("#submit-msg");
  const p = buildRunConfig();
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  msg.className = "notice info";
  msg.style.display = "block";
  msg.textContent = "正在调用真实求解器…";
  renderLedger();

  try {
    const payload = await API.solve(p, state.runMode);
    const adapted = adaptRun(payload);

    if (adapted.status === "failed") {
      const errs = adapted.errors || [];
      msg.className = "notice warn";
      msg.innerHTML = `<strong>准入校验未通过，未执行求解（求解次数不变）：</strong><br>` +
        errs.map((e) => `· ${e.code || ""} ${e.message || ""}${e.requirement ? `（要求：${e.requirement}）` : ""}`).join("<br>");
      state.frozen = adapted;   // 结果区如实显示失败原因
      state.submittedKey = paramsKey(p);
      renderResultPanel();
      switchTab("result");
      return;
    }

    state.solveCount += 1;      // 仅此处递增
    state.frozen = adapted;
    state.submittedKey = paramsKey(p);
    const layerNames = Object.keys(adapted.layers);
    state.result.layer = adapted.layers.height ? "height" : (layerNames[0] || null);
    state.result.snapIdx = adapted.snapshots.length - 1;

    msg.className = "notice ok";
    msg.textContent = `已提交并完成：${adapted.runId}（求解次数 ${state.solveCount}，耗时 ${(adapted.elapsedS || 0).toFixed(3)} s）`;
    renderLedger();
    renderParamPanel();
    renderResultPanel();
    switchTab("result");
  } catch (err) {
    msg.className = "notice warn";
    msg.innerHTML = `<strong>请求失败：</strong>${err.message || String(err)}` +
      (err.suggestion ? `<br>${err.suggestion}` : "") +
      ((err.errors || []).map((e) => `<br>· ${e.code} ${e.message}`).join(""));
  } finally {
    btn.disabled = wasDisabled;
  }
}

/* ============================================================
 * 渲染：结果面板
 * ============================================================ */

const STATUS_ZH = { completed: "已完成", failed: "失败", cancelled: "已取消" };

/* 「项-值」表：后端已把诊断格式化成字符串行（见 ui_service._fmt_cell），
 * 前端只负责显示，不再自行判断类型或格式。 */
function kvTable(rows, title) {
  if (!rows || !rows.length) return "";
  return `<div class="section-title">${title}</div><table class="data"><tbody>` +
    rows.map((r) => `<tr><td style="width:46%">${r["项"]}</td>
      <td style="font-family:var(--mono)">${r["值"] === null || r["值"] === undefined ? "—" : r["值"]}</td></tr>`).join("") +
    `</tbody></table>`;
}

/* 批次 H/I/J 的诊断面板：几何修正 / 冻结几何批量 / 受限阈值协议 / 相结构。
 * 未启用的部分**不显示**，而不是显示全 0 假数据。 */
function diagnosticPanels(f) {
  const p = f.panels || {};
  const rows = f.rows || {};
  let html = "";

  const geo = p.geometry;
  if (geo && geo.enabled) {
    const lossy = geo.lossy_approximation;
    html += `<div class="notice ${lossy ? "warn" : "info"}" style="margin:8px 0">
      <strong>几何修正</strong>：${geo.oblique_incidence ? "斜入射" : "正入射"}${geo.dynamic_angle ? " + 动态角度（逐点法向）" : ""}
      ｜光轴夹角 ${fmtNum(geo.incidence_deg_axial)}°｜窗口内最大入射角 ${fmtNum(geo.max_incidence_deg)}°
      ｜法向厚度转换 ${geo.normal_thickness_conversions} 次｜含遮挡事件 ${geo.n_events_with_shadowing} 个
      ${lossy ? "<br><strong>含近似</strong>：法向厚度按一阶关系 Δh=-a_n/n_z 换算；遮挡/背向单元的直接照射记为 0（不代表材料内部无响应）。" : ""}
      <br><span style="color:var(--text-3)">${geo.boundary_note || ""}</span>
    </div>`;
    html += kvTable(rows.geometry, "几何修正明细");
  }

  const acc = p.acceleration;
  if (acc) {
    const grouped = !!acc.grouped;
    html += `<div class="notice ${grouped ? "ok" : "info"}" style="margin:8px 0">
      <strong>求解模式</strong>：${grouped ? `冻结几何分组（批大小 ${fmtNum(acc.batch_size_configured)}）` : "逐脉冲参考"}
      ｜实际后端 ${acc.effective_backend || "—"}
      ${acc.fallback_reason ? `<br>未启用批量：${acc.fallback_reason}` : ""}
      ${acc.snapshot_on_block_boundary ? "<br><strong>快照在块边界记录</strong>（回放粒度近似）。" : ""}
      ${acc.boundary_note ? `<br><span style="color:var(--text-3)">${acc.boundary_note}</span>` : ""}
    </div>`;
    html += kvTable(rows.acceleration, "批量诊断明细");
  }

  const thr = p.threshold;
  if (thr && thr.available) {
    html += `<div class="notice info" style="margin:8px 0">
      <strong>受限阈值协议</strong>：观测 ${thr.observable || "—"}｜基准 ${thr.fluence_basis || "—"}｜阈值 ${fmtNum(thr.threshold_internal)}
      ｜超阈单元 ${fmtNum(thr.exceeded_cells_final)}
      <br>该面板记录的是<strong>分类标记</strong>：只按本事件入射能流判是否超阈，<strong>不产生去除量、不参与深度更新</strong>，也不代表任何热学量。
    </div>`;
    html += kvTable(rows.threshold, "阈值协议明细");
  } else if (f.blocked && f.blocked.threshold_mask) {
    html += `<div class="notice info" style="margin:8px 0"><strong>阈值标记不可用</strong>：${f.blocked.threshold_mask}</div>`;
  }

  const st = p.structure;
  if (st && Object.keys(st).length) {
    html += `<div class="notice info" style="margin:8px 0"><strong>相结构诊断</strong>：
      类型 ${st.structure_type || "—"}｜目标体积分数 ${fmtNum(st.target_volume_fraction)}｜实际体积分数 ${fmtNum(st.actual_volume_fraction)}
      ${st.note ? `<br><span style="color:var(--text-3)">${st.note}</span>` : ""}</div>`;
  }

  return html;
}

function renderResultPanel() {
  const box = $("#result-body");
  const f = state.frozen;
  if (!f) {
    box.innerHTML = `<div class="empty"><div class="big">尚无结果</div>
      请在「参数与运行」里提交计算，或在「历史运行」里读取已有结果。</div>`;
    return;
  }
  const m = materialOf(f.materialId);
  const wm = f.watermark;
  const stale = isStale();

  const layerNames = Object.keys(f.layers);
  if (!layerNames.includes(state.result.layer)) state.result.layer = layerNames[0] || null;

  let html = "";
  html += stale
    ? `<div class="notice warn">当前参数已修改，下方显示的是 <strong>${f.runId}</strong>；要按新参数出结果请重新「提交计算」。</div>`
    : `<div class="notice ok">结果状态：${f.runId}（最新提交）</div>`;

  html += `<div class="watermark">${wm.material_id}｜${wm.family}／${wm.grade}｜模式 ${wm.run_mode}｜单位 ${wm.unit_system}｜证据 ${wm.evidence_status}｜${wm.physical_prediction_allowed ? "物理预测" : "非物理预测"}</div>`;
  wm.warnings.forEach((w) => (html += `<div class="notice warn" style="margin:6px 0">⚠ ${w}</div>`));

  const st = f.stats;
  html += `<div class="metrics">
    <div class="metric"><div class="k">状态</div><div class="v">${STATUS_ZH[f.status] || f.status}</div></div>
    <div class="metric"><div class="k">事件</div><div class="v">${f.eventsProcessed}/${f.eventsTotal}</div></div>
    <div class="metric"><div class="k">去除体积</div><div class="v">${f.removalAvailable ? fmtNum(st.removal_volume_internal) : "不提供（threshold_only）"}</div></div>
    <div class="metric"><div class="k">中心深度</div><div class="v">${f.removalAvailable ? fmtNum(st.center_depth_internal) : "不提供"}</div></div>
  </div>`;
  html += `<div class="watermark">单位：长度 ${wm.length_label}｜深度 ${wm.depth_label}｜能流 ${wm.fluence_label}（CSV 与界面统计同源）</div>`;

  /* 批次 H/I/J：几何修正 / 批量 / 阈值协议 / 相结构（未启用则不显示） */
  html += diagnosticPanels(f);
  if ((f.errors || []).length) {
    html += `<div class="notice error" style="margin:8px 0"><strong>错误：</strong>` +
      f.errors.map((e) => `<br>· ${e.code || ""} ${e.message || ""}${e.requirement ? `（要求：${e.requirement}）` : ""}`).join("") +
      `</div>`;
  }

  if (!layerNames.length) {
    html += `<div class="empty"><div class="big">没有可渲染的数组</div>该来源可能未保存表面，或结果为 threshold_only。</div>`;
  } else {
    /* 子页签 */
    html += `<div class="subtabs" role="tablist">
      <button data-sub="morph" class="${state.result.subtab === "morph" ? "active" : ""}">形貌</button>
      <button data-sub="sec" class="${state.result.subtab === "sec" ? "active" : ""}">截面</button>
      <button data-sub="tl" class="${state.result.subtab === "tl" ? "active" : ""}">时间轴</button>
    </div>`;

    const blockedCaps = Object.entries(f.blocked)
      .map(([k, why]) => `<div class="notice info" style="margin:6px 0">图层「${layerLabel(k)}」不可用：${why}</div>`).join("");

    if (state.result.subtab === "morph") {
      html += `<div class="control-row">
        <div class="field"><label>图层</label><select id="r-layer">${layerNames.map((l) =>
          `<option value="${l}" ${l === state.result.layer ? "selected" : ""}>${layerLabel(l)}</option>`).join("")}</select></div>
        <div class="field slim"><label>抽稀步长</label><input type="range" id="r-stride" min="1" max="8" value="2"></div>
        <label class="check"><input type="checkbox" id="r-T"> 转置（视图旋转）</label>
        <label class="check"><input type="checkbox" id="r-fx"> 水平翻转</label>
        <label class="check"><input type="checkbox" id="r-fy"> 垂直翻转</label>
      </div>` + blockedCaps +
      `<div class="chart-wrap"><p class="chart-title" id="morph-title"></p>
        <div class="heatmap-flex"><canvas id="morph-canvas"></canvas>
        <div class="colorbar"><div style="font-size:10px;color:var(--text-3)" class="cb-hi"></div>
        <canvas id="morph-bar" style="width:22px"></canvas>
        <div style="font-size:10px;color:var(--text-3)" class="cb-lo"></div></div></div></div>`;
    } else if (state.result.subtab === "sec") {
      html += f.depth
        ? `<div class="control-row">
            <div class="field slim"><label>截面方向</label><select id="s-axis"><option value="x">x</option><option value="y">y</option></select></div>
            <div class="field" style="min-width:220px"><label>位置下标</label><input type="range" id="s-idx" min="0" value="0"></div>
          </div>
          <div class="chart-wrap"><p class="chart-title" id="sec-title"></p><canvas id="sec-canvas"></canvas></div>
          <div class="watermark">截面从已求解场读取，不重新求解。</div>`
        : `<div class="notice info">该结果不提供深度（threshold_only 或未保存）——界面不显示数值零冒充「无去除」。</div>`;
    } else {
      html += f.snapshots.length
        ? `<div class="control-row"><div class="field" style="min-width:260px">
            <label>快照（回放）</label><input type="range" id="t-idx" min="0" max="${f.snapshots.length - 1}" value="${state.result.snapIdx}"></div>
            <div class="watermark" id="t-meta" style="flex:1"></div></div>
          <div class="chart-wrap"><p class="chart-title">快照回放｜${layerLabel(state.result.layer) || ""}</p>
            <div class="heatmap-flex"><canvas id="tl-canvas"></canvas>
            <div class="colorbar"><div style="font-size:10px;color:var(--text-3)" class="cb-hi"></div>
            <canvas id="tl-bar" style="width:22px"></canvas>
            <div style="font-size:10px;color:var(--text-3)" class="cb-lo"></div></div></div></div>
          <div class="watermark">拖动时间轴只读取已保存快照，求解次数不变。</div>`
        : `<div class="notice info">本次运行没有保存快照（snapshot_policy=none 或未达快照节点）。</div>`;
    }
  }

  /* 页脚：警告/错误/相结构诊断 */
  if (f.warnings.length) {
    html += `<details class="expander"><summary>警告（${f.warnings.length}）</summary><div class="body">` +
      f.warnings.map((w) => `<div>· ${w}</div>`).join("") + "</div></details>";
  }
  if (f.errors.length) {
    html += `<details class="expander" open><summary>错误（${f.errors.length}）</summary><div class="body">` +
      f.errors.map((e) => `<div>· [${e.code}] ${e.message}（${e.field_path}）</div>`).join("") + "</div></details>";
  }
  if (f.diagnostics) html += renderDiagnostics(f.diagnostics);

  box.innerHTML = html;
  bindResultEvents();
  paintResultCharts();
}

function renderDiagnostics(d) {
  const phaseRows = d.phases.map((p) =>
    `<tr><td>${p.name}</td><td>${p.Fth}</td><td>${p.delta}</td><td>${p.events}</td><td>${p.share}</td></tr>`).join("");
  const truncRows = d.truncations.map((t) =>
    `<tr><td>${t.event}</td><td>${t.from} → ${t.to}</td><td>${t.candidate}</td><td>${t.applied}</td><td>${t.unapplied}</td></tr>`).join("");
  return `<details class="expander"><summary>相结构诊断｜${d.structure_type}｜算法 ${d.algorithm_version}｜种子 ${d.seed}｜相数 ${d.phases.length}</summary>
    <div class="body">
      <div class="watermark">目标体积分数：${d.target_volume_fraction}｜实际体积分数（${d.volume_fraction_method}）：${d.actual_volume_fraction}。目标比例不代表有限样本必然等于该值，两者分别报告。</div>
      ${d.same_phase_merge ? `<div class="watermark">同相相邻铺层已预先合并；未在人为层边界处截断。</div>` : ""}
      <p class="chart-title">各相阈值/去除尺度为<strong>内联合成定义</strong>（内部单位：F_ref / L_ref），不从材料卡借用。</p>
      <table class="data"><thead><tr><th>相</th><th>阈值</th><th>去除尺度</th><th>响应事件数</th><th>体积占比</th></tr></thead><tbody>${phaseRows}</tbody></table>
      <p class="chart-title" style="margin-top:12px">跨相截断（有损近似）：实际去除取「候选」与「到下一不同相界面距离」的较小值。</p>
      <table class="data"><thead><tr><th>事件</th><th>相过渡</th><th>候选去除量</th><th>实际应用</th><th>未应用候选去除体积</th></tr></thead><tbody>${truncRows}</tbody></table>
      <div class="notice warn" style="margin-top:8px">⚠ ${d.truncation_note}</div>
    </div></details>`;
}

function bindResultEvents() {
  $$("#result-body .subtabs button").forEach((b) =>
    b.addEventListener("click", () => { state.result.subtab = b.dataset.sub; renderResultPanel(); }));
  const layerSel = $("#r-layer");
  if (layerSel) layerSel.addEventListener("change", () => { state.result.layer = layerSel.value; paintResultCharts(); });
  ["r-stride", "r-T", "r-fx", "r-fy"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("input", paintResultCharts);
  });
  const ax = $("#s-axis"), si = $("#s-idx");
  if (ax) ax.addEventListener("change", paintResultCharts);
  if (si) si.addEventListener("input", paintResultCharts);
  const ti = $("#t-idx");
  if (ti) ti.addEventListener("input", () => { state.result.snapIdx = +ti.value; paintResultCharts(); });
}

/* 从冻结结果取图层数组并应用视图变换（只读，不求解） */
function orientedLayer(f, name, stride, T, fx, fy) {
  const src = f.layers[name];
  if (!src) return null;
  const { nx, ny } = f.grid;
  const onx = T ? ny : nx, ony = T ? nx : ny;
  const ox = Math.max(1, Math.floor(onx / stride)), oy = Math.max(1, Math.floor(ony / stride));
  const out = new Float64Array(ox * oy);
  for (let j = 0; j < oy; j++)
    for (let i = 0; i < ox; i++) {
      let a = i * stride, b = j * stride;
      if (T) [a, b] = [b, a];
      if (fx) a = nx - 1 - a;
      if (fy) b = ny - 1 - b;
      out[j * ox + i] = src[b * nx + a];
    }
  return { data: out, nx: ox, ny: oy };
}

function paintResultCharts() {
  const f = state.frozen;
  if (!f) return;
  const mCanvas = $("#morph-canvas");
  if (mCanvas && state.result.subtab === "morph") {
    const stride = +($("#r-stride")?.value || 2);
    const view = orientedLayer(f, state.result.layer, stride,
      $("#r-T")?.checked, $("#r-fx")?.checked, $("#r-fy")?.checked);
    if (view) {
      drawHeatmap(mCanvas, $("#morph-bar"), view.data, view.nx, view.ny,
        state.result.layer === "threshold_mask" ? { vmin: 0, vmax: 1 } : {});
      $("#morph-title").textContent =
        `${layerLabel(state.result.layer)}｜${f.watermark.material_id}｜模式 ${f.runMode}`;
    }
  }
  const sCanvas = $("#sec-canvas");
  if (sCanvas && f.depth && state.result.subtab === "sec") {
    const axis = $("#s-axis")?.value || "x";
    const { nx, ny, xs, ys } = f.grid;
    const slider = $("#s-idx");
    slider.max = (axis === "x" ? ny : nx) - 1;
    if (+slider.value > +slider.max) slider.value = Math.floor(+slider.max / 2);
    const idx = +slider.value;
    const coords = axis === "x" ? Array.from(xs) : Array.from(ys);
    const vals = [];
    if (axis === "x") for (let i = 0; i < nx; i++) vals.push(f.depth[idx * nx + i]);
    else for (let j = 0; j < ny; j++) vals.push(f.depth[j * nx + idx]);
    drawLineChart(sCanvas, coords, [{ x: coords, y: vals, name: "深度" }],
      { xlabel: `坐标（${f.watermark.length_label}）`, ylabel: `深度（${f.watermark.depth_label}）`, height: 320 });
    $("#sec-title").textContent = `截面（${axis}，下标 ${idx}）｜${f.watermark.material_id}`;
  }
  const tCanvas = $("#tl-canvas");
  if (tCanvas && f.snapshots.length && state.result.subtab === "tl") {
    const i = Math.min(state.result.snapIdx, f.snapshots.length - 1);
    const snap = f.snapshots[i < 0 ? f.snapshots.length - 1 : i];
    $("#t-meta").textContent =
      `快照 ${(i < 0 ? f.snapshots.length - 1 : i) + 1}/${f.snapshots.length}｜事件 ${snap.eventIndex}｜t = ${snap.timeS} s｜遍 ${snap.passId}｜${snap.final ? "最终" : "过程"}`;
    drawHeatmap(tCanvas, $("#tl-bar"), snap.height, f.grid.nx, f.grid.ny);
  }
}

/* ============================================================
 * 渲染：参考评估器
 * ============================================================ */

function renderRefPanel() {
  $("#ref-case").innerHTML = STORE.references.map((c) =>
    `<option value="${c.id}" ${c.id === state.ref.caseId ? "selected" : ""}>${c.label}</option>`).join("");
  const box = $("#ref-result");
  const r = state.ref.result;
  if (!r) {
    box.innerHTML = `<div class="empty"><div class="big">尚未评估</div>选择参考算例后点击「评估」。只复现文献公式与协议量，<strong>不求解网格</strong>。</div>`;
    return;
  }
  let html = `<div class="notice info">输出语义：${r.outputSemantics || "—"}｜` +
    `事件核允许：${r.eventKernelAllowed ? "是" : "否"}｜` +
    `公式核查：${r.verifiedByFormula ? "已做" : "未做"}｜` +
    `实验复现：${r.verifiedByExperiment ? "是" : "**否（只做公式核查）**"}</div>`;
  html += `<div class="watermark">材料 ${r.materialId || "—"}｜协议 ${r.protocolId || "—"}｜条件匹配 ${r.conditionMatch || "—"}｜证据 ${r.evidenceStatus || "—"}</div>`;
  html += `<table class="data"><thead><tr><th>量</th><th>值</th><th>单位</th></tr></thead><tbody>` +
    (r.values || []).map((row) =>
      `<tr><td>${row[0]}</td><td style="font-family:var(--mono)">${fmtNum(row[1])}</td><td>${row[2] || ""}</td></tr>`).join("") +
    "</tbody></table>";
  html += `<div class="watermark">来源公式：${r.sourceEquation || "—"}｜来源图表：${r.sourceFigureOrTable || "—"}</div>`;
  (r.notes || []).forEach((n) => (html += `<div class="watermark">· ${n}</div>`));
  (r.warnings || []).forEach((w) => (html += `<div class="notice warn">⚠ ${w}</div>`));
  box.innerHTML = html;
}

/* 参考评估器：调后端（真实公式核查），前端不再自造结果。 */
async function onEvaluateReference() {
  const box = $("#ref-result");
  const caseId = state.ref.caseId;
  if (!caseId) return;
  box.innerHTML = `<div class="notice info">正在评估 ${caseId} …</div>`;
  try {
    const r = await API.post("/api/reference", { case: caseId });
    state.ref.result = r;
    renderRefPanel();
  } catch (err) {
    box.innerHTML = `<div class="notice warn"><strong>评估失败：</strong>${err.message || String(err)}` +
      ((err.errors || []).map((e) => `<br>· ${e.code} ${e.message}`).join("")) + `</div>`;
  }
}

/* ============================================================
 * 渲染：查表
 * ============================================================ */

function curveOf(id) { return STORE.curves.find((c) => c.curve_id === id); }

function interpLinear(points, x) {
  const n = points.length;
  if (x < points[0][0] || x > points[n - 1][0]) return null;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= x) lo = mid; else hi = mid;
  }
  const [x0, y0] = points[lo], [x1, y1] = points[hi];
  if (x1 === x0) return y0;
  return y0 + ((y1 - y0) * (x - x0)) / (x1 - x0);
}

function renderTablePanel() {
  $("#t-curve").innerHTML = STORE.curves.map((c) =>
    `<option value="${c.curve_id}" ${c.curve_id === state.table.curveId ? "selected" : ""}>${c.name}</option>`).join("");
  const curve = curveOf(state.table.curveId);

  $("#t-identity").innerHTML =
    `<table class="data"><thead><tr><th>去向</th><th>是否允许</th></tr></thead><tbody>
      <tr><td>进入逐事件核（event_depth_increment）</td><td><span class="tag ${curve.can_enter_event_kernel ? "yes" : "no"}">${curve.can_enter_event_kernel ? "允许" : "禁止"}</span></td></tr>
      <tr><td>进入评估器（体积/平均率/累计）</td><td><span class="tag yes">允许</span></td></tr>
      <tr><td>反推局部深度/生成局部形貌</td><td><span class="tag ${curve.can_enter_event_kernel ? "yes" : "no"}">${curve.can_enter_event_kernel ? "允许" : "禁止"}</span></td></tr>
    </tbody></table>
    <div class="watermark">x：${curve.x_name}（${curve.x_unit}）｜y：${curve.y_name}（${curve.y_unit}）｜来源：${curve.source}｜来源类型 ${curve.source_type}</div>` +
    (curve.can_enter_event_kernel ? "" :
      `<div class="notice warn">该曲线<strong>不得进入逐事件核</strong>，也不得用于生成局部形貌；只能进评估器（体积/平均率/累计曲线在无额外形状假设时不能唯一反推局部深度）。</div>`) +
    curve.notes.map((n) => `<div class="watermark">· ${n}</div>`).join("") +
    curve.limitations.map((l) => `<div class="notice warn" style="margin:6px 0">⚠ ${l}</div>`).join("");

  $("#t-raw").innerHTML =
    `<table class="data"><thead><tr><th>#</th><th>x（${curve.x_unit}）</th><th>y（${curve.y_unit}）</th></tr></thead><tbody>` +
    curve.raw_points.map((p, i) => `<tr><td>${i + 1}</td><td style="font-family:var(--mono)">${p[0]}</td><td style="font-family:var(--mono)">${p[1]}</td></tr>`).join("") +
    "</tbody></table>";

  const [lo, hi] = curve.valid_range;
  $("#t-xs").value = `${lo}, ${+((lo + hi) / 2).toFixed(3)}, ${hi}`;

  renderTableResult();
  paintCurveChart();
}

function renderTableResult() {
  const box = $("#t-result");
  const res = state.table.result;
  if (!res || res.curveId !== state.table.curveId) {
    box.innerHTML = `<div class="empty" style="padding:24px"><div class="big">尚未查询</div>输入 x 后点击「查值」。越界不返回 0，也不外推或钳到端点。</div>`;
    return;
  }
  let html = `<table class="data"><thead><tr><th>x</th><th>y（None=越界，非 0）</th><th>在区间内</th></tr></thead><tbody>` +
    res.rows.map((r) => `<tr><td style="font-family:var(--mono)">${r.x}</td>
      <td style="font-family:var(--mono)">${r.value === null ? "None（越界）" : fmtNum(r.value)}</td>
      <td><span class="tag ${r.inRange ? "yes" : "no"}">${r.inRange ? "是" : "否"}</span></td></tr>`).join("") +
    "</tbody></table>";
  html += res.status === "ok"
    ? `<div class="notice ok">状态：ok｜全部查询点位于有效区间内。</div>`
    : `<div class="notice warn">状态：${res.status}｜${res.reason}</div>`;
  res.notes.forEach((n) => (html += `<div class="watermark">· ${n}</div>`));
  box.innerHTML = html;
}

/* 查值：走后端 /api/lookup（真实插值与越界语义），前端不再自行插值。 */
async function onLookup() {
  const curve = curveOf(state.table.curveId);
  const allowOor = $("#t-oor").checked;
  if (!curve) return;
  let xs;
  try {
    xs = $("#t-xs").value.split(",").filter((s) => s.trim() !== "").map((s) => {
      const v = parseFloat(s.trim());
      if (Number.isNaN(v)) throw new Error("bad");
      return v;
    });
    if (!xs.length) throw new Error("bad");
  } catch {
    state.table.result = {
      curveId: curve.curve_id, status: "PARSE_ERROR",
      reason: "查询 x 必须是逗号分隔的数值。", rows: [], notes: [],
    };
    renderTableResult();
    return;
  }
  const box = $("#t-result");
  box.innerHTML = `<div class="notice info">正在向后端查值…</div>`;
  try {
    const resp = await API.lookup(curve.name, xs, $("#t-method").value, allowOor);
    if (!resp.ok) {
      const e = (resp.errors || [{}])[0];
      state.table.result = {
        curveId: curve.curve_id, status: e.code || "ERROR",
        reason: e.message || "", rows: [], notes: [e.requirement, e.suggestion].filter(Boolean),
      };
    } else {
      const r = resp.result;
      const rows = (r.x || []).map((x, i) => ({
        x, inRange: !!(r.in_range || [])[i], value: (r.values || [])[i],
      }));
      state.table.result = {
        curveId: curve.curve_id, status: r.status,
        reason: r.reason || "", rows,
        notes: [
          `后端插值方法：${r.method || "linear"}`,
          "越界项返回 None（不是 0），不外推、不钳端点；低于量测区间 ≠ 无去除。",
          `求解次数未变化：${resp.solveCountUnchanged ? "是" : "否"}`,
        ],
      };
    }
  } catch (err) {
    state.table.result = {
      curveId: curve.curve_id, status: (err.errors && err.errors[0] && err.errors[0].code) || "ERROR",
      reason: err.message || String(err), rows: [],
      notes: (err.errors || []).map((e) => `${e.code}: ${e.message}`).concat(err.suggestion || []),
    };
  }
  renderTableResult();
}

function paintCurveChart() {
  const curve = curveOf(state.table.curveId);
  const canvas = $("#t-chart");
  if (!canvas) return;
  const [lo, hi] = curve.valid_range;
  const gx = [], gy = [];
  for (let i = 0; i <= 200; i++) {
    const x = lo + ((hi - lo) * i) / 200;
    gx.push(x); gy.push(interpLinear(curve.raw_points, x));
  }
  drawLineChart(canvas, gx, [
    { x: gx, y: gy, name: "线性插值" },
    { x: curve.raw_points.map((p) => p[0]), y: curve.raw_points.map((p) => p[1]), name: "原始点", color: "#b45309", markers: true, dash: [3, 4] },
  ], { xlabel: `${curve.x_name}（${curve.x_unit}）`, ylabel: `${curve.y_name}（${curve.y_unit}）`, height: 340 });
  $("#t-chart-note").textContent =
    "当前演示环境未安装 SciPy：不显示 PCHIP（不会自动退化为线性）。插值图与查值均不调用求解器；侧边栏求解次数保持不变。";
}

/* ============================================================
 * 渲染：历史运行
 * ============================================================ */

function renderHistoryPanel() {
  const sel = $("#h-run");
  if (!STORE.runs.length) {
    sel.innerHTML = `<option value="">（尚无历史运行）</option>`;
    $("#h-note").textContent = "先提交一次计算，运行会落到 runs/ 下，这里即可读取。";
    return;
  }
  if (state.history.idx >= STORE.runs.length) state.history.idx = 0;
  sel.innerHTML = STORE.runs.map((r, i) =>
    `<option value="${i}" ${i === state.history.idx ? "selected" : ""}>${r.run_id}｜${r.status_zh || r.status}｜${r.run_mode || ""}｜${(r.written_at_utc || "").slice(0, 19)}</option>`).join("");
  const r = STORE.runs[state.history.idx];
  $("#h-note").textContent =
    `读取 ${r.run_id}（目录 ${r.run_dir}）。读取**只**递增「读取次数」，不调用求解器。`;
}

async function onLoadHistory() {
  const r = STORE.runs[+$("#h-run").value];
  const msg = $("#h-msg");
  if (!r) return;
  state.history.idx = +$("#h-run").value;
  msg.className = "notice info";
  msg.textContent = `正在读取 ${r.run_id} …`;
  try {
    const payload = await API.readRun(r.run_id);
    state.readCount += 1;            // 只读，不求解
    const adapted = adaptRun(payload);
    state.frozen = adapted;
    /* 读回的是**盘上已有**结果，不对应当前表单参数 → 结果区按「上一次运行」显示 */
    state.submittedKey = null;
    const layerNames = Object.keys(adapted.layers || {});
    state.result.layer = adapted.layers && adapted.layers.height ? "height" : (layerNames[0] || null);
    state.result.snapIdx = (adapted.snapshots || []).length - 1;
    if (adapted.status !== "completed") {
      msg.className = "notice warn";
      msg.textContent = "该运行未完成（失败/取消），只代表上一次运行的部分结果，不得当作成功输出。";
    } else {
      msg.className = "notice ok";
      msg.textContent = `已读取：${adapted.runId}（读取次数 ${state.readCount}，求解次数不变 ${state.solveCount}）`;
    }
    renderLedger();
    renderResultPanel();
    switchTab("result");
  } catch (err) {
    msg.className = "notice warn";
    msg.innerHTML = `<strong>读取失败：</strong>${err.message || String(err)}` +
      ((err.errors || []).map((e) => `<br>· ${e.code} ${e.message}`).join(""));
  }
}

/* ============================================================
 * Tab 切换与全局绑定
 * ============================================================ */

function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "result") paintResultCharts();
  if (name === "table") paintCurveChart();
}

/* 批次 H：七材料能力入口。三类条目含义不同，界面必须把它们分开显示，
 * 尤其 deferred 是「规格要求开放但实现未支持」的**缺口**，不得读作已开放。 */
function renderCapsPanel() {
  const box = $("#caps-table");
  if (!box) return;
  const sum = STORE.entrySummary;
  if (!sum) {
    $("#caps-summary").innerHTML = "";
    box.innerHTML = `<div class="empty">尚未加载能力入口（后端不可用或未返回）。</div>`;
    $("#caps-note").textContent = "";
    return;
  }
  const kindTag = (k) => {
    const cls = k === "opened" ? "yes" : (k === "blocked" ? "no" : "");
    const label = k === "opened" ? "已开放" : (k === "blocked" ? "红线拦截" : "缺口（未支持）");
    return `<span class="tag ${cls}">${label}</span>`;
  };
  $("#caps-summary").innerHTML = `<div class="metrics">
    <div class="metric"><div class="k">已开放</div><div class="v">${sum.n_opened}</div></div>
    <div class="metric"><div class="k">红线拦截</div><div class="v">${sum.n_blocked}</div></div>
    <div class="metric"><div class="k">缺口（未支持）</div><div class="v">${sum.n_deferred}</div></div>
    <div class="metric"><div class="k">未核验</div><div class="v">${sum.n_unverified}</div></div>
  </div>`;
  box.innerHTML = `<table class="data"><thead><tr>
      <th>材料族</th><th>条目</th><th>类别</th><th>绑定</th><th>核验</th><th>说明</th>
    </tr></thead><tbody>` +
    STORE.entries.map((r) => `<tr>
      <td>${r.family}</td><td>${r.item}</td>
      <td>${kindTag(r.kind)}</td>
      <td style="font-family:var(--mono);font-size:11px">${r.binding || "-"}</td>
      <td>${r.verified ? "已核验" : "未核验"}</td>
      <td>${r.detail || ""}${r.note ? `<br><span style="color:var(--text-3)">${r.note}</span>` : ""}</td>
    </tr>`).join("") + "</tbody></table>";
  const deferred = (sum.deferred_items || []).map((d) => `${d.family}／${d.item}`).join("；");
  $("#caps-note").textContent =
    `探针全部成立：${sum.all_probes_hold ? "是" : "**否（拦截失效或缺口消失）**"}｜` +
    `全部已核验：${sum.all_verified ? "是" : "否"}` +
    (deferred ? `｜缺口：${deferred}` : "") +
    `｜本表只反映探针实跑结果，不代替人工决策。`;
}

function renderAll() {
  renderSidebar();
  renderParamPanel();
  renderResultPanel();
  renderRefPanel();
  renderTablePanel();
  renderCapsPanel();
  renderHistoryPanel();
}

function bindGlobal() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  $("#material-select").addEventListener("change", (e) => {
    state.materialId = e.target.value;
    renderSidebar();
    renderParamPanel();
  });
  $("#mode-select").addEventListener("change", (e) => {
    state.runMode = e.target.value;
    renderSidebar();
    renderParamPanel();
  });
  $("#template-select").addEventListener("change", (e) => { state.templateId = e.target.value; });
  $("#load-template").addEventListener("click", async () => {
    await loadTemplate($("#template-select").value);
    renderParamPanel();
  });

  $$("#tab-params input, #tab-params select").forEach((el) => {
    if (el.id === "template-select") return;
    el.addEventListener("input", renderParamPanel);
    el.addEventListener("change", renderParamPanel);
  });

  $("#submit-run").addEventListener("click", onSubmit);

  $("#ref-case").addEventListener("change", (e) => { state.ref.caseId = e.target.value; });
  $("#ref-eval").addEventListener("click", onEvaluateReference);

  $("#t-curve").addEventListener("change", (e) => { state.table.curveId = e.target.value; renderTablePanel(); });
  $("#t-lookup").addEventListener("click", onLookup);

  $("#h-run").addEventListener("change", (e) => { state.history.idx = +e.target.value; renderHistoryPanel(); });
  $("#h-load").addEventListener("click", onLoadHistory);

  window.addEventListener("resize", () => { paintResultCharts(); paintCurveChart(); });
}

/* 默认选择：优先挑**能真实出物理结果**的材料与匹配的模板。 */
function pickDefaults() {
  const physical = STORE.materials.filter((m) => m.physical && (m.allowed_run_modes || []).length);
  const mat = physical[0] || STORE.materials.find((m) => (m.allowed_run_modes || []).length) || STORE.materials[0];
  state.materialId = mat ? mat.id : null;
  const modes = (mat && mat.allowed_run_modes) || [];
  state.runMode = modes.includes("reference_case") ? "reference_case" : (modes[0] || null);
  /* 找 runMode 匹配的模板；找不到就用第一个 */
  const ex = STORE.examples || [];
  const match = ex.find((e) => e.runMode === state.runMode && e.materialId === state.materialId)
    || ex.find((e) => e.runMode === state.runMode)
    || ex[0];
  state.templateId = match ? match.name : null;
  state.table.curveId = (STORE.curves[0] || {}).curve_id || null;
  state.ref.caseId = (STORE.references[0] || {}).id || null;
}

function showBootError(err) {
  const box = document.getElementById("boot-error") || (() => {
    const d = document.createElement("div");
    d.id = "boot-error";
    document.body.insertBefore(d, document.body.firstChild);
    return d;
  })();
  box.className = "notice error";
  box.style.margin = "12px";
  box.innerHTML = `<strong>无法连接后端，页面数据不可用。</strong><br>` +
    `${(err && err.message) || err}<br>` +
    `<span style="font-size:12px">请先启动服务：<code>python -m ufdemo.webapp</code>，` +
    `并通过它给出的地址打开本页（不要用 file:// 直接打开）。</span>` +
    ((err && err.suggestion) ? `<br><span style="font-size:12px">${err.suggestion}</span>` : "");
  /* 后端不可用时禁用求解，避免给出「看起来能用」的假象 */
  const btn = $("#submit-run");
  if (btn) { btn.disabled = true; btn.title = "后端不可用"; }
}

document.addEventListener("DOMContentLoaded", async () => {
  try {
    await bootstrapData();
    API.base = "";            // 同源：静态页与 API 由同一服务提供
    pickDefaults();
    await loadTemplate(state.templateId);
    renderAll();
    bindGlobal();
  } catch (err) {
    showBootError(err);
  }
});
