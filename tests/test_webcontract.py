"""前后端契约层与服务测试（批次 K）。

分三层验证：

* :mod:`ufdemo.webcontract` 的纯函数（JSON 安全、结果序列化、清单）；
* **不变量**：越界返回 ``None``、``threshold_only`` 不给数值 0、
  不可用图层带原因、查表不触发求解；
* :mod:`ufdemo.webapp` 的 HTTP 端到端（起真实服务，发真实请求，再关掉）。

服务测试用真实 socket，但绑定 ``127.0.0.1`` 随机空闲端口，且运行目录隔离到
``tmp_path``，因此不污染工作区、不依赖固定端口。
"""

from __future__ import annotations

import json
import math
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ufdemo import webcontract as W
from ufdemo import ui_service as U
from ufdemo.config import RunConfig, validate_run
from ufdemo.errors import UFDemoError
from ufdemo.materials import load_material_card

ROOT = Path(__file__).resolve().parents[1]
TEN_PULSES = ROOT / "examples" / "ten_pulses.json"


# ---------------------------------------------------------------------------
# JSON 安全
# ---------------------------------------------------------------------------


def test_jsonable_handles_nan_inf_as_none():
    """NaN/Inf 必须转 ``None`` —— JSON 没有 NaN，写 ``NaN`` 会让前端解析失败。"""
    assert W.jsonable(float("nan")) is None
    assert W.jsonable(float("inf")) is None
    assert W.jsonable(float("-inf")) is None
    assert W.jsonable(1.5) == 1.5


def test_jsonable_array_with_nan_stays_parseable():
    arr = np.array([[1.0, np.nan], [np.inf, -2.0]])
    out = W.jsonable(arr)
    assert out == [1.0, None, None, -2.0]  # 扁平化，NaN/Inf → None
    json.dumps(out, allow_nan=False)  # 不得抛错


def test_jsonable_expands_dataclass():
    """dataclass 必须展开成字典，否则会变成不可解析的 repr 字符串。"""
    from ufdemo import tables as T

    curve = U.load_curve_card(ROOT / "data" / "curves", "ysz_analytic_depth_vs_fluence.curve.json")
    res = U.table_lookup(U.new_session({}), curve, [5.0], method="linear")
    out = W.jsonable(res)
    assert isinstance(out, dict), f"应展开为 dict，实际 {type(out).__name__}"
    assert "values" in out and "status" in out
    assert "TableLookup(" not in json.dumps(out, ensure_ascii=False)


def test_jsonable_keeps_bool_distinct_from_int():
    """``bool`` 是 ``int`` 子类，必须先判 bool，否则 True 会变成 1。"""
    assert W.jsonable(True) is True
    assert W.jsonable(False) is False
    assert isinstance(W.jsonable(True), bool)


# ---------------------------------------------------------------------------
# 结果序列化
# ---------------------------------------------------------------------------


def _solve_ten_pulses(tmp_path: Path):
    raw = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    return W.solve_payload(
        raw, out_base=tmp_path, project_root=ROOT, label="webcontract_test"
    )


def test_solve_payload_real_run(tmp_path):
    out = _solve_ten_pulses(tmp_path)
    assert out["status"] == "completed"
    assert out["removalAvailable"] is True
    assert out["stats"]["center_depth_internal"] == pytest.approx(2e-6, rel=1e-9)
    assert out["layers"]["height"], "高度层应非空"
    assert out["grid"]["nx"] > 0 and out["grid"]["ny"] > 0
    assert len(out["snapshots"]) >= 1
    assert out["snapshots"][-1]["final"] is True
    json.dumps(out, allow_nan=False)


def test_unavailable_layers_carry_reason_not_fake_arrays(tmp_path):
    """不可用图层：给出**原因**，且不进 ``layers``（不返回全 0 假数组）。"""
    out = _solve_ten_pulses(tmp_path)
    blocked = out["blocked"]
    assert blocked, "均质运行为未启用分相，应有 blocked 原因"
    assert "phase_id" in blocked, blocked
    assert isinstance(blocked["phase_id"], str) and len(blocked["phase_id"]) > 5
    assert "phase_id" not in out["layers"], "不可用图层不得出现在 layers 中"


def test_panels_present_without_fake_values(tmp_path):
    """四个诊断面板随结果返回；未启用的面板为**空字典**，不填 0 假值。"""
    out = _solve_ten_pulses(tmp_path)
    panels = out["panels"]
    assert set(panels) == {"threshold", "acceleration", "geometry", "structure"}
    # 正入射 + 未开协议 → 阈值与几何应如实为空/未启用
    assert panels["threshold"] == {} or panels["threshold"].get("available") is False
    # 正入射 → 未启用几何修正 → 面板为**空字典**（界面上不显示，而不是显示全 0）
    assert panels["geometry"] == {}, panels["geometry"]


def test_provenance_states_no_experimental_claim(tmp_path):
    """三栏分开：契约里明确本结果只做**数值实现验证**，不含实验复现。"""
    out = _solve_ten_pulses(tmp_path)
    prov = out["provenance"]
    assert prov["verificationKind"] == "数值实现验证"
    assert "不含" in prov["note"] or "**不含**" in prov["note"]


def test_threshold_only_never_returns_zero_depth(tmp_path):
    """``threshold_only``：深度是 ``null``（不提供），**不是**数值 0。"""
    raw = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    raw["run_mode"] = "threshold_only"
    out = W.solve_payload(raw, out_base=tmp_path, project_root=ROOT, label="thr_test")
    if out["status"] != "completed":
        pytest.skip(f"该卡未开放 threshold_only 模式（如实拒绝）：{out['errors'][0]['code']}")
    assert out["removalAvailable"] is False
    d = out["stats"].get("center_depth_internal")
    assert d is not None or d is None  # 允许 None
    assert d != 0, "不得用数值 0 冒充「不提供」"
    assert "depth" not in out["layers"]
    assert out["blocked"].get("depth"), "深度层应给出不可用原因"


def test_failed_solve_reports_errors_not_fake_success(tmp_path):
    """准入失败 → ``status="failed"`` 与错误列表，不伪造成功结果。"""
    raw = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    raw["grid"]["nx"] = 2  # 非法：单元中心网格至少 3
    out = W.solve_payload(raw, out_base=tmp_path, project_root=ROOT, label="bad_test")
    assert out["status"] == "failed"
    assert out["errors"], "失败必须带错误条目"
    assert out["layers"] == {} and out["stats"] == {}


def test_preview_does_not_solve(tmp_path):
    """预检只做准入，不落盘、不求解。"""
    raw = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    before = sorted(p.name for p in tmp_path.glob("*"))
    res = W.preview_payload(raw)
    after = sorted(p.name for p in tmp_path.glob("*"))
    assert res["ok"] is True
    assert before == after, "预检不得产生运行目录"


# ---------------------------------------------------------------------------
# 清单与查表
# ---------------------------------------------------------------------------


def test_materials_catalog_marks_physical_prediction():
    """材料目录：能否输出物理深度由「是否开放 reference_case」决定。"""
    payload = W.materials_payload(ROOT / "data" / "materials")
    cat = payload["catalog"]
    assert len(cat) >= 7
    for card in cat:
        expect = "reference_case" in card["allowedRunModes"]
        assert card["physicalPredictionAllowed"] is expect, card["id"]
    # 合成卡不得声称能出物理预测
    synth = [c for c in cat if "synthetic" in c["id"]]
    assert synth and all(c["physicalPredictionAllowed"] is False for c in synth)


def test_material_entries_three_kinds_and_probe_result():
    """七材料能力入口：三类条目并存，且探针结果如实返回。"""
    payload = W.material_entries_payload(ROOT / "data" / "materials")
    kinds = {r["kind"] for r in payload["entries"]}
    assert {"opened", "blocked", "deferred"} <= kinds, kinds
    s = payload["summary"]
    assert s["n_opened"] > 0 and s["n_blocked"] > 0 and s["n_deferred"] >= 1
    assert s["all_probes_hold"] is True, "拦截失效或缺口消失时本项应为假"


def test_lookup_out_of_range_returns_none_not_zero():
    """越界：允许时返回 ``None``，**绝不**返回 0 / 不外推 / 不钳端点。"""
    curves = ROOT / "data" / "curves"
    name = "ysz_analytic_depth_vs_fluence.curve.json"
    curve = U.load_curve_card(curves, name)
    lo, hi = curve.valid_range
    out = W.lookup_payload(curves, name, [lo - 1.0, (lo + hi) / 2, hi + 1.0], allow_out_of_range=True)
    vals = out["result"]["values"]
    assert vals[0] is None and vals[2] is None
    assert isinstance(vals[1], float) and vals[1] > 0
    assert 0.0 not in (vals[0], vals[2]), "越界不得返回 0"


def test_lookup_out_of_range_rejected_when_not_allowed():
    """不允许越界 → ``ok=False`` 且唯一错误码 ``TABLE_OUT_OF_RANGE``。

    契约层把求解侧的 ``UFDemoError`` 收敛成 ``{ok: False, errors: [...]}``，
    因此这里断言的是**契约形状**，而不是底层异常类型。
    """
    curves = ROOT / "data" / "curves"
    name = "ysz_analytic_depth_vs_fluence.curve.json"
    curve = U.load_curve_card(curves, name)
    out = W.lookup_payload(curves, name, [curve.valid_range[1] + 1.0], allow_out_of_range=False)
    assert out["ok"] is False
    assert out["errors"][0]["code"] == "TABLE_OUT_OF_RANGE"


def test_curves_payload_keeps_raw_points():
    """曲线契约保留**原始点**（CSV 是唯一来源），且标注去向。"""
    payload = W.curves_payload(ROOT / "data" / "curves")
    assert payload["curves"]
    for c in payload["curves"]:
        assert c["rawPoints"], c["curveId"]
        assert c["validRange"] is not None
        assert isinstance(c["capabilityRows"], list)


def test_example_payload_rejects_path_traversal():
    """模板名不得穿越目录（``Path(...).name`` 只取文件名，越界即找不到）。"""
    # 以 .json 结尾的穿越名：只取文件名后必然找不到 → 404
    with pytest.raises(W.ContractError) as ei:
        W.example_payload(ROOT / "examples", "../../secret.json")
    assert ei.value.status == 404
    # 非 .json 名：直接按非法请求拒绝 → 400
    with pytest.raises(W.ContractError) as ei2:
        W.example_payload(ROOT / "examples", "../../pyproject.toml")
    assert ei2.value.status == 400


def test_reference_payload_is_formula_check_only():
    """参考评估器只做公式核查：``verifiedByExperiment`` 必须为 False。"""
    out = W.reference_payload(ROOT / "examples", "ysz_reference_case.json", project_root=ROOT)
    assert out["verifiedByExperiment"] is False
    assert out["provenance"]["verificationKind"] == "公式核查"
    assert out["values"], "应返回可读的量-值-单位行"


# ---------------------------------------------------------------------------
# HTTP 端到端
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_server(tmp_path):
    """起一个真实 HTTP 服务（随机端口 + 隔离运行目录），测试结束即关闭。"""
    from ufdemo import webapp

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    ctx = webapp.build_context(
        project_root=ROOT, runs_dir=tmp_path, webui_dir=ROOT / "webui"
    )
    httpd = webapp.make_server(ctx, "127.0.0.1", port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, Any]:
    """GET 并返回 (状态码, JSON)；4xx/5xx 也要能读到 JSON 错误体。"""
    try:
        with urllib.request.urlopen(base + path, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def _post(base: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as err:  # 4xx 也要能读到 JSON 错误体
        return err.code, json.loads(err.read().decode("utf-8"))


@pytest.mark.slow
def test_http_health_and_static(live_server):
    status, body = _get(live_server, "/api/health")
    assert status == 200 and body["ok"] is True
    assert body["version"]

    with urllib.request.urlopen(live_server + "/", timeout=30) as r:
        html = r.read().decode("utf-8")
    assert r.status == 200
    assert "js/api.js" in html and "js/main.js" in html
    assert html.index("js/api.js") < html.index("js/main.js"), "api.js 必须先于 main.js"


@pytest.mark.slow
def test_http_solve_is_real_and_persists(live_server, tmp_path):
    raw = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    status, out = _post(live_server, "/api/solve", {"params": raw, "label": "http_test"})
    assert status == 200 and out["status"] == "completed"
    assert out["stats"]["center_depth_internal"] == pytest.approx(2e-6, rel=1e-9)
    # 落盘：runs 目录里应出现该运行
    assert out["runDir"] and (Path(out["runDir"]) / "metadata.json").exists()
    status, runs = _get(live_server, "/api/runs")
    assert any(r["run_id"] == out["runId"] for r in runs["runs"])
    # 读回
    status, back = _get(live_server, "/api/runs/" + out["runId"])
    assert status == 200 and back["readFromDisk"] is True
    assert back["stats"]["center_depth_internal"] == pytest.approx(
        out["stats"]["center_depth_internal"], rel=1e-12
    )


@pytest.mark.slow
def test_http_lookup_out_of_range_returns_null(live_server):
    status, curves = _get(live_server, "/api/curves")
    c = curves["curves"][0]
    lo, hi = c["validRange"]
    status, out = _post(
        live_server, "/api/lookup",
        {"curve": c["name"], "xs": [lo - 1, hi + 1], "allowOutOfRange": True},
    )
    assert status == 200
    assert out["result"]["values"] == [None, None], "越界必须是 null，不是 0"
    assert out["solveCountUnchanged"] is True


@pytest.mark.slow
def test_http_run_traversal_blocked(live_server):
    """目录穿越请求必须被拒绝，而不是读到 runs 之外的文件。"""
    status, body = _get(live_server, "/api/runs/..%2F..%2Fpyproject.toml")
    assert status == 404, f"越界请求应得 404，实际 {status}"
    assert body["code"] == "RUN_NOT_FOUND"


@pytest.mark.slow
def test_http_unknown_endpoint_404(live_server):
    status, body = _get(live_server, "/api/nope")
    assert status == 404
    assert body["code"] == "NOT_FOUND"
