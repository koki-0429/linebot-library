# linestate/session.py —— セッション自動管理デコレータ（再送対策なし）
import functools, uuid
from .store import get_store

STORE = get_store()

def with_session(fn):
    """ユーザーセッションを自動ロード/保存するデコレータ"""
    @functools.wraps(fn)
    def wrapper(event, *args, **kwargs):
        user_id = event.source.user_id

        # 再送対策は省略（以前のevent_key関連を削除）
        sess = STORE.load(user_id)
        result = fn(user_id, sess, event, *args, **kwargs)
        STORE.save(user_id, sess)
        return result
    return wrapper

def new_pending_id():
    """Yes/No確認用の一意なIDを生成"""
    return str(uuid.uuid4())

def guard_postback(sess, parsed_pid):
    """古いボタン無効化のための正当性チェック"""
    return bool(sess.get("pending_id")) and parsed_pid == sess["pending_id"]
