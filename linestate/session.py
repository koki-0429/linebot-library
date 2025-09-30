# linestate/session.py
import functools, uuid
from .store import get_store

STORE = get_store()

def with_session(fn):
    """ハンドラ前後で自動 load/save。イベント再送（重複）も無害化。"""
    @functools.wraps(fn)
    def wrapper(event, *args, **kwargs):
        user_id = event.source.user_id

        # 再送対策: message.id（テキスト等） or postback.data+timestamp をキー化
        event_key = getattr(getattr(event, "message", None), "id", None) \
            or (getattr(getattr(event, "postback", None), "data", "") + ":" + str(getattr(event, "timestamp", "")))

        if STORE.mark_idempotent(str(event_key)):
            # 既処理イベントは無視（または同レスポンスの再送にしても良い）
            return

        sess = STORE.load(user_id)
        result = fn(user_id, sess, event, *args, **kwargs)
        STORE.save(user_id, sess)
        return result
    return wrapper

def new_pending_id():
    return str(uuid.uuid4())

def guard_postback(sess, parsed_pid):
    """pending_id が一致するポストバックだけ通す（過去ボタン無効化）"""
    return bool(sess.get("pending_id")) and parsed_pid == sess["pending_id"]
