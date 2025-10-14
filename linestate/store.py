# store.py —— SQLiteでユーザーごとの状態を永続化（再送対策なし）
import os, json, sqlite3, time
from contextlib import contextmanager

DB_PATH = os.getenv("SESSION_DB_PATH", "sessions.db")
SESSION_TTL = int(os.getenv("SESSION_TTL", "0"))  # 秒。0=無期限

def _now() -> int:
    return int(time.time())

@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def _init():
    with _conn() as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id    TEXT PRIMARY KEY,
            sess_json  TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """)
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
_init()

class SQLiteStore:
    """ユーザーごとのセッションを保存する軽量データベース"""

    def __init__(self):
        self._default = {
            "i": 0,
            "vals": {},
            "pending_id": None,
            "prompted": False,
        }

    def _cleanup_if_needed(self, con):
        if SESSION_TTL > 0:
            cutoff = _now() - SESSION_TTL
            con.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))

    def load(self, user_id):
        with _conn() as con:
            self._cleanup_if_needed(con)
            row = con.execute(
                "SELECT sess_json FROM sessions WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if not row:
                return json.loads(json.dumps(self._default))
            try:
                return json.loads(row["sess_json"])
            except json.JSONDecodeError:
                return json.loads(json.dumps(self._default))

    def save(self, user_id, sess):
        with _conn() as con:
            self._cleanup_if_needed(con)
            con.execute(
                "INSERT INTO sessions (user_id, sess_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET sess_json=excluded.sess_json, updated_at=excluded.updated_at",
                (user_id, json.dumps(sess, ensure_ascii=False), _now())
            )

store = SQLiteStore()

def get_store():
    """linestate.sessionから呼び出されるストア取得関数"""
    return store
