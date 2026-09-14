"""零外部依赖的本地 Web 服务：静态前端 + 真实求解 API。

**为什么用 stdlib**：本工程的核心约定是「科学计算核心不依赖界面对象」，
且界面层不得引入额外运行时。:mod:`http.server` 足以支撑本地单人使用
（静态文件 + 少量 JSON API），因此**不引入 FastAPI/Flask**——少一个依赖，
少一处版本漂移。若将来需要多用户并发或鉴权，再单独评估。

**线程模型**：``ThreadingHTTPServer`` 处理并发请求，但**求解加全局锁**——
``solve`` 是 CPU/内存密集的，串行化避免多个大网格同时求解把内存打满。

**不做的事**：不自动降级模式、不改参数、不把失败包装成成功。
所有错误以 ``{code, message, field_path, requirement, suggestion}`` 返回，
前端逐条显示。

用法::

    python -m ufdemo.webapp                 # 默认 127.0.0.1:8787
    python -m ufdemo.webapp --port 9000
    python -m ufdemo.webapp --runs-dir runs/_web    # 隔离输出
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from . import __version__
from . import webcontract as W
from .errors import UFDemoError
from .webcontract import ContractError


class HttpError(Exception):
    """HTTP 层错误（请求体过大 / JSON 不合法 / 缺参数）。

    与 :class: 分开：后者的错误码是**材料与求解语义**的
    封闭集合，而 HTTP 协议层的问题（400/413）不属于那套语义。
    """

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = int(status)
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field_path": self.extra.get("field_path"),
            "requirement": self.extra.get("requirement"),
            "suggestion": self.extra.get("suggestion"),
        }


# ---------------------------------------------------------------------------
# 异常 → HTTP 错误体：**唯一**转换点
#
# 历史教训（审查缺陷 F01）：曾写成
#     self._send_error_json(err.code, err.message, status=400, **err.to_dict())
# 而 ``to_dict()`` 已含 ``code``/``message`` → ``TypeError: got multiple values
# for argument 'code'``。该异常在 ``_send_error_json`` 执行**之前**抛出，
# 于是一个响应都发不出去，客户端只看到连接被关（``RemoteDisconnected``）。
# 当时 ``ContractError``/``HttpError`` 分支已过滤，只有 ``UFDemoError`` 漏改，
# 因此是**间歇性**故障。现在收敛为一处，杜绝再次漂移。
# ---------------------------------------------------------------------------

# 已由位置参数占用的键：展开 extra 时必须剔除，否则重复传参。
_RESERVED_ERROR_KEYS: tuple[str, ...] = ("code", "message")


def _json_safe(value: Any) -> Any:
    """JSON 安全化 —— 直接复用契约层的 :func:`webcontract.jsonable`。

    **不在这里另写一份规则**：``jsonable`` 已是本工程「什么能写进 JSON」的
    唯一权威（``NaN``/``±Inf`` → ``None``、ndarray → list、dataclass → dict、
    ``bool`` 先于 ``int``）。两份实现迟早漂移，而漂移的代价是
    **响应发出不去**（``allow_nan=False`` 抛 ``ValueError``）。

    为什么在这层还要再过一遍：契约层绝大多数出口已 ``jsonable`` 过，
    但 ``lookup_payload`` 等分支会把 ``err.to_dict()`` 直接塞进 **success**
    payload（``ok=False``）；一旦其中含 NaN，序列化就会在响应写出**前**失败。
    这层是「绝不丢失响应」的最后一道网。
    """
    return W.jsonable(value)


def error_response(err: BaseException) -> tuple[str, str, int, dict[str, Any]]:
    """异常 → ``(code, message, status, extra)``。

    ``extra`` 里**不含** ``code`` / ``message``，可直接展开传给
    :meth:`WebAppHandler._send_error_json`。

    状态码沿用各异常既有语义，**不统一压平**：
    ``ContractError`` / ``HttpError`` 用自带 ``status``（如 404 / 413），
    ``UFDemoError`` 映射 400，其余兜底 500。
    """
    if isinstance(err, (ContractError, HttpError)):
        status = int(getattr(err, "status", 400))
    elif isinstance(err, UFDemoError):
        status = 400  # 材料/求解语义错误 ⇒ 客户端请求不合法
    else:
        return "INTERNAL_ERROR", f"{type(err).__name__}: {err}", 500, {}

    body = _json_safe(err.to_dict())
    extra = {k: v for k, v in body.items() if k not in _RESERVED_ERROR_KEYS}
    return str(err.code), str(err.message), status, extra


# 单次请求体上限（8 MB）：解算参数是纯 JSON，超过即视为异常请求
MAX_BODY_BYTES = 8 * 1024 * 1024

# 求解串行化：避免并发大网格求解耗尽内存
_SOLVE_LOCK = threading.Lock()


class AppContext:
    """服务运行所需的路径与元信息（显式传入，便于测试隔离）。"""

    def __init__(
        self,
        *,
        project_root: Path,
        webui_dir: Path,
        runs_dir: Path,
        curves_dir: Path,
        material_dir: Path,
        examples_dir: Path,
        measured_dir: Path | None = None,
    ) -> None:
        self.project_root = project_root
        self.webui_dir = webui_dir
        self.runs_dir = runs_dir
        self.curves_dir = curves_dir
        self.material_dir = material_dir
        self.examples_dir = examples_dir
        # U05：实测数据集目录。默认 None → 用随包资源；显式传入便于测试隔离。
        from . import default_measured_dir as _dmd

        self.measured_dir = Path(measured_dir) if measured_dir is not None else _dmd()
        # V2（C2–C5）：三工作区要用到的目录。
        # · 实验 CSV 常在**仓库外**（上层 `数据/`）→ 不存在时如实报"未找到"，不伪造；
        # · 基线候选与上游样例在仓库内。
        self.experiment_dir = self.project_root.parent / "数据"
        self.baselines_dir = self.project_root / "data" / "baselines"
        self.upstream_dir = self.project_root / "examples" / "upstream"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "projectRoot": str(self.project_root),
            "webuiDir": str(self.webui_dir),
            "runsDir": str(self.runs_dir),
            "curvesDir": str(self.curves_dir),
            "materialDir": str(self.material_dir),
            "examplesDir": str(self.examples_dir),
            "measuredDir": str(self.measured_dir),
        }


class WebAppHandler(BaseHTTPRequestHandler):
    """静态文件 + JSON API。``ctx`` 由 :func:`serve` 注入。"""

    ctx: AppContext
    server_version = "ufdemo-webapp"
    protocol_version = "HTTP/1.1"

    # -- 基础工具 -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # 默认日志把每个静态请求都打到 stderr，噪音太大；只记录摘要
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        # ``allow_nan=False``：JSON 没有 NaN/Inf，写出去前端会解析失败。
        # 这里再过一遍 ``_json_safe`` 作为**最后一道网** —— 契约层若有哪条
        # 分支漏了归一化（例如把 raw 错误体塞进 success payload），
        # 也绝不能让整个响应丢失、把连接断掉。
        body = json.dumps(
            _json_safe(payload), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        payload: dict[str, Any] = {"code": code, "message": message}
        payload.update(
            {k: v for k, v in extra.items() if k not in _RESERVED_ERROR_KEYS}
        )
        # 三个常用键始终存在（可为 None），前端无需做存在性判断
        for key in ("field_path", "requirement", "suggestion"):
            payload.setdefault(key, None)
        self._send_json(_json_safe(payload), status=status)

    def _send_exception(self, err: BaseException) -> None:
        """**唯一**异常出口：任何异常都变成可解析 JSON，绝不静默断连。"""
        code, message, status, extra = error_response(err)
        if status >= 500:
            traceback.print_exc()
        self._send_error_json(code, message, status=status, **extra)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise HttpError(
                "REQUEST_TOO_LARGE",
                f"请求体 {length} 字节超过上限 {MAX_BODY_BYTES}",
                status=413,
                field_path="body",
                requirement=f"<= {MAX_BODY_BYTES} 字节",
            )
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise HttpError(
                "BAD_REQUEST_BODY",
                f"请求体不是合法 JSON：{err}",
                status=400,
                field_path="body",
                requirement="UTF-8 编码的 JSON 对象",
            ) from err
        if not isinstance(data, dict):
            raise HttpError(
                "BAD_REQUEST_BODY",
                "请求体必须是 JSON 对象",
                status=400,
                field_path="body",
                requirement="JSON 对象",
            )
        return data

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path.startswith("/api/"):
                self._handle_api_get(path)
            else:
                self._serve_static(path)
        except Exception as err:  # noqa: BLE001 - 兜底，避免服务因单个请求崩掉
            # 统一出口：ContractError / HttpError / UFDemoError / 未预期异常
            # 一律转成 JSON 错误体。**不要再分多个 except 分支**——那正是
            # F01 的成因（某一分支漏过滤 code/message 就整条路径失效）。
            self._send_exception(err)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            self._handle_api_post(path)
        except Exception as err:  # noqa: BLE001
            self._send_exception(err)

    # -- API ----------------------------------------------------------------

    def _handle_api_get(self, path: str) -> None:
        ctx = self.ctx
        if path == "/api/health":
            self._send_json({"ok": True, **ctx.to_dict()})
        elif path == "/api/materials":
            self._send_json(W.materials_payload(ctx.material_dir))
        elif path == "/api/examples":
            self._send_json(W.examples_payload(ctx.examples_dir))
        elif path.startswith("/api/examples/"):
            name = path[len("/api/examples/"):].strip("/")
            if not name:
                self._send_error_json("BAD_REQUEST", "缺少模板名", status=400)
                return
            self._send_json(W.example_payload(ctx.examples_dir, name))
        elif path == "/api/curves":
            self._send_json(W.curves_payload(ctx.curves_dir))
        elif path == "/api/datasets":
            # U05：实测数据集与权限。判定与界面同源（datasets.evaluate）。
            self._send_json(W.datasets_payload(ctx.measured_dir))
        elif path == "/api/cases":
            # U08：文献算例回放（实测条件 + 实测结果，不经过模型）。
            self._send_json(W.cases_payload(ctx.measured_dir))
        elif path == "/api/diamond-evaluator":
            # U06：过程响应评估器摘要（支持范围 + 三档指标 + 门槛判定）。
            self._send_json(W.diamond_evaluator_payload(ctx.measured_dir))
        elif path == "/api/shared-background":
            self._send_json(W.shared_background_payload())
        elif path == "/api/pulse-energy":
            q = urllib.parse.urlparse(self.path).query
            f = float((urllib.parse.parse_qs(q).get("f") or ["0"])[0] or 0)
            self._send_json(W.pulse_energy_payload(f))
        elif path == "/api/experiment-tables":
            self._send_json(W.experiment_tables_payload(self.ctx.experiment_dir))
        elif path == "/api/baselines":
            self._send_json(W.baselines_payload(self.ctx.baselines_dir))
        elif path == "/api/upstream":
            self._send_json(W.upstream_payload(self.ctx.upstream_dir))
        elif path == "/api/references":
            self._send_json(W.references_payload(ctx.examples_dir))
        elif path == "/api/runs":
            self._send_json(W.runs_payload(ctx.runs_dir))
        elif path.startswith("/api/runs/"):
            run_id = path[len("/api/runs/"):].strip("/")
            if not run_id:
                self._send_error_json("BAD_REQUEST", "缺少运行标识", status=400)
                return
            self._send_json(W.read_run_payload(ctx.runs_dir, run_id))
        elif path == "/api/layers":
            # 图层标签与不可用口径与 Streamlit 侧同源，避免两处各写一份
            from . import ui_service as U

            self._send_json(
                {
                    "layers": {k: {"label": v} for k, v in U.LAYER_LABELS.items()},
                    "unavailable": dict(U.UNAVAILABLE_LAYERS),
                    "forbiddenTerms": list(U.FORBIDDEN_TERMS),
                }
            )
        else:
            self._send_error_json("NOT_FOUND", f"未知端点：{path}", status=404)

    def _handle_api_post(self, path: str) -> None:
        ctx = self.ctx
        if path == "/api/solve":
            body = self._read_body()
            params = body.get("params")
            if not isinstance(params, dict):
                raise HttpError(
                    "BAD_REQUEST_BODY",
                    "缺少 params 对象",
                    status=400,
                    field_path="params",
                    requirement="完整配置字典（与 examples/*.json 同构）",
                )
            with _SOLVE_LOCK:
                payload = W.solve_payload(
                    params,
                    out_base=ctx.runs_dir,
                    project_root=ctx.project_root,
                    label=str(body.get("label") or "web_run"),
                    curves_dir=ctx.curves_dir,   # U07：实测曲线驱动的求解
                )
            self._send_json(payload)
        elif path == "/api/preview":
            body = self._read_body()
            params = body.get("params")
            if not isinstance(params, dict):
                raise HttpError("BAD_REQUEST_BODY", "缺少 params 对象", status=400, field_path="params")
            self._send_json(W.preview_payload(params, project_root=ctx.project_root))
        elif path == "/api/evaluator-predict":
            # U06：工艺三输入 → 过程响应预测（宽/深/Ra）。
            # **不触发求解、不产生形貌**：这是另一条独立通道，不是 /api/solve。
            body = self._read_body()
            try:
                inputs = {
                    "power_W": float(body["power_W"]),
                    "scan_speed_m_s": float(body["scan_speed_m_s"]),
                    "passes": float(body["passes"]),
                }
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise HttpError(
                    "BAD_REQUEST_BODY",
                    "需要 power_W / scan_speed_m_s / passes 三个数值",
                    status=400,
                    field_path="power_W|scan_speed_m_s|passes",
                    requirement="三个有限数值",
                ) from exc
            import math as _m
            if not all(_m.isfinite(v) for v in inputs.values()):
                raise HttpError(
                    "BAD_REQUEST_BODY", "三个输入都必须是有限数值", status=400,
                    field_path="power_W|scan_speed_m_s|passes",
                    requirement="有限实数",
                )
            from . import ui_service as _U

            ev = _U.build_diamond_evaluator(ctx.measured_dir)
            if ev is None:
                self._send_json({"schema": "ufdemo.web.evaluator_predict/1",
                                 "available": False,
                                 "reason": "缺少 data/measured/diamond_rsm_measured.csv"})
                return
            try:
                pred = ev.predict(inputs, strict=True)
            except UFDemoError as err:
                # 越界**显式拒绝**（不外推）—— 用结构化错误告诉前端为什么
                self._send_json({"schema": "ufdemo.web.evaluator_predict/1",
                                 "available": True, "ok": False,
                                 "error": err.to_dict(),
                                 "hint": "工况超出采样范围时不外推；请改用箱内工况。"})
                return
            self._send_json({"schema": "ufdemo.web.evaluator_predict/1",
                             "available": True, "ok": True, **pred.to_dict()})
        elif path == "/api/lookup":
            body = self._read_body()
            curve = body.get("curve")
            xs = body.get("xs")
            if not curve or not isinstance(xs, list):
                raise HttpError(
                    "BAD_REQUEST_BODY",
                    "缺少 curve 或 xs",
                    status=400,
                    field_path="curve|xs",
                    requirement="curve 为曲线卡名，xs 为数值数组",
                )
            try:
                values = [float(v) for v in xs]
            except (TypeError, ValueError, OverflowError) as exc:
                raise HttpError(
                    "BAD_REQUEST_BODY", "xs 必须是有限数值数组", status=400,
                    field_path="xs", requirement="每个元素可转换为有限浮点数",
                ) from exc
            import math
            if not all(math.isfinite(v) for v in values):
                # 非有限查询点属于查表语义错误：保持契约层的 200/ok=false
                # 形状，让前端可以展示 NUMERIC_NONFINITE，而不是把它误报成
                # HTTP 协议失败。
                self._send_json(W.lookup_payload(ctx.curves_dir, str(curve), values,
                    method=str(body.get("method") or "linear"),
                    allow_out_of_range=bool(body.get("allowOutOfRange", False))))
                return
            self._send_json(W.lookup_payload(ctx.curves_dir, str(curve), values,
                method=str(body.get("method") or "linear"),
                allow_out_of_range=bool(body.get("allowOutOfRange", False))))
        elif path == "/api/curve-grid":
            body = self._read_body()
            curve = body.get("curve")
            if not curve:
                raise HttpError("BAD_REQUEST_BODY", "缺少 curve", status=400, field_path="curve")
            raw_n = body.get("n", 200)
            try:
                n = int(raw_n)
            except (TypeError, ValueError, OverflowError) as exc:
                raise HttpError("BAD_REQUEST_BODY", "n 必须是整数", status=400, field_path="n") from exc
            if isinstance(raw_n, bool) or (isinstance(raw_n, float) and raw_n != n):
                raise HttpError("BAD_REQUEST_BODY", "n 必须是整数", status=400, field_path="n")
            self._send_json(W.curve_grid_payload(ctx.curves_dir, str(curve), n=n,
                method=str(body.get("method") or "linear")))
        elif path == "/api/guard-depth-export":
            body = self._read_body()
            self._send_json(
                W.guard_depth_export_payload(
                    str(body.get("unitSystem") or "SI"), str(body.get("filename") or "depth.csv")
                )
            )
        elif path == "/api/reference":
            body = self._read_body()
            case = body.get("case")
            if not case:
                raise HttpError("BAD_REQUEST_BODY", "缺少 case", status=400, field_path="case")
            self._send_json(
                W.reference_payload(
                    ctx.examples_dir, str(case), project_root=ctx.project_root
                )
            )
        elif path == "/api/calibrate":
            body = self._read_body()
            from .webcontract import calibrate_payload as _cal

            self._send_json(_cal(body, project_root=self.ctx.project_root))
        elif path == "/api/plan":
            body = self._read_body()
            from .webcontract import plan_payload as _plan

            self._send_json(_plan(body, project_root=self.ctx.project_root))
        elif path == "/api/upstream-compare":
            body = self._read_body()
            from .webcontract import upstream_compare_payload as _cmp

            self._send_json(_cmp(body, project_root=self.ctx.project_root))
        elif path == "/api/material-entries":
            # 七材料能力入口（批次 H）：逐条实跑探针，可能较慢；单独端点便于按需请求
            self._send_json(W.material_entries_payload(ctx.material_dir))
        else:
            self._send_error_json("NOT_FOUND", f"未知端点：{path}", status=404)

    # -- 静态文件 -----------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        root = self.ctx.webui_dir
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            root_resolved = root.resolve()
        except OSError:
            root_resolved = root
        # 防目录穿越：目标必须落在 webui 目录内
        if root_resolved not in target.parents and target != root_resolved:
            self._send_error_json("FORBIDDEN", "路径越界", status=403)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            # SPA 回退：未知的非 API 路径回到首页
            fallback = root / "index.html"
            if fallback.exists() and "." not in rel.split("/")[-1]:
                target = fallback
            else:
                self._send_error_json("NOT_FOUND", f"静态资源不存在：{rel}", status=404)
                return
        ctype, _ = mimetypes.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def build_context(
    *,
    project_root: Path | None = None,
    runs_dir: str | Path | None = None,
    webui_dir: Path | None = None,
    measured_dir: str | Path | None = None,
) -> AppContext:
    """构造服务上下文。``runs_dir`` 缺省跟随 ``UFDEMO_RUNS_DIR``（与界面同源）。

    ``webui_dir`` / ``examples_dir`` 缺省走 :mod:`ufdemo` 的**资源解析**
    （``default_webui_dir()`` / ``default_examples_dir()``），而不是
    ``project_root() / "webui"`` —— 后者在 wheel 安装后指向不存在的目录，
    会让界面启动即 ``SystemExit``。
    """
    from . import (
        default_curves_dir,
        default_examples_dir,
        default_material_dir,
        default_measured_dir,
        default_runs_dir,
        default_webui_dir,
        project_root as _pr,
    )

    root = Path(project_root) if project_root is not None else _pr()
    webui = Path(webui_dir) if webui_dir is not None else default_webui_dir()
    runs = Path(runs_dir) if runs_dir is not None else default_runs_dir()
    return AppContext(
        project_root=root,
        webui_dir=webui,
        runs_dir=runs,
        curves_dir=default_curves_dir(),
        material_dir=default_material_dir(),
        examples_dir=default_examples_dir(),
        measured_dir=default_measured_dir() if measured_dir is None else Path(measured_dir),
    )


def make_server(ctx: AppContext, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundWebAppHandler", (WebAppHandler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    runs_dir: str | Path | None = None,
    webui_dir: Path | None = None,
    measured_dir: str | Path | None = None,
    quiet: bool = False,
) -> None:
    ctx = build_context(runs_dir=runs_dir, webui_dir=webui_dir, measured_dir=measured_dir)
    if not ctx.webui_dir.exists():
        raise SystemExit(
            f"前端目录不存在：{ctx.webui_dir}\n"
            "请确认 ultrafast-demo/webui/ 存在（本仓库自带）。"
        )
    httpd = make_server(ctx, host, port)
    if not quiet:
        print(f"ufdemo webapp {__version__}")
        print(f"  前端目录 : {ctx.webui_dir}")
        print(f"  运行目录 : {ctx.runs_dir}")
        print(f"  曲线目录 : {ctx.curves_dir}")
        print(f"  打开地址 : http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ufdemo 本地 Web 界面（静态前端 + 真实求解 API）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--runs-dir", default=None, help="运行输出根目录（默认跟随 UFDEMO_RUNS_DIR）")
    ap.add_argument("--webui-dir", default=None, help="前端静态目录（默认 <项目>/webui）")
    args = ap.parse_args(argv)
    serve(
        host=args.host,
        port=args.port,
        runs_dir=args.runs_dir,
        webui_dir=Path(args.webui_dir) if args.webui_dir else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
