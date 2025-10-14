# linestate/store.py —— PostgreSQLでセッションを永続化（再送対策なし）
import os, json, time
import psycopg2
from contextlib import contextmanager

# Render の Postgres 画面「Connections」の “Internal Database URL” をこの環境変数に入れる
DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL is not set. Set it to your Postgres connection string.")

# Free/Starter等のRenderではSSL必須になることが多いので保険として付与
if "sslmode=" not in DB_URL:
    DB_URL = f"{DB_URL}?sslmode=require"

# セッションの自動削除秒（0で無効）
SESSION_TTL = int(os.getenv("SESSION_TTL", "0"))

def _now_epoch() -> int:
    return int(time.time())

@contextmanager
def _conn():
    con = psycopg2.connect(DB_URL)
    try:
        yield con
        con.commit()
    finally:
        con.close()

def _init():
    with _conn() as con, con.cursor() as cur:
        # TEXTで保存して json.loads/json.dumps で扱う（シンプルで方言が少ない）
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id    TEXT PRIMARY KEY,
            sess_json  TEXT NOT NULL,
            updated_at BIGINT NOT NULL
        )
        """)
_init()

class PostgresStore:
    """ユーザーごとのセッションをPostgreSQLに保存するストア"""

    def __init__(self):
        self._default = {
            "i": 0,
            "vals": {},
            "pending_id": None,
            "prompted": False,
        }

    def _cleanup_if_needed(self, con):
        if SESSION_TTL > 0:
            cutoff = _now_epoch() - SESSION_TTL
            with con.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE updated_at < %s", (cutoff,))

    def load(self, user_id):
        with _conn() as con:
            self._cleanup_if_needed(con)
            with con.cursor() as cur:
                cur.execute("SELECT sess_json FROM sessions WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    # デフォルトを“コピー”して返す（破壊的変更の影響を避ける）
                    return json.loads(json.dumps(self._default))
                try:
                    return json.loads(row[0])
                except Exception:
                    return json.loads(json.dumps(self._default))

    def save(self, user_id, sess):
        with _conn() as con:
            self._cleanup_if_needed(con)
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions (user_id, sess_json, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET sess_json = EXCLUDED.sess_json,
                                  updated_at = EXCLUDED.updated_at
                    """,
                    (user_id, json.dumps(sess, ensure_ascii=False), _now_epoch())
                )

store = PostgresStore()

def get_store():
    """linestate.session から呼ばれる取得関数"""
    return store
