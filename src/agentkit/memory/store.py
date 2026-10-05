"""SQLite 持久化。

**消息以 JSON 整体存储，不拆分为关系表。** 内容块是个判别联合
（text / reasoning / tool_use / tool_result），拆成列意味着每加一种块类型就要改
表结构、改读写代码、写迁移。而 ``Message`` 已经是 pydantic 模型，序列化/反序列化
本来是免费的。代价是没法用 SQL 直接查「所有包含某个工具的调用」——那种分析
走 :mod:`agentkit.observability` 的事件日志更合适，不该让事务库承担。

**本类采用同步接口。** SQLite 的本地文件读写在这个数据量下是微秒级的，
为它引入一整套异步接口不划算。但**不能在事件循环里直接调**——单条消息虽小，
一台机器上几百个并发 run 叠起来照样会卡住循环。所以 :class:`~agentkit.memory.manager.MemoryManager`
统一用 ``asyncio.to_thread`` 包一层，把这里的同步语义关在那一层里。

**线程安全和 ``check_same_thread=False``。** 这个开关名字有误导性：它不是
「允许跨线程共享」，而是「关掉 sqlite3 的线程归属检查」——真正的安全责任转移给了
调用方。所以这里配了一把锁，把所有访问串行化。SQLite 本身在写操作上就是串行的，
加锁不损失实际吞吐，却能保证跨线程调用不会静默损坏数据。

**约束**：``check_same_thread=False`` 关闭了 sqlite3 的线程归属检查，
调用方必须自行保证串行访问。该保证由下面的锁提供——缺少它时，
跨线程访问会抛出 ``ProgrammingError``，而非静默损坏数据。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..core.types import Message

__all__ = ["SessionStore", "SessionInfo"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL DEFAULT '',
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
"""


def _serialized(method):
    """把一次存储操作串行化。

    写成装饰器而不是在每个方法里手写 ``with self._lock``，是为了让「忘记加锁」
    这件事没法悄悄发生——新加的方法要么带上它，要么被 code review 一眼看出来。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class SessionInfo:
    """会话摘要。"""

    id: str
    created_at: float
    updated_at: float
    message_count: int
    metadata: dict


class SessionStore:
    """会话与消息的 SQLite 存储。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        # check_same_thread=False 关掉 sqlite3 的线程归属检查，
        # 换来的是必须自己保证串行访问——下面那把锁就是干这个的。
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 用 RLock 而不是 Lock：append_messages 内部会调 ensure_session 和 _next_seq，
        # 普通锁会在同一个线程里自锁死。
        self._lock = threading.RLock()
        # 外键约束默认是关的，不显式打开的话 ON DELETE CASCADE 不会生效，
        # 删掉会话后会留下一堆孤儿消息。
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ 会话

    @_serialized
    def ensure_session(self, session_id: str, **metadata: object) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO sessions (id, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (session_id, now, now, json.dumps(metadata, ensure_ascii=False)),
        )
        self._conn.commit()

    @_serialized
    def session_exists(self, session_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row is not None

    @_serialized
    def list_sessions(self) -> list[SessionInfo]:
        rows = self._conn.execute(
            """
            SELECT s.id, s.created_at, s.updated_at, s.metadata,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n
            FROM sessions s
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
        return [
            SessionInfo(
                id=row["id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=row["n"],
                metadata=json.loads(row["metadata"] or "{}"),
            )
            for row in rows
        ]

    @_serialized
    def delete_session(self, session_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------ 消息

    @_serialized
    def append_messages(
        self,
        session_id: str,
        messages: Sequence[Message],
        *,
        run_id: str = "",
    ) -> int:
        """追加若干条消息，返回新写入的条数。"""
        if not messages:
            return 0

        self.ensure_session(session_id)
        start = self._next_seq(session_id)
        now = time.time()

        self._conn.executemany(
            """
            INSERT INTO messages (session_id, run_id, seq, role, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    run_id,
                    start + offset,
                    message.role,
                    message.model_dump_json(),
                    now,
                )
                for offset, message in enumerate(messages)
            ],
        )
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        self._conn.commit()
        return len(messages)

    @_serialized
    def load_messages(self, session_id: str, *, limit: int | None = None) -> list[Message]:
        """读取会话消息。

        :param limit: 只取**最近** N 条。注意 SQL 的 LIMIT 是从头取的，
            所以子查询里按 ``seq DESC`` 取完再翻回来。
        """
        if limit is None:
            rows = self._conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT payload FROM (
                    SELECT payload, seq FROM messages
                    WHERE session_id = ? ORDER BY seq DESC LIMIT ?
                ) ORDER BY seq ASC
                """,
                (session_id, limit),
            ).fetchall()

        return [Message.model_validate_json(row["payload"]) for row in rows]

    @_serialized
    def message_count(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"])

    def _next_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS s FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["s"]) + 1

    # ------------------------------------------------------------ 生命周期

    @_serialized
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
