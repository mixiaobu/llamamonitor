"""
db.py — SQLite 持久化（Phase 2 + Phase 7 + Phase 9 + Phase 11）。

表：state / daily_usage / live_samples / mtp_position_daily（v1）、
gpu_samples / gpu_daily（v2）、monitor_events / data_gaps / backup_history（v3）。

- 数据库路径由调用方从统一配置解析后传入（config.AppConfig.database_path：
  database.path 非空用用户值，空则为 %LOCALAPPDATA%\\LlamaMonitor\\monitor.db）；
- wal=True 时启用 WAL 日志模式（config.database.wal）；**不假设**返回值就是 WAL：
  实际 journal_mode 读回并写日志，设置失败 WARNING 后继续可用模式；
- 所有写操作（state 更新 + daily 累加 + live 插入 + 过期清理）在单个事务中完成，
  任一异常整体回滚，不会出现半写入状态（**写失败绝不更新 baseline**——
  state 没提交，下一轮从旧 baseline 重新算完整 delta）；
- Phase 11 可靠性：
  * PRAGMA busy_timeout=5000（避免短暂锁直接失败；不做无限 retry）；
  * 写事务对 SQLITE_BUSY / "database is locked" 做有限 retry（3 次，50/100/200ms）；
  * quick_check（启动时一次，非完整 integrity_check）-> 数据库健康状态机
    healthy / warning / corrupt / unavailable（corrupt 时禁止修改类写，绝不自动删库）；
  * monitor_events / data_gaps 事件与缺口记录（去重、有限保留）。
- 过期时长来自配置 collector.live_retention_hours（retention_seconds 参数，默认 48h）；
  data_gaps 长期保留（历史数据可信度的一部分，不做短保留清理）；
  monitor_events 保留 EVENT_RETENTION_DAYS（365 天）。
- 读写为同步调用，运行在采集事件循环线程内（每 5 秒一次，开销可忽略）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

__all__ = [
    "Database",
    "local_date",
    "LIVE_SAMPLE_RETENTION_SECONDS",
    "LIVE_SAMPLE_COLUMNS",
    "CURRENT_SCHEMA_VERSION",
    "EVENT_RETENTION_DAYS",
    "DB_HEALTH_HEALTHY",
    "DB_HEALTH_WARNING",
    "DB_HEALTH_CORRUPT",
    "DB_HEALTH_UNAVAILABLE",
    "DB_HEALTH_INCOMPATIBLE",
    "DB_HEALTH_PROTECTIVE",
]

logger = logging.getLogger("llamamonitor.db")

# Phase 11：写锁 / 健康 / 事件保留
BUSY_TIMEOUT_MS = 5000          # PRAGMA busy_timeout（短暂锁等待上限；不做无限 retry）
TX_RETRY_DELAYS = (0.05, 0.10, 0.20)   # SQLITE_BUSY / "database is locked" 有限重试
EVENT_RETENTION_DAYS = 365      # monitor_events 保留天数（data_gaps 永久保留）
EVENT_RETENTION_MAX_ROWS = 100000  # 事件表行数硬上限（双保险，优先按天数）

# 数据库健康状态
DB_HEALTH_HEALTHY = "healthy"
DB_HEALTH_WARNING = "warning"
DB_HEALTH_CORRUPT = "corrupt"
DB_HEALTH_UNAVAILABLE = "unavailable"
# Phase 12：数据库 schema 比本程序**更新**（降级保护）——只读、不迁移、不写入；
# 也用于 pre-migration backup 失败后放弃迁移的保护状态。
DB_HEALTH_INCOMPATIBLE = "incompatible"
# "不健康"集合：修改类写入应停止（protective mode）
DB_HEALTH_PROTECTIVE = (DB_HEALTH_CORRUPT, DB_HEALTH_UNAVAILABLE, DB_HEALTH_INCOMPATIBLE)

# 当前 schema 版本：
# v2 = Phase 9（GPU 表 + runtime 列扩展）；v3 = Phase 11（可靠性/数据质量表）；
# 版本机制见 _connect/_migrate
CURRENT_SCHEMA_VERSION = 4

# live_samples 默认保留时长：48 小时（实际值来自配置 collector.live_retention_hours）
LIVE_SAMPLE_RETENTION_SECONDS = 48 * 3600

# live_samples 的列（不含自增 id），也是 apply_sample 的 live_row 必须提供的键
LIVE_SAMPLE_COLUMNS = (
    "prompt_delta",
    "cached_delta",
    "output_delta",
    "prompt_tps",
    "decode_tps",
    "requests_processing",
    "requests_deferred",
    "context_max",
    "mtp_accept_rate",
    "kv_cache_usage_ratio",   # v2 新增（gauge 0~1；服务器无该指标时 NULL）
    "busy_slots",             # v2 新增（gauge；服务器无该指标时 NULL）
)

# daily_usage 的可累加列（date 为主键）
_DAILY_COLUMNS = (
    "prompt_tokens",
    "cached_tokens",
    "output_tokens",
    "draft_tokens",
    "accepted_tokens",
    "prompt_seconds",
    "predicted_seconds",
    "draft_sequences",        # v2 新增（llamacpp:spec_decode_num_drafts_total 日累计 delta）
)

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS state (
    metric_name TEXT PRIMARY KEY,   -- llama.cpp 指标全名
    value REAL NOT NULL             -- 上一次保存的 Counter 值
);

CREATE TABLE IF NOT EXISTS daily_usage (
    date TEXT PRIMARY KEY,          -- 本机系统日期 YYYY-MM-DD
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    draft_tokens INTEGER NOT NULL DEFAULT 0,
    accepted_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_seconds REAL NOT NULL DEFAULT 0,
    predicted_seconds REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS live_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,     -- Unix 秒（UTC），采集时刻
    prompt_delta INTEGER,           -- 本轮 prompt token delta
    cached_delta INTEGER,           -- 本轮缓存 token delta
    output_delta INTEGER,           -- 本轮输出 token delta
    prompt_tps REAL,                -- prompt_delta / prompt_seconds_delta（分母<=0 为 NULL）
    decode_tps REAL,                -- output_delta / predicted_seconds_delta（分母<=0 为 NULL）
    requests_processing INTEGER,    -- gauge：处理中请求数（无则 NULL）
    requests_deferred INTEGER,      -- gauge：延迟请求数（无则 NULL）
    context_max INTEGER,            -- gauge：可用上下文（无则 NULL）
    mtp_accept_rate REAL            -- accepted/draft*100（draft_delta=0 为 NULL）
);
CREATE INDEX IF NOT EXISTS idx_live_samples_timestamp ON live_samples(timestamp);

CREATE TABLE IF NOT EXISTS mtp_position_daily (
    date TEXT NOT NULL,
    position TEXT NOT NULL,
    accepted_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, position)
);
"""

_GPU_TABLES = """
CREATE TABLE IF NOT EXISTS gpu_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,        -- Unix 秒（采集时刻）
    gpu_uuid TEXT NOT NULL,         -- 稳定身份：nvidia-smi UUID（index 会随驱动/插拔变化，只用于显示）
    gpu_index INTEGER,
    gpu_name TEXT,
    utilization_percent REAL,       -- N/A / Not Supported 一律解析为 NULL
    memory_used_mb REAL,
    memory_total_mb REAL,
    temperature_c REAL,
    power_draw_w REAL,
    fan_percent REAL,
    sm_clock_mhz REAL,
    memory_clock_mhz REAL,
    pcie_generation INTEGER,
    pcie_width INTEGER
);
CREATE INDEX IF NOT EXISTS idx_gpu_samples_timestamp ON gpu_samples(timestamp);
CREATE INDEX IF NOT EXISTS idx_gpu_samples_uuid_ts ON gpu_samples(gpu_uuid, timestamp);

CREATE TABLE IF NOT EXISTS gpu_daily (
    date TEXT NOT NULL,             -- 本机系统日期 YYYY-MM-DD（与 daily_usage 完全同规则）
    gpu_uuid TEXT NOT NULL,
    gpu_name TEXT,
    sample_count INTEGER NOT NULL DEFAULT 0,
    utilization_count INTEGER NOT NULL DEFAULT 0,
    utilization_sum REAL NOT NULL DEFAULT 0,
    utilization_max REAL,
    memory_used_count INTEGER NOT NULL DEFAULT 0,
    memory_used_sum_mb REAL NOT NULL DEFAULT 0,
    memory_used_max_mb REAL,
    temperature_count INTEGER NOT NULL DEFAULT 0,
    temperature_sum REAL NOT NULL DEFAULT 0,
    temperature_max REAL,
    power_count INTEGER NOT NULL DEFAULT 0,
    power_sum_w REAL NOT NULL DEFAULT 0,
    power_max_w REAL,
    energy_wh REAL NOT NULL DEFAULT 0,   -- 相邻采样梯形积分估算（见 gpu_collector）
    PRIMARY KEY (date, gpu_uuid)
);
"""


# Phase 11：可靠性 / 数据质量表（v3）。
# monitor_events：事件日志（不是采样日志）——只记状态转换/异常，行数远小于 live_samples。
# data_gaps：已知的监控缺口（采样断档）；长期保留（是历史数据可信度的一部分，不做 48h 清理）。
# backup_history：备份元数据（文件系统仍是最终 backup 来源；此表损坏不影响找回备份）。
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,       -- Unix 秒（事件发生时刻）
    event_type TEXT NOT NULL,         -- server_online/server_offline/counter_reset/monitor_start/
                                      -- monitor_stop/sleep_gap/invalid_metrics/database_recovery/
                                      -- database_integrity_error/migration/backup_created/data_gap ...
    severity TEXT NOT NULL DEFAULT 'info',  -- info / warning / error
    source TEXT NOT NULL DEFAULT 'collector',  -- collector / gpu / application / database / backup
    details_json TEXT                  -- 结构化详情（JSON 文本）
);
CREATE INDEX IF NOT EXISTS idx_monitor_events_ts ON monitor_events(timestamp);

CREATE TABLE IF NOT EXISTS data_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_timestamp INTEGER NOT NULL,  -- Unix 秒
    end_timestamp INTEGER NOT NULL,    -- Unix 秒（>= start）
    duration_seconds REAL NOT NULL,
    source TEXT NOT NULL,              -- llama / gpu / application
    reason TEXT NOT NULL,              -- server_offline / monitor_restart / system_pause_or_sleep /
                                       -- unknown
    token_recoverable INTEGER NOT NULL DEFAULT 1,      -- gap 期间的 token 能否用 Counter 差值恢复
    possible_token_loss INTEGER NOT NULL DEFAULT 0,    -- 1 = 可能有不可恢复的 token 丢失
    resolved INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_data_gaps_start ON data_gaps(start_timestamp);

CREATE TABLE IF NOT EXISTS backup_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,        -- Unix 秒
    type TEXT NOT NULL,                -- automatic / manual
    path TEXT NOT NULL,
    size INTEGER,
    verified INTEGER NOT NULL DEFAULT 0,   -- 备份后 quick_check 是否通过
    success INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_backup_history_ts ON backup_history(timestamp);
"""

# Phase 13（schema v4）：通用 runtime metadata（key/value）。
# 用途：更新子系统的持久化状态（如 app.update.last_check / app.update.github_etag）。
# **不**用于 metric counter（state 表专属）；updater 永远不 ALTER 既有表（§67）。
_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,          -- runtime metadata key（app.* 命名空间）
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL    -- Unix 秒
);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """幂等 ALTER TABLE ADD COLUMN（migration 崩溃-重试时可能重复执行）。"""
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    v1 -> v2（Phase 9）：
    - live_samples 增 kv_cache_usage_ratio / busy_slots（gauge；历史行 NULL）；
    - daily_usage 增 draft_sequences（历史行 0）；
    - 新建 gpu_samples / gpu_daily。
    全部幂等语句：不 DROP、不 DELETE、不动旧数据。
    """
    _add_column_if_missing(conn, "live_samples", "kv_cache_usage_ratio", "REAL")
    _add_column_if_missing(conn, "live_samples", "busy_slots", "INTEGER")
    _add_column_if_missing(conn, "daily_usage", "draft_sequences", "INTEGER NOT NULL DEFAULT 0")
    conn.executescript(_GPU_TABLES)


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """
    v2 -> v3（Phase 11）：新建 monitor_events / data_gaps / backup_history。
    全部 IF NOT EXISTS 幂等语句：不 DROP、不 DELETE、不动旧数据。
    """
    conn.executescript(_SCHEMA_V3)
    # migration 事件本身（事件表刚建好；details 记录来源版本）
    try:
        conn.execute(
            "INSERT INTO monitor_events(timestamp, event_type, severity, source, details_json) "
            "VALUES(?, 'migration', 'info', 'database', ?)",
            (int(time.time()), _json_dumps({"from": 2, "to": 3})),
        )
    except Exception:
        logger.warning("写入 migration 事件失败（不影响迁移本身）", exc_info=True)


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """
    v3 -> v4（Phase 13）：新建 app_state（通用 runtime metadata key/value）。
    全部 IF NOT EXISTS 幂等语句：不 DROP、不 DELETE、不动旧数据。
    """
    conn.executescript(_SCHEMA_V4)
    try:
        conn.execute(
            "INSERT INTO monitor_events(timestamp, event_type, severity, source, details_json) "
            "VALUES(?, 'migration', 'info', 'database', ?)",
            (int(time.time()), _json_dumps({"from": 3, "to": 4})),
        )
    except Exception:
        logger.warning("写入 migration 事件失败（不影响迁移本身）", exc_info=True)


# 数据库版本 -> 迁移函数（执行后库从该版本升到 version+1）
_MIGRATIONS: dict[int, object] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
}


def local_date(now: float | None = None) -> str:
    """本机系统日期 YYYY-MM-DD（daily_usage 的归集依据；now 为 Unix 秒，默认当前时刻）。"""
    if now is None:
        return datetime.now().strftime("%Y-%m-%d")
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _num(value):
    """None 原样返回；整数值的 float 转 int（展示干净）；其余保持 float。"""
    if value is None:
        return None
    f = float(value)
    if f == f and f not in (float("inf"), float("-inf")) and f.is_integer():
        return int(f)
    return f


def _json_dumps(obj) -> str:
    """事件/缺口 details 的 JSON 序列化（紧凑、非 ASCII 不转义、异常兜底 "{}"）。"""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _json_loads(text: str | None) -> dict:
    """details_json 反序列化（None/损坏 -> {}；只接受 dict）。"""
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


class Database:
    """SQLite 封装。单连接（同一线程使用），所有写入在单个事务内完成。"""

    def __init__(
        self,
        path: str | os.PathLike,
        wal: bool = True,
        retention_seconds: float | None = None,
        pre_migration_backup_dir: str | os.PathLike | None = None,
    ) -> None:
        self.path = str(path)
        self._wal = wal
        # Phase 12：schema 迁移前的自动快照目录（None = 跳过 pre-migration backup，
        # 例如测试/浏览器模式；生产入口 desktop.py 传入 %LOCALAPPDATA%\LlamaMonitor\backups）。
        # pre_migration_*.db 属于特殊恢复点，不参与 automatic backup 轮换。
        self._pre_migration_backup_dir = (
            str(pre_migration_backup_dir) if pre_migration_backup_dir is not None else None
        )
        # live_samples 保留时长：来自配置 collector.live_retention_hours（None -> 默认 48h）
        self.retention_seconds = (
            LIVE_SAMPLE_RETENTION_SECONDS if retention_seconds is None else retention_seconds
        )
        self._conn: sqlite3.Connection | None = None
        # 写锁：所有写操作（采集落盘 / 清理 / 重置 / 备份）串行化。
        # 生产环境所有 DB 访问都在事件循环线程（单线程不变量），
        # 该锁使这一约束显式化，并防止未来多线程误用。
        self._write_lock = threading.Lock()
        # Phase 11：数据库健康状态（quick_check 之后由外部 set_health 更新）。
        # healthy=正常；warning=可用但有隐患（如 journal_mode 非 WAL）；
        # corrupt=quick_check 失败（禁止修改类写，只读尽量可用）；
        # unavailable=连连接/读取都失败。
        self._health = DB_HEALTH_HEALTHY
        self._health_detail: str | None = None
        self.journal_mode: str | None = None  # 实际 journal_mode（不假设 WAL 一定成功）

    @property
    def health(self) -> str:
        return self._health

    @property
    def health_detail(self) -> str | None:
        return self._health_detail

    def set_health(self, status: str, detail: str | None = None) -> None:
        """更新健康状态（status 必须是已知值之一）。"""
        if status not in (
            DB_HEALTH_HEALTHY, DB_HEALTH_WARNING, DB_HEALTH_CORRUPT,
            DB_HEALTH_UNAVAILABLE, DB_HEALTH_INCOMPATIBLE,
        ):
            raise ValueError(f"unknown database health status: {status!r}")
        self._health = status
        self._health_detail = detail

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Phase 12：打开前记录"已有数据"状态（pre-migration backup 判定用）
            try:
                self._pre_existing_bytes = os.path.getsize(self.path)
            except OSError:
                self._pre_existing_bytes = 0
            conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000.0)
            conn.row_factory = sqlite3.Row
            # Phase 11：busy_timeout 显式设置（connect 的 timeout 参数已覆盖，
            # 这里再设一次 PRAGMA 使意图明确；短暂锁等待 5s 而非立即失败，但不无限重试）
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
            try:
                db_version = conn.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.DatabaseError:
                db_version = 0
            if db_version > CURRENT_SCHEMA_VERSION:
                # Phase 12 降级保护：数据库由**更新**的 LlamaMonitor 版本创建。
                # 绝不建表 / 迁移 / 降低 schema：切换到只读连接，健康=incompatible
                # （protective mode：只读展示，停止修改类写）。
                logger.error(
                    "Database user_version=%d 高于本程序支持的 %d（由更新版本创建）："
                    "进入只读 incompatible 模式（不迁移、不写入）",
                    db_version, CURRENT_SCHEMA_VERSION,
                )
                conn.close()
                # 只读连接：file URI（Windows 路径转正斜杠）；读取 WAL 库时
                # SQLite 会自动包含已存在的 -wal/-shm 内容
                uri = "file:" + self.path.replace("\\", "/") + "?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0)
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
                self._health = DB_HEALTH_INCOMPATIBLE
                self._health_detail = (
                    f"database schema v{db_version} is newer than this app "
                    f"(supports v{CURRENT_SCHEMA_VERSION}); opened read-only"
                )
                try:
                    self.journal_mode = (conn.execute("PRAGMA journal_mode;").fetchone() or [None])[0]
                except sqlite3.DatabaseError:
                    self.journal_mode = None
                self._conn = conn
                return conn
            try:
                # 1) 确保 v1 基础表存在（新建库或 legacy 库都适用，IF NOT EXISTS 不碰旧数据）
                conn.executescript(_SCHEMA_V1)
                # 2) 逐版本 schema migration（legacy 库先标记 v1 再升 v3）
                self._migrate(conn)
            except Exception:
                # migration 失败：释放连接（不留下打开的文件句柄），异常向上传播
                conn.close()
                raise
            if self._wal:
                # 不假设返回值就是 WAL：读回实际 journal_mode 并写日志
                conn.execute("PRAGMA journal_mode=WAL;")
            self.journal_mode = (conn.execute("PRAGMA journal_mode;").fetchone() or [None])[0]
            jm = (self.journal_mode or "").lower()
            if self._wal and jm != "wal":
                logger.warning("Database journal mode 期望 wal，实际 %r（继续可用模式）", self.journal_mode)
            else:
                logger.info("Database journal mode: %s", self.journal_mode)
            self._conn = conn
        return self._conn

    def quick_check(self) -> tuple[bool, str | None]:
        """
        PRAGMA quick_check（比完整 integrity_check 快，适合每次启动跑一次）。
        返回 (ok, detail)：ok=True 表示返回 'ok'；否则 detail 为首条失败信息。
        **不自动删除/重建数据库**——损坏只上报健康状态，由上层决定是否保护模式。
        """
        conn = self._connect()
        try:
            row = conn.execute("PRAGMA quick_check;").fetchone()
            result = row[0] if row else None
        except sqlite3.DatabaseError as exc:
            return False, str(exc)
        if result == "ok":
            return True, None
        return False, str(result)

    def checkpoint(self, mode: str = "PASSIVE") -> None:
        """
        PRAGMA wal_checkpoint（默认 PASSIVE）。
        用于 graceful shutdown / 手动备份前后让 WAL 落盘；**不用 TRUNCATE** 做高频操作。
        失败只 WARNING（不影响数据）。
        """
        if self._conn is None:
            return
        try:
            self._conn.execute(f"PRAGMA wal_checkpoint({mode});")
        except sqlite3.DatabaseError as exc:
            logger.warning("wal_checkpoint(%s) 失败（不影响数据）: %r", mode, exc)

    def _tx_with_retry(self, fn, *, what: str):
        """
        在有限重试下执行一个写事务 `fn(conn)`（fn 内自己 `with conn:` 开事务）。
        只对 SQLITE_BUSY / 'database is locked' 重试（3 次，50/100/200ms）；
        其他异常立即抛出。最终失败抛最后一次异常（调用方负责记录 + 本轮不更新 baseline）。
        """
        last: Exception | None = None
        for attempt, delay in enumerate((0.0, *TX_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if ("database is locked" in msg) or ("busy" in msg):
                    last = exc
                    continue
                raise
        assert last is not None
        logger.error("写事务 %r 重试 %d 次后仍失败（本轮不更新 baseline，下轮重算）: %r",
                     what, len(TX_RETRY_DELAYS), last)
        raise last

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """
        把数据库从当前 user_version 迁移到 CURRENT_SCHEMA_VERSION。

        - user_version=0：新建库或 Phase 8 legacy 库（表已由 _SCHEMA_V1 就位）——
          先标记为 1，再继续逐级迁移。legacy 库的 daily_usage / live_samples /
          state 数据全部原样保留，不删除、不重建；
        - 每个 migration 在独立事务内执行：失败整体回滚 + ERROR 日志，
          下次启动从同一版本重试（migration 均为幂等语句）；
        - 成功后设置 PRAGMA user_version（放在事务外，避免回滚歧义）。
        """
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute("PRAGMA user_version = 1")
            version = 1
            logger.debug("Schema: 标记为基础版本 1（新建库或 Phase 8 legacy 库）")
        if version < CURRENT_SCHEMA_VERSION and self._pre_migration_backup_needed(version):
            # Phase 12：schema 迁移前自动快照（SQLite Backup API + quick_check 验证）。
            # 失败 -> 不继续迁移，进入 incompatible 保护状态（只读、停止写入）；
            # 数据库保持旧版本原样，下次启动（或修复后备份目录问题）会重试。
            if not self._pre_migration_backup(conn, version, CURRENT_SCHEMA_VERSION):
                self.set_health(
                    DB_HEALTH_INCOMPATIBLE,
                    f"pre-migration backup failed before v{version}->v{CURRENT_SCHEMA_VERSION}; "
                    "migration skipped, database kept at v" + str(version) + " (read-only)",
                )
                logger.error(
                    "pre-migration backup 失败：放弃 schema migration v%d->v%d，"
                    "进入只读保护状态（数据库保持 v%d 原样）",
                    version, CURRENT_SCHEMA_VERSION, version,
                )
                return
        while version < CURRENT_SCHEMA_VERSION:
            migrate = _MIGRATIONS.get(version)
            if migrate is None:
                raise RuntimeError(f"未知的 schema 版本 {version}（当前支持到 {CURRENT_SCHEMA_VERSION}）")
            try:
                with conn:  # 事务：任一条目失败 -> 整体回滚
                    migrate(conn)
            except Exception as exc:
                logger.error("Schema migration %d -> %d 失败（已回滚，下次启动重试）: %r", version, version + 1, exc)
                raise
            conn.execute(f"PRAGMA user_version = {version + 1}")
            logger.info("Schema migration %d -> %d 完成", version, version + 1)
            version += 1

    def _pre_migration_backup_needed(self, version: int) -> bool:
        """
        是否需要 pre-migration backup（Phase 12）：
        - 未配置备份目录（测试/浏览器模式）-> 不需要（直接迁移）；
        - 打开前文件已存在且非空（既有库 / legacy 库，有真实数据要保护）-> 需要；
        - 全新空库（文件此前不存在或 0 字节）-> 不需要。
        """
        if not self._pre_migration_backup_dir:
            return False
        return getattr(self, "_pre_existing_bytes", 0) > 0

    def _pre_migration_backup(
        self, conn: sqlite3.Connection, old_version: int, new_version: int
    ) -> bool:
        """
        Schema 迁移前自动快照（Phase 12）：
        - 文件名 `pre_migration_v{old}_to_v{new}_{YYYYmmdd_HHMMSS}.db`，
          放在备份目录（%LOCALAPPDATA%\\LlamaMonitor\\backups）；
        - 用 SQLite Backup API（对当前活连接做一致性快照，WAL 安全，不是文件复制）；
        - 对**备份文件本身** quick_check 验证；
        - 特殊恢复点：不参与 automatic backup 的 keep_count 轮换（前缀不同）。
        失败返回 False（调用方应放弃本次迁移）。
        """
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = Path(self._pre_migration_backup_dir) / (
            f"pre_migration_v{old_version}_to_v{new_version}_{stamp}.db"
        )
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dst = sqlite3.connect(str(dest))
            try:
                conn.backup(dst)  # SQLite Backup API：一致性快照
            finally:
                dst.close()
            check = sqlite3.connect(str(dest))
            try:
                ok = (check.execute("PRAGMA quick_check").fetchone() or [None])[0] == "ok"
            finally:
                check.close()
            if not ok:
                with contextlib.suppress(OSError):
                    dest.unlink()
                logger.error("pre-migration backup 文件验证失败（quick_check 非 ok）: %s", dest)
                return False
            logger.info("Pre-migration backup 完成: %s", dest)
            return True
        except Exception as exc:
            logger.error("pre-migration backup 失败: %r", exc)
            with contextlib.suppress(OSError):
                if dest.exists():
                    dest.unlink()
            return False

    def ensure_migrated(self) -> None:
        """
        在当前连接上（重新）执行 schema migration（Phase 12）。
        供 check-database 端点在 quick_check 通过后重试"被放弃的迁移"
        （例如 pre-migration backup 上次失败，修复备份目录后重试）。
        已是最新/更高版本时是 no-op。
        """
        conn = self._connect()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < CURRENT_SCHEMA_VERSION:
            self._migrate(conn)

    def get_schema_version(self) -> int:
        """当前数据库 schema 版本（PRAGMA user_version）。"""
        return self._connect().execute("PRAGMA user_version").fetchone()[0]

    def get_today_logical_tokens(self) -> int:
        """
        今日 Logical Tokens（prompt+cached+output，与 Dashboard/托盘同一口径）。
        单行主键查询；必须在事件循环线程调用（单连接不变量）。
        """
        conn = self._connect()
        row = conn.execute(
            "SELECT prompt_tokens + cached_tokens + output_tokens AS t "
            "FROM daily_usage WHERE date = ?",
            (local_date(),),
        ).fetchone()
        return int(row["t"] or 0) if row else 0

    # ---------- 读取 ----------

    def get_state(self) -> dict[str, float]:
        """上一次保存的 Counter：{指标全名: 值}。"""
        conn = self._connect()
        rows = conn.execute("SELECT metric_name, value FROM state").fetchall()
        return {row["metric_name"]: row["value"] for row in rows}

    def get_daily_usage(self, days: int | None = None) -> list[dict]:
        """按日期升序返回每日汇总；days=N 时只取最近 N 天。"""
        conn = self._connect()
        sql = (
            "SELECT date, prompt_tokens, cached_tokens, output_tokens, draft_tokens, "
            "accepted_tokens, prompt_seconds, predicted_seconds, draft_sequences FROM daily_usage"
        )
        if days is not None:
            rows = conn.execute(sql + " ORDER BY date DESC LIMIT ?", (days,)).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(sql + " ORDER BY date ASC").fetchall()
        return [dict(row) for row in rows]

    def get_live_samples(self, hours: float | None = 48.0) -> list[dict]:
        """
        按 timestamp 升序返回 live_samples。

        hours=None 返回全部行；否则只返回最近 hours 小时内（基于系统当前时间）。
        """
        conn = self._connect()
        if hours is None:
            rows = conn.execute("SELECT * FROM live_samples ORDER BY timestamp ASC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM live_samples WHERE timestamp >= ? ORDER BY timestamp ASC",
                (time.time() - hours * 3600,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_mtp_position_daily(self, date: str) -> dict[str, int]:
        """
        指定日期的 per-position 接受数：{position: accepted_tokens}。

        服务器不提供 position 数据（或当天尚无增量）时返回空 dict。
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT position, accepted_tokens FROM mtp_position_daily WHERE date = ?",
            (date,),
        ).fetchall()
        return {row["position"]: row["accepted_tokens"] for row in rows}

    def get_latest_live_sample(self) -> dict | None:
        """最近一条 live_sample（按插入顺序）；尚无任何采样时返回 None。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM live_samples ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ---------- 写入（单事务） ----------

    def apply_sample(
        self,
        state_values: dict[str, float],
        live_row: dict,
        daily_date: str,
        daily_increments: dict,
        now: float | None = None,
        position_increments: dict | None = None,
    ) -> None:
        """
        持久化一次成功采集。整个方法在一个事务内完成，异常时整体回滚。

        - state_values: 本轮要保存的 Counter {全名: 值}（缺失字段的指标不出现，保持旧值）
        - live_row: 本轮 live_samples 行，必须覆盖 LIVE_SAMPLE_COLUMNS 全部键
        - daily_date: 本轮归属的本机日期 'YYYY-MM-DD'
        - daily_increments: daily 累加增量 {列名: 增量}（缺失字段传 0 或 None）
        - now: Unix 秒（默认当前时刻），同时用于 48h 清理截断
        - position_increments: 本轮 per-position 接受数增量 {position: 增量}
          （仅当服务器提供 position 数据且增量 >0 时传入）
        """
        conn = self._connect()
        now = time.time() if now is None else now

        def _do() -> None:
            with conn:  # 事务上下文：全部成功才提交，任一异常整体回滚
                # 1) 更新 state（最后一次 Counter）
                for name, value in state_values.items():
                    conn.execute(
                        "INSERT INTO state(metric_name, value) VALUES(?, ?) "
                        "ON CONFLICT(metric_name) DO UPDATE SET value = excluded.value",
                        (name, value),
                    )
                # 2) 累加 daily_usage（当天行不存在时自动创建）
                params = [daily_date]
                for col in _DAILY_COLUMNS:
                    v = daily_increments.get(col)
                    if v is None:
                        v = 0 if col.endswith("_tokens") else 0.0
                    params.append(_num(v))
                conn.execute(
                    "INSERT INTO daily_usage(date, "
                    + ", ".join(_DAILY_COLUMNS)
                    + ") VALUES("
                    + ", ".join(["?"] * (len(_DAILY_COLUMNS) + 1))
                    + ") "
                    "ON CONFLICT(date) DO UPDATE SET "
                    + ", ".join(f"{col} = {col} + excluded.{col}" for col in _DAILY_COLUMNS),
                    params,
                )
                # 3) 插入本轮 live_sample
                conn.execute(
                    "INSERT INTO live_samples(timestamp, "
                    + ", ".join(LIVE_SAMPLE_COLUMNS)
                    + ") VALUES("
                    + ", ".join(["?"] * (len(LIVE_SAMPLE_COLUMNS) + 1))
                    + ")",
                    [live_row["timestamp"]] + [_num(live_row.get(col)) for col in LIVE_SAMPLE_COLUMNS],
                )
                # 4) 清理超过保留时长的 live_samples（时长来自配置）
                conn.execute(
                    "DELETE FROM live_samples WHERE timestamp < ?",
                    (now - self.retention_seconds,),
                )
                # 5) 累加 per-position 接受数（当天行不存在时自动创建）
                if position_increments:
                    for position, increment in position_increments.items():
                        if increment is None:
                            continue
                        conn.execute(
                            "INSERT INTO mtp_position_daily(date, position, accepted_tokens) "
                            "VALUES(?, ?, ?) "
                            "ON CONFLICT(date, position) DO UPDATE SET "
                            "accepted_tokens = accepted_tokens + excluded.accepted_tokens",
                            (daily_date, position, _num(increment) or 0),
                        )

        # Phase 11：有限重试（50/100/200ms）；失败整体回滚 -> 调用方保持旧 baseline，
        # 下一轮从旧 baseline 重新计算完整 delta（不会丢、不会双计）
        with self._write_lock:
            self._tx_with_retry(_do, what="apply_sample")

    # ---------- 事件 / 数据缺口 / 备份（Phase 11） ----------

    def record_event(
        self,
        event_type: str,
        severity: str = "info",
        source: str = "collector",
        details: dict | None = None,
        now: float | None = None,
    ) -> int | None:
        """
        写一条 monitor_events 记录，并顺带执行事件保留清理（有限、幂等）。
        失败只 WARNING（事件日志丢失不应影响采集主流程），返回新行 id 或 None。
        """
        conn = self._connect()
        ts = int(now) if now is not None else int(time.time())
        try:

            def _do() -> int:
                with conn:
                    cur = conn.execute(
                        "INSERT INTO monitor_events(timestamp, event_type, severity, source, details_json) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (ts, event_type, severity, source, _json_dumps(details or {})),
                    )
                    self._enforce_event_retention(conn, ts)
                    return cur.lastrowid

            return self._tx_with_retry(_do, what=f"record_event:{event_type}")
        except sqlite3.Error as exc:
            logger.warning("写入事件 %s 失败（不影响采集）: %r", event_type, exc)
            return None

    def get_app_state(self, key: str) -> str | None:
        """读 runtime metadata（app_state 表，schema v4）；不存在返回 None。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else None

    def set_app_state(self, key: str, value: str, now: float | None = None) -> bool:
        """
        写 runtime metadata（app_state 表，schema v4；UPSERT）。
        失败只 WARNING（runtime metadata 丢失不应影响主流程），返回是否成功。
        """
        conn = self._connect()
        ts = int(now) if now is not None else int(time.time())
        try:

            def _do() -> None:
                with conn:
                    conn.execute(
                        "INSERT INTO app_state(key, value, updated_at) VALUES(?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                        "updated_at = excluded.updated_at",
                        (key, value, ts),
                    )

            self._tx_with_retry(_do, what=f"set_app_state:{key}")
            return True
        except sqlite3.Error as exc:
            logger.warning("写入 runtime metadata %s 失败: %r", key, exc)
            return False

    @staticmethod
    def _enforce_event_retention(conn: sqlite3.Connection, now_ts: int) -> None:
        """monitor_events 保留 EVENT_RETENTION_DAYS 天 + 行数硬上限（data_gaps 不清理）。"""
        conn.execute(
            "DELETE FROM monitor_events WHERE timestamp < ?",
            (now_ts - EVENT_RETENTION_DAYS * 86400,),
        )
        conn.execute(
            "DELETE FROM monitor_events WHERE id NOT IN "
            "(SELECT id FROM monitor_events ORDER BY id DESC LIMIT ?)",
            (EVENT_RETENTION_MAX_ROWS,),
        )

    def get_events(self, limit: int = 100, since_ts: float | None = None) -> list[dict]:
        """最近 limit 条事件（倒序），details_json 已解析为 dict。"""
        conn = self._connect()
        if since_ts is not None:
            rows = conn.execute(
                "SELECT * FROM monitor_events WHERE timestamp >= ? ORDER BY id DESC LIMIT ?",
                (int(since_ts), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM monitor_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["details"] = _json_loads(d.pop("details_json", None))
            out.append(d)
        return out

    def record_gap(
        self,
        start_ts: float,
        end_ts: float,
        source: str,
        reason: str,
        token_recoverable: bool = True,
        possible_token_loss: bool = False,
        now: float | None = None,
    ) -> int | None:
        """
        记录一个**已结束**的已知监控缺口（data_gaps 永久保留）。
        duration 用 end-start（秒，float）。失败只 WARNING，返回新行 id 或 None。
        """
        conn = self._connect()
        duration = max(0.0, float(end_ts) - float(start_ts))
        try:

            def _do() -> int:
                with conn:
                    cur = conn.execute(
                        "INSERT INTO data_gaps(start_timestamp, end_timestamp, duration_seconds, "
                        "source, reason, token_recoverable, possible_token_loss, resolved) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, 1)",
                        (
                            int(start_ts), int(end_ts), duration, source, reason,
                            1 if token_recoverable else 0, 1 if possible_token_loss else 0,
                        ),
                    )
                    return cur.lastrowid

            return self._tx_with_retry(_do, what="record_gap")
        except sqlite3.Error as exc:
            logger.warning("写入 data_gap 失败: %r", exc)
            return None

    def get_gaps(
        self,
        date: str | None = None,
        since_ts: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        查询 data_gaps：
        - date='YYYY-MM-DD'：与该日期有交集的缺口（start<=当天末 且 end>=当天首）；
        - since_ts：start>=since_ts 的缺口；
        - 都不传：最近 limit 条（倒序）。
        """
        conn = self._connect()
        if date is not None:
            from datetime import datetime as _dt
            start_day = _dt.strptime(date, "%Y-%m-%d").timestamp()
            end_day = start_day + 86400
            rows = conn.execute(
                "SELECT * FROM data_gaps WHERE start_timestamp < ? AND end_timestamp >= ? "
                "ORDER BY id DESC LIMIT ?",
                (end_day, start_day, limit),
            ).fetchall()
        elif since_ts is not None:
            rows = conn.execute(
                "SELECT * FROM data_gaps WHERE start_timestamp >= ? ORDER BY id DESC LIMIT ?",
                (int(since_ts), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM data_gaps ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_gap_stats(self, date: str) -> dict:
        """
        某自然日的缺口统计：gap_count / possible_token_loss(是否存在) /
        total_gap_seconds / token_recoverable(是否全部可恢复)。
        """
        gaps = self.get_gaps(date=date, limit=100000)
        total = sum(g["duration_seconds"] for g in gaps)
        return {
            "gap_count": len(gaps),
            "possible_token_loss": any(g["possible_token_loss"] for g in gaps),
            "total_gap_seconds": total,
            "token_recoverable": all(g["token_recoverable"] for g in gaps) if gaps else True,
        }

    def get_first_and_last_sample(self) -> tuple[float | None, float | None]:
        """(首次, 最近) 有效 live_sample 的 timestamp（Unix 秒）；无数据 -> (None, None)。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM live_samples"
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def record_backup(
        self,
        btype: str,
        path: str,
        size: int | None,
        verified: bool,
        success: bool = True,
        now: float | None = None,
    ) -> None:
        """写一条 backup_history（文件系统仍是最终备份来源，此表只是元数据）。"""
        conn = self._connect()
        ts = int(now) if now is not None else int(time.time())
        try:

            def _do() -> None:
                with conn:
                    conn.execute(
                        "INSERT INTO backup_history(timestamp, type, path, size, verified, success) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (ts, btype, path, size, 1 if verified else 0, 1 if success else 0),
                    )

            self._tx_with_retry(_do, what="record_backup")
        except sqlite3.Error as exc:
            logger.warning("写入 backup_history 失败: %r", exc)

    def get_last_backup(self, btype: str | None = None) -> dict | None:
        """最近一条成功的 backup_history（type=None 不限类型）。"""
        conn = self._connect()
        if btype is not None:
            row = conn.execute(
                "SELECT * FROM backup_history WHERE type=? AND success=1 AND verified=1 "
                "ORDER BY id DESC LIMIT 1",
                (btype,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM backup_history WHERE success=1 AND verified=1 "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_backup_history(self, limit: int = 50) -> list[dict]:
        """最近的 backup_history 行（倒序）。文件系统仍是最终备份来源，
        此表只提供验证状态等元数据。"""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM backup_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 数据管理（Phase 8） ----------

    def get_data_info(self) -> dict:
        """存储统计信息：日期范围 / 每日行数 / 实时采样行数。"""
        conn = self._connect()
        row = conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(*) FROM daily_usage"
        ).fetchone()
        live = conn.execute("SELECT COUNT(*) FROM live_samples").fetchone()[0]
        return {
            "first_recorded_date": row[0],
            "last_recorded_date": row[1],
            "daily_rows": row[2],
            "live_samples": live,
        }

    def clear_live_samples(self, vacuum: bool = False) -> int:
        """
        清空实时历史：live_samples + gpu_samples（不动 daily_usage / gpu_daily / state）。

        vacuum=True 时在删除事务提交后执行 VACUUM（用户主动清理时才收缩文件；
        VACUUM 不能在事务内执行，所以放在事务外）。
        """
        conn = self._connect()
        with self._write_lock:
            with conn:
                deleted = conn.execute("DELETE FROM live_samples").rowcount
                deleted += conn.execute("DELETE FROM gpu_samples").rowcount
            if vacuum:
                conn.execute("VACUUM")
        return deleted

    def reset_statistics(self) -> dict:
        """
        重置历史统计：同一事务内 DELETE
        daily_usage + live_samples + gpu_samples + gpu_daily + mtp_position_daily。

        **保留 state 表（当前 Counter baseline 不动，含 per-position MTP baseline）**：
        重置后 Collector 继续从上次保存的 Counter 值计算增量，旧 Token 不会被重新计入。
        GPU 数据不是累计 Counter，重置后直接从 0 开始新的 GPU daily。
        返回各表删除行数字典。
        """
        conn = self._connect()
        out: dict[str, int] = {}
        with self._write_lock:
            with conn:
                for key, table in (
                    ("daily_deleted", "daily_usage"),
                    ("live_deleted", "live_samples"),
                    ("gpu_samples_deleted", "gpu_samples"),
                    ("gpu_daily_deleted", "gpu_daily"),
                    ("mtp_position_deleted", "mtp_position_daily"),
                ):
                    out[key] = conn.execute(f"DELETE FROM {table}").rowcount
        return out

    def backup_to(self, dest_path: str | os.PathLike) -> int:
        """
        用 SQLite Backup API 生成一致性快照备份（数据库运行中、WAL 开启均安全；
        禁止直接文件复制）。返回备份文件大小（字节）。
        """
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        dest_conn = sqlite3.connect(str(dest_path))
        try:
            with self._write_lock:
                conn.backup(dest_conn)
        finally:
            dest_conn.close()
        return dest_path.stat().st_size

    # ---------- GPU（Phase 9） ----------

    def save_gpu_samples(
        self,
        samples: list,
        energy_wh: dict | None = None,
        now: float | None = None,
        retention_seconds: float | None = None,
    ) -> None:
        """
        持久化一轮 GPU 采样（单个事务）：
        1) 插入 gpu_samples；
        2) 增量累计 gpu_daily（每条样本按**样本自身的本机日期**归属，午夜跨日正确，
           不重扫全天数据）；
        3) 删除超过保留时长的 gpu_samples（config.gpu.history_retention_hours）。

        samples: list[gpu_collector.GpuSnapshot]
        energy_wh: {gpu_uuid: {date: 本轮新增能量 Wh}}（gpu_collector 用相邻采样的
        **monotonic** 梯形积分算好、并按精确午夜分割到自然日；跨午夜时同一段能量
        出现在两个 date 键下；该 GPU 的第一个采样为空 dict）。
        GPU 不是累计 Counter，重置统计后从 0 开始。
        （兼容旧格式 {gpu_uuid: 单值 Wh}：视为全部归样本自身日期。）
        """
        conn = self._connect()
        now = time.time() if now is None else now
        energy = energy_wh or {}

        def _do() -> None:
            with conn:
                for s in samples:
                    conn.execute(
                        "INSERT INTO gpu_samples(timestamp, gpu_uuid, gpu_index, gpu_name, "
                        "utilization_percent, memory_used_mb, memory_total_mb, temperature_c, "
                        "power_draw_w, fan_percent, sm_clock_mhz, memory_clock_mhz, "
                        "pcie_generation, pcie_width) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            s.timestamp, s.uuid, s.index, s.name, s.utilization_percent,
                            s.memory_used_mb, s.memory_total_mb, s.temperature_c, s.power_draw_w,
                            s.fan_percent, s.sm_clock_mhz, s.memory_clock_mhz,
                            s.pcie_generation, s.pcie_width,
                        ),
                    )
                    # 归一化能量：{date: Wh}（旧格式单值 -> 样本自身日期）
                    raw = energy.get(s.uuid)
                    if isinstance(raw, dict):
                        energy_by_date = {d: float(v) for d, v in raw.items() if v}
                    elif raw:
                        energy_by_date = {local_date(s.timestamp): float(raw)}
                    else:
                        energy_by_date = {}
                    self._accumulate_gpu_daily(conn, s, energy_by_date)
                if retention_seconds:
                    conn.execute(
                        "DELETE FROM gpu_samples WHERE timestamp < ?",
                        (now - retention_seconds,),
                    )

        with self._write_lock:
            self._tx_with_retry(_do, what="save_gpu_samples")

    @staticmethod
    def _accumulate_gpu_daily(conn: sqlite3.Connection, s, energy_by_date: dict[str, float]) -> None:
        """单条样本增量累计 gpu_daily（sum/count/max + 能量累加；缺失字段不计数）。

        能量按 energy_by_date 分别累加到对应自然日（跨午夜分割后的两天各自入账）；
        样本自身的 gauge（util/memory/...）永远只归样本自身日期。
        """
        date = local_date(s.timestamp)
        conn.execute(
            "INSERT INTO gpu_daily(date, gpu_uuid, gpu_name, sample_count) VALUES(?, ?, ?, 1) "
            "ON CONFLICT(date, gpu_uuid) DO UPDATE SET "
            "sample_count = sample_count + 1, "
            "gpu_name = COALESCE(excluded.gpu_name, gpu_daily.gpu_name)",
            (date, s.uuid, s.name),
        )
        # 能量：对每个受影响的自然日累加（该日行不存在时按需创建，仅记能量）
        for d, wh in energy_by_date.items():
            if not wh:
                continue
            if d == date:
                conn.execute(
                    "UPDATE gpu_daily SET energy_wh = energy_wh + ? WHERE date = ? AND gpu_uuid = ?",
                    (wh, d, s.uuid),
                )
            else:
                conn.execute(
                    "INSERT INTO gpu_daily(date, gpu_uuid, gpu_name, energy_wh) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(date, gpu_uuid) DO UPDATE SET energy_wh = energy_wh + excluded.energy_wh",
                    (d, s.uuid, s.name, wh),
                )
        if s.utilization_percent is not None:
            conn.execute(
                "UPDATE gpu_daily SET utilization_count = utilization_count + 1, "
                "utilization_sum = utilization_sum + ?, "
                "utilization_max = MAX(COALESCE(utilization_max, ?), ?) "
                "WHERE date = ? AND gpu_uuid = ?",
                (s.utilization_percent, s.utilization_percent, s.utilization_percent, date, s.uuid),
            )
        if s.memory_used_mb is not None:
            conn.execute(
                "UPDATE gpu_daily SET memory_used_count = memory_used_count + 1, "
                "memory_used_sum_mb = memory_used_sum_mb + ?, "
                "memory_used_max_mb = MAX(COALESCE(memory_used_max_mb, ?), ?) "
                "WHERE date = ? AND gpu_uuid = ?",
                (s.memory_used_mb, s.memory_used_mb, s.memory_used_mb, date, s.uuid),
            )
        if s.temperature_c is not None:
            conn.execute(
                "UPDATE gpu_daily SET temperature_count = temperature_count + 1, "
                "temperature_sum = temperature_sum + ?, "
                "temperature_max = MAX(COALESCE(temperature_max, ?), ?) "
                "WHERE date = ? AND gpu_uuid = ?",
                (s.temperature_c, s.temperature_c, s.temperature_c, date, s.uuid),
            )
        if s.power_draw_w is not None:
            conn.execute(
                "UPDATE gpu_daily SET power_count = power_count + 1, "
                "power_sum_w = power_sum_w + ?, power_max_w = MAX(COALESCE(power_max_w, ?), ?) "
                "WHERE date = ? AND gpu_uuid = ?",
                (s.power_draw_w, s.power_draw_w, s.power_draw_w, date, s.uuid),
            )

    def get_gpu_latest(self) -> list[dict]:
        """每张 GPU 的最近一条采样（按 uuid 去重；index 缺失的排后面）。"""
        conn = self._connect()
        rows = conn.execute(
            "SELECT g.* FROM gpu_samples g "
            "JOIN (SELECT gpu_uuid, MAX(timestamp) AS mt FROM gpu_samples GROUP BY gpu_uuid) t "
            "ON g.gpu_uuid = t.gpu_uuid AND g.timestamp = t.mt "
            "ORDER BY (g.gpu_index IS NULL), g.gpu_index, g.gpu_uuid"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_gpu_samples_since(self, since_ts: float, gpu_uuid: str | None = None) -> list[dict]:
        """since_ts 之后的 gpu_samples（时间升序；gpu_uuid 指定时只取该 GPU）。"""
        conn = self._connect()
        if gpu_uuid is not None:
            rows = conn.execute(
                "SELECT * FROM gpu_samples WHERE timestamp >= ? AND gpu_uuid = ? ORDER BY timestamp ASC",
                (since_ts, gpu_uuid),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM gpu_samples WHERE timestamp >= ? ORDER BY timestamp ASC",
                (since_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_gpu_daily(self, days: int | None = None) -> list[dict]:
        """
        GPU 每日累计（日期升序）。

        days=N 时只取最近 N 个自然日（与 /api/daily 同一日期规则）。
        平均值为 API 层由 sum/count 计算；count=0 时为 None。
        """
        conn = self._connect()
        if days is not None:
            cutoff = local_date(time.time() - (days - 1) * 86400)
            rows = conn.execute(
                "SELECT * FROM gpu_daily WHERE date >= ? ORDER BY date ASC, gpu_uuid",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM gpu_daily ORDER BY date ASC, gpu_uuid"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_gpu_sample_count(self) -> int:
        conn = self._connect()
        return conn.execute("SELECT COUNT(*) FROM gpu_samples").fetchone()[0]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
