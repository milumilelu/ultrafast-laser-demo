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
    ) -> None:
        self.project_root = project_root
        self.webui_dir = webui_dir
        self.runs_dir = runs_dir
        self.curves_dir = curves_dir
        self.material_dir = material_dir
        self.examples_dir = examples_dir

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "projectRoot": str(self.project_root),
            "webuiDir": str(self.webui_dir),
            "runsDir": str(self.runs_dir),
            "curvesDir": str(self.curves_dir),
            "materialDir": str(self.material_dir),
            "examplesDir": str(self.examples_dir),
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
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        self._send_json(
            {
                "code": code,
                "message": message,
                "field_path": extra.get("field_path"),
                "requirement": extra.get("requirement"),
                "suggestion": extra.get("suggestion"),
            },
            status=status,
        )

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
        except (ContractError, HttpError) as err:
            # 注意：to_dict() 已含 code/message，展开会与位置参数**重复传参**
            # （TypeError: got multiple values for argument 'code'），
            # 结果是一个响应都发不出去、客户端只看到连接被关。
            self._send_error_json(
                err.code, err.message, status=err.status,
                **{k: v for k, v in err.to_dict().items() if k not in ("code", "message")},
            )
        except UFDemoError as err:
            self._send_error_json(err.code, err.message, status=400, **err.to_dict())
        except Exception as err:  # noqa: BLE001 - 兜底，避免服务因单个请求崩掉
            traceback.print_exc()
            self._send_error_json("INTERNAL_ERROR", f"{type(err).__name__}: {err}", status=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            self._handle_api_post(path)
        except (ContractError, HttpError) as err:
            # 注意：to_dict() 已含 code/message，展开会与位置参数**重复传参**
            # （TypeError: got multiple values for argument 'code'），
            # 结果是一个响应都发不出去、客户端只看到连接被关。
            self._send_error_json(
                err.code, err.message, status=err.status,
                **{k: v for k, v in err.to_dict().items() if k not in ("code", "message")},
            )
        except UFDemoError as err:
            self._send_error_json(err.code, err.message, status=400, **err.to_dict())
        except Exception as err:  # noqa: BLE001
            traceback.print_exc()
            self._send_error_json("INTERNAL_ERROR", f"{type(err).__name__}: {err}", status=500)

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
                )
            self._send_json(payload)
        elif path == "/api/preview":
            body = self._read_body()
            params = body.get("params")
            if not isinstance(params, dict):
                raise HttpError("BAD_REQUEST_BODY", "缺少 params 对象", status=400, field_path="params")
            self._send_json(W.preview_payload(params))
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
            self._send_json(
                W.lookup_payload(
                    ctx.curves_dir,
                    str(curve),
                    [float(v) for v in xs],
                    method=str(body.get("method") or "linear"),
                    allow_out_of_range=bool(body.get("allowOutOfRange", False)),
                )
            )
        elif path == "/api/curve-grid":
            body = self._read_body()
            curve = body.get("curve")
            if not curve:
                raise HttpError("BAD_REQUEST_BODY", "缺少 curve", status=400, field_path="curve")
            self._send_json(
                W.curve_grid_payload(
                    ctx.curves_dir,
                    str(curve),
                    n=int(body.get("n") or 200),
                    method=str(body.get("method") or "linear"),
                )
            )
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
) -> AppContext:
    """构造服务上下文。``runs_dir`` 缺省跟随 ``UFDEMO_RUNS_DIR``（与界面同源）。"""
    from . import default_curves_dir, default_material_dir, default_runs_dir, project_root as _pr

    root = Path(project_root) if project_root is not None else _pr()
    webui = Path(webui_dir) if webui_dir is not None else (root / "webui")
    runs = Path(runs_dir) if runs_dir is not None else default_runs_dir()
    return AppContext(
        project_root=root,
        webui_dir=webui,
        runs_dir=runs,
        curves_dir=default_curves_dir(),
        material_dir=default_material_dir(),
        examples_dir=root / "examples",
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
    quiet: bool = False,
) -> None:
    ctx = build_context(runs_dir=runs_dir, webui_dir=webui_dir)
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
