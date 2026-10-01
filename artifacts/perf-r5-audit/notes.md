# Round 5 — 推理性能页 审计发现（Reality Audit 笔记）

## 数据源现状（实测 127.0.0.1:9091）
- /props 有 build_info = b10976-987498f45  ✅ 但前端显示 --
  - 根因：build_model_info() 只从 `llama-server --version` 子进程取 build_info（PATH 无该 exe → None），
    从不读 /props 的 build_info 字段。→ §159-§161 bug，必修。
- /slots：speculative=true, speculative.types="none,draft-mtp"（MTP 已启用），
  但 is_processing=false 且仍带 n_prompt_tokens=2801（上一任务残留）→ §127-§144 idle 残留，必修。
  - 注意：spec 说 MTP enabled 但 spec_decode_num_drafts 可能仍为 0（未推理过）→ 三态：
    未启用 / 已启用·暂无样本 / 有数据。
- /v1/models data[0].meta: n_params=27320697856, size=17367629824, ftype=Q4_K - Medium, n_ctx=262144。
  - models[0].details.format=gguf（§158 可与量化组合 GGUF · Q4_K · Medium）。

## live_samples 表（db.py L154-166）现有列
  timestamp, prompt_delta, cached_delta, output_delta, prompt_tps, decode_tps,
  requests_processing, requests_deferred, context_max, mtp_accept_rate,
  kv_cache_usage_ratio, busy_slots
  → 缺 prompt_seconds / predicted_seconds（窗口加权平均 §47 需要）。
  → 低风险迁移 v5→v6：加这两列 REAL（历史行 NULL）。

## 每日 MTP 聚合：daily_usage 已有 draft_tokens/accepted_tokens/draft_sequences +
  mtp_position_daily(date,position,accepted_tokens)。7d/30d/全部 = SUM over range（§109）。
  平均 Draft 长度 = draft_tokens/draft_sequences；平均接受长度 = accepted_tokens/draft_sequences（§87-§89）。

## 顶部活跃 Slot：从 /slots is_processing 统计（§17），不用 n_busy_slots_per_decode。
  n_busy_slots_per_decode → 运行时状态单独展示（§18），改名"平均忙碌 Slot / Decode"。

## 术语冻结
  MTP（Multi-Token Prediction）——首现全称；不写"Multi-Token 预测"。
  验证步数（原"推测验证轮次"）。最大观测序列长度（原"上下文高水位"）。
  新处理 Prompt（原"实际处理 Token"）/ 缓存复用 Prompt / 已生成 Token / 剩余生成预算。
  模型文件大小（原"模型大小"）。并行 Slot。运行时状态（原"服务器运行状态"）。
  模型与运行环境（原"模型与服务"）。Slot 监控（原"当前 Slot"）。
  按 Draft 位置的已接受 Token（原"各 Draft 位置已接受 Token"）。
