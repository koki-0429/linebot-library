# app.py —— FlaskでLINE Botを動かす本体（SQLiteセッション対応版）
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, PostbackAction,
    TemplateSendMessage, ButtonsTemplate, StickerMessage
)

# ← 自作ライブラリをインポート
from linestate.session import with_session, new_pending_id, guard_postback

import os
import psycopg2

def _get_db_url():
    db_url = os.getenv("DATABASE_URL")
    if db_url and "sslmode=" not in db_url:
        db_url = f"{db_url}?sslmode=require"
    return db_url

def save_registration_to_db(user_id, v):
    """確定データ（名前・住所・電話番号）を登録"""
    db_url = _get_db_url()
    if not db_url:
        return
    con = psycopg2.connect(db_url)
    try:
        with con:
            with con.cursor() as cur:
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


app = Flask(__name__)

# ==== チャネル設定 ====
LINE_CHANNEL_ACCESS_TOKEN = "Lz8pLDVenpyNTFB0wx3HF8GMQYdsB58T9s82W7f9iO0VO7BheRuOMZON92Yr5l9GUikRJIPZBwmJwCCGLOVovgEK2ta+hX/YWlHcfFS8xSJ7HTRjvhm6S4mA/xcsbLYJ5sv8Ek+tX+mLeR+QYoqyVwdB04t89/1O/w1cDnyilFU="
LINE_CHANNEL_SECRET = "a4e8c0c832d864a32d06acc0354e8fd3"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ==== 入力フロー定義 ====
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

def prompt(reply_token, key):
    """次の入力を促す案内文を送信"""
    line_bot_api.reply_message(reply_token, TextSendMessage(text=LABELS[key]))

# ==== Webhook受信 ====
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# ==== テキストメッセージ処理 ====
@handler.add(MessageEvent, message=TextMessage)
@with_session
def on_text(user_id, sess, event, _dest=None):
    text = event.message.text.strip()

    # セッション初期化（初回）
    sess.setdefault("i", 0)
    sess.setdefault("vals", {})
    sess.setdefault("pending_id", None)
    sess.setdefault("prompted", False)

    # pending中は入力拒否
    if sess["pending_id"]:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="現在確認中です。Yes/Noから選択してください。")
        )
        return

    # 初回入力開始
    if not sess["prompted"] and sess["i"] == 0 and not any(sess["vals"].values()):
        prompt(event.reply_token, FLOW[sess["i"]])
        sess["prompted"] = True
        return

    # 入力受付 → 確認ボタン生成
    cur = FLOW[sess["i"]]
    sess["buffer"] = text
    pid = new_pending_id()
    sess["pending_id"] = pid

    yes = f"pid={pid}&field={cur}&ans=yes"
    no  = f"pid={pid}&field={cur}&ans=no"

    line_bot_api.reply_message(
        event.reply_token,
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

# ==== スタンプメッセージ処理 ====
@handler.add(MessageEvent, message=StickerMessage)
@with_session
def on_sticker(user_id, sess, event, _dest=None):
    if sess.get("pending_id"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="確認中はスタンプは無効です。Yes/Noを選択してください。")
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="スタンプは未対応です。テキストで入力してください。")
        )

# ==== Postback（Yes/Noボタン）処理 ====
@handler.add(PostbackEvent)
@with_session
def on_postback(user_id, sess, event, _dest=None):
    parsed = {}
    for item in event.postback.data.split("&"):
        if "=" in item:
            k, v = item.split("=", 1)
            parsed[k] = v
    pid, field, ans = parsed.get("pid"), parsed.get("field"), parsed.get("ans")

    # 古いボタンの無効化
    if not guard_postback(sess, pid):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="有効な操作ではありません。"))
        return

    cur = FLOW[sess["i"]]
    if field != cur:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="現在確認していない項目への操作です。"))
        return

    # Yes/No分岐
    if ans == "yes":
        sess["vals"][cur] = sess.get("buffer")
        sess["buffer"] = None
        sess["pending_id"] = None
        sess["prompted"] = False

        if sess["i"] == len(FLOW) - 1:
            v = sess["vals"]

            save_registration_to_db(user_id, v)

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"登録完了:\nお名前:{v.get('name')}\nご住所:{v.get('address')}\nお電話:{v.get('phone')}")
            )
            sess.clear()
        else:
            sess["i"] += 1
            prompt(event.reply_token, FLOW[sess["i"]])
    else:
        sess["buffer"] = None
        sess["pending_id"] = None
        sess["prompted"] = False
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="もう一度入力してください。"))
        prompt(event.reply_token, cur)

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=True)
