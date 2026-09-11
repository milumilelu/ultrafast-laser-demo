/* ============================================================
 * 演示数据层 —— 取自 ultrafast-demo 工程真实材料卡 / 模板 / 文献登记表
 * （material_card_templates.json, 2026-09-10 版）
 * 本页面不连接真实求解器，所有数值仅用于界面演示。
 * ============================================================ */

"use strict";

const DEMO = {};

/* ---------------- 运行模式 ---------------- */

DEMO.RUN_MODES = {
  event_kernel: "逐事件形貌（event_kernel）",
  threshold_only: "仅阈值判定（threshold_only）",
  synthetic_normalized: "合成无量纲演示（synthetic）",
};

/* ---------------- 材料目录 ----------------
 * capabilities: 名称 → { available, reason, missing? }
 * response: 供内置模拟求解器使用（物理材料为真实文献值）
 * ------------------------------------------------------ */

DEMO.MATERIALS = [
  {
    id: "zirconia_ysz_reference",
    family: "氧化锆",
    grade: "论文YSZ相应加工条件，不泛化所有氧化锆",
    evidence_status: "literature_reported",
    readiness: "reference_multishot_kernel",
    physical: true,
    laser: "1030 nm｜208 fs｜33.3 kHz",
    allowed_run_modes: ["event_kernel", "threshold_only"],
    response: { kind: "logarithmic", Fth: 12890.0, delta_eff_m: 3.9e-7 },
    capabilities: {
      "事件核形貌": { available: true, reason: "参考多脉冲核已按文献公式核查" },
      "阈值判定": { available: true, reason: "加工有效阈值 Fth=1.289 J/cm²" },
      "参考评估器": { available: true, reason: "YSZ 参考算例（批次 D）" },
      "查表曲线": { available: true, reason: "单事件深度增量曲线已登记" },
      "分相结构": { available: false, reason: "均质材料卡，无分相结构", missing: ["structure_type"] },
    },
    limitations: [
      "参考加工条件内起步；不得作为已经验证的任意单脉冲卡。",
      "静态 Fth1=1.483 属于另一试验/拟合分支，不能与本有效核拼接。",
    ],
  },
  {
    id: "sic4h_cface_1035nm_multishot",
    family: "SiC",
    grade: "N掺杂4H-SiC C面",
    evidence_status: "literature_reported",
    readiness: "reference_multishot_kernel",
    physical: true,
    laser: "1035 nm｜300 fs｜200 kHz",
    allowed_run_modes: ["event_kernel", "threshold_only"],
    response: {
      kind: "incubation",
      Fth1: 23500, Fth_inf: 7000, k_inc: 0.0199, delta_eff_m: 2.24e-8,
    },
    capabilities: {
      "事件核形貌": { available: true, reason: "指数孵化有效核已按文献公式核查" },
      "阈值判定": { available: true, reason: "双阈值：改性 2.35 / 结构转变 4.97 J/cm²" },
      "参考评估器": { available: true, reason: "SiC 参考算例（批次 D）" },
      "查表曲线": { available: true, reason: "平均去除率曲线（仅评估器）" },
      "分相结构": { available: false, reason: "均质双阈值卡，无分相结构", missing: ["structure_type"] },
    },
    limitations: [
      "δ 为多脉冲微槽拟合均值；不能解释为每脉冲固定深度。",
      "有效 N 与逐事件递推须分别验证；不覆盖 RB-SiC / 烧结 SiC / 复合颗粒。",
    ],
  },
  {
    id: "inconel718_1030nm_n10",
    family: "高温合金",
    grade: "Inconel 718",
    evidence_status: "literature_reported",
    readiness: "threshold_only",
    physical: true,
    laser: "1030 nm｜约267 fs",
    allowed_run_modes: ["threshold_only"],
    response: { kind: "threshold_only", Fth: 1400.0, note: "N=10 阈值" },
    capabilities: {
      "事件核形貌": { available: false, reason: "缺去除尺度 δ，不输出深度", missing: ["delta_eff_m"] },
      "阈值判定": { available: true, reason: "N=10 加工阈值 0.14 J/cm²" },
      "参考评估器": { available: false, reason: "未登记参考算例", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: false, reason: "均质材料卡，无分相结构", missing: ["structure_type"] },
    },
    limitations: ["不能把 N10 阈值当成 Fth1；不是任意镍基/钴基高温合金卡。"],
  },
  {
    id: "cfrp_t700_yb01_800nm",
    family: "CFRP",
    grade: "T700 / YB01",
    evidence_status: "literature_reported",
    readiness: "threshold_only",
    physical: true,
    laser: "800 nm｜90 fs｜1 kHz",
    allowed_run_modes: ["threshold_only"],
    response: { kind: "threshold_only", Fth: 8400.0, note: "整体等效，孵化 S=0.8855" },
    capabilities: {
      "事件核形貌": { available: false, reason: "δ 缺失时不输出绝对深度", missing: ["delta_eff_m"] },
      "阈值判定": { available: true, reason: "整体阈值（多脉冲扫描推断）" },
      "参考评估器": { available: false, reason: "未登记参考算例", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: false, reason: "铺层角/层厚/纤维体积分数均为 null", missing: ["layup_angles_deg", "ply_thickness_m", "fiber_volume_fraction"] },
    },
    limitations: ["整体阈值不是两相阈值；δ 缺失时不输出绝对深度；不声称预测分层。"],
  },
  {
    id: "diamond_scd_cvd_1030nm",
    family: "金刚石",
    grade: "单晶CVD",
    evidence_status: "literature_reported",
    readiness: "threshold_only",
    physical: true,
    laser: "1030 nm｜400/700 fs",
    allowed_run_modes: ["threshold_only"],
    response: { kind: "threshold_only", Fth: 82000.0, note: "400 fs 单脉冲阈值" },
    capabilities: {
      "事件核形貌": { available: false, reason: "δ 与孵化参数缺失", missing: ["delta_eff_m", "incubation"] },
      "阈值判定": { available: true, reason: "400 fs：8.2 J/cm²｜700 fs：12.9 J/cm²" },
      "参考评估器": { available: false, reason: "未登记参考算例", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: false, reason: "均质材料卡，无分相结构", missing: ["structure_type"] },
    },
    limitations: [
      "不混合取平均不同研究的阈值；2.32 J/cm² 候选脉宽未核，不作默认。",
      "改性标记不等于石墨化定量模拟；仅净去除高度场不预测隆起。",
    ],
  },
  {
    id: "alsic_phase_template",
    family: "铝基碳化硅",
    grade: "SiCp/AA2024候选，实际配方待确认",
    evidence_status: "unverified",
    readiness: "missing_response",
    physical: true,
    laser: "1030 nm｜800 fs（参考）",
    allowed_run_modes: [],
    response: null,
    capabilities: {
      "事件核形貌": { available: false, reason: "两相 Fth/δ 均缺失", missing: ["Al_matrix.Fth", "SiC_particle.Fth", "delta_eff_m"] },
      "阈值判定": { available: false, reason: "相阈值为 null", missing: ["phases.Fth"] },
      "参考评估器": { available: false, reason: "未登记参考算例", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: false, reason: "体积分数/粒径实测值缺失（仅有纳秒研究几何示例）", missing: ["actual_volume_fraction", "actual_particle_size_m"] },
    },
    limitations: [
      "不平均两相阈值；不拿纳秒阈值替代飞秒值；",
      "不可将块体 4H-SiC 直接当成颗粒相实测响应。",
    ],
  },
  {
    id: "glass_ceramic_template",
    family: "微晶玻璃",
    grade: null,
    evidence_status: "unverified",
    readiness: "missing_response",
    physical: true,
    laser: "1030 nm｜550–600 fs（候选）",
    allowed_run_modes: [],
    response: null,
    capabilities: {
      "事件核形貌": { available: false, reason: "Fth/δ/孵化均缺失", missing: ["Fth", "delta_eff_m", "incubation"] },
      "阈值判定": { available: false, reason: "阈值为 null", missing: ["Fth"] },
      "参考评估器": { available: false, reason: "未登记参考算例", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: false, reason: "均质等效卡，无分相结构", missing: ["structure_type"] },
    },
    limitations: [
      "禁止将石英、硼硅玻璃或镀膜损伤阈值代入。",
      "透明材料内部改性、贝塞尔切割与裂纹扩展不在表面高度场范围。",
    ],
  },
  {
    id: "analytic_fixture_not_a_material",
    family: "解析测试卡",
    grade: "人工解析 fixture（非材料）",
    evidence_status: "unverified",
    readiness: "analytic_fixture",
    physical: false,
    laser: "归一化（F_ref / L_ref）",
    allowed_run_modes: ["event_kernel", "threshold_only"],
    response: { kind: "logarithmic", Fth: 1.0, delta_eff_m: 0.05, normalized: true },
    capabilities: {
      "事件核形貌": { available: true, reason: "解析核，供数值实现验证" },
      "阈值判定": { available: true, reason: "归一化阈值 1.0 F_ref" },
      "参考评估器": { available: false, reason: "fixture 不参与文献复现", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: false, reason: "均质 fixture", missing: ["structure_type"] },
    },
    limitations: ["人工构造的解析测试卡，不代表任何真实材料。"],
  },
  {
    id: "synthetic_demo_isotropic",
    family: "合成演示",
    grade: "各向同性合成示例（无量纲）",
    evidence_status: "unverified",
    readiness: "synthetic_demo",
    physical: false,
    laser: "归一化（F_ref / L_ref）",
    allowed_run_modes: ["synthetic_normalized"],
    response: { kind: "logarithmic", Fth: 1.0, delta_eff_m: 0.08, normalized: true },
    capabilities: {
      "事件核形貌": { available: true, reason: "合成核，仅验证求解流程与界面" },
      "阈值判定": { available: true, reason: "归一化阈值 1.0 F_ref" },
      "参考评估器": { available: false, reason: "合成示例不参与文献复现", missing: ["reference_case"] },
      "查表曲线": { available: false, reason: "未登记曲线卡", missing: ["curve_card"] },
      "分相结构": { available: true, reason: "可叠加合成颗粒/铺层结构（内联定义）" },
    },
    limitations: [
      "合成模式内部一律无量纲，禁止导出物理深度。",
      "只验证求解流程、几何与界面，不能据此比较七种材料真实加工速度。",
    ],
  },
];

/* ---------------- 配置模板（对应 examples/） ---------------- */

DEMO.TEMPLATES = [
  { id: "analytic_single_pulse.json", label: "解析单脉冲（默认）", params: {
    nx: 161, ny: 161, dx: 0.5, dy: 0.5, E: 10, w0: 16, f: 33300,
    v: 0.5, x0: -30, x1: 30, roi: 40, snap: "events" } },
  { id: "ysz_reference_case.json", label: "YSZ 文献工况复现", params: {
    nx: 161, ny: 161, dx: 0.5, dy: 0.5, E: 10, w0: 16, f: 33300,
    v: 0.5, x0: -30, x1: 30, roi: 40, snap: "events" } },
  { id: "sic_reference_case.json", label: "SiC 文献工况复现", params: {
    nx: 161, ny: 161, dx: 0.4, dy: 0.4, E: 3.2, w0: 14, f: 200000,
    v: 2.0, x0: -25, x1: 25, roi: 32, snap: "events" } },
  { id: "synthetic_demo_point.json", label: "合成演示单点（无量纲）", params: {
    nx: 121, ny: 121, dx: 1.0, dy: 1.0, E: 12, w0: 15, f: 1000,
    v: 0.0, x0: 0, x1: 0, roi: 60, snap: "events" } },
  { id: "synthetic_particle_composite.json", label: "合成颗粒复合 + 跨相截断", params: {
    nx: 141, ny: 141, dx: 0.8, dy: 0.8, E: 14, w0: 16, f: 2000,
    v: 0.4, x0: -35, x1: 35, roi: 55, snap: "events" } },
];

/* ---------------- 查表曲线卡（批次 F） ---------------- */

DEMO.CURVES = [
  {
    curve_id: "ysz_event_depth_increment_vs_fluence",
    name: "YSZ 单事件深度增量 — 峰值能流",
    x_name: "峰值能流 F0", x_unit: "J/cm²",
    y_name: "单事件深度增量", y_unit: "µm",
    source: "S01 图7（目视登记）", source_type: "literature_figure",
    can_enter_event_kernel: true,
    valid_range: [1.289, 50.1],
    /* Fth = 1.289 J/cm², δ = 0.39 µm；y = δ·ln(F/Fth) */
    raw_points: [
      [1.289, 0.0], [1.8, 0.13], [2.6, 0.27], [3.9, 0.43],
      [6.0, 0.60], [10.0, 0.80], [20.0, 1.07], [35.0, 1.29], [50.1, 1.43],
    ],
    notes: ["原始点全部保留；插值默认分段线性。"],
    limitations: ["仅 1030 nm / 208 fs / APS 8YSZ 协议区间有效。"],
  },
  {
    curve_id: "sic_mean_removal_rate_vs_pulses",
    name: "4H-SiC 平均去除率 — 脉冲数",
    x_name: "脉冲数 N", x_unit: "1",
    y_name: "平均去除率", y_unit: "µm/pulse",
    source: "S02 表2（目视登记）", source_type: "literature_table",
    can_enter_event_kernel: false,
    valid_range: [1, 200],
    raw_points: [
      [1, 0.118], [5, 0.086], [10, 0.066], [20, 0.049],
      [40, 0.036], [80, 0.027], [120, 0.023], [160, 0.021], [200, 0.019],
    ],
    notes: ["平均率是多脉冲累计的均值，不是局部每脉冲深度。"],
    limitations: ["体积/平均率/累计曲线在无额外形状假设时不能唯一反推局部深度。"],
  },
];

/* ---------------- 参考评估器算例（批次 D） ---------------- */

DEMO.REFERENCE_CASES = [
  {
    id: "ysz_reference_case.json",
    label: "YSZ 参考算例",
    material_id: "zirconia_ysz_reference",
    output_semantics: "formula_checked",
    output_semantics_zh: "只做公式核查，未做实验复现",
    event_kernel_allowed: true,
    verified_by_experiment: false,
    condition_match: "matched（1030 nm / 208 fs / 扫描）",
    values: [
      ["Fth（加工有效阈值）", "12890", "J/m²"],
      ["δ_eff（去除尺度）", "3.9e-07", "m"],
      ["有效脉冲数定义", "(π/4)·(2w0·f)/v", "—"],
      ["有效脉冲数（w0=16µm, f=33.3kHz, v=0.5m/s）", "1.674", "—"],
      ["单事件深度增量（F0=5.01 J/cm²）", "0.529", "µm"],
    ],
    sweep: null,
    notes: ["YSZ 与 SiC 的有效脉冲数不共用公式。", "累计深度不得进入逐事件主循环。"],
    warnings: ["平均率与累计深度只进评估器；本算例未做实验复现。"],
  },
  {
    id: "sic_reference_case.json",
    label: "SiC 参考算例",
    material_id: "sic4h_cface_1035nm_multishot",
    output_semantics: "formula_checked",
    output_semantics_zh: "只做公式核查，未做实验复现",
    event_kernel_allowed: true,
    verified_by_experiment: false,
    condition_match: "matched（1035 nm / 300 fs / C面）",
    values: [
      ["Fth1（拟合首脉冲阈值）", "23500", "J/m²"],
      ["Fth∞（饱和阈值）", "7000", "J/m²"],
      ["孵化系数 k_inc", "0.0199", "1/pulse"],
      ["δ_eff（多脉冲均值）", "2.24e-08", "m"],
      ["有效脉冲数定义", "K·(2w0·f)/v（K 为工况系数）", "—"],
    ],
    sweep: {
      columns: ["N", "Fth(N) (J/m²)", "累计深度 (µm)"],
      rows: [
        [1, "23500.0", "0.000"], [2, "22174.6", "0.003"], [5, "18762.1", "0.022"],
        [10, "14713.2", "0.068"], [50, "7623.9", "0.412"], [100, "7109.4", "0.742"],
        [1000, "7000.0", "3.881"], ["1e9", "7000.0", "—（仅核对极限）"],
      ],
    },
    notes: ["Fth(N)=F∞+(F1−F∞)·exp(−k·(N−1))。", "N=1e9 仅用于核对 Fth(N)→F∞ 极限，其累计深度无物理意义。"],
    warnings: ["δ 为微槽拟合均值，不能解释为每脉冲固定深度。"],
  },
];

/* ---------------- 历史运行（演示） ---------------- */

DEMO.HISTORY_RUNS = [
  {
    run_id: "20260910T2115_ui_run_ysz_scan",
    status: "completed", status_zh: "已完成",
    run_mode: "event_kernel",
    material_id: "zirconia_ysz_reference",
    written_at: "2026-09-10 21:15 UTC",
    template: "ysz_reference_case.json",
    note: "YSZ 参考槽扫描，33.3 kHz / 0.5 m/s。",
  },
  {
    run_id: "20260910T2230_ui_run_synthetic_particle",
    status: "completed", status_zh: "已完成",
    run_mode: "synthetic_normalized",
    material_id: "synthetic_demo_isotropic",
    written_at: "2026-09-10 22:30 UTC",
    template: "synthetic_particle_composite.json",
    note: "合成颗粒复合结构，含跨相界面截断诊断。",
    composite: true,
  },
  {
    run_id: "20260910T2248_ui_run_cfrp_threshold",
    status: "failed", status_zh: "失败",
    run_mode: "threshold_only",
    material_id: "cfrp_t700_yb01_800nm",
    written_at: "2026-09-10 22:48 UTC",
    template: "analytic_single_pulse.json",
    note: "中途取消的阈值判定运行（部分结果）。",
  },
];

/* ---------------- 图层定义（与 LAYER_LABELS 对齐） ---------------- */

DEMO.LAYERS = {
  height: "表面高度场（去除为负）",
  fluence_dose: "累计能流剂量",
  threshold_mask: "超阈值标记（0/1）",
  phase_id: "相标签（phase_id）",
};

/* 合成颗粒复合运行的相结构诊断（内联合成定义，内部单位） */
DEMO.COMPOSITE_DIAGNOSTICS = {
  structure_type: "particle_composite",
  algorithm_version: "g07-analytic-1",
  seed: 20260910,
  target_volume_fraction: 0.45,
  actual_volume_fraction: 0.4372,
  volume_fraction_method: "网格计数估计",
  same_phase_merge: true,
  phases: [
    { name: "Al_matrix（合成）", Fth: "1.0 F_ref", delta: "0.08 L_ref", events: 1214, share: "56.3%" },
    { name: "SiC_particle（合成）", Fth: "2.6 F_ref", delta: "0.03 L_ref", events: 942, share: "43.7%" },
  ],
  truncations: [
    { event: 38, from: "Al_matrix", to: "SiC_particle", candidate: "0.0081", applied: "0.0044", unapplied: "0.0037" },
    { event: 57, from: "SiC_particle", to: "Al_matrix", candidate: "0.0025", applied: "0.0019", unapplied: "0.0006" },
    { event: 103, from: "Al_matrix", to: "SiC_particle", candidate: "0.0079", applied: "0.0052", unapplied: "0.0027" },
    { event: 156, from: "Al_matrix", to: "SiC_particle", candidate: "0.0083", applied: "0.0031", unapplied: "0.0052" },
  ],
  truncation_note:
    "跨相截断是有损近似：实际去除取「候选去除量」与「到下一不同相界面距离」的较小值；" +
    "被截断部分记为「未应用候选去除体积」（内部单位 L_ref³），它不是剩余热量，不回流、不重分配。",
};
