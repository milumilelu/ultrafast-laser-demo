"""pytest 公共配置。

* 把 ``src`` 加入 ``sys.path``，使干净环境无需先 ``pip install -e .`` 也能跑测试；
* 提供 G01 解析参数与配置覆写工具，保证各测试文件使用同一套常量。
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ufdemo.config import RunConfig, load_config  # noqa: E402
from ufdemo.materials import load_material_card  # noqa: E402
from ufdemo.solver import solve  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "analytic_fixture.json"
EXAMPLES = ROOT / "examples"
DATA_MATERIALS = ROOT / "data" / "materials"

# --- G01 解析常量（执行细则 10 节）------------------------------------------
FTH = 1.0e4                 # J/m^2 = 1 J/cm^2
DELTA = 1.0e-7              # m = 100 nm
W0 = 1.0e-5                 # m = 10 um
F0_PEAK = math.exp(2.0) * FTH          # 73890.5609893065 J/m^2
EP = F0_PEAK * math.pi * W0 * W0 / 2.0  # 11.6067021786817 uJ
PEAK_DEPTH = 2.0 * DELTA                # 200 nm
R_A = W0                                # 10 um
V_ANALYTIC = math.pi * W0 * W0 * DELTA / 4.0 * (math.log(F0_PEAK / FTH) ** 2)  # 31.4159265358979 um^3


@pytest.fixture(scope="session")
def fixture_card():
    return load_material_card(FIXTURE_PATH)


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def make_config(raw: dict, base_dir: Path | None = None) -> RunConfig:
    raw = copy.deepcopy(raw)
    base = base_dir or EXAMPLES
    return RunConfig.from_dict(raw, base_dir=str(base))


def run_example(name: str, **overrides):
    raw = load_example(name)
    for dotted, value in overrides.items():
        target = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            target = target[p]
        target[parts[-1]] = value
    cfg = make_config(raw)
    return cfg, solve(cfg, load_material_card(ROOT / raw["material_card_file"]))
