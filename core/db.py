"""
SQLite storage for rewritten books and access grants.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DB_PATH = Path("app.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH.as_posix())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              source_filename TEXT,
              theme TEXT,
              temperature REAL,
              include_research INTEGER NOT NULL DEFAULT 0,
              original_text TEXT,
              paraphrased_text TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS moderator_book_access (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              book_id INTEGER NOT NULL,
              moderator_username TEXT NOT NULL,
              granted_at TEXT NOT NULL,
              granted_by TEXT NOT NULL,
              UNIQUE(book_id, moderator_username),
              FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_comments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              book_id INTEGER NOT NULL,
              author TEXT NOT NULL,
              comment_text TEXT NOT NULL,
              paragraph_index INTEGER,
              created_at TEXT NOT NULL,
              FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              book_id INTEGER NOT NULL,
              version_number INTEGER NOT NULL,
              paraphrased_text TEXT NOT NULL,
              change_note TEXT,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            """
            INSERT INTO book_versions (book_id, version_number, paraphrased_text, change_note, created_at, created_by)
            SELECT b.id, 1, b.paraphrased_text, 'Первоначальная версия (миграция)', b.created_at, b.created_by
            FROM books b
            WHERE NOT EXISTS (SELECT 1 FROM book_versions v WHERE v.book_id = b.id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS article_generation_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              theme TEXT NOT NULL,
              article_text TEXT NOT NULL,
              source TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              saved_book_id INTEGER,
              FOREIGN KEY(saved_book_id) REFERENCES books(id) ON DELETE SET NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              operation TEXT NOT NULL,
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              cost_usd REAL
            );
            """
        )
        _migrate_books_style_columns(conn)


def _migrate_books_style_columns(conn: sqlite3.Connection) -> None:
    for col, decl in (
        ("style_science", "INTEGER"),
        ("style_depth", "INTEGER"),
        ("style_accuracy", "INTEGER"),
        ("style_readability", "INTEGER"),
        ("style_source_quality", "INTEGER"),
    ):
        try:
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass


@contextmanager
def db() -> Iterable[sqlite3.Connection]:
    init_db()
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_book(
    *,
    title: str,
    source_filename: str | None,
    theme: str | None,
    temperature: float | None,
    include_research: bool,
    original_text: str | None,
    paraphrased_text: str,
    created_by: str,
    style_science: int | None = None,
    style_depth: int | None = None,
    style_accuracy: int | None = None,
    style_readability: int | None = None,
    style_source_quality: int | None = None,
) -> int:
    from settings_manager import settings_manager

    title = (title or "").strip() or "Без названия"
    created_by = (created_by or "").strip() or "unknown"

    def _iv(val: int | None, key: str, lo: int, hi: int, default: int) -> int:
        if val is not None:
            return max(lo, min(hi, int(val)))
        return max(lo, min(hi, int(settings_manager.get(key, default))))

    ss = _iv(style_science, "style_science", 1, 5, 3)
    sd = _iv(style_depth, "style_depth", 1, 5, 3)
    sa = _iv(style_accuracy, "style_accuracy", 1, 5, 3)
    sr = _iv(style_readability, "style_readability", 1, 7, 3)
    sq = _iv(style_source_quality, "style_source_quality", 1, 5, 3)

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO books (
              title, source_filename, theme, temperature, include_research,
              original_text, paraphrased_text, created_at, created_by,
              style_science, style_depth, style_accuracy, style_readability, style_source_quality
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                source_filename,
                theme,
                temperature,
                1 if include_research else 0,
                original_text,
                paraphrased_text,
                _now_iso(),
                created_by,
                ss,
                sd,
                sa,
                sr,
                sq,
            ),
        )
        book_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO book_versions (book_id, version_number, paraphrased_text, change_note, created_at, created_by)
            VALUES (?, 1, ?, 'Первоначальная версия', ?, ?)
            """,
            (book_id, paraphrased_text, _now_iso(), created_by),
        )
        return book_id


def list_books() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, source_filename, created_at, created_by FROM books ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_book(book_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (int(book_id),)).fetchone()
        return dict(row) if row else None


def rename_book(book_id: int, title: str) -> None:
    title = (title or "").strip() or "Без названия"
    with db() as conn:
        conn.execute("UPDATE books SET title = ? WHERE id = ?", (title, int(book_id)))


def list_book_access(book_id: int) -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT moderator_username FROM moderator_book_access WHERE book_id = ? ORDER BY moderator_username",
            (int(book_id),),
        ).fetchall()
        return [r["moderator_username"] for r in rows]


def grant_access(*, book_id: int, moderator_username: str, granted_by: str) -> None:
    moderator_username = (moderator_username or "").strip()
    if not moderator_username:
        raise ValueError("Moderator username is required")
    granted_by = (granted_by or "").strip() or "unknown"
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO moderator_book_access (book_id, moderator_username, granted_at, granted_by)
            VALUES (?, ?, ?, ?)
            """,
            (int(book_id), moderator_username, _now_iso(), granted_by),
        )


def revoke_access(*, book_id: int, moderator_username: str) -> None:
    moderator_username = (moderator_username or "").strip()
    with db() as conn:
        conn.execute(
            "DELETE FROM moderator_book_access WHERE book_id = ? AND moderator_username = ?",
            (int(book_id), moderator_username),
        )


def list_books_for_moderator(moderator_username: str) -> list[dict[str, Any]]:
    moderator_username = (moderator_username or "").strip()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.title, b.source_filename, b.created_at, b.created_by
            FROM books b
            INNER JOIN moderator_book_access a ON a.book_id = b.id
            WHERE a.moderator_username = ?
            ORDER BY b.id DESC
            """,
            (moderator_username,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_book_paraphrased(
    book_id: int,
    paraphrased_text: str,
    *,
    change_note: str | None = None,
    created_by: str | None = None,
) -> None:
    book_id = int(book_id)
    created_by = (created_by or "").strip() or "unknown"
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_v FROM book_versions WHERE book_id = ?",
            (book_id,),
        ).fetchone()
        next_version = int(row["next_v"]) if row else 1
        conn.execute(
            """
            INSERT INTO book_versions (book_id, version_number, paraphrased_text, change_note, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (book_id, next_version, paraphrased_text or "", change_note or "Редактирование", _now_iso(), created_by),
        )
        conn.execute(
            "UPDATE books SET paraphrased_text = ? WHERE id = ?",
            (paraphrased_text or "", book_id),
        )


def list_versions(book_id: int) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, version_number, paraphrased_text, change_note, created_at, created_by
            FROM book_versions
            WHERE book_id = ?
            ORDER BY version_number DESC
            """,
            (int(book_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_version(version_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM book_versions WHERE id = ?", (int(version_id),)).fetchone()
        return dict(row) if row else None


def restore_version(book_id: int, version_id: int, created_by: str | None = None) -> bool:
    v = get_version(version_id)
    if not v or v.get("book_id") != book_id:
        return False
    update_book_paraphrased(
        book_id,
        v["paraphrased_text"],
        change_note=f"Восстановлено из версии {v['version_number']}",
        created_by=created_by or "unknown",
    )
    return True


def add_comment(
    *,
    book_id: int,
    author: str,
    comment_text: str,
    paragraph_index: int | None = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO book_comments (book_id, author, comment_text, paragraph_index, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(book_id), (author or "").strip() or "unknown", (comment_text or "").strip(), paragraph_index, _now_iso()),
        )
        return int(cur.lastrowid)


def list_comments(book_id: int) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, author, comment_text, paragraph_index, created_at
            FROM book_comments
            WHERE book_id = ?
            ORDER BY created_at ASC
            """,
            (int(book_id),),
        ).fetchall()
        return [dict(r) for r in rows]


# --- История генерации статей ---


def add_article_to_history(
    *,
    theme: str,
    article_text: str,
    source: str,
    created_by: str,
) -> int:
    """Добавляет сгенерированную статью в историю. source: 'topic' | 'docs'."""
    theme = (theme or "").strip() or "Без темы"
    created_by = (created_by or "").strip() or "unknown"
    source = (source or "topic").strip().lower()
    if source not in ("topic", "docs"):
        source = "topic"
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO article_generation_history (theme, article_text, source, created_at, created_by, saved_book_id)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (theme, article_text or "", source, _now_iso(), created_by),
        )
        return int(cur.lastrowid)


def list_article_history(limit: int = 50) -> list[dict[str, Any]]:
    """Список статей из истории генерации, новые сверху."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, theme, article_text, source, created_at, created_by, saved_book_id
            FROM article_generation_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_article_saved(history_id: int, book_id: int) -> None:
    """Отмечает статью в истории как сохранённую в книги."""
    with db() as conn:
        conn.execute(
            "UPDATE article_generation_history SET saved_book_id = ? WHERE id = ?",
            (int(book_id), int(history_id)),
        )


# --- Учёт расхода токенов ---


def log_token_usage(
    *,
    created_by: str,
    operation: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float | None = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO token_usage
            (created_at, created_by, operation, provider, model, input_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), (created_by or "").strip() or "unknown",
             operation, provider, model, int(input_tokens), int(output_tokens), cost_usd),
        )
        return int(cur.lastrowid)


def get_token_usage_totals(period_days: int = 30) -> dict[str, Any]:
    """Суммарный расход за period_days дней."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0) AS total_input,"
            " COALESCE(SUM(output_tokens),0) AS total_output,"
            " COALESCE(SUM(cost_usd),0.0) AS total_cost,"
            " COUNT(*) AS num_requests"
            " FROM token_usage WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        return dict(row) if row else {
            "total_input": 0, "total_output": 0,
            "total_cost": 0.0, "num_requests": 0,
        }


def list_token_usage(limit: int = 50) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, created_by, operation, provider, model,"
            " input_tokens, output_tokens, cost_usd"
            " FROM token_usage ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

