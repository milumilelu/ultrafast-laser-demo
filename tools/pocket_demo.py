"""200×200 μm 矩形槽仿真 + 自包含 HTML 报告。

任务书 [U1] §12 的 nominal 编程区域就是 **200×200 μm**（弓字形填充）。
本脚本按该尺寸跑一次真实求解，并生成一份**自带 canvas 绘图、无外部依赖**的
HTML 报告，便于直接查看槽形貌。

用法::

    PYTHONPATH=src python tools/pocket_demo.py
    PYTHONPATH=src python tools/pocket_demo.py --spacing 4 --layers 1 --freq 20

⚠️ 报告里所有数值都是**模型预测**；没有导入实测高度图时不构成二维形貌验证。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_BASELINE = ROOT / "data" / "baselines" / "alsic_223fs_candidate.json"
BASE_CARD = "tests/fixtures/analytic_fixture.json"
OUT_HTML = ROOT / "docs" / "reports" / "pocket_200um.html"


def build_and_run(*, region_um: float, dx_um: float, spacing_um: float,
                  layers: int, f_khz: float, v_mm_s: float, tau_fs: float,
                  gain: float, baseline: Path) -> dict:
    from ufdemo.calibration import ExperimentRow, PredictionSpec, build_row_config
    from ufdemo.materials import MaterialSpec
    from ufdemo.solver import solve

    bl = json.loads(baseline.read_text(encoding="utf-8"))
    ov = {
        "kind": "log_fixed", "output_semantics": "event_depth_increment",
        "fluence_basis": "incident_peak_fluence", "depth_direction": "surface_normal",
        "threshold_J_m2": bl["thresholdJm2"], "delta_m": bl["deltaM"],
    }
    row = ExperimentRow(sample_id="pocket", pulse_duration_fs=tau_fs,
                        repetition_rate_kHz=f_khz, scan_speed_mm_s=v_mm_s,
                        hatch_spacing_um=spacing_um, pass_count=layers,
                        mean_depth_um=1.0)
    spec = PredictionSpec(material_card_file=BASE_CARD, window_um=region_um,
                          dx_um=dx_um, response_override=ov)
    cfg = build_row_config(row, spec=spec, gain=gain)

    raw = json.loads((ROOT / BASE_CARD).read_text(encoding="utf-8"))
    raw["response"] = {**(raw.get("response") or {}), **ov}
    material = MaterialSpec.from_dict(raw)

    t0 = time.perf_counter()
    res = solve(cfg, material)
    dt = time.perf_counter() - t0
    if res.status != "completed" or res.surface is None:
        raise SystemExit(f"求解未完成：{res.status} {res.errors}")

    import numpy as np

    drop = (res.surface.initial_height - res.surface.height) * 1e6   # μm
    g = res.surface.grid
    ny, nx = drop.shape
    row_idx = int(np.argmax(drop.mean(axis=1)))
    return {
        "ok": True,
        "regionUm": region_um, "dxUm": dx_um * 1e6 if dx_um < 1e-3 else dx_um,
        "gridNx": nx, "gridNy": ny,
        "spacingUm": spacing_um, "layers": layers,
        "fKHz": f_khz, "vMmS": v_mm_s, "tauFs": tau_fs, "gain": gain,
        "events": int(res.events_processed), "solveSeconds": dt,
        "depthUm": drop,
        "sectionAlongXUm": drop[row_idx],
        "sectionAlongYUm": drop[:, nx // 2],
        "xAxisUm": (np.arange(nx) - nx // 2) * g.dx_m * 1e6,
        "yAxisUm": (np.arange(ny) - ny // 2) * g.dy_m * 1e6,
        "spotBasis": dict(cfg.raw.get("_spot_radius_basis") or {}),
        "spotRadiusUm": cfg.laser.spot_radius_m * 1e6,
        "pulseEnergyUJ": cfg.laser.pulse_energy_J * 1e6,
        "pulses": int(res.events_processed),
    }


def _downsample(a, target: int = 200):
    import numpy as np

    ny, nx = a.shape
    sy = max(1, int(np.ceil(ny / target)))
    sx = max(1, int(np.ceil(nx / target)))
    return a[::sy, ::sx], sy, sx


def write_html(r: dict, out: Path) -> Path:
    import numpy as np

    d = r["depthUm"]
    sub, sy, sx = _downsample(d, 200)
    stats = {
        "meanUm": float(d.mean()),
        "maxUm": float(d.max()),
        "minTouchedUm": float(d[d > 0].min()) if np.any(d > 0) else 0.0,
        "p5Um": float(np.percentile(d, 5)),
        "p50Um": float(np.percentile(d, 50)),
        "p95Um": float(np.percentile(d, 95)),
        "stdUm": float(d.std()),
        "coverage": float((d > 0).mean()),
        "ripplePvUm": float(np.percentile(d, 95) - np.percentile(d, 5)),
    }
    stats["flatnessPct"] = (stats["ripplePvUm"] / stats["meanUm"] * 100.0) if stats["meanUm"] else 0.0

    payload = {
        "gridNx": int(sub.shape[1]), "gridNy": int(sub.shape[0]),
        "dxUm": r["dxUm"] * sx, "dyUm": r["dxUm"] * sy,
        "regionUm": r["regionUm"],
        "depthUm": [[round(float(v), 3) for v in row] for row in sub],
        "sectionAlongXUm": [round(float(v), 3) for v in r["sectionAlongXUm"]],
        "sectionAlongYUm": [round(float(v), 3) for v in r["sectionAlongYUm"]],
        "xAxisUm": [round(float(v), 3) for v in r["xAxisUm"]],
        "yAxisUm": [round(float(v), 3) for v in r["yAxisUm"]],
        "stats": stats,
        "meta": {
            "spacingUm": r["spacingUm"], "layers": r["layers"],
            "fKHz": r["fKHz"], "vMmS": r["vMmS"], "tauFs": r["tauFs"],
            "gain": r["gain"], "events": r["events"], "solveSeconds": r["solveSeconds"],
            "pulseEnergyUJ": r["pulseEnergyUJ"], "spotRadiusUm": r["spotRadiusUm"],
            "fullNx": r["gridNx"], "fullNy": r["gridNy"],
            "spotBasis": {k: v for k, v in (r["spotBasis"] or {}).items()},
        },
    }

    html = _TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>200×200 μm 矩形槽仿真（模型预测）</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--border:#e2e8f0;--text:#0f172a;--t2:#475569;--t3:#94a3b8;
--accent:#0d9488;--warn-bg:#fffbeb;--warn-b:#fde68a;--info-bg:#eff6ff;--info-b:#bfdbfe;
--mono:"SFMono-Regular",Consolas,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--t2);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;
padding:16px 18px;margin-bottom:16px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
h2{font-size:15px;margin:0 0 10px}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:10px 0}
.metric{background:#f8fafc;border:1px solid var(--border);border-radius:9px;padding:8px 10px}
.metric .k{color:var(--t3);font-size:11px}
.metric .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
table.kv{width:100%;border-collapse:collapse;font-size:13px}
table.kv th,table.kv td{text-align:left;padding:5px 9px;border-bottom:1px solid var(--border)}
table.kv th{color:var(--t2);font-weight:600;white-space:nowrap;background:#f8fafc;width:38%}
canvas{width:100%;display:block;border:1px solid var(--border);border-radius:9px;background:#fff}
.wrapc{position:relative}
.bar{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--t3);margin-top:6px}
.bar .grad{flex:1;height:10px;border-radius:5px}
.note{background:var(--warn-bg);border:1px solid var(--warn-b);
border-radius:9px;padding:10px 12px;font-size:13px;margin:12px 0}
.info{background:var(--info-bg);border:1px solid var(--info-b);border-radius:9px;
padding:10px 12px;font-size:13px;margin:12px 0}
code{font-family:var(--mono);font-size:12px;background:#f1f5f9;padding:1px 5px;border-radius:4px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:840px){.grid2{grid-template-columns:1fr}}
</style></head><body><div class="wrap">

<h1>200×200 μm 矩形槽仿真</h1>
<div class="sub" id="sub"></div>

<div class="note">
<strong>这是模型预测，不是实测。</strong>未导入实测高度图，
不构成二维形貌验证。依据：矩形区域按<strong>弓字形填充</strong>（[U1] §12），
焦点固定在原始上表面（<strong>不做逐层 Z 调整</strong>）。
</div>

<div class="card">
<h2>概况</h2>
<div class="metrics" id="metrics"></div>
<div id="geom"></div>
</div>

<div class="card">
<h2>俯视：矩形槽深度分布</h2>
<div class="wrapc"><canvas id="heat" height="420"></canvas></div>
<div class="bar"><span id="cmin"></span><div class="grad" id="grad"></div><span id="cmax"></span></div>
<div class="sub" style="margin:8px 0 0" id="heatnote"></div>
</div>

<div class="grid2">
<div class="card">
  <h2>垂直扫描方向（Y）截面</h2>
  <div class="sub" style="margin:0 0 8px">最能看出<strong>搭接起伏与漏加工</strong></div>
  <canvas id="secY" height="230"></canvas>
</div>
<div class="card">
  <h2>沿扫描方向（X）截面</h2>
  <div class="sub" style="margin:0 0 8px">一条扫描线上的纵向起伏</div>
  <canvas id="secX" height="230"></canvas>
</div>
</div>

<div class="card">
<h2>深度直方图</h2>
<canvas id="hist" height="200"></canvas>
<div class="sub" style="margin:8px 0 0">
  若底部理想平整，直方图应是一个**窄峰**；拖尾说明存在过深切槽。
</div>
</div>

<div class="card">
<h2>参数与依据</h2>
<div id="params"></div>
<div class="info" id="basis"></div>
</div>

</div>
<script>
const D = __DATA__;
const S = D.stats, M = D.meta;
const fmt = (v,n=2)=> (v===null||v===undefined||Number.isNaN(v))?"—":Number(v).toFixed(n);

document.getElementById("sub").textContent =
  `${D.regionUm}×${D.regionUm} μm ｜ 间距 h=${M.spacingUm} μm ｜ 层数 N=${M.layers} ｜ ` +
  `f=${M.fKHz} kHz ｜ v=${M.vMmS} mm/s ｜ τ=${M.tauFs} fs ｜ 增益 a=${fmt(M.gain,4)}`;

const mets = [
  ["平均深度", fmt(S.meanUm,1)+" μm"],
  ["最大深度", fmt(S.maxUm,1)+" μm"],
  ["覆盖率", (S.coverage*100).toFixed(1)+" %"],
  ["起伏 P95−P5", fmt(S.ripplePvUm,1)+" μm"],
  ["相对起伏", fmt(S.flatnessPct,1)+" %"],
  ["标准差", fmt(S.stdUm,1)+" μm"],
  ["事件数", M.events.toLocaleString()],
  ["求解耗时", fmt(M.solveSeconds,1)+" s"],
];
document.getElementById("metrics").innerHTML = mets.map(([k,v])=>
  `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

document.getElementById("geom").innerHTML = `<table class="kv">
<tr><th>网格</th><td>${M.fullNx}×${M.fullNy}（显示下采样到 ${D.gridNx}×${D.gridNy}）</td></tr>
<tr><th>脉冲能量</th><td>${fmt(M.pulseEnergyUJ,1)} µJ（= P<sub>物镜后</sub>/f）</td></tr>
<tr><th>求解用光斑半径</th><td>${fmt(M.spotRadiusUm,4)} µm</td></tr>
<tr><th>单层扫描线数</th><td>${Math.floor(D.regionUm/M.spacingUm)+1} 条（按请求间距 ${M.spacingUm} µm 布点，不拉伸）</td></tr>
</table>`;

function cmap(t){ // viridis 近似
  const stops=[[68,1,84],[72,40,120],[62,74,137],[49,104,142],[38,130,142],
               [31,158,137],[53,183,121],[109,205,89],[180,222,44],[253,231,37]];
  t=Math.max(0,Math.min(1,t)); const x=t*(stops.length-1); const i=Math.floor(x); const f=x-i;
  const a=stops[i], b=stops[Math.min(i+1,stops.length-1)];
  return `rgb(${Math.round(a[0]+(b[0]-a[0])*f)},${Math.round(a[1]+(b[1]-a[1])*f)},${Math.round(a[2]+(b[2]-a[2])*f)})`;
}

// 俯视热图
(function(){
  const c=document.getElementById("heat"); const nx=D.gridNx, ny=D.gridNy;
  const dpr=window.devicePixelRatio||1; const W=c.clientWidth||900;
  const cell=Math.max(1,Math.floor(Math.min(W/nx, 420/ny)));
  c.width=cell*nx*dpr; c.height=cell*ny*dpr; c.style.height=(cell*ny)+"px";
  const ctx=c.getContext("2d"); ctx.scale(dpr,dpr);
  const vmax=S.maxUm||1;
  for(let j=0;j<ny;j++) for(let i=0;i<nx;i++){
    ctx.fillStyle=cmap(D.depthUm[j][i]/vmax);
    ctx.fillRect(i*cell, j*cell, cell, cell);
  }
  document.getElementById("grad").style.background=
    `linear-gradient(90deg,${[0,.25,.5,.75,1].map(t=>cmap(t)).join(",")})`;
  document.getElementById("cmin").textContent="0 μm";
  document.getElementById("cmax").textContent=fmt(vmax,0)+" μm";
  document.getElementById("heatnote").textContent=
    `色标 = 深度（0 → 最深）。每格 ${fmt(D.dxUm,3)}×${fmt(D.dyUm,3)} μm；`+
    `图上可见弓字形扫描留下的**平行条纹**——条纹间距就是填充间距 h。`;
})();

// 折线
function line(canvasId, xs, ys, color){
  const c=document.getElementById(canvasId);
  const dpr=window.devicePixelRatio||1; const W=c.clientWidth||500, H=230;
  c.width=W*dpr; c.height=H*dpr; c.style.height=H+"px";
  const ctx=c.getContext("2d"); ctx.scale(dpr,dpr);
  const PL=56,PR=14,PT=12,PB=34, iw=W-PL-PR, ih=H-PT-PB;
  const xLo=Math.min(...xs), xHi=Math.max(...xs);
  const yLo=0, yHi=Math.max(...ys)*1.08||1;
  const X=v=>PL+((v-xLo)/(xHi-xLo||1))*iw, Y=v=>PT+ih-((v-yLo)/(yHi-yLo))*ih;
  ctx.strokeStyle="#e2e8f0"; ctx.fillStyle="#94a3b8"; ctx.font="11px sans-serif";
  for(let k=0;k<=4;k++){ const v=yLo+(yHi-yLo)*k/4;
    ctx.beginPath(); ctx.moveTo(PL,Y(v)); ctx.lineTo(PL+iw,Y(v)); ctx.stroke();
    ctx.fillText(v.toFixed(0), 8, Y(v)+4);
  }
  ctx.strokeStyle="#cbd5e1"; ctx.beginPath();
  ctx.moveTo(PL,PT); ctx.lineTo(PL,PT+ih); ctx.lineTo(PL+iw,PT+ih); ctx.stroke();
  ctx.strokeStyle=color; ctx.lineWidth=1.6; ctx.beginPath();
  xs.forEach((x,i)=>{ i?ctx.lineTo(X(x),Y(ys[i])):ctx.moveTo(X(x),Y(ys[i])); });
  ctx.stroke();
  ctx.fillStyle="#475569"; ctx.fillText("深度 (μm)", 6, 10);
  ctx.textAlign="right"; ctx.fillText("位置 (μm)", W-6, H-6); ctx.textAlign="left";
}
line("secY", D.yAxisUm, D.sectionAlongYUm, "#0d9488");
line("secX", D.xAxisUm, D.sectionAlongXUm, "#b45309");

// 直方图
(function(){
  const c=document.getElementById("hist");
  const dpr=window.devicePixelRatio||1; const W=c.clientWidth||900, H=200;
  c.width=W*dpr; c.height=H*dpr; c.style.height=H+"px";
  const ctx=c.getContext("2d"); ctx.scale(dpr,dpr);
  const flat=[]; for(const r of D.depthUm) for(const v of r) flat.push(v);
  flat.sort((a,b)=>a-b);
  const lo=flat[0], hi=flat[flat.length-1], nb=40, bw=(hi-lo)/nb||1;
  const bins=new Array(nb).fill(0);
  for(const v of flat){ let k=Math.floor((v-lo)/bw); if(k>=nb)k=nb-1; if(k<0)k=0; bins[k]++; }
  const mx=Math.max(...bins);
  const PL=56,PR=14,PT=12,PB=34, iw=W-PL-PR, ih=H-PT-PB;
  ctx.fillStyle="#94a3b8"; ctx.font="11px sans-serif";
  for(let k=0;k<=4;k++){ const v=mx*k/4; const y=PT+ih-(k/4)*ih;
    ctx.fillText(Math.round(v), 8, y+4);
    ctx.strokeStyle="#e2e8f0"; ctx.beginPath(); ctx.moveTo(PL,y); ctx.lineTo(PL+iw,y); ctx.stroke(); }
  for(let i=0;i<nb;i++){
    const x=PL+(i/nb)*iw, y=PT+ih-(bins[i]/mx)*ih;
    ctx.fillStyle="#0d9488"; ctx.fillRect(x+0.5, y, iw/nb-1, PT+ih-y);
  }
  ctx.fillStyle="#475569";
  ctx.fillText(fmt(lo,0)+" μm", PL, H-12);
  ctx.textAlign="right"; ctx.fillText(fmt(hi,0)+" μm", PL+iw, H-12); ctx.textAlign="left";
})();

document.getElementById("params").innerHTML = `<table class="kv">
<tr><th>目标区域</th><td>${D.regionUm}×${D.regionUm} μm（[U1] §12 的 nominal 编程区域）</td></tr>
<tr><th>填充间距 h</th><td>${M.spacingUm} μm</td></tr>
<tr><th>层数 N</th><td>${M.layers}（每层一次完整弓字形扫描）</td></tr>
<tr><th>重复频率 / 扫描速度</th><td>${M.fKHz} kHz / ${M.vMmS} mm/s</td></tr>
<tr><th>脉宽 / 增益</th><td>${M.tauFs} fs / a=${fmt(M.gain,4)}</td></tr>
<tr><th>俯视条纹间距</th><td>${M.spacingUm} μm（= h，直接看得见）</td></tr>
</table>`;

const b = M.spotBasis || {};
document.getElementById("basis").innerHTML =
  (b.declared_width_um
    ? `几何依据：<strong>实测单线宽度 ${fmt(b.declared_width_um,3)} μm</strong> → ` +
      `等效光斑半径 <strong>${fmt(b.spot_radius_um,4)} μm</strong>；` +
      `名义 w0 ${fmt(b.nominal_waist_um,4)} μm <strong>只作光学记录</strong>` +
      `（它给出的烧蚀宽度只有 ${fmt(b.nominal_width_um,3)} μm）。` +
      `焦点固定在原始上表面，<strong>不做逐层 Z 调整</strong>。`
    : `未声明实测单线宽度 → 用名义 w0 ${fmt(b.spot_radius_um,4)} μm。`);
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", type=float, default=200.0)
    ap.add_argument("--dx", type=float, default=0.5)
    ap.add_argument("--spacing", type=float, default=4.0)
    ap.add_argument("--layers", type=int, default=1)
    ap.add_argument("--freq", type=float, default=20.0)
    ap.add_argument("--speed", type=float, default=50.0)
    ap.add_argument("--tau", type=float, default=223.0)
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--out", default=str(OUT_HTML))
    a = ap.parse_args()

    print(f"  区域 {a.region}×{a.region} μm  dx={a.dx} μm  h={a.spacing}  N={a.layers}")
    print("  正在求解（200×200 在 0.5 μm 网格上约 16 万格，需要一点时间）…")
    r = build_and_run(region_um=a.region, dx_um=a.dx, spacing_um=a.spacing,
                      layers=a.layers, f_khz=a.freq, v_mm_s=a.speed,
                      tau_fs=a.tau, gain=a.gain, baseline=Path(a.baseline))
    import numpy as np

    d = r["depthUm"]
    print(f"  求解完成：{r['events']:,} 事件，{r['solveSeconds']:.1f}s")
    print(f"    平均 {d.mean():.1f} μm  最大 {d.max():.1f} μm  "
          f"覆盖 {(d>0).mean():.1%}  起伏 P95−P5 {np.percentile(d,95)-np.percentile(d,5):.1f} μm")
    out = write_html(r, Path(a.out))
    print(f"  报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
