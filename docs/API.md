# LlamaMonitor HTTP API（Phase 14）

> 监听地址：**127.0.0.1:8765**（仅回环；端口被占用时应用启动失败，不静默换端口）。
> 无认证（回环隔离 + 本地单用户假设）；FastAPI `docs_url/redoc_url/openapi_url`
> 全部关闭（AUDIT-SEC-005，无文档信息泄露面）。
> 所有时间戳为 Unix 秒；日期为本机系统日期 `YYYY-MM-DD`。
> 错误形状统一为 `{"error": {"code": "...", "message": "..."}}`。

## 访问控制（AUDIT-SEC-001/002）

所有**修改类/敏感**端点通过 `_require_loopback` 依赖强制 `scope["client"][0]`
为回环地址（127.0.0.1 / ::1）；非回环来源返回 **403**。
只读展示端点不设限（无敏感数据）。下表"回环"列标 **L** 的端点受此保护。

## 端点一览

### 状态 / 概览（只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Dashboard（static/index.html） |
| GET | `/api/status` | 服务器在线状态 + 最近样本 + gauge 快照 |
| GET | `/api/version` | 应用版本 / schema 版本 / 平台信息 |
| GET | `/api/health` | application / database 健康 + journal_mode + 最近有效样本间隔 |
| GET | `/api/summary` | Today & Total token 汇总（含 MTP 派生字段） |
| GET | `/api/daily` | 最近 N 天 daily_usage + 每日质量字段（coverage/gap） |
| GET | `/api/live` | 最近 N 小时 live_samples 序列 |
| GET | `/api/mtp` | 当前 MTP 接受率明细 |
| GET | `/api/mtp/daily` | 历史每日 MTP（含 per-position） |
| GET | `/api/runtime` | 运行时信息（启动时间、轮询间隔、保留期等） |
| GET | `/api/data/info` | 数据目录大小 / 各表行数 |

### 配置（回环 L = 修改类受 403 保护）

| 方法 | 路径 | L | 说明 |
|---|---|---|---|
| GET | `/api/config` | ✓ | 当前配置（回环保护，AUDIT-SEC-001） |
| PUT | `/api/config` | ✓ | 更新配置（写 config.json） |
| GET | `/api/config/defaults` | | 默认配置形状 |
| POST | `/api/config/test-connection` | ✓ | 探测 llama-server /metrics 连通性 |

### GPU（只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/gpu/status` | GPU 采集可用性 / 最近快照 |
| GET | `/api/gpu/live` | 最近 N 小时 gpu_samples |
| GET | `/api/gpu/daily` | 每日 GPU 聚合（利用率/显存/温度/功耗/能耗） |

### 数据管理

| 方法 | 路径 | L | 说明 |
|---|---|---|---|
| GET | `/api/data/quality` | | 数据质量：今日覆盖率/缺口 + 历史缺口 + 最近缺口明细 |
| GET | `/api/data/export/daily.csv` | | daily_usage CSV 导出（公式注入防护，AUDIT-SEC-003） |
| GET | `/api/data/export/gpu_daily.csv` | | gpu_daily CSV 导出（同上） |
| POST | `/api/data/backup` | ✓ | 触发手动备份 |
| GET | `/api/data/backups` | | 备份列表（kind: automatic/manual/pre_migration/pre_update/legacy） |
| POST | `/api/data/check-database` | ✓ | PRAGMA quick_check（返回 healthy/corrupt + detail） |
| POST | `/api/data/clear-live` | ✓ | 清空 live_samples（需 `{"confirm": true}`） |
| POST | `/api/data/reset-statistics` | ✓ | 重置 daily 统计（需 `{"confirm": "RESET"}`） |

### 应用集成（回环 L）

| 方法 | 路径 | L | 说明 |
|---|---|---|---|
| GET | `/api/app/integration` | ✓ | 自启动 / 开机项 / 托盘状态（AUDIT-SEC-002） |
| PUT | `/api/app/autostart` | ✓ | 开关自启动（注册表 Run 键） |
| POST | `/api/app/open-folder` | ✓ | 打开数据目录 |
| POST | `/api/app/exit` | ✓ | 触发优雅退出 |

### 更新（全部回环 L）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/update/status` | 更新状态机 + 可用版本 + 错误 |
| POST | `/api/update/check` | 手动检查（签名验证 + manifest 解析） |
| POST | `/api/update/download` | 下载 installer（流式 SHA-256） |
| POST | `/api/update/install` | 安装（pre-update backup + Popen 前 TOCTOU 复验，AUDIT-DATA-001） |
| POST | `/api/update/cancel` | 取消下载/安装 |

## 性能注意（AUDIT-DB-003）

- `/api/daily`、`/api/data/quality`、CSV 导出：**全部** live 样本查询走
  索引范围 + 预分组（单次取数），不是按天逐次全表扫；
- 前端轮询：Dashboard 每 1s 更新（仅窗口可见时）、daily/gpu/update 各有
  in-flight 去重 + 30s fetch 超时（AUDIT-WEB-002/003）。

## 版本兼容

- API 无版本号（单进程本地应用）；schema 版本见 `/api/version`；
- 更新流程由 Ed25519 签名信任链保护（见 docs/UPDATE_SECURITY.md）。
