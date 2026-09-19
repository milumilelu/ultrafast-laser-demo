"""光机所设备的 **compressor 步数 ↔ 脉冲宽度** 标定表。

⚠️ **设计表里那列"脉宽"不是脉宽，是 compressor 步数。**
（`LHS实验设计120组.xlsx` 里该列列名就是 `compressor`；值为 36140 / 43800 / …）

用前必须转换，否则**工艺参数全错**。

来源：用户 2026-09-18 提供的设备标定截图（OCR 识别，与用户手发的数字一致）。
12 个标定点、单调递增。**两点间线性插值；区间外不外推。**
"""

from __future__ import annotations

#: compressor steps → pulse duration (fs)
COMPRESSOR_TO_FS: dict[int, float] = {
    29400: 223,   36140: 500,    43800: 1000,  58400: 2000,
    72900: 3000,  87300: 4000,   101800: 5000, 116200: 6000,
    130900: 7000, 145300: 8000,  159800: 9000, 173500: 10000,
}

_KEYS = sorted(COMPRESSOR_TO_FS)


def compressor_to_fs(steps: float) -> float:
    """compressor 步数 → 脉冲宽度 (fs)。

    **超出标定范围直接报错，不外推** —— 区间外没有标定数据，
    外推出来的脉宽是编的。
    """
    steps = float(steps)
    if steps < _KEYS[0] or steps > _KEYS[-1]:
        raise ValueError(
            f"compressor={steps:g} 超出标定范围 [{_KEYS[0]}, {_KEYS[-1]}]；"
            f"不要外推 —— 该区间外没有标定数据"
        )
    for a, b in zip(_KEYS, _KEYS[1:]):
        if a <= steps <= b:
            fa, fb = COMPRESSOR_TO_FS[a], COMPRESSOR_TO_FS[b]
            return fa + (fb - fa) * (steps - a) / (b - a)
    return float(COMPRESSOR_TO_FS[int(steps)])


def fs_to_compressor(fs: float) -> float:
    """反向：脉冲宽度 (fs) → compressor 步数（同样不外推）。"""
    ks = sorted(COMPRESSOR_TO_FS.values())
    fs = float(fs)
    if fs < ks[0] or fs > ks[-1]:
        raise ValueError(f"脉宽 {fs:g} fs 超出标定范围 [{ks[0]:g}, {ks[-1]:g}]")
    pairs = sorted((v, k) for k, v in COMPRESSOR_TO_FS.items())
    for (fa, ca), (fb, cb) in zip(pairs, pairs[1:]):
        if fa <= fs <= fb:
            return ca + (cb - ca) * (fs - fa) / (fb - fa)
    return float(pairs[-1][1])


if __name__ == "__main__":
    print("compressor → 脉宽 (fs)")
    for k in _KEYS:
        print(f"  {k:>7} → {COMPRESSOR_TO_FS[k]:>7.0f}")
