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

    curve = U.load_curve_card(ROOT / "data" / "curves", "analytic_fixture_depth_vs_fluence.curve.json")
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
    name = "analytic_fixture_depth_vs_fluence.curve.json"
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
    name = "analytic_fixture_depth_vs_fluence.curve.json"
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


def test_curves_payload_exposes_entry_class():
    """每张曲线卡都要带 ``entryClass``，供前端把人工解析/合成数据
    从默认入口分流到「人工解析测试」。

    这是**接口契约**：前端据此分流，缺字段会退化成「全部当实测」，
    正是 U04 要消除的误导。
    """
    payload = W.curves_payload(ROOT / "data" / "curves")
    assert payload["curves"]
    for c in payload["curves"]:
        assert "entryClass" in c, f"{c.get('curveId')} 缺 entryClass"
        assert c["entryClass"] in ("measured", "fixture")
    # 当前仓库三张卡都是 fixture（公式重算/人工解析/合成），
    # 因此**不应**有任何卡片声称自己是实测数据
    assert all(c["entryClass"] == "fixture" for c in payload["curves"]), (
        "现有曲线卡都不是实测数据，不得标为 measured"
    )


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


# ---------------------------------------------------------------------------
# 业务错误必须返回 JSON（审查缺陷 F01 / 升级任务 U01）
#
# 回归背景：``except UFDemoError`` 分支写成
# ``self._send_error_json(err.code, err.message, status=400, **err.to_dict())``，
# 而 ``to_dict()`` 已含 ``code``/``message`` → ``TypeError: got multiple values
# for argument 'code'``。异常在 ``_send_error_json`` **执行之前**抛出，
# 于是一个响应都发不出去，客户端只看到连接被关（``RemoteDisconnected``）。
#
# 相邻的 ``ContractError``/``HttpError`` 分支当时已过滤，只有 ``UFDemoError``
# 漏改 → **不是所有错误都触发**，属间歇性故障，更难排查。
# ---------------------------------------------------------------------------


def test_error_response_is_single_conversion_point():
    """唯一错误→HTTP 转换点：三类异常都不产生重复关键字参数。"""
    from ufdemo import webapp

    cases = [
        UFDemoError("TABLE_OUT_OF_RANGE", "越界", field_path="x", actual=1.0),
        W.ContractError("RUN_NOT_FOUND", "没有该运行", status=404, field_path="run_dir"),
        webapp.HttpError("BAD_REQUEST_BODY", "请求体不合法", status=400, field_path="body"),
        RuntimeError("兜底"),
    ]
    for err in cases:
        code, message, status, extra = webapp.error_response(err)
        assert isinstance(code, str) and code
        assert isinstance(message, str) and message
        assert isinstance(status, int)
        # 关键：extra 里不得再出现位置参数已占用的键
        assert "code" not in extra, f"{type(err).__name__} 的 extra 泄漏了 code"
        assert "message" not in extra, f"{type(err).__name__} 的 extra 泄漏了 message"
        # 能真的序列化出去（与 _send_error_json 同一口径）
        json.dumps({"code": code, "message": message, **extra},
                   ensure_ascii=False, allow_nan=False)


def test_error_response_preserves_status_and_zh_code():
    """三类异常的既有状态码与中文原因不得被统一化抹掉。"""
    from ufdemo import webapp

    _, _, status, _ = webapp.error_response(
        W.ContractError("RUN_NOT_FOUND", "没有该运行", status=404))
    assert status == 404, "ContractError 自带 404，不得被改成 400"

    _, _, status, _ = webapp.error_response(
        webapp.HttpError("REQUEST_TOO_LARGE", "过大", status=413))
    assert status == 413

    _, message, status, extra = webapp.error_response(
        UFDemoError("CONFIG_INVALID", "配置无效", field_path="grid.nx"))
    assert status == 400, "UFDemoError 映射 400"
    assert extra.get("field_path") == "grid.nx", "字段路径必须保留"
    assert extra.get("code_zh") == "配置无效", "UFDemoError 应带上中文码"


def test_error_response_survives_nonfinite_actual():
    """``actual`` 为 NaN/Inf 时也必须能发出 JSON（allow_nan=False 会拒绝 NaN）。"""
    from ufdemo import webapp

    _, _, _, extra = webapp.error_response(
        UFDemoError("NUMERIC_NONFINITE", "非有限", actual=float("nan")))
    json.dumps(extra, ensure_ascii=False, allow_nan=False)  # 不得抛 ValueError


@pytest.mark.slow
def test_http_business_error_returns_json_and_keeps_connection(live_server):
    """F01 核心验收：业务错误必须返回可解析 JSON，且**不断开连接**。

    分两类，因为本工程的错误出口有**两条**，行为不同（这本身是正确设计）：

    * **HTTP 层拒绝** —— 异常冒泡到 ``do_GET``/``do_POST``，返回 4xx + JSON。
      这些正是原先 ``except UFDemoError`` 漏改的分支。
    * **契约层优雅拒绝** —— ``solve_payload`` 内部 ``try/except`` 后返回
      ``200 + ok=False + errors[]``（前端逐条显示校验报告，而不是把它当网络错误）。
      这类**不应该**变成 500，也不应该断连。

    两类都断言同一件事：**连接没被关、响应是 JSON**。
    """
    # -- A. HTTP 层拒绝：必须 4xx + JSON --
    http_layer_rejects: list[tuple[str, dict[str, Any]]] = [
        ("/api/lookup", {"curve": "__no_such_curve__", "xs": [1.0]}),
        ("/api/curve-grid", {"curve": "__no_such_curve__"}),
        ("/api/reference", {"case": "__no_such_case__"}),
    ]
    for path, body in http_layer_rejects:
        # 关键：不捕获连接异常 —— 若连接被关，测试直接抛错失败
        status, payload = _post(live_server, path, body)
        assert 400 <= status < 500, f"{path} 期望 4xx，实际 {status}：{payload!r}"
        assert isinstance(payload, dict), f"{path} 未返回 JSON 对象：{payload!r}"
        assert payload.get("code"), f"{path} 缺少 code：{payload!r}"
        assert payload.get("message"), f"{path} 缺少 message：{payload!r}"

    # -- A2. 非有限输入（NaN）同样走 UFDemoError 分支 --
    status, payload = _post(
        live_server, "/api/lookup", {"curve": "__no_such_curve__", "xs": ["nan"]})
    assert 400 <= status < 500, f"NaN 输入期望 4xx，实际 {status}"
    assert payload.get("code"), payload

    # NaN 打到**真实存在**的曲线：越界/语义错误也必须给出 JSON，而不是断连
    status, curves = _get(live_server, "/api/curves")
    assert curves["curves"], "应至少有一条曲线"
    real_curve = curves["curves"][0]["name"]
    status, payload = _post(
        live_server, "/api/lookup", {"curve": real_curve, "xs": [float("nan")]})
    assert isinstance(payload, dict), f"NaN 打到真实曲线时未返回 JSON：{payload!r}"
    assert payload.get("code") or payload.get("ok") is not None, payload

    # -- B. 契约层优雅拒绝：200 + status="failed" + errors[]，且不是断连 --
    graceful: list[dict[str, Any]] = [
        {"material_card_file": "data/materials/__no_such_card__.json"},
        {"grid": {"nx": 1, "ny": 1}},  # 非法几何
    ]
    base = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    for override in graceful:
        params = {**base, **override}
        if "grid" in override:
            params["grid"] = {**base["grid"], **override["grid"]}
        status, payload = _post(live_server, "/api/solve", {"params": params})
        assert status == 200, f"契约层拒绝应返回 200（不是 500/断连），实际 {status}"
        # 契约层用 status="failed" 表达失败（见 webcontract._failure → run_to_payload），
        # 不用 ok=False 那种形状；两种都接受，但必须明确是失败且带 errors。
        assert payload.get("status") == "failed" or payload.get("ok") is False, \
            f"应优雅失败：{payload!r}"
        errs = payload.get("errors") or []
        assert errs, f"失败必须带 errors：{payload!r}"
        assert errs[0].get("code"), f"errors[0] 缺 code：{errs[0]!r}"
        assert errs[0].get("message"), f"errors[0] 缺 message：{errs[0]!r}"
        json.dumps(payload, allow_nan=False)  # 失败响应体也必须无 NaN


@pytest.mark.slow
def test_http_malformed_params_never_kills_connection(live_server):
    """畸形 params 也必须得到 JSON —— 哪怕内部抛的是未预期异常。"""
    # ``grid`` 类型完全错误：RunConfig 可能抛非 UFDemoError。无论哪种，
    # 统一出口都应把它变成 JSON，而不是让连接静默关闭。
    for bad in ({"grid": "not-an-object"}, {"laser": None}, {"path": 42}):
        status, payload = _post(live_server, "/api/solve", {"params": bad})
        assert isinstance(payload, dict), f"{bad} 未返回 JSON：{payload!r}"
        assert payload.get("code") or payload.get("errors") or payload.get("ok") is not None


@pytest.mark.slow
def test_http_error_paths_do_not_kill_server(live_server):
    """连发多个坏请求后，服务仍能正常响应 —— 证明单请求失败不拖垮服务。"""
    for _ in range(3):
        try:
            _post(live_server, "/api/lookup", {"curve": "__no_such_curve__", "xs": [1.0]})
        except Exception:  # noqa: BLE001
            # 故意吞掉：本测试只关心「服务还活着」，单次请求的失败原因
            # 由 test_http_business_error_returns_json_and_keeps_connection 断言。
            pass
    status, body = _get(live_server, "/api/health")
    assert status == 200 and body["ok"] is True


# ---------------------------------------------------------------------------
# 非有限数值不得让任何出口失败（U01 连带发现）
#
# 三处出口全部用 ``allow_nan=False``：``io.stable_json``、``webapp._send_json``、
# 以及 CLI。若 ``UFDemoError.to_dict()`` 原样保留 NaN 的 ``actual``，
# 就会在**错误处理路径上再抛异常** —— 诊断文件写不出来、HTTP 错误响应发不出去。
# 归一化放在 ``errors._jsonable``（源头），webapp 再兜一道网。
# ---------------------------------------------------------------------------


def test_ufdemo_error_to_dict_is_json_safe_with_nonfinite_actual():
    """``to_dict()`` 必须能直接喂给 ``json.dumps(allow_nan=False)``。"""
    for bad in (float("nan"), float("inf"), float("-inf")):
        err = UFDemoError("NUMERIC_NONFINITE", "非有限数值", field_path="x", actual=bad)
        d = err.to_dict()
        assert d["actual"] is None, f"actual={bad!r} 应归一化为 None，实际 {d['actual']!r}"
        json.dumps(d, ensure_ascii=False, allow_nan=False)  # 不得抛 ValueError
        # 属性本身保持原值（只改序列化结果，不改语义）
        assert isinstance(err.actual, float)


def test_ufdemo_error_to_dict_nested_nonfinite_is_safe():
    """``actual`` 是容器时也要逐层归一化（如数组形式的非法输入）。"""
    err = UFDemoError(
        "NUMERIC_NONFINITE", "非有限数值",
        actual={"xs": [1.0, float("nan"), {"deep": float("inf")}]},
    )
    d = err.to_dict()
    json.dumps(d, ensure_ascii=False, allow_nan=False)
    assert d["actual"] == {"xs": [1.0, None, {"deep": None}]}


def test_stable_json_accepts_error_dict_with_nan_actual(tmp_path):
    """落盘路径同样不得因 NaN 崩溃（``io.stable_json`` 用 allow_nan=False）。"""
    from ufdemo.io import stable_json

    err = UFDemoError("NUMERIC_NONFINITE", "非有限数值", actual=float("nan"))
    text = stable_json({"errors": [err.to_dict()]})
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["errors"][0]["actual"] is None


def test_webapp_and_error_sanitizers_agree():
    """``webapp._json_safe`` 与 ``errors._jsonable`` 语义必须一致（防漂移）。"""
    from ufdemo import webapp
    from ufdemo.errors import _jsonable

    samples: list[Any] = [
        float("nan"), float("inf"), float("-inf"), 1.5, True, False,
        None, "x", [1.0, float("nan")], {"a": float("inf"), "b": [float("nan")]},
    ]
    for s in samples:
        assert webapp._json_safe(s) == _jsonable(s), f"两者对 {s!r} 处理不一致"


@pytest.mark.slow
def test_http_nonfinite_query_returns_json_not_error(live_server):
    """真实曲线 + NaN 查询：契约层优雅拒绝（``ok=False``），不得变成 500。"""
    status, curves = _get(live_server, "/api/curves")
    name = curves["curves"][0]["name"]
    status, payload = _post(live_server, "/api/lookup", {"curve": name, "xs": [float("nan")]})
    assert status == 200, f"应为契约层优雅拒绝（200），实际 {status}：{payload!r}"
    assert payload.get("ok") is False, payload
    errs = payload.get("errors") or []
    assert errs and errs[0].get("code"), payload
    json.dumps(payload, allow_nan=False)  # 响应体内不得留 NaN


@pytest.mark.slow
def test_http_history_readback_keeps_grid_and_snapshot_meta(live_server):
    """**读历史必须带回网格与快照元信息**（U03-8 暴露的缺陷，非 F01–F08 之一）。

    背景：``run_to_payload`` 的 ``grid`` 取自 ``frozen.surface()``，
    而读历史路径 ``result is None`` → ``grid`` 变空字典。后果只在**读历史**
    这条路径出现：

    * 时间轴 ``drawHeatmap(..., f.grid.nx, f.grid.ny)`` 收到 ``undefined``；
    * ``createImageData(undefined, undefined)`` 抛
      ``Value is not of type 'long'`` → 回放画布永远空白；
    * 截面 ``Array.from(f.grid.xs)`` 也拿不到坐标。

    快照元信息同理：契约把它嵌在 ``label`` 子对象里、键名 snake_case
    （``event_index`` / ``time_s`` / ``pass_id``），前端若读顶层 camelCase
    就会显示 “事件 undefined”。
    """
    raw = json.loads(TEN_PULSES.read_text(encoding="utf-8"))
    status, out = _post(live_server, "/api/solve", {"params": raw, "label": "grid_hist"})
    assert status == 200 and out["status"] == "completed"
    run_id = out["runId"]

    live_grid = out["grid"]
    assert live_grid.get("nx") and live_grid.get("ny"), "实时提交必须带网格"

    status, back = _get(live_server, "/api/runs/" + run_id)
    assert status == 200 and back["readFromDisk"] is True

    g = back.get("grid") or {}
    assert g.get("nx") == live_grid["nx"], f"读历史 nx 丢失：{g}"
    assert g.get("ny") == live_grid["ny"], f"读历史 ny 丢失：{g}"
    assert g.get("reconstructedFromDisk") is True, "读历史应标注网格来自盘上重建"
    assert len(g.get("xs") or []) == g["nx"], "读历史应带回 x 坐标"
    assert len(g.get("ys") or []) == g["ny"], "读历史应带回 y 坐标"

    snaps = back.get("snapshots") or []
    assert snaps, "应有快照"
    label = snaps[-1].get("label") or {}
    for key in ("event_index", "time_s", "pass_id"):
        assert key in label, f"快照 label 缺 {key}：{label}"
        assert label[key] is not None, f"快照 {key} 为 None：{label}"
    # 前端渲染读的正是 label 里的这三个键（snake_case），不是顶层 camelCase
    assert not any(k in snaps[-1] for k in ("eventIndex", "timeS", "passId")), (
        "契约不应同时提供顶层 camelCase 别名，否则前端会读错分支"
    )
