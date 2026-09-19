"""
backup.py — 数据库备份管理器（Phase 11：可靠性）

规则（与规格一致）：
- 用 **SQLite Online Backup API**（`source.backup(destination)`）做备份——
  一致性快照，绝不 shutil.copy 正在被 WAL 写入的数据库文件；
- 备份在**独立连接**上进行（备份线程里新建 source 连接），不占用主连接
  （单连接不变量：主连接只在事件循环线程使用；本模块所有方法设计为
  从 asyncio.to_thread 调用）；
- 文件名：`auto_monitor_YYYYMMDD_HHMMSS.db`（自动）/
  `manual_monitor_YYYYMMDD_HHMMSS.db`（手动）；旧版 `monitor_*.db` 保留展示；
- **先验证、后生效**：新备份用 PRAGMA quick_check 验证；失败则删除新文件、
  保留旧备份（轮转只删"新的验证过的自动备份"之外的旧 auto_*，绝不误删）；
- 轮转只删 `auto_monitor_*`（手动备份与 legacy 永不自动删）；
  顺序：先创建并验证新备份，成功后再删旧（新备份验证失败时旧备份一个不删）；
- 自动备份触发：距上次成功自动备份 >= backup.interval_hours（按 auto_* 文件
  mtime 判断——文件系统是最终事实，backup_history 只是元数据）；
  不依赖精确午夜、不用 Task Scheduler；
- 启动时检查一次是否到期（不是"每次启动必做"）。

本模块全部同步方法；调用方（server lifespan / API）用 asyncio.to_thread 调用，
避免阻塞事件循环。备份元数据（backup_history / backup_created 事件）由调用方
在事件循环线程写入主连接。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from clock import Clock, default_clock

logger = logging.getLogger("llamamonitor.backup")

AUTO_PREFIX = "auto_monitor_"
MANUAL_PREFIX = "manual_monitor_"
LEGACY_PREFIX = "monitor_"   # Phase 8 旧格式（保留展示，不参与轮转）

CHECK_INTERVAL_SECONDS = 60.0  # 后台检查周期（秒）：只 stat 文件，开销可忽略


@dataclass
class BackupResult:
    """一次备份尝试的结果。"""

    success: bool
    btype: str                        # automatic / manual
    path: str | None = None
    size: int | None = None
    verified: bool = False
    error: str | None = None
    rotated: list = field(default_factory=list)   # 本次删除的旧自动备份文件名

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "type": self.btype,
            "path": self.path,
            "size": self.size,
            "verified": self.verified,
            "error": self.error,
            "rotated": list(self.rotated),
        }


class BackupManager:
    """自动/手动备份 + 验证 + 轮转。无内部状态（每次调用读文件系统，天然幂等）。"""

    def __init__(
        self,
        db_path: str | Path,
        backups_dir: str | Path,
        keep_count: int = 14,
        clock: Clock | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backups_dir = Path(backups_dir)
        self.keep_count = max(1, int(keep_count))
        self.clock = clock or default_clock

    # ---------- 基础工具 ----------

    def _timestamp_name(self, prefix: str, now: float | None = None) -> str:
        from datetime import datetime
        ts = datetime.fromtimestamp(self.clock.now() if now is None else now)
        return f"{prefix}{ts.strftime('%Y%m%d_%H%M%S')}.db"

    def _all_backups(self) -> list[Path]:
        if not self.backups_dir.is_dir():
            return []
        return [
            f for f in self.backups_dir.iterdir()
            if f.is_file() and f.suffix == ".db" and (
                f.name.startswith(AUTO_PREFIX) or f.name.startswith(MANUAL_PREFIX)
                or f.name.startswith(LEGACY_PREFIX)
            )
        ]

    def _auto_backups(self) -> list[Path]:
        return [f for f in self._all_backups() if f.name.startswith(AUTO_PREFIX)]

    def is_due(self, interval_hours: float) -> bool:
        """
        自动备份是否到期：没有 auto_* 文件 -> 到期；
        否则距最新 auto_* 的 mtime >= interval_hours -> 到期。
        （文件系统是最终事实；不读 backup_history。）
        """
        autos = self._auto_backups()
        if not autos:
            return True
        newest = max(f.stat().st_mtime for f in autos)
        return (self.clock.now() - newest) >= interval_hours * 3600.0

    # ---------- 备份 ----------

    def create_backup(self, btype: str = "automatic", now: float | None = None) -> BackupResult:
        """
        执行一次备份（SQLite Online Backup API + quick_check 验证 + 轮转）。

        btype: 'automatic' | 'manual'。
        流程：
        1) 打开 source（独立连接）与 destination（新文件）；source.backup(dest)；
        2) 关闭；对 destination 只读打开 PRAGMA quick_check 验证；
        3) 验证失败：删除新文件（旧备份不动），success=False；
        4) 验证成功：automatic 时轮转（只删超额的 auto_*，含刚创建的这份一起排序）；
           手动备份不轮转。
        本方法自己完成全部文件操作；backup_history / 事件由调用方在主连接写。
        """
        if btype not in ("automatic", "manual"):
            raise ValueError(f"unknown backup type: {btype!r}")
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        prefix = AUTO_PREFIX if btype == "automatic" else MANUAL_PREFIX
        now = now if now is not None else self.clock.now()
        dest = self.backups_dir / self._timestamp_name(prefix, now)
        # 同秒重名（手动连点）：加序号
        seq = 0
        while dest.exists():
            seq += 1
            dest = dest.with_name(dest.stem + f"_{seq}" + dest.suffix)
        src: sqlite3.Connection | None = None
        dst: sqlite3.Connection | None = None
        try:
            if not self.db_path.is_file():
                return BackupResult(False, btype, error="source database file does not exist")
            src = sqlite3.connect(self.db_path, timeout=10.0)
            dst = sqlite3.connect(dest)
            src.backup(dst)   # SQLite Online Backup API：一致性快照（支持 WAL）
            dst.close()
            dst = None
            verified = self.verify_backup(dest)
            if not verified:
                try:
                    dest.unlink()
                except OSError:
                    pass
                logger.error("备份 %s 验证（quick_check）失败，已删除新备份、保留旧备份", dest.name)
                return BackupResult(False, btype, path=str(dest), error="backup failed quick_check verification")
            size = dest.stat().st_size
            rotated: list[str] = []
            if btype == "automatic":
                rotated = self._rotate_auto()
            logger.info("备份完成: %s (%d bytes, 验证通过%s)", dest.name, size,
                        f", 轮转删除 {len(rotated)} 个旧自动备份" if rotated else "")
            return BackupResult(True, btype, path=str(dest), size=size, verified=True, rotated=rotated)
        except (sqlite3.Error, OSError) as exc:
            # 失败：删除半成品新文件（若存在），旧备份不受影响
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass
            logger.error("备份失败（%s）: %r（保留旧备份）", dest.name, exc)
            return BackupResult(False, btype, path=str(dest), error=repr(exc))
        finally:
            for conn in (src, dst):
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    def verify_backup(self, path: str | Path) -> bool:
        """对备份文件做 PRAGMA quick_check（只读打开，不动原数据库）。"""
        path = Path(path)
        if not path.is_file():
            return False
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            row = conn.execute("PRAGMA quick_check;").fetchone()
            return bool(row and row[0] == "ok")
        except sqlite3.DatabaseError:
            return False
        finally:
            if conn is not None:
                conn.close()

    def _rotate_auto(self) -> list[str]:
        """
        自动备份轮转：只删 auto_monitor_*（按 mtime 降序，超出 keep_count 之外的删掉）。
        手动 / legacy 永不删。返回删除的文件名列表。
        """
        autos = sorted(self._auto_backups(), key=lambda f: f.stat().st_mtime, reverse=True)
        removed: list[str] = []
        for f in autos[self.keep_count:]:
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as exc:
                logger.warning("轮转删除旧自动备份 %s 失败: %r", f.name, exc)
        return removed

    # ---------- 列表 ----------

    def list_backups(self) -> list[dict]:
        """备份列表（新 -> 旧）：name / size / mtime / kind（automatic/manual/legacy）。"""
        out = []
        for f in self._all_backups():
            try:
                st = f.stat()
            except OSError:
                continue
            if f.name.startswith(AUTO_PREFIX):
                kind = "automatic"
            elif f.name.startswith(MANUAL_PREFIX):
                kind = "manual"
            else:
                kind = "legacy"
            out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime), "kind": kind})
        out.sort(key=lambda d: d["mtime"], reverse=True)
        return out
