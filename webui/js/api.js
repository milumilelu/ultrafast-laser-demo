/* ============================================================
 * 真实后端客户端 + 契约映射
 *
 * 数据来源：ufdemo.webapp 提供的本地 API（静态前端 + 真实求解）。
 * 本文件**只做**「请求 + 契约适配」，不含任何物理逻辑——
 * 渲染所需的一切量都由后端算出（watermark / layers / blocked / panels）。
 *
 * 不变量（与 ui_service 同源，前端不得自行编造）：
 *  - 只有 POST /api/solve 会调用求解器；
 *  - GET /api/runs/<id> 只读盘，不重算；
 *  - 查表越界返回 null（后端决定），前端**不得**把 null 当 0 显示；
 *  - 图层不可用时后端给出原因，前端如实展示，不画全 0 假图。
 * ============================================================ */

"use strict";

const API = {
  base: "",

  async _req(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    let resp;
    try {
      resp = await fetch(this.base + path, opts);
    } catch (err) {
      throw {
        code: "BACKEND_UNREACHABLE",
        message: `无法连接后端：${err.message}`,
        suggestion: "请确认已运行 `python -m ufdemo.webapp`，且页面由该服务提供。",
      };
    }
    let data = null;
    const text = await resp.text();
    if (text) {
      try { data = JSON.parse(text); }
      catch (err) { throw { code: "BAD_JSON", message: `后端返回非 JSON：${text.slice(0, 200)}` }; }
    }
    if (!resp.ok) {
      throw (data && (data.errors || data.message))
        ? (data.errors ? { code: "BACKEND_ERROR", message: "请求被拒绝", errors: data.errors } : data)
        : { code: "HTTP_" + resp.status, message: `HTTP ${resp.status}` };
    }
    return data;
  },

  get(p) { return this._req("GET", p); },
  post(p, body) { return this._req("POST", p, body); },

  health() { return this.get("/api/health"); },
  materials() { return this.get("/api/materials"); },
  examples() { return this.get("/api/examples"); },
  curves() { return this.get("/api/curves"); },
  references() { return this.get("/api/references"); },
  runs() { return this.get("/api/runs"); },
  layers() { return this.get("/api/layers"); },
  readRun(id) { return this.get("/api/runs/" + encodeURIComponent(id)); },
  solve(params, label) { return this.post("/api/solve", { params, label: label || "web_run" }); },
  preview(params) { return this.post("/api/preview", { params }); },
  lookup(curve, xs, method, allowOutOfRange) {
    return this.post("/api/lookup", { curve, xs, method, allowOutOfRange: !!allowOutOfRange });
  },
  curveGrid(curve, n, method) {
    return this.post("/api/curve-grid", { curve, n: n || 200, method: method || "linear" });
  },
  guardDepthExport(unitSystem, filename) {
    return this.post("/api/guard-depth-export", { unitSystem, filename });
  },
};

/* ---------------- 契约适配 ---------------- */

/* 能力键 → 界面标签。后端给的是能力标识，这里只做「显示名」映射，
 * 不改变可用性判断（available/reason 一律照抄后端）。 */
const CAPABILITY_LABELS = {
  event_depth_increment: "事件核形貌",
  threshold_only: "阈值判定",
  synthetic_structure: "合成结构",
  table_import: "查表曲线",
  reference_case: "参考评估器",
  history_dependent: "历史耦合",
  structured_interface: "分相结构",
  multi_response: "多响应候选",
};

/* 运行模式 → 界面标签。真实取值来自材料卡的 allowed_run_modes。 */
const RUN_MODES = {
  reference_case: "文献参考（reference_case）",
  event_kernel: "逐事件形貌（event_kernel）",
  threshold_only: "仅阈值（threshold_only）",
  synthetic_demo: "合成演示（synthetic_demo）",
  multishot: "多脉冲（multishot）",
  synthetic_normalized: "合成·归一化（synthetic_normalized）",
};

function toMaterialCard(card) {
  const caps = {};
  Object.keys(card.capabilities || {}).forEach((key) => {
    const cap = card.capabilities[key];
    caps[CAPABILITY_LABELS[key] || key] = {
      available: !!cap.available,
      reason: cap.reason || "",
      missing: cap.missing || [],
    };
  });
  /* 有效性域的紧凑表述（卡里没登记就不显示，不编造激光参数）。 */
  const validity = [];
  if (card.validityScope) validity.push(`范围 ${card.validityScope}`);
  if (card.protocolId) validity.push(`协议 ${card.protocolId}`);
  if (card.peakFluenceJm2) validity.push(`峰值能流 ${card.peakFluenceJm2} J/m²`);
  return {
    id: card.id,
    family: card.family || "（未登记族）",
    grade: card.grade || "（未登记牌号）",
    evidence_status: card.evidenceStatus,
    source_type: card.sourceType,
    structure_type: card.structureType,
    physical: !!card.physicalPredictionAllowed,
    fixture_only: !!card.fixtureOnly,
    blocked_reason: card.blockedReason || null,
    allowed_run_modes: card.allowedRunModes || [],
    readiness: (card.allowedRunModes || []).length ? (card.responseKind || "—") : "missing_response",
    laser: validity.join("｜") || "（有效性域未登记）",
    response: card.response || {},
    response_kind: card.responseKind,
    threshold_candidates: card.thresholdCandidates || [],
    gates: card.gates || {},
    capabilities: caps,
    limitations: card.limitations || [],
    validity_note: card.validityNote,
    source_equation: card.sourceEquation,
    source_figure_or_table: card.sourceFigureOrTable,
    card_sha256: card.cardSha256,
    card_file: card.cardPath,
  };
}

function toCurve(c) {
  /* capability_rows 是后端给出的「项-值」表，这里解析成渲染层要用的布尔量。
   * **不自行判断**——是否可进事件核由后端的语义闸门决定。 */
  const capMap = {};
  (c.capabilityRows || []).forEach((r) => { capMap[r["项"]] = r["值"]; });
  const canKernel = capMap["可进事件核（语义）"] === "是";
  return {
    name: c.name,
    curve_id: c.curveId,
    material_id: c.materialId,
    material_identity: c.materialIdentity,
    x_quantity: c.xQuantity,
    y_quantity: c.yQuantity,
    /* 渲染层别名：x/y 的名称与单位（真实来源是 quantity 标识） */
    x_name: c.xQuantity || "x",
    x_unit: (c.fixedConditions && c.fixedConditions.x_unit) || "",
    y_name: c.yQuantity || "y",
    y_unit: (c.fixedConditions && c.fixedConditions.y_unit) || "",
    source: c.sourceFigureOrTable || "（未登记来源）",
    output_semantics: c.outputSemantics,
    fixed_conditions: c.fixedConditions,
    protocol: c.protocol,
    source_figure_or_table: c.sourceFigureOrTable,
    valid_range: c.validRange,
    valid_range_note: c.validRangeNote,
    raw_points: c.rawPoints || [],
    duplicate_report: c.duplicateReport,
    evidence_status: c.evidenceStatus,
    source_type: c.sourceType,
    depth_direction: c.depthDirection,
    fluence_basis: c.fluenceBasis,
    notes: c.notes || [],
    limitations: c.limitations || [],
    capability_rows: c.capabilityRows || [],
    can_enter_event_kernel: canKernel,
    /* 去向描述直接取后端文案，避免前端另写一套判断 */
    route_label: capMap["去向"] || "",
  };
}

/* 运行结果归一化：JSON 的 number[] → Float64Array。
 * 渲染层按索引访问数组，转成 TypedArray 后既省内存又与旧代码兼容。 */
function normalizeRun(payload) {
  if (!payload) return payload;
  const out = payload;
  const toArr = (v) => (Array.isArray(v) ? Float64Array.from(v) : v);
  if (out.layers) {
    Object.keys(out.layers).forEach((k) => { out.layers[k] = toArr(out.layers[k]); });
  }
  if (Array.isArray(out.snapshots)) {
    out.snapshots.forEach((s) => { if (s && s.height) s.height = toArr(s.height); });
  }
  if (out.grid && out.grid.xs) out.grid.xs = toArr(out.grid.xs);
  if (out.grid && out.grid.ys) out.grid.ys = toArr(out.grid.ys);
  return out;
}

/* 把后端契约压成「前端渲染期望的形状」：补齐旧字段名，避免渲染层到处改。 */
function adaptRun(payload) {
  const r = normalizeRun(payload);
  if (!r || r.status === "failed") return r;
  r.runId = r.runId;
  r.materialId = r.materialId;
  r.runMode = r.runMode;
  r.watermark = r.watermark || {};
  r.stats = r.stats || {};
  r.layers = r.layers || {};
  r.blocked = r.blocked || {};
  r.snapshots = r.snapshots || [];
  r.errors = r.errors || [];
  r.warnings = r.warnings || [];
  r.grid = r.grid || {};
  if (r.grid && r.grid.nx !== undefined) {
    r.grid = { nx: r.grid.nx, ny: r.grid.ny, xs: r.grid.xs, ys: r.grid.ys };
  }
  r.removalAvailable = !!r.removalAvailable;
  r.depth = r.removalAvailable ? (r.layers.depth || null) : null;
  r.panels = r.panels || {};
  r.rows = r.rows || {};
  return r;
}

/* ---------------- 启动引导 ---------------- */

const STORE = {
  ready: false,
  health: null,
  materials: [],
  entries: [],
  entrySummary: null,
  layerMeta: null,
  examples: [],
  curves: [],
  references: [],
  runs: [],
  error: null,
};

/* 一次把所有清单拉回来。任何一步失败都如实抛出，**不**回退到内置假数据。 */
async function bootstrapData() {
  const [health, materials, examples, curves, references, runs, layers] = await Promise.all([
    API.health(), API.materials(), API.examples(), API.curves(),
    API.references(), API.runs(), API.layers(),
  ]);
  STORE.health = health;
  STORE.materials = (materials.catalog || []).map(toMaterialCard);
  STORE.entries = materials.entries || [];
  STORE.entrySummary = materials.summary || null;
  STORE.examples = examples.examples || [];
  STORE.curves = (curves.curves || []).map(toCurve);
  STORE.references = references.cases || [];
  STORE.runs = runs.runs || [];
  STORE.layerMeta = layers;
  STORE.ready = true;
  return STORE;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    API, STORE, bootstrapData, toMaterialCard, toCurve, normalizeRun, adaptRun,
    CAPABILITY_LABELS, RUN_MODES,
  };
}
