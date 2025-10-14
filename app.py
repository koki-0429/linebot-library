# app.py —— LINE Bot（PostgreSQL保存／reply_token失効時はpushへフォールバック）
import os
import logging
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, PostbackAction,
    TemplateSendMessage, ButtonsTemplate, StickerMessage
)
import psycopg2

# 自作ライブラリ
from linestate.session import with_session, new_pending_id, guard_postback

# ===== ロギング =====
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# ===== 環境変数 =====
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== フロー定義 =====
FLOW = ["name", "address", "phone"]
LABELS = {
    "name": "お名前を入力してください。",
    "address": "ご住所を入力してください。",
    "phone": "お電話番号を入力してください。"
}
CONFIRM = {
    "name": "このお名前でよろしいですか？",
    "address": "このご住所でよろしいですか？",
    "phone": "このお電話番号でよろしいですか？"
}

def prompt(reply_token, key, user_id=None):
    """次の入力を促す案内文を送る（safe_reply 経由）"""
    msg = TextSendMessage(text=LABELS[key])
    # user_id は with_session から渡ってくるので、ここでは省略可
    if user_id is None:
        # fallback: replyのみ（postbackから呼ばれない想定）
        try:
            line_bot_api.reply_message(reply_token, msg)
        except LineBotApiError:
            pass
    else:
        safe_reply(user_id, reply_token, msg)

# ===== DBヘルパー =====
def _get_db_url():
    db_url = os.getenv("DATABASE_URL")
    if db_url and "sslmode=" not in db_url:
        db_url = f"{db_url}?sslmode=require"
    return db_url

def save_registration_to_db(user_id, v):
    """確定データ（name/address/phone）を registrations にUPSERT"""
    db_url = _get_db_url()
    if not db_url:
        app.logger.warning("DATABASE_URL is not set; skip saving registration.")
        return
    try:
        con = psycopg2.connect(db_url)
        try:
            with con:
                with con.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS registrations (
                          user_id    TEXT PRIMARY KEY,
                          name       TEXT,
                          address    TEXT,
                          phone      TEXT,
                          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute(
                        """
                        INSERT INTO registrations (user_id, name, address, phone)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id)
                        DO UPDATE SET
                          name = EXCLUDED.name,
                          address = EXCLUDED.address,
                          phone = EXCLUDED.phone,
                          updated_at = now()
                        """,
                        (user_id, v.get("name"), v.get("address"), v.get("phone"))
                    )
        finally:
            con.close()
    except Exception as e:
        app.logger.exception(f"save_registration_to_db failed: {e}")

# ===== 重要：安全返信ラッパー =====
def safe_reply(user_id, reply_token, messages):
    """
    reply_token が無効（再送・遅延など）でも push に自動フォールバック。
    /callback を落とさず、ユーザーにも必ず届く。
    """
    try:
        line_bot_api.reply_message(reply_token, messages)
    except LineBotApiError as e:
        app.logger.info(f"reply failed ({e}); trying push_message fallback.")
        try:
            if isinstance(messages, list):
                line_bot_api.push_message(user_id, messages)
            else:
                line_bot_api.push_message(user_id, [messages])
        except Exception as e2:
            app.logger.exception(f"push fallback failed: {e2}")

# ===== ルーティング =====
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ===== ハンドラ：テキスト =====
@handler.add(MessageEvent, message=TextMessage)
@with_session
def on_text(user_id, sess, event, _dest=None):
    try:
        text = event.message.text.strip()

        # セッション初期化
        sess.setdefault("i", 0)
        sess.setdefault("vals", {})
        sess.setdefault("pending_id", None)
        sess.setdefault("prompted", False)

        app.logger.info(f"user={user_id} i={sess.get('i')} pending={sess.get('pending_id')} vals={sess.get('vals')}")

        # フォールバック：未入力でpendingなしなら必ず初回プロンプト
        if not sess.get("pending_id"):
            vals = sess.get("vals", {})
            if sess.get("i", 0) == 0 and not any(vals.values()) and not sess.get("prompted", False):
                prompt(event.reply_token, FLOW[0], user_id=user_id)
                sess["prompted"] = True
                return

        # pending中はテキストをブロック
        if sess["pending_id"]:
            safe_reply(user_id, event.reply_token, TextSendMessage(text="現在確認中です。Yes/Noから選択してください。"))
            return

        # 入力受付 → 確認ボタン生成
        cur = FLOW[sess["i"]]
        sess["buffer"] = text
        pid = new_pending_id()
        sess["pending_id"] = pid

        yes = f"pid={pid}&field={cur}&ans=yes"
        no  = f"pid={pid}&field={cur}&ans=no"

        safe_reply(
            user_id, event.reply_token,
            TemplateSendMessage(
                alt_text="入力の確認",
                template=ButtonsTemplate(
                    text=f"{CONFIRM[cur]}\n\n「{text}」",
                    actions=[
                        PostbackAction(label="はい", data=yes),
                        PostbackAction(label="いいえ", data=no),
                    ]
                )
            )
        )
    except Exception as e:
        app.logger.exception(f"/callback on_text error: {e}")
        safe_reply(user_id, event.reply_token, TextSendMessage(text="ただいま処理に時間がかかっています。もう一度お試しください。"))

# ===== ハンドラ：スタンプ =====
@handler.add(MessageEvent, message=StickerMessage)
@with_session
def on_sticker(user_id, sess, event, _dest=None):
    try:
        if sess.get("pending_id"):
            safe_reply(user_id, event.reply_token, TextSendMessage(text="確認中はスタンプは無効です。Yes/Noを選択してください。"))
        else:
            safe_reply(user_id, event.reply_token, TextSendMessage(text="スタンプは未対応です。テキストで入力してください。"))
    except Exception as e:
        app.logger.exception(f"/callback on_sticker error: {e}")

# ===== ハンドラ：ポストバック（Yes/No） =====
@handler.add(PostbackEvent)
@with_session
def on_postback(user_id, sess, event, _dest=None):
    try:
        # data をパース
        parsed = {}
        for item in event.postback.data.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                parsed[k] = v
        pid, field, ans = parsed.get("pid"), parsed.get("field"), parsed.get("ans")

        # 古い/別セッションボタン無効化
        if not guard_postback(sess, pid):
            safe_reply(user_id, event.reply_token, TextSendMessage(text="有効な操作ではありません。"))
            return

        cur = FLOW[sess["i"]]
        if field != cur:
            safe_reply(user_id, event.reply_token, TextSendMessage(text="現在確認していない項目への操作です。"))
            return

        if ans == "yes":
            # 値を確定
            sess["vals"][cur] = sess.get("buffer")
            sess["buffer"] = None
            sess["pending_id"] = None
            sess["prompted"] = False

            if sess["i"] == len(FLOW) - 1:
                v = sess["vals"]

                # 確定データをDBに永続化
                save_registration_to_db(user_id, v)

                safe_reply(
                    user_id, event.reply_token,
                    TextSendMessage(text=f"登録完了:\nお名前:{v.get('name')}\nご住所:{v.get('address')}\nお電話:{v.get('phone')}")
                )
                sess.clear()
            else:
                sess["i"] += 1
                # 次項目の案内
                prompt(event.reply_token, FLOW[sess["i"]], user_id=user_id)
        else:
            # やり直し
            sess["buffer"] = None
            sess["pending_id"] = None
            sess["prompted"] = False
            safe_reply(user_id, event.reply_token, TextSendMessage(text="もう一度入力してください。"))
            prompt(event.reply_token, cur, user_id=user_id)
    except Exception as e:
        app.logger.exception(f"/callback on_postback error: {e}")
        safe_reply(user_id, event.reply_token, TextSendMessage(text="エラーが発生しました。もう一度操作をお願いします。"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=True)
