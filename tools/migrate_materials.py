"""F01/F02 → 执行卡迁移器（执行细则 4.4 节，批次 A / T02）。

流程：

1. 读取 F02 与 F01 的来源记录，计算实际哈希并与任务书指纹比较；不一致写入迁移报告。
2. 为每个参考分支分配稳定 ID（YSZ 静态/加工分支、金刚石不同脉宽、SiC 不同阈值观测量
   分别保存）。
3. 输出 ``docs/reports/material_migration.csv``：旧字段路径、新字段路径、值、单位转换、
   来源、保留/转换/待确认状态和原因。
4. 原始来源快照与执行卡分别保存；缺失 δ 保留 ``null``，能力计算禁用深度，
   不补近似材料值。
5. 每张卡输出准入报告，至少验证四种拒绝情况。
6. 迁移后原文件哈希不变。

用法::

    python tools/migrate_materials.py            # 迁移并写报告
    python tools/migrate_materials.py --check    # 只校验哈希，不写文件

本工具是开发期工具，不是运行期依赖；``openpyxl`` 仅在本脚本内使用。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from ufdemo.config import RunConfig, validate_run  # noqa: E402
from ufdemo.materials import MaterialSpec, load_material_card  # noqa: E402

F01 = PROJECT / "seven_materials_parameter_register.xlsx"
F02 = PROJECT / "material_card_templates.json"

# 任务书第 16.3 节的输入版本指纹
EXPECTED = {
    "F01 seven_materials_parameter_register.xlsx": "f0eb36ba926f48e3c2f180d4b8bf1142fcd63d378726746d1ba243d8a8726f9f",
    "F02 material_card_templates.json": "9cb12bf43e15e918dc4decf34f9c927500400e675442469282f71dc6587c171a",
}
MISSING_INPUTS = [
    ("F03", "paper5.0.tex", "f14265daddb413f0ab43900725883d0ecaa0d79788428daeb2a85b28e63fb6a9"),
    ("F04", "ttm_carrier_drilling_q4_axisymmetric.py", "58794b1cdca8a34ef48ea531b7ae28af5ddb13f8fddddcb932b9952a0770185a"),
]

MAT_OUT = ROOT / "data" / "materials"
REF_OUT = ROOT / "data" / "references"
REPORTS = ROOT / "docs" / "reports"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 执行卡定义（稳定 ID；每个参考分支单独一张卡）
# ---------------------------------------------------------------------------

CARDS: list[dict[str, Any]] = [
    {
        "id": "zirconia_ysz_static_aps8ysz",
        "branch_id": "ysz_static_single_pulse",
        "card_version": "1.0.0",
        "identity": {"family": "氧化锆", "grade": "APS 8 wt% YSZ（静态单脉冲试验分支）",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "homogeneous",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only"],
        "response": {
            "kind": "reference_N_threshold",
            "output_semantics": "threshold_only",
            "fluence_basis": "incident_peak_fluence",
            "depth_direction": "not_applicable",
            "threshold_J_m2": 14830.0,
            "threshold_kind": "single_pulse_static",
            "delta_m": None,
            "incubation": {"kind": "power_law_S_of_f", "S_intercept": 0.969,
                           "S_slope_per_kHz": -0.0029, "validity": "f in 10-150 kHz（拟合区间）",
                           "note": "S=0.969-0.0029*f_kHz；频率单位必须 kHz，不得无界外推。"}
        },
        "history_definition": {"mode": "power_law_S_of_f", "exposure_count_meaning": "reference_only",
                               "history_activation_policy": "未标定扫描条件，默认关闭"},
        "source_equation": "Fth(N) = Fth1 * N^(S-1)，S(f)=0.969-0.0029*f_kHz",
        "source_figure_or_table": "F01 P01/P02/P03（S01）",
        "validity_domain": {"scope": "unknown", "laser": {"wavelength_nm": 1030, "pulse_duration_fs": 208},
                            "note": "静态脉冲试验，低于 200 kHz 分支；适用材料形态未确认。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "缺 δ，且工件身份未由用户确认；仅作阈值参考。"},
        "enabled_by_default": True,
        "threshold_candidates": [
            {"branch_id": "ysz_static_Fth1", "observable_name": "single_pulse_threshold",
             "wavelength_nm": 1030, "pulse_duration_fs": 208, "threshold_J_m2": 14830.0, "source": "S01"}
        ],
        "limitations": [
            "静态 Fth1 分支与加工有效分支必须分开保存，不得随意拼接。",
            "δ 缺失：不得用阈值反推绝对深度。",
        ],
        "provenance": {"source_ids": ["S01"], "from": "F01 P01-P03；F02 zirconia_ysz_reference 的静态分支",
                       "migration_note": "原 F02 未区分静态/加工分支，本次拆分并分别编号。"},
    },
    {
        "id": "zirconia_ysz_machining_effective_n3",
        "branch_id": "ysz_machining_effective",
        "card_version": "1.0.0",
        "identity": {"family": "氧化锆", "grade": "YSZ 论文相应加工卡（1030 nm/208 fs，33.3 kHz）",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "homogeneous",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["reference_case", "threshold_only", "synthetic_demo"],
        "response": {
            "kind": "log_fixed_effective",
            "output_semantics": "event_depth_increment",
            "fluence_basis": "incident_peak_fluence",
            "depth_direction": "surface_normal",
            "threshold_J_m2": 12890.0,
            "threshold_kind": "machining_effective",
            "delta_m": 3.9e-07,
            "incubation_enabled": False,
            "extra_incubation_prohibited_without_refit": True,
        },
        "history_definition": {"mode": "effective_count_embedded", "exposure_count_meaning": "not_per_event",
                              "effective_count": 3,
                              "note": "有效核已含多脉冲效应；禁止叠加未经重新标定的额外孵化。"},
        "reference_protocol": {
            "protocol_id": "ysz_crown_machining_effective_n3",
            "required_laser": {
                "wavelength_m": {"value": 1.03e-06, "rel_tol": 0.01},
                "pulse_duration_s": {"value": 208e-15, "rel_tol": 0.05},
                "repetition_rate_Hz": {"value": 33300.0, "rel_tol": 0.01},
                "spot_radius_m": {"value": 1.6e-05, "rel_tol": 0.05},
            },
            "required_history": {"effective_count": 3, "definition": "area_equivalent_ysz_eq9"},
            "protocol_note": "原文式（9）N_eff=(pi/4)*(2*w0*f)/v；w0=16 um, f=33.3 kHz, N_eff=3 → v=278.9734276388 mm/s。",
        },
        "source_equation": "a = delta*[ln(F/Fth_eff)]_+ ；N_eff,YSZ = (pi/4)*(2*w0*f)/v",
        "source_figure_or_table": "F01 P04/P05/P06/P07（S01 图14 配套有效核）",
        "validity_domain": {"scope": "reference_protocol_only",
                            "protocol_id": "ysz_crown_machining_effective_n3",
                            "peak_fluence_J_m2": 501000.0,
                            "note": "仅在上述已确认的加工条件与历史协议下允许定量；超出拒绝执行。"},
        "applicability": {"physical_material_prediction_allowed": True,
                          "condition": "仅限 reference_protocol 声明的条件匹配工况"},
        "enabled_by_default": True,
        "limitations": [
            "有效加工阈值不是 Fth1；不得与静态分支拼接。",
            "任意单脉冲条件、任意氧化锆牌号均不适用。",
        ],
        "provenance": {"source_ids": ["S01"], "from": "F01 P04-P07；F02 zirconia_ysz_reference 的加工分支",
                       "migration_note": "保留有效脉冲数与阈值类型；补充 required_laser 后才允许参考执行。"},
    },
    {
        "id": "alsic_sicp_aa2024_1030nm",
        "branch_id": "alsic_sicp_aa2024",
        "card_version": "1.0.0",
        "identity": {"family": "铝基碳化硅", "grade": "SiCp/AA2024 候选（配方待确认）",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "particle_composite",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["synthetic_demo"],
        "response": {"kind": "missing", "output_semantics": "threshold_only", "fluence_basis": None,
                     "depth_direction": None, "threshold_J_m2": None, "delta_m": None},
        "history_definition": {"mode": "none", "exposure_count_meaning": "not_applicable"},
        "validity_domain": {"scope": "unknown", "laser": {"wavelength_nm": 1030, "pulse_duration_fs": 800},
                            "note": "本轮可访问正文范围不足，未核得完整阈值与去除尺度。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "两相去除核缺失；只开放合成结构与资料浏览。"},
        "enabled_by_default": True,
        "microstructure": {"actual_volume_fraction": None, "actual_particle_size_m": None,
                           "synthetic_geometry_example": {"SiC_volume_fraction": 0.45,
                                                          "mean_particle_diameter_m": 3e-05,
                                                          "source": "S09",
                                                          "from_nanosecond_study_geometry_only": True}},
        "phases": [{"name": "Al_matrix", "threshold_J_m2": None, "delta_m": None},
                   {"name": "SiC_particle", "threshold_J_m2": None, "delta_m": None}],
        "limitations": [
            "不平均两相阈值；不拿纳秒阈值替代飞秒值。",
            "不得把块体 4H-SiC 卡当成颗粒相标定。",
        ],
        "provenance": {"source_ids": ["S07", "S08", "S09"], "from": "F01 P10-P15；F02 alsic_phase_template"},
    },
    {
        "id": "cfrp_t700_yb01_800nm",
        "branch_id": "cfrp_t700_yb01",
        "card_version": "1.0.0",
        "identity": {"family": "CFRP", "grade": "T700 / YB01", "material_identity_confirmed_by_user": False},
        "structure_type": "laminated_fiber_composite",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only", "synthetic_demo"],
        "response": {
            "kind": "reference_N_threshold",
            "output_semantics": "threshold_only",
            "fluence_basis": "incident_peak_fluence",
            "depth_direction": "not_applicable",
            "threshold_J_m2": 8400.0,
            "threshold_kind": "multipulse_fitted_Fth1",
            "delta_m": None,
            "incubation": {"kind": "power_law_incubation", "S": 0.8855,
                           "formula": "Fth(N)=Fth1*N^(S-1)"},
        },
        "history_definition": {"mode": "power_law_incubation", "exposure_count_meaning": "local_effective_count",
                               "history_activation_policy": "由扫描多脉冲试验反演；未标定条件默认关闭"},
        "reference_protocol": {
            "protocol_id": "cfrp_t700_yb01_threshold_only",
            "required_laser": {
                "wavelength_m": {"value": 8.0e-07, "rel_tol": 0.02},
                "pulse_duration_s": {"value": 90e-15, "rel_tol": 0.05},
                "repetition_rate_Hz": {"value": 1000.0, "rel_tol": 0.02},
                "spot_radius_m": {"value": 3.0e-06, "rel_tol": 0.10},
            },
            "required_history": {"effective_count": 1, "definition": "Fth1 反演基准，仅为占位定义"},
            "protocol_note": "整体等效参数，不是树脂与纤维分别的参数。",
        },
        "source_equation": "Fth(N) = Fth1 * N^(S-1)",
        "source_figure_or_table": "F01 P16-P18（S03）",
        "validity_domain": {"scope": "reference_protocol_only", "protocol_id": "cfrp_t700_yb01_threshold_only",
                            "note": "仅协议限定阈值展示；绝对深度不开放。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "δ 缺失，只能做协议限定阈值展示与合成铺层。"},
        "enabled_by_default": True,
        "phases": [{"name": "resin", "threshold_J_m2": None, "delta_m": None},
                   {"name": "fiber", "threshold_J_m2": None, "delta_m": None}],
        "limitations": ["整体阈值不是两相阈值。", "δ 缺失时不输出绝对深度。", "不声称预测分层或热影响区。"],
        "provenance": {"source_ids": ["S03", "S18", "S21"], "from": "F01 P16-P21；F02 cfrp_t700_yb01_800nm"},
    },
    {
        "id": "inconel718_1030nm_n10",
        "branch_id": "inconel718_fixed_point_n10",
        "card_version": "1.0.0",
        "identity": {"family": "高温合金", "grade": "Inconel 718", "material_identity_confirmed_by_user": False},
        "structure_type": "homogeneous",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only", "synthetic_demo"],
        "response": {
            "kind": "reference_N_threshold",
            "output_semantics": "threshold_only",
            "fluence_basis": "incident_peak_fluence",
            "depth_direction": "not_applicable",
            "threshold_J_m2": 1400.0,
            "threshold_kind": "fixed_point_N10",
            "delta_m": None,
        },
        "history_definition": {"mode": "fixed_point_count", "exposure_count_meaning": "fixed_N_not_per_event",
                               "effective_count": 10,
                               "note": "阈值的 N 定义固定为 10，禁止当作单脉冲阈值。"},
        "reference_protocol": {
            "protocol_id": "inconel718_n10_threshold_reference",
            "required_laser": {
                "wavelength_m": {"value": 1.03e-06, "rel_tol": 0.02},
                "pulse_duration_s": {"value": 267e-15, "rel_tol": 0.10},
            },
            "required_history": {"effective_count": 10, "definition": "fixed_point_N10"},
            "protocol_note": "同文加工系列约 267 fs；λ=1030 nm 为绿光/近红外分支。",
        },
        "source_equation": "固定点 N=10 阈值（图4）",
        "source_figure_or_table": "F01 P22-P24（S04 图4）",
        "validity_domain": {"scope": "reference_protocol_only", "protocol_id": "inconel718_n10_threshold_reference",
                            "note": "只做 N=10 阈值参考；深度与体积数据未数字化。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "δ 缺失，且文章去除效率曲线本轮未数字化反演。"},
        "enabled_by_default": True,
        "threshold_candidates": [
            {"branch_id": "inconel718_n10_green", "observable_name": "threshold_at_N10_green",
             "wavelength_nm": 1030, "pulse_duration_fs": 267, "threshold_J_m2": 1400.0, "source": "S04"},
            {"branch_id": "inconel718_n10_uv", "observable_name": "threshold_at_N10_uv",
             "wavelength_nm": 515, "pulse_duration_fs": 267, "threshold_J_m2": 1000.0, "source": "S04"},
        ],
        "optional_thermal_backup": {"rho_kg_m3": 8190.0, "Cp_J_kgK": 435.0, "k_W_mK": 11.4,
                                    "note": "备用数据，不表示已具备温度或 HAZ 预测能力。"},
        "limitations": ["不能把 N=10 阈值当成 Fth1。", "不是任意镍基/钴基高温合金卡。"],
        "provenance": {"source_ids": ["S04", "S12", "S13"], "from": "F01 P22-P30；F02 inconel718_1030nm_n10"},
    },
    {
        "id": "glass_ceramic_unbranded_1030nm",
        "branch_id": "glass_ceramic_unbranded",
        "card_version": "1.0.0",
        "identity": {"family": "微晶玻璃", "grade": None, "material_identity_confirmed_by_user": False},
        "structure_type": "homogeneous_effective",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["synthetic_demo"],
        "response": {"kind": "missing", "output_semantics": "threshold_only", "fluence_basis": None,
                     "depth_direction": None, "threshold_J_m2": None, "delta_m": None},
        "history_definition": {"mode": "none", "exposure_count_meaning": "not_applicable"},
        "validity_domain": {"scope": "unknown", "note": "实际牌号未定：必须先区分 ZERODUR、锂二硅酸盐等。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "完整响应缺失；只开放合成均质表面与导入接口。"},
        "enabled_by_default": True,
        "separate_reference_candidates": [
            {"branch_id": "glass_ceramic_dental_1030nm", "grade": "dental glass-ceramic, brand unconfirmed",
             "wavelength_nm": 1030, "pulse_duration_fs_range": [550, 600], "source": "S10"},
            {"branch_id": "zerodur_thermal_only", "grade": "SCHOTT ZERODUR", "thermal_only": True,
             "rho_kg_m3": 2530, "Cp_J_kgK": 800, "k_W_mK": 1.46, "source": "S11"},
        ],
        "limitations": ["禁止将石英、硼硅玻璃或镀膜损伤阈值代入。", "透明材料内部改性不在表面高度场范围。"],
        "provenance": {"source_ids": ["S10", "S11"], "from": "F01 P31-P37；F02 glass_ceramic_template"},
    },
    {
        "id": "sic_4h_cface_1035nm_multishot",
        "branch_id": "sic4h_cface_multishot",
        "card_version": "1.0.0",
        "identity": {"family": "SiC", "grade": "N 掺杂 4H-SiC C 面", "material_identity_confirmed_by_user": False},
        "structure_type": "homogeneous_dual_threshold",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only"],
        "response": {
            "kind": "reference_N_threshold",
            "output_semantics": "threshold_only",
            "fluence_basis": "incident_peak_fluence",
            "depth_direction": "not_applicable",
            "threshold_J_m2": 23500.0,
            "threshold_kind": "single_pulse_modification",
            "delta_m": None,
        },
        "multi_response": {
            "kind": "exponential_saturation_effective_N",
            "output_semantics": "mean_depth_per_effective_pulse",
            "Fth1_fitted_J_m2": 23500.0,
            "Fth_infinity_J_m2": 7000.0,
            "k_inc_per_pulse": 0.0199,
            "delta_eff_mean_m": 2.24e-08,
            "formula": "Fth(N)=F_inf+(F1-F_inf)*exp(-k*(N-1)); AR=delta_eff*max(log(F0/Fth(N)),0)",
            "note": "delta_eff 为多脉冲微槽拟合的有效深度尺度均值，不是每脉冲固定深度。",
        },
        "history_definition": {"mode": "effective_N_saturation", "exposure_count_meaning": "effective_count_not_event_index",
                               "history_activation_policy": "未标定扫描条件，默认关闭；逐事件扩展须另标 engineering_extension"},
        "reference_protocol": {
            "protocol_id": "sic4h_cface_multishot_reference_N",
            "required_laser": {
                "wavelength_m": {"value": 1.035e-06, "rel_tol": 0.01},
                "pulse_duration_s": {"value": 300e-15, "rel_tol": 0.05},
                "repetition_rate_Hz": {"value": 200000.0, "rel_tol": 0.02},
            },
            "required_history": {"effective_count": 720, "definition": "original_paper_effective_N"},
            "protocol_note": "首个参考评估器允许直接输入原文的有效 N；完整工况映射须另核原图/原表。",
        },
        "source_equation": "式(5) N_eff=K*(2*w0*f)/v；式(6) Fth(N)=F_inf+(F1-F_inf)*exp(-k*(N-1))；式(7) AR=delta_eff*ln(F0/Fth(N))",
        "source_figure_or_table": "F01 P38-P43（S02 图6、式5–式7）",
        "validity_domain": {"scope": "reference_protocol_only", "protocol_id": "sic4h_cface_multishot_reference_N",
                            "note": "平均率与有效 N 语义限定；逐事件递推未验证。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "平均去除率不是逐事件增量；δ 为拟合均值，禁止当作每脉冲固定深度。"},
        "enabled_by_default": True,
        "threshold_candidates": [
            {"branch_id": "sic4h_n1_modification", "observable_name": "single_pulse_modification_threshold",
             "wavelength_nm": 1035, "pulse_duration_fs": 300, "threshold_J_m2": 23500.0, "source": "S02"},
            {"branch_id": "sic4h_n1_structural_change", "observable_name": "single_pulse_structural_transformation_threshold",
             "wavelength_nm": 1035, "pulse_duration_fs": 300, "threshold_J_m2": 49700.0, "source": "S02"},
        ],
        "limitations": [
            "改性标记不等于已去除体积。",
            "不能将高阈值直接替换低阈值拟合后还称原文复现。",
            "不覆盖 RB-SiC、烧结 SiC 或复合材料颗粒相。",
        ],
        "provenance": {"source_ids": ["S02"], "from": "F01 P38-P43；F02 sic4h_cface_1035nm_multishot",
                       "migration_note": "两个阈值观测量分别编号；平均率语义单独登记。"},
    },
    {
        "id": "diamond_scd_cvd_1030nm_400fs",
        "branch_id": "diamond_scd_400fs",
        "card_version": "1.0.0",
        "identity": {"family": "金刚石", "grade": "单晶 CVD（400 fs 分支）",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "net_removal_with_optional_modification_mask",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only"],
        "response": {"kind": "reference_N_threshold", "output_semantics": "threshold_only",
                     "fluence_basis": "incident_peak_fluence", "depth_direction": "not_applicable",
                     "threshold_J_m2": 82000.0, "threshold_kind": "single_pulse", "delta_m": None},
        "history_definition": {"mode": "none", "exposure_count_meaning": "not_applicable"},
        "reference_protocol": {
            "protocol_id": "diamond_scd_1030nm_400fs_threshold",
            "required_laser": {"wavelength_m": {"value": 1.03e-06, "rel_tol": 0.01},
                               "pulse_duration_s": {"value": 400e-15, "rel_tol": 0.05}},
            "required_history": {"effective_count": 1, "definition": "single_pulse"},
            "protocol_note": "不得与 700 fs 分支混合取平均。",
        },
        "source_equation": "单脉冲阈值（2019）",
        "source_figure_or_table": "F01 P47（S05）",
        "validity_domain": {"scope": "reference_protocol_only", "protocol_id": "diamond_scd_1030nm_400fs_threshold",
                            "note": "仅条件对应阈值展示。"},
        "applicability": {"physical_material_prediction_allowed": False, "reason": "δ 缺失，无配套去除曲线。"},
        "enabled_by_default": True,
        "limitations": ["改性标记不等于石墨化定量模拟。", "PCD、NPD 与含结合剂金刚石另建卡。"],
        "provenance": {"source_ids": ["S05"], "from": "F01 P47；F02 diamond_scd_cvd_1030nm"},
    },
    {
        "id": "diamond_scd_cvd_1030nm_700fs",
        "branch_id": "diamond_scd_700fs",
        "card_version": "1.0.0",
        "identity": {"family": "金刚石", "grade": "单晶 CVD（700 fs 分支）",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "net_removal_with_optional_modification_mask",
        "evidence_status": "literature_reported",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only"],
        "response": {"kind": "reference_N_threshold", "output_semantics": "threshold_only",
                     "fluence_basis": "incident_peak_fluence", "depth_direction": "not_applicable",
                     "threshold_J_m2": 129000.0, "threshold_kind": "single_pulse", "delta_m": None},
        "history_definition": {"mode": "none", "exposure_count_meaning": "not_applicable"},
        "reference_protocol": {
            "protocol_id": "diamond_scd_1030nm_700fs_threshold",
            "required_laser": {"wavelength_m": {"value": 1.03e-06, "rel_tol": 0.01},
                               "pulse_duration_s": {"value": 700e-15, "rel_tol": 0.05}},
            "required_history": {"effective_count": 1, "definition": "single_pulse"},
            "protocol_note": "与 400 fs 分支分开保存，禁止混用。",
        },
        "source_equation": "单脉冲阈值（2019）",
        "source_figure_or_table": "F01 P48（S05）",
        "validity_domain": {"scope": "reference_protocol_only", "protocol_id": "diamond_scd_1030nm_700fs_threshold",
                            "note": "仅条件对应阈值展示。"},
        "applicability": {"physical_material_prediction_allowed": False, "reason": "δ 缺失，无配套去除曲线。"},
        "enabled_by_default": True,
        "limitations": ["不得与 400 fs 分支混合取平均。"],
        "provenance": {"source_ids": ["S05"], "from": "F01 P48；F02 diamond_scd_cvd_1030nm"},
    },
    {
        "id": "diamond_scd_cvd_1030nm_pulsewidth_unconfirmed",
        "branch_id": "diamond_scd_recent_candidate",
        "card_version": "1.0.0",
        "identity": {"family": "金刚石", "grade": "单晶 CVD（脉宽未核实候选）",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "net_removal_with_optional_modification_mask",
        "evidence_status": "unverified",
        "source_type": "research_card_migration",
        "allowed_run_modes": ["threshold_only"],
        "enabled_by_default": False,
        "blocked_reason": "2026 年 2.32 J/cm² 候选值的脉宽未核实（仅知 300 kHz），不得设为默认",
        "response": {"kind": "reference_N_threshold", "output_semantics": "threshold_only",
                     "fluence_basis": "unknown", "depth_direction": "not_applicable",
                     "threshold_J_m2": 23200.0, "threshold_kind": "single_pulse_candidate", "delta_m": None},
        "history_definition": {"mode": "none", "exposure_count_meaning": "not_applicable"},
        "reference_protocol": {
            "protocol_id": "diamond_scd_1030nm_candidate",
            "required_laser": {"wavelength_m": {"value": 1.03e-06, "rel_tol": 0.01},
                               "pulse_duration_s": {"value": None, "rel_tol": 0.05},
                               "repetition_rate_Hz": {"value": 300000.0, "rel_tol": 0.05}},
            "required_history": {"effective_count": 1, "definition": "single_pulse"},
            "protocol_note": "脉宽未核实：完整条件核实前不启用。",
        },
        "source_equation": "单脉冲阈值候选（2026）",
        "source_figure_or_table": "F01 P49（S06）",
        "validity_domain": {"scope": "unknown", "note": "仅保留在研究记录；脉宽与能流基准未核实。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "候选值：关键条件未核实。"},
        "limitations": ["仅作研究记录，不进入默认预测。"],
        "provenance": {"source_ids": ["S06"], "from": "F01 P49；F02 diamond threshold_candidates[2]（enabled_by_default=false）"},
    },
]


# ---------------------------------------------------------------------------
# 迁移映射（F01 记录 → 执行卡字段）
# ---------------------------------------------------------------------------

# P编号 -> (目标卡ID, 目标字段, 状态, 原因)
MIGRATION_MAP: dict[str, tuple[str, str, str, str]] = {
    "P01": ("zirconia_ysz_static_aps8ysz", "response.threshold_J_m2", "kept", "单脉冲静态阈值保留原定义"),
    "P02": ("zirconia_ysz_static_aps8ysz", "response.incubation.S_intercept", "kept", "S(f) 截距，频率单位 kHz"),
    "P03": ("zirconia_ysz_static_aps8ysz", "response.incubation.S_slope_per_kHz", "converted", "1/kHz → 保留 kHz 定义并显式标注，避免无界外推"),
    "P04": ("zirconia_ysz_machining_effective_n3", "response.threshold_J_m2", "kept", "有效加工阈值，与静态分支分离"),
    "P05": ("zirconia_ysz_machining_effective_n3", "response.delta_m", "kept", "0.39 μm；与 Fth_eff 配套"),
    "P06": ("zirconia_ysz_machining_effective_n3", "reference_protocol.required_laser.spot_radius_m", "converted", "光斑直径 32 μm → 1/e² 半径 16 μm（明确高斯束约定）"),
    "P07": ("zirconia_ysz_machining_effective_n3", "validity_domain.peak_fluence_J_m2", "kept", "加工例峰值能流，仅作条件一致性检查"),
    "P08": ("zirconia_ysz_static_aps8ysz", "optional_thermal_backup.rho_kg_m3", "to_confirm", "3YZ 制造商值，非 APS 8YSZ 涂层；只作备用数据"),
    "P09": ("zirconia_ysz_static_aps8ysz", "optional_thermal_backup.k_W_mK", "to_confirm", "同上，未做温度或 HAZ 预测"),
    "P10": ("alsic_sicp_aa2024_1030nm", "validity_domain.laser.wavelength_nm", "kept", "仅波长条件；响应参数缺失"),
    "P11": ("alsic_sicp_aa2024_1030nm", "response.threshold_J_m2", "to_confirm", "本轮未核得完整数值，保持 null"),
    "P12": ("alsic_sicp_aa2024_1030nm", "microstructure.synthetic_geometry_example.SiC_volume_fraction", "converted", "45 vol% 仅作另一纳秒研究的结构参考，不用于飞秒标定"),
    "P13": ("alsic_sicp_aa2024_1030nm", "microstructure.synthetic_geometry_example.mean_particle_diameter_m", "converted", "30 μm 同上，仅几何参考"),
    "P14": ("alsic_sicp_aa2024_1030nm", "optional_thermal_backup.k_W_mK", "to_confirm", "不同产品（ZL101A+65vol%）；不得与 AA2024 混配"),
    "P15": ("alsic_sicp_aa2024_1030nm", "optional_thermal_backup.Cp_J_kgK", "to_confirm", "供应商理论参考值"),
    "P16": ("cfrp_t700_yb01_800nm", "response.threshold_J_m2", "kept", "整体等效 Fth1；不是树脂/纤维分别阈值"),
    "P17": ("cfrp_t700_yb01_800nm", "response.incubation.S", "kept", "幂律指数 S"),
    "P18": ("cfrp_t700_yb01_800nm", "reference_protocol.required_laser.spot_radius_m", "kept", "论文名义光斑半径 3 μm"),
    "P19": ("cfrp_t700_yb01_800nm", "response.delta_m", "to_confirm", "δ 缺失，保持 null，能力计算禁用深度"),
    "P20": ("cfrp_t700_yb01_800nm", "provenance.notes.selective_texture_F_low_J_m2", "to_confirm", "12 J/cm² 是选择性加工工况，不是阈值"),
    "P21": ("cfrp_t700_yb01_800nm", "provenance.notes.selective_texture_F_high_J_m2", "to_confirm", "13 J/cm² 同上；纤维体积分数定义未确认故不录入"),
    "P22": ("inconel718_1030nm_n10", "response.threshold_J_m2", "kept", "N=10 固定点阈值，threshold_kind 显式登记"),
    "P23": ("inconel718_1030nm_n10", "threshold_candidates[0]", "kept", "绿光分支 N=10"),
    "P24": ("inconel718_1030nm_n10", "threshold_candidates[1]", "kept", "UV 分支 N=10，与绿光分别编号"),
    "P25": ("inconel718_1030nm_n10", "response.delta_m", "to_confirm", "δ/S/Fth1 未数字化反演，保持 null"),
    "P26": ("inconel718_1030nm_n10", "optional_thermal_backup.rho_kg_m3", "kept", "备用数据"),
    "P27": ("inconel718_1030nm_n10", "optional_thermal_backup.k_W_mK", "kept", "备用数据"),
    "P28": ("inconel718_1030nm_n10", "optional_thermal_backup.Cp_J_kgK", "kept", "备用数据"),
    "P29": ("inconel718_1030nm_n10", "optional_thermal_backup.solidus_C", "converted", "°C 保留原始温标并标注，不参与求解"),
    "P30": ("inconel718_1030nm_n10", "optional_thermal_backup.liquidus_C", "converted", "同上"),
    "P31": ("glass_ceramic_unbranded_1030nm", "separate_reference_candidates[0].pulse_duration_fs_range[0]", "kept", "牙科玻璃陶瓷脉宽下限；牌号未确认"),
    "P32": ("glass_ceramic_unbranded_1030nm", "separate_reference_candidates[0].pulse_duration_fs_range[1]", "kept", "同上"),
    "P33": ("glass_ceramic_unbranded_1030nm", "response.threshold_J_m2", "to_confirm", "实际牌号未定，保持 null"),
    "P34": ("glass_ceramic_unbranded_1030nm", "separate_reference_candidates[1].rho_kg_m3", "kept", "ZERODUR 热物性，仅 thermal_only"),
    "P35": ("glass_ceramic_unbranded_1030nm", "separate_reference_candidates[1].Cp_J_kgK", "kept", "同上"),
    "P36": ("glass_ceramic_unbranded_1030nm", "separate_reference_candidates[1].k_W_mK", "kept", "同上"),
    "P37": ("glass_ceramic_unbranded_1030nm", "separate_reference_candidates[1].n_d", "to_confirm", "可见光 d 线折射率，不能直接用作 1030 nm 折射率"),
    "P38": ("sic_4h_cface_1035nm_multishot", "threshold_candidates[0]", "kept", "单脉冲材料改性阈值，单独编号"),
    "P39": ("sic_4h_cface_1035nm_multishot", "threshold_candidates[1]", "kept", "单脉冲结构转变阈值，单独编号，不与改性阈值合并"),
    "P40": ("sic_4h_cface_1035nm_multishot", "multi_response.Fth_infinity_J_m2", "kept", "饱和阈值 F∞"),
    "P41": ("sic_4h_cface_1035nm_multishot", "multi_response.k_inc_per_pulse", "kept", "饱和速率常数"),
    "P42": ("sic_4h_cface_1035nm_multishot", "multi_response.delta_eff_mean_m", "converted", "22.4 nm 作为多脉冲拟合有效尺度均值，语义写为 mean_depth_per_effective_pulse"),
    "P43": ("sic_4h_cface_1035nm_multishot", "reference_protocol.required_laser.repetition_rate_Hz", "kept", "200 kHz 工况"),
    "P44": ("sic_4h_cface_1035nm_multishot", "provenance.notes.user_code_defaults.rho_kg_m3", "to_confirm", "上传代码默认值，未独立校准，不进入执行参数"),
    "P45": ("sic_4h_cface_1035nm_multishot", "provenance.notes.user_code_defaults.Cp_J_kgK", "to_confirm", "同上"),
    "P46": ("sic_4h_cface_1035nm_multishot", "provenance.notes.user_code_defaults.k_W_mK", "to_confirm", "同上"),
    "P47": ("diamond_scd_cvd_1030nm_400fs", "response.threshold_J_m2", "kept", "400 fs 分支单独建卡"),
    "P48": ("diamond_scd_cvd_1030nm_700fs", "response.threshold_J_m2", "kept", "700 fs 分支单独建卡，禁止与 400 fs 混合"),
    "P49": ("diamond_scd_cvd_1030nm_pulsewidth_unconfirmed", "response.threshold_J_m2", "to_confirm", "候选值：脉宽未核实，enabled_by_default=false"),
    "P50": ("diamond_scd_cvd_1030nm_400fs", "response.delta_m", "to_confirm", "δ 缺失，保持 null"),
    "P51": ("diamond_scd_cvd_1030nm_400fs", "optional_thermal_backup.k_grade_low_W_mK", "to_confirm", "供货等级范围，不是统一值"),
    "P52": ("diamond_scd_cvd_1030nm_400fs", "optional_thermal_backup.k_grade_high_W_mK", "to_confirm", "同上"),
    "P53": ("diamond_scd_cvd_1030nm_400fs", "optional_thermal_backup.rho_kg_m3", "to_confirm", "型号、温度与方向仍需核验"),
}


def read_f01_records() -> list[dict[str, Any]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("需要 openpyxl 才能读取 F01：pip install openpyxl") from exc
    wb = openpyxl.load_workbook(F01, data_only=True)
    ws = wb["参数证据"]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[3]
    out = []
    for r in rows[4:]:
        if r[0] is None:
            continue
        out.append(dict(zip(header, r)))
    return out


def write_card(card: dict[str, Any]) -> Path:
    MAT_OUT.mkdir(parents=True, exist_ok=True)
    p = MAT_OUT / f"{card['id']}.json"
    p.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def write_thermal_symbol_cards(cards: list[dict[str, Any]]) -> None:
    """把 P29/P30 的温标字段补进 Inconel 卡（保留原 °C 定义）。"""
    for card in cards:
        if card["id"] == "inconel718_1030nm_n10":
            card["optional_thermal_backup"]["solidus_C"] = 1260.0
            card["optional_thermal_backup"]["liquidus_C"] = 1336.0


def probe_config(card_id: str, **overrides: Any) -> RunConfig:
    """构造用于准入探针的最小配置（含 G01 参数的通用网格与光束）。"""
    import math

    base: dict[str, Any] = {
        "schema_version": "1.0",
        "run_mode": "reference_case",
        "unit_system": "SI",
        "material_id": card_id,
        "seed": 0,
        "grid": {"nx": 41, "ny": 41, "dx_m": 1e-6, "dy_m": 1e-6, "center_x_m": 0.0, "center_y_m": 0.0},
        "laser": {
            "wavelength_m": 1.03e-06,
            "pulse_duration_s": 208e-15,
            "pulse_energy_J": math.exp(2.0) * 1.0e4 * math.pi * (1.0e-5) ** 2 / 2.0,
            "repetition_rate_Hz": 33300.0,
            "spot_radius_m": 1.0e-05,
            "focus_xyz_m": [0.0, 0.0, 0.0],
            "direction_unit": [0.0, 0.0, 1.0],
        },
        "path": {"t0_s": 0.0,
                 "segments": [{"segment_id": 0, "pass_id": 0, "start_s": 0.0,
                               "end_s": 1.0 / 33300.0, "start_xyz_m": [0, 0, 0], "end_xyz_m": [0, 0, 0],
                               "laser_on": True}]},
        "solver": {"mode": "reference", "geometry_feedback": "fixed_geometry", "history_enabled": False,
                   "tail_epsilon": 1e-08, "memory_budget_bytes": 2147483648},
        "output": {"snapshot_policy": "none"},
    }
    for k, v in overrides.items():
        base[k] = v
    return RunConfig.from_dict(base)


def admission_report(cards_by_id: dict[str, MaterialSpec]) -> list[dict[str, Any]]:
    """四种拒绝情况 + 卡片准入摘要（细则 4.4 第 5 项）。"""
    rows: list[dict[str, Any]] = []

    def run_probe(name: str, card_id: str, expect: str, **overrides: Any) -> None:
        cfg = probe_config(card_id, **overrides)
        rep = validate_run(cfg, cards_by_id[card_id])
        codes = sorted({e["code"] for e in rep.errors})
        ok = (not rep.ok) if expect == "reject" else rep.ok
        rows.append({
            "probe": name,
            "material_id": card_id,
            "expected": expect,
            "observed_ok": rep.ok,
            "observed_codes": ";".join(codes) or "(无)",
            "verdict": "PASS" if ok else "FAIL",
            "detail": "；".join(e["message"] for e in rep.errors) or "；".join(rep.notes),
        })

    # 1. 缺 δ 的真实材料请求深度 → 必须拒绝
    run_probe("missing_delta_requests_depth", "cfrp_t700_yb01_800nm", "reject")
    # 2. N=10 当作 N=1 → 必须拒绝
    run_probe("n10_treated_as_n1", "inconel718_1030nm_n10", "reject",
              reference_conditions={"protocol_id": "inconel718_n10_threshold_reference",
                                    "fluence_basis": "incident_peak_fluence", "effective_count": 1})
    # 3. 协议未知 → 必须拒绝
    run_probe("unknown_protocol", "inconel718_1030nm_n10", "reject",
              reference_conditions={"protocol_id": "some_other_protocol",
                                    "fluence_basis": "incident_peak_fluence", "effective_count": 10})
    # 4. 能流基准不匹配 → 必须拒绝
    run_probe("fluence_basis_mismatch", "inconel718_1030nm_n10", "reject",
              reference_conditions={"protocol_id": "inconel718_n10_threshold_reference",
                                    "fluence_basis": "absorbed_fluence", "effective_count": 10})
    # 5. 脉宽未核实的候选值 → 必须拒绝
    run_probe("unconfirmed_pulsewidth_candidate", "diamond_scd_cvd_1030nm_pulsewidth_unconfirmed", "reject",
              reference_conditions={"protocol_id": "diamond_scd_1030nm_candidate",
                                    "fluence_basis": "incident_peak_fluence", "effective_count": 1})
    # 6. YSZ 有效加工核条件匹配 → 允许
    run_probe("ysz_protocol_matched", "zirconia_ysz_machining_effective_n3", "accept",
              laser={"wavelength_m": 1.03e-06, "pulse_duration_s": 208e-15,
                     "pulse_energy_J": 2.01464053689406e-04, "repetition_rate_Hz": 33300.0,
                     "spot_radius_m": 1.6e-05, "focus_xyz_m": [0.0, 0.0, 0.0],
                     "direction_unit": [0.0, 0.0, 1.0]},
              reference_conditions={"protocol_id": "ysz_crown_machining_effective_n3",
                                    "fluence_basis": "incident_peak_fluence", "effective_count": 3})
    # 7. YSZ 条件不匹配（光斑半径不符）→ 必须拒绝
    run_probe("ysz_protocol_mismatch", "zirconia_ysz_machining_effective_n3", "reject",
              reference_conditions={"protocol_id": "ysz_crown_machining_effective_n3",
                                    "fluence_basis": "incident_peak_fluence", "effective_count": 3})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验输入哈希，不写文件")
    args = ap.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    REF_OUT.mkdir(parents=True, exist_ok=True)

    before = {str(F01): sha256_file(F01), str(F02): sha256_file(F02)}
    records = read_f01_records()

    hash_rows = []
    for name, expected in EXPECTED.items():
        actual = before[str(PROJECT / name.split(" ", 1)[1])]
        hash_rows.append({"input": name, "expected_sha256": expected, "actual_sha256": actual,
                          "match": actual == expected})
    for fid, fname, expected in MISSING_INPUTS:
        p = PROJECT / fname
        found = p.exists()
        hash_rows.append({"input": f"{fid} {fname}", "expected_sha256": expected,
                          "actual_sha256": sha256_file(p) if found else None,
                          "match": (sha256_file(p) == expected) if found else False,
                          "note": None if found else "文件未找到：记录缺失，不阻塞人工解析主线"})

    print("== 输入哈希核对 ==")
    for r in hash_rows:
        print(f"  {r['input']}: {'MATCH' if r['match'] else 'MISMATCH/缺失'} {r.get('actual_sha256')}")

    if args.check:
        return 0

    # --- 写执行卡 ---------------------------------------------------------
    write_thermal_symbol_cards(CARDS)
    # 合成演示卡（非物理，前缀 _ 以便目录加载时区分）
    synthetic = {
        "schema_version": "1.0",
        "id": "synthetic_demo_isotropic",
        "card_version": "1.0.0",
        "identity": {"family": "synthetic_demo", "grade": "isotropic_normalized_surface",
                     "material_identity_confirmed_by_user": False},
        "structure_type": "homogeneous",
        "evidence_status": "unverified",
        "source_type": "synthetic_definition",
        "fixture_only": True,
        "allowed_run_modes": ["synthetic_demo", "threshold_only"],
        "response": {
            "kind": "log_fixed",
            "output_semantics": "event_depth_increment",
            "fluence_basis": "normalized_incident_peak",
            "depth_direction": "surface_normal",
            "threshold_over_F_ref": 1.0,
            "delta_over_delta_ref": 1.0,
        },
        "history_definition": {"mode": "none", "exposure_count_meaning": "not_applicable"},
        "source_equation": "a/F_ref 序列：a = delta_ref*[ln(F/F_ref)]_+（无量纲）",
        "source_figure_or_table": "执行细则 4.1/4.2 合成模式规定",
        "validity_domain": {"scope": "analytic_all",
                            "note": "无量纲合成定义；不赋予真实材料预测意义。"},
        "applicability": {"physical_material_prediction_allowed": False,
                          "reason": "合成定义：只验证流程、几何与界面。"},
        "enabled_by_default": True,
        "limitations": ["禁止导出物理 μm 深度。", "禁止据此比较七种材料真实加工速度。"],
        "provenance": {"source_ids": [], "from": "细则 2.2 沙盒规则"},
    }
    paths = [write_card(c) for c in CARDS]
    sp = MAT_OUT / "_synthetic_demo_isotropic.json"
    sp.write_text(json.dumps(synthetic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths.append(sp)
    print(f"== 执行卡：{len(paths)} 张写入 {MAT_OUT.relative_to(ROOT)} ==")

    # --- 原始来源快照（与执行卡分开保存）----------------------------------
    snap = REF_OUT / "material_card_templates_source_snapshot.json"
    shutil.copyfile(F02, snap)
    (REF_OUT / "seven_materials_parameter_register_source_snapshot.sha256").write_text(
        f"{before[str(F01)]}  seven_materials_parameter_register.xlsx\n", encoding="utf-8")
    (REF_OUT / "INPUT_VERSION_FINGERPRINTS.json").write_text(json.dumps({
        "source_files": hash_rows,
        "note": "原始来源快照按字节复制；执行卡另行生成，两者分别保存。",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"== 来源快照写入 {REF_OUT.relative_to(ROOT)} ==")

    # --- 迁移 CSV ---------------------------------------------------------
    mig_rows = []
    for rec in records:
        pid = rec["编号"]
        tgt = MIGRATION_MAP.get(pid)
        if tgt is None:
            mig_rows.append({"record_id": pid, "material": rec["材料"], "grade": rec["牌号/相"],
                             "parameter": rec["参数"], "old_field_path": f"参数证据[{pid}]",
                             "new_field_path": None, "value_literature": rec["文献值"],
                             "value_SI": rec["SI值"], "unit_from": rec["原单位"], "unit_to": rec["SI单位"],
                             "conversion": "未映射", "source_id": rec["来源ID"],
                             "status": "to_confirm", "reason": "未映射记录：需人工确认去向"})
            continue
        card_id, field, status, reason = tgt
        mig_rows.append({
            "record_id": pid, "material": rec["材料"], "grade": rec["牌号/相"], "parameter": rec["参数"],
            "old_field_path": f"参数证据[{pid}]", "new_field_path": f"{card_id}.{field}",
            "value_literature": rec["文献值"], "value_SI": rec["SI值"],
            "unit_from": rec["原单位"], "unit_to": rec["SI单位"],
            "conversion": f"×{rec['SI倍率']}" if rec["SI倍率"] not in (None, 1, "1") else "无",
            "source_id": rec["来源ID"], "status": status, "reason": reason,
        })
    # F02 → 执行卡的字段级迁移
    f02_map = [
        ("materials[].response", "response.{kind,output_semantics,fluence_basis,depth_direction,threshold_J_m2,delta_m}", "converted", "研究模板的 response 判别类型化并补充语义字段"),
        ("materials[].aggregate_response", "response.incubation / history_definition", "converted", "整体响应拆为孵化定义与历史定义"),
        ("materials[].multipulse_response", "multi_response", "converted", "多脉冲拟合语义单独登记为 mean_depth_per_effective_pulse"),
        ("materials[].readiness", "capabilities（推导）", "converted", "不再以 readiness 文本判断能力，改由完整条件推导"),
        ("materials[].reference_geometry.spot_diameter_m", "reference_protocol.required_laser.spot_radius_m", "converted", "直径 → 1/e² 半径，换算因子 1/2"),
        ("materials[].threshold_candidates[]", "threshold_candidates[]（含 branch_id）", "converted", "每个阈值候选分配稳定分支 ID"),
        ("normalized_demo", "reference_scales（配置层）", "converted", "归一化参数移到运行配置，材料卡不再携带 F_ref/delta_ref"),
        ("sources[]", "provenance.source_ids + data/references 快照", "kept", "来源条目保留，原文件另存字节快照"),
    ]
    for old, new, status, reason in f02_map:
        mig_rows.append({"record_id": "F02", "material": "(研究模板字段)", "grade": "", "parameter": old,
                         "old_field_path": old, "new_field_path": new, "value_literature": "", "value_SI": "",
                         "unit_from": "", "unit_to": "", "conversion": "", "source_id": "",
                         "status": status, "reason": reason})

    mig_csv = REPORTS / "material_migration.csv"
    with open(mig_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(mig_rows[0].keys()))
        w.writeheader()
        w.writerows(mig_rows)
    print(f"== 迁移记录：{len(mig_rows)} 行 → {mig_csv.relative_to(ROOT)} ==")

    # --- 准入报告 ---------------------------------------------------------
    cards_by_id = {c["id"]: load_material_card(p) for c, p in zip(CARDS, paths[:-1])}
    adm = admission_report(cards_by_id)
    adm_csv = REPORTS / "material_admission.csv"
    with open(adm_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(adm[0].keys()))
        w.writeheader()
        w.writerows(adm)
    print("== 准入探针 ==")
    for r in adm:
        print(f"  [{r['verdict']}] {r['probe']}（期望 {r['expected']}）→ {r['observed_codes']}")

    # --- 哈希复核：原文件必须不变 -----------------------------------------
    after = {str(F01): sha256_file(F01), str(F02): sha256_file(F02)}
    unchanged = before == after
    print(f"== 原文件哈希不变：{unchanged} ==")
    if not unchanged:
        print("  !! 原文件被修改，违反细则 4.4 第 6 项", file=sys.stderr)
        return 1

    (REPORTS / "input_hash_check.csv").write_text(
        "input,expected_sha256,actual_sha256,match,note\n" + "\n".join(
            f"{r['input']},{r['expected_sha256']},{r.get('actual_sha256')},{r['match']},{r.get('note','')}"
            for r in hash_rows) + "\n",
        encoding="utf-8-sig",
    )
    return 0 if all(r["verdict"] == "PASS" for r in adm) else 1


if __name__ == "__main__":
    raise SystemExit(main())
