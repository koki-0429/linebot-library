# linestate/store.py
import json, os

try:
    import redis
except ImportError:
    redis = None

SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))  # 30分

class BaseStore:
    def load(self, user_id): ...
    def save(self, user_id, sess): ...
    def mark_idempotent(self, event_key): ...

class MemoryStore(BaseStore):
    def __init__(self):
        self.db = {}
        self.idem = set()

    def load(self, uid):
        return self.db.get(uid, {"i": 0, "vals": {}, "pending_id": None})

    def save(self, uid, sess):
        self.db[uid] = sess

    def mark_idempotent(self, key):
        if key in self.idem:
            return True  # 既処理
        self.idem.add(key)
        return False

class RedisStore(BaseStore):
    def __init__(self):
        assert redis, "pip install redis が必要です"
        self.r = redis.Redis(
            host=os.getenv("REDIS_HOST"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD"),
            decode_responses=True,
        )

    def _k(self, uid):  return f"sess:{uid}"
    def _ik(self, key): return f"idem:{key}"

    def load(self, uid):
        raw = self.r.get(self._k(uid))
        return json.loads(raw) if raw else {"i": 0, "vals": {}, "pending_id": None}

    def save(self, uid, sess):
        self.r.set(self._k(uid), json.dumps(sess, ensure_ascii=False), ex=SESSION_TTL)

    def mark_idempotent(self, key):
        # 既に存在すれば True（=既処理）
        return self.r.set(self._ik(key), "1", ex=SESSION_TTL, nx=True) is None

def get_store():
    if os.getenv("REDIS_HOST"):
        return RedisStore()
    return MemoryStore()
