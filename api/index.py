"""
Finviet Zalo OA Webhook - Vercel Serverless
泡泡自动回复机器人
"""
import os
import json
import logging
import requests
import threading
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────
VERIFY_TOKEN  = os.environ.get('ZALO_VERIFY_TOKEN', 'finviet_webhook_2026')
APP_ID        = os.environ.get('ZALO_APP_ID', '')
ACCESS_TOKEN  = os.environ.get('ZALO_ACCESS_TOKEN', '')

# ── 话术库 ─────────────────────────────────────────
SCRIPTS = {
    'opening': """Chào bạn! 👋
Mình là Bong Bong (泡泡) - trợ lý tuyển dụng của KINDLITE VIET NAM.
Cảm ơn bạn đã quan tâm đến cơ hội hợp tác thanh toán di động 🇨🇳💳

Bạn muốn tìm hiểu điều gì?
1️⃣ Công việc cụ thể là gì?
2️⃣ Thu nhập như thế nào?
3️⃣ Điều kiện tham gia?
4️⃣ Tôi muốn đăng ký ngay

Nhập số hoặc gõ câu hỏi nhé 😊""",

    '1': """Công việc chính: đi thị trường khu vực bạn phụ trách 📍

→ Tìm quán ăn, cà phê, cửa hàng có khách Trung Quốc
→ Giới thiệu kết nối WeChat Pay / Alipay
→ Hỗ trợ cài đặt và theo dõi sau ký hợp đồng

✅ Tự quản lý lịch làm việc
✅ Không cần điểm danh, không cần lên văn phòng
✅ Làm việc tại Hải Phòng hoặc TP.HCM

Bạn muốn hỏi thêm gì không? 😊""",

    '2': """Thu nhập phụ thuộc loại hình hợp tác 💰

📌 Nhân viên toàn thời gian:
   Lương cơ bản + hoa hồng KPI

📌 Nhân viên bán thời gian / đại lý:
   Thu nhập = phí mở điểm mỗi cửa hàng thành công

Con số cụ thể sẽ được trao đổi trực tiếp khi bạn gặp đội ngũ của chúng tôi.

Bạn đang ở thành phố nào? (Hải Phòng / TP.HCM) 🏙️""",

    '3': """Không yêu cầu bằng cấp hay kinh nghiệm 🙌

✅ Biết tiếng Việt
✅ Thích giao tiếp, chịu đi thị trường
✅ Quen thuộc khu vực Hải Phòng hoặc TP.HCM
✅ Có xe máy

Ưu tiên: có kinh nghiệm bán hàng, biết tiếng Trung/Anh

Bạn muốn đăng ký thử không? Nhập 4️⃣ để bắt đầu 😊""",

    '4': """Tuyệt vời! 🎉 Để chúng tôi liên hệ lại với bạn, cần 3 thông tin:

👤 Họ tên của bạn là gì?
📍 Bạn đang ở thành phố nào? (Hải Phòng / TP.HCM)
📱 Số điện thoại Zalo/điện thoại của bạn?

Bạn có thể gửi cả 3 thông tin một lúc nhé, ví dụ:
「Nguyễn Văn A, TP.HCM, 0901234567」""",

    'thanks': """Cảm ơn bạn! ✅
Đội ngũ KINDLITE sẽ liên hệ với bạn trong vòng 24 giờ.

Nếu có thêm câu hỏi, cứ nhắn mình nhé 😊
Chúc bạn một ngày tốt lành! 🌟""",

    'default': """Xin lỗi, mình chưa hiểu câu hỏi của bạn 😅

Bạn có thể chọn:
1️⃣ Công việc cụ thể là gì?
2️⃣ Thu nhập như thế nào?
3️⃣ Điều kiện tham gia?
4️⃣ Tôi muốn đăng ký ngay

Hoặc nhắn bất kỳ câu hỏi nào, mình sẽ cố gắng trả lời! 🫧"""
}

# ── 用户状态（内存，无持久化）─────────────────────
user_states = {}

def get_reply(user_id, text):
    text = text.strip()
    
    # 等待注册信息
    if user_states.get(user_id) == 'waiting_info':
        # 收到包含逗号的信息，认为是注册资料
        if ',' in text or '，' in text:
            user_states[user_id] = 'done'
            return SCRIPTS['thanks']
    
    # 关键词匹配
    if any(w in text.lower() for w in ['xin chào', 'hello', 'hi', 'chào', '你好', '开始']):
        return SCRIPTS['opening']
    if text in ['1', '①']:
        return SCRIPTS['1']
    if text in ['2', '②']:
        return SCRIPTS['2']
    if text in ['3', '③']:
        return SCRIPTS['3']
    if text in ['4', '④', 'đăng ký', 'dang ky', '注册', '报名']:
        user_states[user_id] = 'waiting_info'
        return SCRIPTS['4']
    if any(w in text.lower() for w in ['lương', 'thu nhập', 'tiền', 'income', '收入', '工资']):
        return SCRIPTS['2']
    if any(w in text.lower() for w in ['làm gì', 'công việc', 'job', 'work', '工作', '做什么']):
        return SCRIPTS['1']
    if any(w in text.lower() for w in ['điều kiện', 'yêu cầu', 'condition', '条件', '要求']):
        return SCRIPTS['3']
    
    # 第一次发消息默认开场白
    if user_id not in user_states:
        user_states[user_id] = 'started'
        return SCRIPTS['opening']
    
    return SCRIPTS['default']


def _do_send_message(user_id, text):
    """实际发送消息到 Zalo API（后台执行）"""
    if not ACCESS_TOKEN:
        log.warning("No ACCESS_TOKEN set, skip sending")
        return
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {
        'access_token': ACCESS_TOKEN,
        'Content-Type': 'application/json'
    }
    payload = {
        "recipient": {"user_id": user_id},
        "message": {"text": text}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        log.info(f"Send msg to {user_id}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log.error(f"Send failed: {e}")


def send_message_async(user_id, text):
    """异步发送消息，立即返回，不阻塞 webhook 响应"""
    thread = threading.Thread(target=_do_send_message, args=(user_id, text))
    thread.start()


# ── 路由 ───────────────────────────────────────────
@app.route('/', methods=['GET'])
def index():
    return jsonify({'status': 'Finviet Zalo Webhook OK', 'time': datetime.now().isoformat()})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})


@app.route('/<path:verifier_path>', methods=['GET'])
def zalo_verify(verifier_path):
    """Zalo 域名归属验证 - 返回完整 HTML 验证文件"""
    if verifier_path.endswith('.html') and 'zalo_verifier' in verifier_path:
        # 从文件名提取 token: zalo_verifierTOKEN.html → TOKEN
        token = verifier_path.replace('zalo_verifier', '').replace('.html', '')
        html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta property="zalo-platform-site-verification" content="{token}" />
</head>
<body>
There Is No Limit To What You Can Accomplish Using Zalo!
</body>
</html>'''
        return html_content, 200, {'Content-Type': 'text/html'}
    return 'Not Found', 404


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Zalo webhook 验证 - 支持两种参数格式"""
    # Zalo 官方格式
    mode      = request.args.get('mode') or request.args.get('hub.mode')
    token     = request.args.get('VerifyToken') or request.args.get('hub.verify_token')
    challenge = request.args.get('challenge') or request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return challenge, 200
    return 'Forbidden', 403


@app.route('/webhook', methods=['POST'])
def webhook_receive():
    """接收 Zalo 推送事件 - 必须 <2s 内响应，否则 Zalo 认为失败"""
    try:
        data = request.get_json(force=True)
        log.info(f"Event: {json.dumps(data)[:200]}")

        event_name = data.get('event_name', '')

        # ✅ 立即返回 200，避免 Zalo 超时
        # 消息发送放到后台线程，不阻塞响应
        if event_name == 'user_send_text':
            user_id = data.get('sender', {}).get('id', '')
            text    = data.get('message', {}).get('text', '')
            if user_id and text:
                reply = get_reply(user_id, text)
                send_message_async(user_id, reply)

        elif event_name == 'follow':
            user_id = data.get('follower', {}).get('id', '')
            if user_id:
                send_message_async(user_id, SCRIPTS['opening'])

        elif event_name == 'unfollow':
            log.info(f"User unfollowed OA")

    except Exception as e:
        log.error(f"Webhook error: {e}")

    # 立即返回，不等待消息发送完成
    return jsonify({'status': 'ok'})
