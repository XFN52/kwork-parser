import sqlite3
import logging
import contextlib
from typing import Dict, Any, List, Optional, Generator, Union
from pathlib import Path

from src.models import KworkProject

logger = logging.getLogger(__name__)


class ProjectStorage:
    """
    SQLite storage for project deduplication and history tracking.
    """

    def __init__(self, db_path: str = "data/kwork_parser.db"):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_projects (
                    id TEXT PRIMARY KEY,
                    source TEXT DEFAULT 'kwork',
                    title TEXT NOT NULL,
                    price REAL,
                    category_id INTEGER,
                    offers_count INTEGER DEFAULT 0,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified INTEGER DEFAULT 1,
                    data_json TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_seen_projects_first_seen
                ON seen_projects (first_seen_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cached_proposals (
                    project_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    suggested_price INTEGER NOT NULL,
                    suggested_days INTEGER NOT NULL,
                    proposal_text TEXT NOT NULL,
                    mode_name TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_id, mode)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cached_proposals_created
                ON cached_proposals (created_at)
            """)
            
            # Check for existing table schema and migrate if needed
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(seen_projects)")
            cols = {row["name"]: row["type"].upper() for row in cur.fetchall()}
            
            if cols and (cols.get("id") == "INTEGER" or "source" not in cols):
                logger.info("Миграция таблицы seen_projects: переход на TEXT id и добавление source...")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS seen_projects_v2 (
                        id TEXT PRIMARY KEY,
                        source TEXT DEFAULT 'kwork',
                        title TEXT NOT NULL,
                        price REAL,
                        category_id INTEGER,
                        offers_count INTEGER DEFAULT 0,
                        first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notified INTEGER DEFAULT 1,
                        data_json TEXT DEFAULT ''
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO seen_projects_v2 (id, source, title, price, category_id, offers_count, first_seen_at, notified, data_json)
                    SELECT CAST(id AS TEXT), 'kwork', title, price, category_id, offers_count, first_seen_at, notified, data_json
                    FROM seen_projects
                """)
                conn.execute("DROP TABLE seen_projects")
                conn.execute("ALTER TABLE seen_projects_v2 RENAME TO seen_projects")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_projects_first_seen ON seen_projects (first_seen_at)")

            cur.execute("PRAGMA table_info(cached_proposals)")
            p_cols = {row["name"]: row["type"].upper() for row in cur.fetchall()}
            if p_cols and p_cols.get("project_id") == "INTEGER":
                logger.info("Миграция таблицы cached_proposals: переход project_id на TEXT...")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS cached_proposals_v2 (
                        project_id TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        suggested_price INTEGER NOT NULL,
                        suggested_days INTEGER NOT NULL,
                        proposal_text TEXT NOT NULL,
                        mode_name TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (project_id, mode)
                    )
                """)
                conn.execute("""
                    INSERT OR IGNORE INTO cached_proposals_v2 (project_id, mode, suggested_price, suggested_days, proposal_text, mode_name, created_at)
                    SELECT CAST(project_id AS TEXT), mode, suggested_price, suggested_days, proposal_text, mode_name, created_at
                    FROM cached_proposals
                """)
                conn.execute("DROP TABLE cached_proposals")
                conn.execute("ALTER TABLE cached_proposals_v2 RENAME TO cached_proposals")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cached_proposals_created ON cached_proposals (created_at)")

            conn.commit()

    def is_seen(self, project_id: Union[int, str]) -> bool:
        """Check if project has already been seen/processed."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM seen_projects WHERE id = ?", (str(project_id),))
            return cur.fetchone() is not None

    def mark_seen(self, project: KworkProject, notified: bool = True) -> None:
        """Record project as seen in database with full json data cache."""
        with self._get_connection() as conn:
            source = getattr(project, "source", "kwork")
            conn.execute("""
                INSERT INTO seen_projects 
                (id, source, title, price, category_id, offers_count, notified, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    title = excluded.title,
                    price = excluded.price,
                    category_id = excluded.category_id,
                    offers_count = excluded.offers_count,
                    notified = excluded.notified,
                    data_json = excluded.data_json
            """, (
                str(project.id),
                source,
                project.title,
                project.price_limit,
                project.category_id,
                project.offers_count,
                1 if notified else 0,
                project.model_dump_json(),
            ))
            conn.commit()

    def get_project(self, project_id: Union[int, str]) -> Optional[KworkProject]:
        """Retrieve cached KworkProject by ID."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT data_json FROM seen_projects WHERE id = ?", (str(project_id),))
            row = cur.fetchone()
            if row and row["data_json"]:
                try:
                    return KworkProject.model_validate_json(row["data_json"])
                except Exception as e:
                    logger.error(f"Ошибка десериализации проекта #{project_id} из БД: {e}")
            return None

    def get_recent_projects(self, limit: int = 10, source: Optional[str] = None) -> List[KworkProject]:
        """Retrieve the most recently saved projects from database, optionally filtered by source."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            if source:
                cur.execute("""
                    SELECT data_json FROM seen_projects 
                    WHERE data_json != '' AND data_json IS NOT NULL AND source = ?
                    ORDER BY first_seen_at DESC, id DESC
                    LIMIT ?
                """, (source, limit))
            else:
                cur.execute("""
                    SELECT data_json FROM seen_projects 
                    WHERE data_json != '' AND data_json IS NOT NULL
                    ORDER BY first_seen_at DESC, id DESC
                    LIMIT ?
                """, (limit,))
            projects: List[KworkProject] = []
            for row in cur.fetchall():
                try:
                    projects.append(KworkProject.model_validate_json(row["data_json"]))
                except Exception as e:
                    logger.error(f"Ошибка десериализации недавнего проекта: {e}")
            return projects



    def cleanup_old_records(self, days: int = 30) -> int:
        """Remove records older than `days` days to keep DB file compact."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM seen_projects WHERE first_seen_at < datetime('now', ?)",
                (f"-{days} days",)
            )
            deleted_count = cur.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info(f"Очищено {deleted_count} устаревших записей из базы данных (старше {days} дней).")
            return deleted_count

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics about tracked projects."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM seen_projects")
            total = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM seen_projects WHERE notified = 1")
            total_notified = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*) FROM seen_projects 
                WHERE date(first_seen_at) = date('now')
            """)
            today_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM seen_projects WHERE source = 'profi'")
            profi_count = cur.fetchone()[0]

            return {
                "total_seen": total,
                "total_notified": total_notified,
                "seen_today": today_count,
                "profi_count": profi_count,
                "kwork_count": total - profi_count,
            }

    def add_subscriber(self, chat_id: int) -> bool:
        """Register a chat ID for notifications."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
            return cur.rowcount > 0

    def remove_subscriber(self, chat_id: int) -> bool:
        """Unregister a chat ID from notifications."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_subscribers(self) -> List[int]:
        """Get all registered subscriber chat IDs."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT chat_id FROM subscribers")
            return [row[0] for row in cur.fetchall()]

    def save_proposal(self, project_id: Union[int, str], mode: str, proposal: Dict[str, Any]) -> None:
        """Saves generated proposal to prevent re-billing on retries or accidental re-clicks."""
        mode_clean = mode.lower().strip()
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO cached_proposals 
                (project_id, mode, suggested_price, suggested_days, proposal_text, mode_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(project_id, mode) DO UPDATE SET
                    suggested_price = excluded.suggested_price,
                    suggested_days = excluded.suggested_days,
                    proposal_text = excluded.proposal_text,
                    mode_name = excluded.mode_name,
                    created_at = CURRENT_TIMESTAMP
            """, (
                str(project_id),
                mode_clean,
                int(proposal.get("suggested_price", 0)),
                int(proposal.get("suggested_days", 1)),
                str(proposal.get("proposal_text", "")),
                str(proposal.get("mode_name", mode_clean.capitalize())),
            ))
            conn.commit()

    def get_cached_proposal(self, project_id: Union[int, str], mode: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached proposal for project_id and mode if available."""
        mode_clean = mode.lower().strip()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT suggested_price, suggested_days, proposal_text, mode_name, created_at
                FROM cached_proposals 
                WHERE project_id = ? AND mode = ?
            """, (str(project_id), mode_clean))
            row = cur.fetchone()
            if row and row["proposal_text"]:
                return {
                    "suggested_price": row["suggested_price"],
                    "suggested_days": row["suggested_days"],
                    "proposal_text": row["proposal_text"],
                    "mode_name": row["mode_name"],
                    "created_at": row["created_at"],
                    "is_fallback": False,
                    "is_cached": True,
                }
            return None

    def clear_cached_proposal(self, project_id: Union[int, str], mode: Optional[str] = None) -> None:
        """Deletes cached proposal(s) when explicit regeneration is requested."""
        with self._get_connection() as conn:
            if mode:
                conn.execute(
                    "DELETE FROM cached_proposals WHERE project_id = ? AND mode = ?",
                    (str(project_id), mode.lower().strip()),
                )
            else:
                conn.execute(
                    "DELETE FROM cached_proposals WHERE project_id = ?",
                    (str(project_id),),
                )
            conn.commit()
