# ADR-0007：运行目录布局、events.csv 与禁止覆盖

* 状态：已采纳｜日期：2026-09-10｜批次：C（T08）

## 决定

1. **目录布局**严格按细则 11.1，并**新增 `events.csv`**：

   ```text
   runs/<run_id>/
     config.json            # 原始输入快照（回放用；不是内部换算值）
     material_snapshot.json # 卡片原文 + 卡哈希 + 水印 + 能力表
     metadata.json          # 状态、哈希、代码版本、环境、警告、实际启用功能
     final_surface.npz      # 仅成功时生成
     partial_surface.npz    # 取消/失败时生成
     statistics.csv         # 长表：metric, value, unit
     profiles.csv           # 截面（按 section 分组）
     events.csv             # 事件流：index/time/position/energy/segment/pass
     diagnostics.json       # 事件数、能量账本、去除账本、阈值计数
     snapshots/index.json
     snapshots/snap_*.npz
   ```

   `events.csv` 是批次 B 的交付物之一（“事件 CSV”），用于独立核对统一时钟与
   `v/f` 间距，行数由 `output.events_csv_max_rows` 限额，避免长序列爆盘。

2. **`run_id` 唯一且不覆盖**：`make_run_id` 使用 UTC 时间 + 随机后缀；
   `new_run_dir` 在目录已存在时抛 `CONFIG_INVALID`。CLI 的 `--out` 指向已存在
   目录时同样拒绝（可用 `--force-new-suffix` 自动追加后缀）。

3. **状态机**：`running → completed | cancelled | failed`。
   只有全部必要文件写入成功后 `metadata.json` 才写 `completed`。
   取消/失败时 `metadata.json` 追加 `partial_result_note`，`load_run` 会把
   “上一次运行”的警告带回。

4. **原子写入**：JSON 与 NPZ 先写 `.tmp` 再 `os.replace`。

5. **稳定 JSON**：`sort_keys=True`、`allow_nan=False`、缩进固定；
   `config_hash` 即该表示的 SHA-256，用于复现比对。

6. **NPZ 不使用 pickle**：`np.savez_compressed` 只写数值数组；
   `load_run` 用 `allow_pickle=False`，不会反序列化不可信 pickle。
   字符串数组（如快照的 `unit` 标签）按 NumPy 字符串 dtype 保存。

## 后果

* `config.json` 保存**原始输入**而不是内部换算值，保证“重读 = 重放输入”。
  内部量（含轴顺序、单位上下文、方向向量归一化信息）记在 `metadata.json`。
* `python -m ufdemo inspect <run_dir>` 可独立核对，不依赖界面。
