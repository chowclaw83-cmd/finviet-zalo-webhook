"""
Finviet Zalo OA Webhook - Vercel Serverless v2.0
泡泡 (Bong Bong) 自动回复机器人

新功能：
- Supabase 状态持久化（用户状态、对话历史）
- 线索入库（姓名+城市+电话 自动写入 Supabase，并更新 Zalo 备注）
- 未命中关键词记录（后台可查看并一键转成 FAQ）
- 消息日志完整记录
- 商家/业务员分流（默认商家；业务员报备姓名+电话后解锁专属FAQ）
- Zalo 备注 API：收到电话/Zalo号后自动回写用户备注
"""
import os
import json
import logging
import hmac
import hashlib
import requests
import threading
from datetime import datetime
from flask import Flask, request, jsonify

# ── 异步任务线程池（fire-and-forget，不阻塞 webhook）─────────────────────
from concurrent.futures import ThreadPoolExecutor
_bg_executor = ThreadPoolExecutor(max_workers=3)

# ── FAQ Extra 内存缓存（5分钟刷新一次，避免每次查 Supabase）─────────────
_faq_extra_cache: dict = {}
_faq_extra_cache_time: float = 0
_FAQ_CACHE_TTL_SECONDS = 300

# OpenAI SDK
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Supabase
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────
VERIFY_TOKEN   = os.environ.get('ZALO_VERIFY_TOKEN', 'finviet_webhook_2026')
APP_ID         = os.environ.get('ZALO_APP_ID', '')
ACCESS_TOKEN   = os.environ.get('ZALO_ACCESS_TOKEN', '')
OA_SECRET      = os.environ.get('ZALO_OA_SECRET', '')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
SUPABASE_URL   = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY   = os.environ.get('SUPABASE_KEY', '')

# OpenAI 客户端
if OPENAI_API_KEY and OPENAI_AVAILABLE:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY,
        base_url="https://api.gptsapi.net/v1"
    )
else:
    openai_client = None

# Supabase 客户端
_supabase: "Client | None" = None

def get_supabase():
    global _supabase
    if _supabase is None and SUPABASE_AVAILABLE and SUPABASE_URL and SUPABASE_KEY:
        try:
            _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as e:
            log.error(f"Supabase init error: {e}")
    return _supabase

# ════════════════════════════════════════════════════════════
# Supabase 持久化辅助函数
# 表结构见：/docs/supabase_schema.sql（本文件末尾附）
# ════════════════════════════════════════════════════════════

def db_get_user_state(user_id: str) -> dict:
    """获取用户状态，返回 dict 或空 dict"""
    sb = get_supabase()
    if not sb:
        return {}
    try:
        res = sb.table('zalo_user_states').select('*').eq('user_id', user_id).single().execute()
        return res.data or {}
    except Exception:
        return {}


def db_upsert_user_state(user_id: str, updates: dict):
    """更新或创建用户状态（fire-and-forget）"""
    def _do():
        sb = get_supabase()
        if not sb:
            return
        try:
            row = {'user_id': user_id, 'updated_at': datetime.utcnow().isoformat(), **updates}
            sb.table('zalo_user_states').upsert(row, on_conflict='user_id').execute()
        except Exception as e:
            log.error(f"db_upsert_user_state error: {e}")
    _bg_executor.submit(_do)


def db_log_message(user_id: str, direction: str, text: str,
                   matched_faq: str = None, matched_type: str = None,
                   user_type: str = 'merchant'):
    """记录消息日志（fire-and-forget，不阻塞 webhook）"""
    def _do():
        sb = get_supabase()
        if not sb:
            return
        try:
            sb.table('zalo_message_logs').insert({
                'user_id': user_id,
                'direction': direction,
                'text': text[:1000],
                'matched_faq': matched_faq,
                'matched_type': matched_type,
                'user_type': user_type,
                'created_at': datetime.utcnow().isoformat(),
            }).execute()
        except Exception as e:
            log.error(f"db_log_message error: {e}")
    _bg_executor.submit(_do)


def db_log_unmatched(user_id: str, text: str, user_type: str = 'merchant'):
    """记录未命中关键词（fire-and-forget）"""
    def _do():
        sb = get_supabase()
        if not sb:
            return
        try:
            sb.table('zalo_unmatched_queries').insert({
                'user_id': user_id,
                'text': text[:500],
                'user_type': user_type,
                'status': 'pending',
                'created_at': datetime.utcnow().isoformat(),
            }).execute()
        except Exception as e:
            log.error(f"db_log_unmatched error: {e}")
    _bg_executor.submit(_do)


def db_save_lead(user_id: str, name: str, city: str, phone: str, user_type: str = 'merchant'):
    """保存线索（fire-and-forget）"""
    def _do():
        sb = get_supabase()
        if not sb:
            return
        try:
            sb.table('zalo_leads').upsert({
                'user_id': user_id,
                'name': name,
                'city': city,
                'phone': phone,
                'user_type': user_type,
                'status': 'new',
                'updated_at': datetime.utcnow().isoformat(),
                'created_at': datetime.utcnow().isoformat(),
            }, on_conflict='user_id').execute()
        except Exception as e:
            log.error(f"db_save_lead error: {e}")
    _bg_executor.submit(_do)


def db_get_faq_extra() -> dict:
    """从 Supabase 读取后台新增的 FAQ（5分钟缓存，避免每次查库）"""
    global _faq_extra_cache, _faq_extra_cache_time
    import time
    now = time.time()
    if _faq_extra_cache and (now - _faq_extra_cache_time) < _FAQ_CACHE_TTL_SECONDS:
        return _faq_extra_cache
    sb = get_supabase()
    if not sb:
        return _faq_extra_cache or {}
    try:
        res = sb.table('zalo_faq_extra').select('keyword,answer').eq('active', True).execute()
        _faq_extra_cache = {row['keyword'].lower(): row['answer'] for row in (res.data or [])}
        _faq_extra_cache_time = now
        return _faq_extra_cache
    except Exception:
        return _faq_extra_cache or {}
    except Exception as e:
        log.error(f"db_get_faq_extra error: {e}")
        return {}


# ════════════════════════════════════════════════════════════
# Zalo API：更新用户备注
# ════════════════════════════════════════════════════════════

def update_zalo_tag(user_id: str, tag_name: str):
    """给 Zalo 用户打标签（tag）"""
    if not ACCESS_TOKEN:
        return
    url = "https://openapi.zalo.me/v2.0/oa/tag/tagfollower"
    headers = {'access_token': ACCESS_TOKEN, 'Content-Type': 'application/json'}
    try:
        r = requests.post(url, headers=headers,
                          json={"follower_id": user_id, "tag_name": tag_name},
                          timeout=10)
        log.info(f"tag user {user_id} as '{tag_name}': {r.status_code}")
    except Exception as e:
        log.error(f"update_zalo_tag error: {e}")


def update_zalo_note(user_id: str, note: str):
    """更新 Zalo OA 关注者备注（显示在 OA 后台粉丝列表）"""
    if not ACCESS_TOKEN:
        return
    url = "https://openapi.zalo.me/v2.0/oa/follower"
    headers = {'access_token': ACCESS_TOKEN, 'Content-Type': 'application/json'}
    try:
        r = requests.post(url, headers=headers,
                          json={"follower_id": user_id, "name": note},
                          timeout=10)
        log.info(f"update note for {user_id}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log.error(f"update_zalo_note error: {e}")


# ════════════════════════════════════════════════════════════
# 签名验证
# ════════════════════════════════════════════════════════════

def verify_zalo_mac(data: dict, timestamp: str, signature: str) -> bool:
    if not OA_SECRET or not signature:
        return True
    body_for_sign = {k: v for k, v in data.items() if k != 'signature'}
    body_json = json.dumps(body_for_sign, separators=(',', ':'))
    raw = str(APP_ID) + body_json + timestamp + OA_SECRET
    expected = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    ok = hmac.compare_digest(expected, signature)
    if not ok:
        log.warning(f"MAC mismatch: expected={expected}, got={signature}")
    return ok


# ════════════════════════════════════════════════════════════
# FAQ 数据库（商家版）
# ════════════════════════════════════════════════════════════

FAQ_KB = {
    # 一、关于 Finviet / 公司资质
    "finviet": """Finviet là công ty thanh toán được Ngân hàng Nhà nước Việt Nam (SBV) cấp phép hoạt động thanh toán. Giấy phép số: ĐVCNTT-001.

✅ 100% hợp pháp
✅ Giải pháp thanh toán quốc tế cho khách Trung/Hàn/Nhật
✅ Thanh toán qua hệ thống NAPAS - cùng cấp với UnionPay (Trung Quốc), KFTC (Hàn Quốc)

Bạn cần xem giấy phép không? 😊""",

    "công ty": """Finviet là công ty thanh toán được cấp phép bởi Ngân hàng Nhà nước Việt Nam.

✅ Kinh doanh hợp pháp 100%
✅ Hợp tác với NAPAS - hệ thống thanh toán quốc gia
✅ Hỗ trợ WeChat Pay, Alipay, KakaoPay

Bạn muốn tìm hiểu thêm điều gì? 🫧""",

    "giấy phép": """Có! Finviet được Ngân hàng Nhà nước Việt Nam cấp phép hoạt động thanh toán. Đây là giấy phép chính thức, không phải dịch vụ lậu hay chui đâu! ✅

Nhờ giấy phép này mà tiền của khách quốc tế được bảo vệ an toàn qua hệ thống ngân hàng nhà nước. Bạn yên tâm nhé! 😊""",

    "牌照": """Có giấy phép chính thức từ Ngân hàng Nhà nước Việt Nam (SBV). Không phải app lậu! ✅

Giấy phép này đảm bảo:
- Tiền được thanh toán qua NAPAS (cùng cấp UnionPay)
- Khách hàng được bảo vệ quyền lợi
- An toàn pháp lý cho cửa hàng bạn

Cần xem thêm thông tin không? 🫧""",

    "an toàn": """An toàn tuyệt đối! Finviet được Ngân hàng Nhà nước cấp phép, tiền đi qua hệ thống NAPAS - cùng hệ thống với UnionPay (Trung Quốc) và KFTC (Hàn Quốc).

✅ Tiền không bao giờ mất
✅ Thanh toán tự động qua ngân hàng
✅ Có hợp đồng 3 bên bảo vệ quyền lợi

Bạn hoàn toàn yên tâm nhé! 😊""",

    # 二、收中日韩游客的钱
    "khách trung quốc": """Bên em đang giúp anh/chị giải quyết một vấn đề rất thực tế: khách Trung Quốc, Hàn Quốc, Nhật Bản họ quen thanh toán không tiền mặt bằng Alipay, WeChat, Kakao nên điện thoại lúc nào cũng có mấy hình thức này.

Nếu cửa hàng mình nhận được mấy hình thức này, khách có thể quét mã và thanh toán ngay tại quán mình.

Cách anh/chị đang nhận tiền hiện tại không bị ảnh hưởng gì hết, bên em chỉ bổ sung thêm một cách nhận tiền từ khách nước ngoài thôi.

Mình cứ dùng thử trước, có khách là có thêm doanh thu, còn nếu khách tới mà không thanh toán được thì họ sẽ đi chỗ khác.""",

    "thanh toán quốc tế": """Finviet giúp bạn nhận tiền từ khách du lịch Trung Quốc, Hàn Quốc, Nhật Bản một cách dễ dàng và hợp pháp.

🌏 WeChat Pay - 微信支付 (Trung Quốc)
🌏 Alipay - 支付宝 (Trung Quốc)
🌏 KakaoPay (Hàn Quốc)
🌏 Thẻ quốc tế Visa/Mastercard

Khách quét mã QR → Tiền tự động về tài khoản ngân hàng của bạn trong ngày! ✅

Bạn ở khu vực nào? Hải Phòng hay TP.HCM? 🏙️""",

    "wechat": """Có! Finviet hỗ trợ WeChat Pay (微信支付) - ứng dụng thanh toán phổ biến nhất Trung Quốc.

📱 Khách Trung Quốc mở WeChat → Quét mã QR của bạn → Thanh toán ngay lập tức
💰 Tiền về tài khoản ngân hàng VNĐ qua NAPAS

Không cần gì thêm, cứ quét là xong! 😊""",

    "alipay": """Có! Finviet hỗ trợ Alipay (支付宝) - ví điện tử lớn nhất Trung Quốc.

📱 Khách Alipay quét mã QR → Tiền tự động về tài khoản ngân hàng VNĐ
✅ 100% hợp pháp qua NAPAS

Bạn đăng ký đi, rất đơn giản! 🫧""",

    "kakao": """Có! Finviet hỗ trợ KakaoPay - ứng dụng thanh toán phổ biến nhất Hàn Quốc.

🇰🇷 Khách Hàn Quốc dùng KakaoPay quét mã → Thanh toán ngay
💰 Tiền về tài khoản VNĐ qua hệ thống ngân hàng

Đăng ký ngay hôm nay! 😊""",

    # 三、钱怎么到账 / NAPAS / 银行
    "tiền về": """【多久到账？】
用 ECO 的话，及时到账；
本地 VietQR，当天 23:00 之前收款，T+1，即第二天到账；
用跨境 VietQR Global，当天 23:00 之前收款，T+2，即第三天到账。

Bao lâu nhận tiền？
Dùng ECO thì nhận tiền ngay；
VietQR nội địa, thu tiền trước 23:00 cùng ngày, T+1, tức ngày hôm sau；
VietQR Global quốc tế, thu tiền trước 23:00 cùng ngày, T+2, tức ngày thứ ba.""",

    "ngân hàng": """Tiền thanh toán được xử lý qua NAPAS - hệ thống thanh toán quốc gia của Việt Nam, rồi tự động chuyển vào tài khoản ngân hàng của bạn.

🏦 Hệ thống NAPAS tương đương UnionPay (Trung Quốc) hoặc KFTC (Hàn Quốc)
✅ An toàn tuyệt đối
✅ Không có rủi ro mất tiền

Bạn dùng ngân hàng nào? Vietcombank, VietinBank, BIDV...? 😊""",

    "napas": """Tiền sẽ đi qua hệ thống Ngân hàng Nhà nước và chuyển thẳng về tài khoản ngân hàng của anh/chị.

Bên em dùng hệ thống NAPAS. Nếu là chủ Trung Quốc thì có thể hiểu giống UnionPay. Nếu là chủ Hàn Quốc thì giống KFTC.

VietQR là mã thanh toán tiêu chuẩn thu tiền nội địa của NAPAS mà mình hay thấy. Còn mã bên em là VietQR Global: dùng để nhận tiền từ khách quốc tế, tức là tiêu chuẩn thu tiền quốc tế của NAPAS.

Người nước ngoài quét mã thanh toán, tiền đi qua hệ thống Ngân hàng Nhà nước Việt Nam, sẽ tự động chuyển vào tài khoản ngân hàng của anh/chị. Nếu tải ECO, bạn xem được số dư real-time như MomoPay/ZaloPay.""",

    "vietqr": """VietQR là mã thanh toán tiêu chuẩn thu tiền nội địa của NAPAS mà mình hay thấy. Còn mã bên em là VietQR Global: dùng để nhận tiền từ khách quốc tế, tức là tiêu chuẩn thu tiền quốc tế của NAPAS.

Người nước ngoài quét mã thanh toán, tiền đi qua hệ thống Ngân hàng Nhà nước Việt Nam, sẽ tự động chuyển vào tài khoản ngân hàng của anh/chị.""",

    "eco": """ECO là ứng dụng quản lý giao dịch của Finviet, giúp bạn:

📱 Xem số dư tài khoản real-time (như Momo/ZaloPay)
📊 Theo dõi lịch sử giao dịch
💰 Biết được khách nào thanh toán, bao nhiêu tiền

Sau khi ký hợp đồng, đội ngũ Finviet sẽ hướng dẫn bạn tải và đăng ký ECO. 😊""",

    "đến chưa": """Bạn yên tâm! Tiền sẽ tự động vào tài khoản ngân hàng qua hệ thống NAPAS.

📱 Nếu tải ECO, bạn xem được số dư real-time
📲 Sau khi ký hợp đồng, bạn sẽ nhận được thông báo tài khoản

Cần kiểm tra giao dịch cụ thể, liên hệ đội ngũ hỗ trợ nhé! 🫧""",

    # 四、费用 / 押金 / 手续费
    "đặt cọc": """Không cần đặt cọc! Bạn đăng ký hoàn toàn miễn phí. ✅

💰 Phí giao dịch chỉ 1.5% - tính luôn vào thanh toán, bạn không phải trả thêm gì cả

Cứ yên tâm, không mất đồng nào khi đăng ký! 😊""",

    "phí": """Phí giao dịch chỉ 1.5% mỗi giao dịch thành công. Không có phí đăng ký, phí bảo trì, hay phí ẩn nào cả! ✅

💡 Ví dụ: Khách thanh toán 100元 (≈ 377,000 VNĐ)
→ Bạn nhận ~372,000 VNĐ (đã trừ 1.5%)
→ Không phải trả thêm bất kỳ khoản nào

Rất đơn giản và minh bạch! 😊""",

    "tiền": """Bạn không mất tiền để đăng ký! Không đặt cọc, không phí ẩn. ✅

💰 Chỉ khi có giao dịch thành công thì mới có phí 1.5%
💰 Nếu không có khách quốc tế thanh toán thì không mất gì cả

Cứ yên tâm đăng ký dùng thử nhé! 😊""",

    "thu nhập": """Thu nhập của bạn = tổng số tiền giao dịch từ khách quốc tế trừ đi 1.5% phí giao dịch.

💡 Ví dụ:
- Khách Trung Quốc thanh toán 500元 = 1,885,000 VNĐ
- Bạn nhận ~1,858,000 VNĐ (đã trừ 1.5%)
→ Mỗi khách như vậy, bạn thu thêm được gần 2 triệu!

Cửa hàng nào có nhiều khách Trung/Hàn/Nhật thì thu nhập càng cao! 🫧""",

    # 五、签约流程 / 资料
    "hợp đồng": """Hợp đồng là hợp đồng 3 bên giữa: Finviet + Cửa hàng của bạn + Ngân hàng/NAPAS.

📝 Các thông tin cần chuẩn bị:
• Giấy phép kinh doanh (đăng ký kinh doanh)
• Chứng minh nhân dân / Căn cước công dân / Hộ chiếu (của người đại diện)
• Số tài khoản ngân hàng
• Số điện thoại liên hệ

✅ Hợp đồng có điều khoản rõ ràng, bảo vệ quyền lợi của bạn
✅ Đội ngũ KINDLITE sẽ hỗ trợ ký hợp đồng tận nơi

Bạn ở Hải Phòng hay TP.HCM để đội ngũ liên hệ hỗ trợ nhé! 😊""",

    "ký hợp đồng": """Đội ngũ KINDLITE sẽ đến gặp bạn trực tiếp để ký hợp đồng, rất đơn giản!

📋 Thông tin cần chuẩn bị:
• Giấy phép kinh doanh
• CMND/CCCD/Hộ chiếu (người đại diện)
• Số tài khoản ngân hàng

⏱️ Thời gian ký: khoảng 15-30 phút
✅ Không mất phí ký hợp đồng

Bạn đăng ký thông tin để đội ngũ liên hệ nhé! Nhập số 4️⃣ để đăng ký 🫧""",

    "đăng ký": """Tuyệt vời! 🎉 Để đội ngũ KINDLITE liên hệ hỗ trợ ký hợp đồng tận nơi, mình cần 3 thông tin:

👤 Họ tên của bạn:
📍 Thành phố (Hải Phòng / TP.HCM):
📱 Số điện thoại Zalo:

Gửi cả 3 thông tin một lần nhé, ví dụ:
「Nguyễn Văn A, TP.HCM, 0901234567」

Chúng tôi sẽ liên hệ trong vòng 24 giờ! 😊""",

    "giấy tờ": """Để ký hợp đồng 3 bên, bạn cần chuẩn bị:

📋 BẮT BUỘC:
• Giấy phép kinh doanh (đăng ký kinh doanh)
• CMND / CCCD / Hộ chiếu (của người ký hợp đồng)
• Số tài khoản ngân hàng (VD: Vietcombank, VietinBank...)

📋 CÓ THỂ CẦN THÊM (tùy trường hợp):
• Giấy ủy quyền (nếu người ký không phải đại diện pháp luật)

Không cần công chứng gì phức tạp, đội ngũ sẽ hỗ trợ bạn! 😊""",

    "không biết chữ": """Không sao! Nếu bạn không biết chữ hoặc cần người đọc hợp đồng giải thích, đội ngũ KINDLITE sẽ hỗ trợ đọc và giải thích từng điều khoản cho bạn trước khi ký. ✅

📝 Bạn chỉ cần ký hoặc điểm chỉ vào hợp đồng
💡 Đội ngũ sẽ giải thích rõ quyền lợi và nghĩa vụ của bạn

Bạn hoàn toàn yên tâm nhé! 😊""",

    "ủy quyền": """Giấy ủy quyền cần khi: người nhận tiền/thực hiện giao dịch ECO không phải là người đại diện pháp luật trên giấy phép kinh doanh.

📋 Nếu ví ECO nhận tiền thuộc về người được ủy quyền:
→ Cần ký giấy ủy quyền

📋 Nếu ví ECO chỉ dùng nhận thông báo (tiền về tài khoản ngân hàng):
→ Không cần giấy ủy quyền, chỉ cần KYC bình thường

Đội ngũ KINDLITE sẽ hướng dẫn cụ thể khi ký hợp đồng nhé! 😊""",

    "thay đổi": """Nếu thay đổi số ví ECO nhận tiền hoặc số nhận thông báo, chỉ cần ký phụ lục thay đổi, không cần ký lại toàn bộ hợp đồng. ✅

📋 Nếu ví chỉ nhận thông báo (không nhận tiền):
→ KYC bình thường, không cần ủy quyền

📋 Nếu ví nhận tiền (không phải đại diện pháp luật):
→ Cần giấy ủy quyền

Liên hệ đội ngũ KINDLITE để được hỗ trợ thủ tục nhé! 🫧""",

    # 六、收款方式 / ZaloPay / MoMo 对比
    "momo": """MomoPay và ZaloPay rất tiện lợi nhưng chỉ nhận được tiền từ khách Việt Nam thôi! 🇻🇳

🌏 Finviet giúp bạn NHẬN THÊM tiền từ khách:
• WeChat Pay 🇨🇳 (Trung Quốc)
• Alipay 🇨🇳 (Trung Quốc)
• KakaoPay 🇰🇷 (Hàn Quốc)
• Thẻ quốc tế 💳

💡 Dùng Finviet + Momo = nhận đủ cả khách Việt lẫn khách quốc tế!

Bạn đăng ký thêm Finviet để tăng thu nhập nhé! 😊""",

    "zalopay": """ZaloPay nhận tiền Việt Nam rất tốt, nhưng không nhận được khách Trung/Hàn/Nhật! 🇻🇳

🌏 Finviet là giải pháp bổ sung để nhận thêm:
• Khách Trung Quốc → WeChat Pay 🇨🇳
• Khách Hàn Quốc → KakaoPay 🇰🇷
• Khách Nhật Bản → Thẻ quốc tế 💳

✅ Không ảnh hưởng gì đến ZaloPay hiện tại của bạn
✅ Có thêm thu nhập từ khách quốc tế

Đăng ký đi, hoàn toàn miễn phí! 😊""",

    "quét mã": """Rất đơn giản! Sau khi đăng ký, bạn sẽ nhận được mã QR thanh toán quốc tế (VietQR Global).

📱 Khi khách Trung/Hàn/Nhật đến:
1️⃣ Khách mở ứng dụng (WeChat/Alipay/KakaoPay)
2️⃣ Quét mã QR VietQR Global của bạn
3️⃣ Xác nhận thanh toán bằng tiền tệ của họ
4️⃣ Tiền tự động quy đổi và về tài khoản ngân hàng VNĐ

💡 Bạn không cần làm gì thêm, cứ treo mã QR là có khách thanh toán! 😊""",

    "sử dụng": """Sau khi ký hợp đồng, bạn sẽ nhận được:

📱 Mã QR thanh toán quốc tế (VietQR Global)
📱 Ứng dụng ECO để theo dõi giao dịch (tùy đăng ký)

📋 Cách dùng:
1️⃣ Treo/mở mã QR thanh toán tại quầy
2️⃣ Khách quốc tế quét mã bằng app của họ
3️⃣ Tiền tự động về tài khoản ngân hàng

✅ Không cần bấm gì, không cần xác nhận thủ công!

Bạn đăng ký để đội ngũ hỗ trợ nhé! 🫧""",

    # 七、风险 / 安全 / 合法
    "lừa đảo": """【是否合法？】
越南国家银行发的牌照，正规业务

Có hợp pháp không？
Được Ngân hàng Nhà nước Việt Nam cấp phép, nghiệp vụ chính quy

【是否安全？】
越南国家银行（SBV）体系结算，VietQR 执行标准，钱是走银行体系，不在我们手里，最终是结算到你自己的银行账户

Có an toàn không？
Thanh toán qua hệ thống Ngân hàng Nhà nước Việt Nam（SBV）, tiêu chuẩn VietQR, tiền đi qua hệ thống ngân hàng, không ở trong tay chúng em, cuối cùng chuyển vào tài khoản ngân hàng của anh / chị""",

    "tiền không về": """Tiền sẽ không bao giờ mất! Tiền đi qua hệ thống NAPAS - hệ thống thanh toán điện tử quốc gia của Việt Nam, được Ngân hàng Nhà nước bảo đảm. 🏦

✅ Giao dịch tự động, không qua trung gian
✅ Có hợp đồng 3 bên bảo vệ quyền lợi
✅ Nếu có vấn đề gì, có cơ chế khiếu nại qua cổng thanh toán

Bạn hoàn toàn yên tâm nhé! 😊""",

    "rủi ro": """Rủi ro gần như bằng 0! Finviet là công ty thanh toán chính thức được cấp phép. ✅

📋 Bảo vệ 3 lớp:
1️⃣ NAPAS (Ngân hàng Nhà nước) - tiền đi qua hệ thống ngân hàng
2️⃣ Hợp đồng 3 bên - quyền lợi rõ ràng
3️⃣ Cổng thanh toán quốc tế (WeChat/Alipay) - có cơ chế khiếu nại

💡 Điều duy nhất cần lưu ý: hợp đồng có điều khoản bồi thường nếu cửa hàng giao hàng không đúng, lừa đảo khách - điều này bảo vệ khách hàng chân chính như bạn! 😊""",

    "điều khoản": """【商家如果不诚信经营，比如交货货不对版，或者已经收款不履行合同/不提供服务/销售假冒产品等，客户会通过支付平台（比如支付宝，微信支付，云闪付等平台）申述，并已经提供充足的证据，届时我们会递交给越南当地的法院联合银行来跟商家交涉。】

Thương nhân nếu kinh doanh không đúng như cam kết, giao hàng không đúng, đã nhận tiền nhưng không cung cấp dịch vụ, bán hàng giả... thì khách sẽ khiếu nại qua cổng thanh toán (Alipay, WeChat Pay, UnionPay...) và có đủ bằng chứng, lúc đó sẽ xử lý theo quy định, kết hợp Tòa án địa phương Việt Nam.""",

    # 八、其他问题
    "thanh toán khi nào": """【多久到账？】
用 ECO 的话，及时到账；
本地 VietQR，当天 23:00 之前收款，T+1，即第二天到账；
用跨境 VietQR Global，当天 23:00 之前收款，T+2，即第三天到账。

Bao lâu nhận tiền？
Dùng ECO thì nhận tiền ngay；
VietQR nội địa, thu tiền trước 23:00 cùng ngày, T+1, tức ngày hôm sau；
VietQR Global quốc tế, thu tiền trước 23:00 cùng ngày, T+2, tức ngày thứ ba.""",

    "không có khách": """Nếu cửa hàng bạn chưa có nhiều khách Trung/Hàn/Nhật, cứ đăng ký trước đi! 💡

✅ Không mất phí đăng ký
✅ Không ảnh hưởng gì đến cách thu tiền hiện tại
✅ Có khách đến là có thêm thu nhập ngay

🌏 Du lịch Việt Nam đang tăng mạnh, đặc biệt khách Trung Quốc đang quay lại sau dịch - đây là thời điểm tốt nhất để đăng ký!

Bạn đăng ký thông tin để đội ngũ liên hệ hỗ trợ nhé! 🫧""",

    "thay đổi thông tin": """Nếu cửa hàng thay đổi thông tin (số tài khoản, số điện thoại, địa chỉ...), chỉ cần ký phụ lục bổ sung, không cần ký lại toàn bộ hợp đồng. ✅

📋 Các trường hợp cụ thể:
• Thay đổi số ví ECO nhận tiền/thông báo → Ký phụ lục thay đổi
• Thay đổi người đại diện → Cần giấy ủy quyền mới + phụ lục
• Thay đổi địa chỉ → Thông báo với đội ngũ Finviet

Liên hệ đội ngũ KINDLITE để được hỗ trợ thủ tục nhanh chóng nhé! 😊""",

    "bao lâu": """Từ lúc đăng ký đến lúc có mã QR thanh toán, thường chỉ mất khoảng 3-7 ngày làm việc. ⏱️

📋 Quy trình:
1️⃣ Bạn gửi thông tin đăng ký → 2️⃣ KINDLITE liên hệ xác nhận → 3️⃣ Ký hợp đồng tận nơi → 4️⃣ Nhận mã QR và bắt đầu sử dụng!

Bạn gửi thông tin đăng ký luôn đi! Nhập 4️⃣ 🫧""",

    "cần gì": """Bạn chỉ cần chuẩn bị:

✅ Giấy phép kinh doanh (đăng ký kinh doanh)
✅ CMND / CCCD / Hộ chiếu (người đại diện)
✅ Số tài khoản ngân hàng (mang theo thẻ ATM là được)
✅ Điện thoại / Zalo để liên hệ

❌ Không cần đặt cọc
❌ Không cần công chứng phức tạp
❌ Không cần có nhiều khách quốc tế sẵn

Đăng ký thôi! Nhập 4️⃣ để bắt đầu 😊""",

    # 九、兼职/全职（补全缺失答案）
    "bán thời gian": """Làm bán thời gian (part-time) hoàn toàn được! ✅

📌 Nhân viên bán thời gian / đại lý tự do:
• Tự sắp xếp lịch làm việc
• Không cần điểm danh, không cần lên văn phòng
• Thu nhập = phí mở điểm mỗi cửa hàng thành công ký hợp đồng

💡 Phù hợp với bạn đang có việc làm khác và muốn có thêm thu nhập!

Bạn muốn biết thêm về mức thu nhập không? Nhập 2️⃣ 😊""",

    "toàn thời gian": """Làm toàn thời gian (full-time) có thu nhập ổn định hơn! 💼

📌 Nhân viên toàn thời gian:
• Lương cơ bản hàng tháng
• Hoa hồng KPI theo kết quả
• Được đào tạo bài bản và hỗ trợ từ đội ngũ

✅ Cơ hội thăng tiến lên A-level agent (đại lý cấp A)
✅ Làm việc tại Hải Phòng hoặc TP.HCM

Bạn muốn tìm hiểu thêm? Nhập 4️⃣ để đăng ký 🫧""",
}


# ════════════════════════════════════════════════════════════
# 业务员专属 FAQ（报备后解锁）
# ════════════════════════════════════════════════════════════

SALESMAN_FAQ_KB = {
    "hoa hồng": """【业务员专属 - 佣金说明】

📌 兼职/自由代理：
• 每成功签约1家商户 → 领取开点佣金（具体金额培训时确认）
• 无底薪，纯佣金制

📌 全职员工：
• 底薪 + KPI 佣金
• 每月目标：XX家商户（入职培训时确认）

⚠️ 佣金结构属于内部数据，请勿对外透露
有问题请直接联系你的上级或城市管理员 😊""",

    "kpi": """【业务员专属 - KPI 指标】

📊 核心KPI：
• 月度新增有效商户数
• 商户活跃率（开通后30天内有交易）
• 覆盖街区数量

📋 有效商户定义：
• 成功签约 + 系统开通 + 30天内产生交易

详细指标请参考培训手册或联系城市管理员 😊""",

    "quy trình": """【业务员专属 - 展业流程】

📋 标准展业步骤：
1️⃣ 在CRM系统报备目标商户（防止抢单）
2️⃣ 拜访商户 → 介绍Finviet服务
3️⃣ 商户有意向 → 预约签约时间
4️⃣ 协助准备资料（营业执照、身份证、银行账户）
5️⃣ 陪同或独立完成签约
6️⃣ 协助安装ECO、测试收款
7️⃣ 在CRM系统标记为"已开户"

⚠️ 注意：必须先在系统报备，否则可能被其他业务员抢占
有问题请联系你的城市管理员 😊""",

    "hệ thống": """【业务员专属 - 后台系统】

🖥️ CRM系统（防撞报备系统）：
• 地址：你的城市管理员会给你开通账号
• 功能：报备商户、查看街区、标记开户状态

📱 使用指南：
• 先登录CRM系统，输入商户名称和地址报备
• 报备成功后进入"保护期"，其他业务员无法抢占
• 保护期内完成签约，标记为"已开户"

遇到系统问题请联系城市管理员 😊""",

    "báo cáo": """【业务员专属 - 如何报备】

📋 报备方式：
• 登录CRM系统 → 新建报备 → 填写商户信息

📍 报备必填：
• 商户名称
• 详细地址
• 联系电话（如有）

⚠️ 报备即进入保护期，请确认你有能力在保护期内完成签约
保护期结束前未签约会自动释放到公共池

有问题请联系城市管理员 😊""",

    "đào tạo": """【业务员专属 - 培训资料】

📚 培训手册：
• 你的城市管理员应已向你发送PDF培训手册
• 包含：产品介绍、话术、常见问题、操作流程

📋 上岗前必读：
1. 产品介绍（第1-2章）
2. 展业话术（第3章）
3. 签约流程（第4章）
4. CRM系统操作（第5章）

如果没有收到培训手册，请联系你的城市管理员 😊""",
}

SALESMAN_FAQ_KEYWORDS = {
    "hoa hồng": [
        "hoa hồng của tôi", "tiền hoa hồng bao nhiêu", "commission",
        "tôi nhận được bao nhiêu", "佣金多少", "我能赚多少", "开点费",
        "收入bao nhiêu", "phần trăm của tôi",
    ],
    "kpi": [
        "kpi", "chỉ tiêu", "target", "目标", "指标", "考核",
        "mỗi tháng phải", "chỉ tiêu tháng",
    ],
    "quy trình": [
        "quy trình", "làm như thế nào", "bắt đầu từ đâu", "các bước",
        "展业流程", "怎么做", "步骤", "流程",
        "quy trình làm việc", "quy trình ký",
    ],
    "hệ thống": [
        "hệ thống", "crm", "phần mềm", "app nội bộ",
        "系统", "后台", "crm系统", "怎么用系统",
        "đăng nhập vào đâu", "link hệ thống",
    ],
    "báo cáo": [
        "báo cáo", "report", "báo cáo cửa hàng", "báo cáo merchant",
        "报备", "报备系统", "怎么报备", "先报备",
        "đăng ký cửa hàng", "ghi tên cửa hàng",
    ],
    "đào tạo": [
        "đào tạo", "training", "tài liệu", "hướng dẫn",
        "培训", "培训材料", "手册", "操作手册",
        "tôi mới vào", "mới tham gia", "mới làm",
    ],
}


# ════════════════════════════════════════════════════════════
# 关键词扩展层 → FAQ_KB 答案映射
# ════════════════════════════════════════════════════════════

FAQ_KEYWORDS = {
    "finviet là gì": [
        "finviet là gì", "finviet la gi", "finviet", "finviet vietnam",
        "công ty finviet", "công ty của bạn", "công ty là gì",
        "你们是什么公司", "finviet是什么", "giới thiệu công ty",
        "công ty", "công ty của", "bạn là công ty nào",
        "what is finviet", "finviet company",
    ],
    "giấy phép": [
        "giấy phép", "có giấy phép", "có phép không", "được cấp phép",
        "chứng chỉ", "牌照", "有牌照吗", "合法吗", "正规吗",
        "có hợp pháp không", "legal", "licensed", "authorized",
        "giấy phép kinh doanh", "giấy phép thanh toán",
        "finviet có giấy phép", "finviet hợp pháp",
    ],
    "khách trung quốc": [
        "khách trung quốc", "khách trung", "游客", "中国游客", "中国客人",
        "khách tàu", "trung quốc", "tiền trung quốc",
        "khách du lịch trung", "du lịch trung",
        "khách trung quốc đến việt nam", "người trung quốc",
    ],
    "wechat": [
        "wechat", "wechat pay", "微信", "微信支付", "微信付",
        "dùng wechat", "quét wechat", "wetchat", "we chat", "ví wechat",
    ],
    "alipay": [
        "alipay", "支付宝", "alipay thanh toán", "dùng alipay",
        "ali pay", "ví alipay", "alipa",
    ],
    "kakao": [
        "kakao", "kakaopay", "韩国", "khách hàn", "khách hàn quốc",
        "kakao pay", "hàn quốc", "thanh toán hàn quốc",
    ],
    "thanh toán quốc tế": [
        "thanh toán quốc tế", "quốc tế", "thanh toán nước ngoài",
        "international payment", "外国游客", "国际支付",
        "khách nước ngoài", "người nước ngoài",
    ],
    "tiền về": [
        "tiền về", "tiền về tài khoản", "tiền có về không",
        "nhận tiền", "tiền đi đâu", "钱到账", "收款", "到账",
        "nhận tiền như thế nào", "làm sao nhận tiền",
    ],
    "ngân hàng": [
        "ngân hàng", "tài khoản", "bank", "về ngân hàng", "银行卡",
        "tài khoản ngân hàng", "số tài khoản", "银行账户", "账户", "账号",
    ],
    "napas": ["napas", "hệ thống napas", "napas là gì", "银行清算"],
    "vietqr": [
        "vietqr", "vietqr global", "mã qr", "quét mã", "二维码",
        "qr code", "qr", "scan qr", "扫码", "扫二维码",
    ],
    "eco": ["eco", "ví eco", "eco wallet", "app eco", "eco app", "eco系统"],
    "bao lâu": [
        "bao lâu", "mất bao lâu", "lâu không", "几天", "khi nào",
        "什么时候", "多久", "how long", "how soon",
    ],
    "đặt cọc": [
        "đặt cọc", "không đặt cọc", "押金", "要押金吗", "保证金",
        "có phải đặt cọc không", "đặt cọc bao nhiêu",
    ],
    "phí": [
        "phí", "phí giao dịch", "phí thanh toán", "手续费", "收多少",
        "费率", "费用", "有phí không", "1.5%", "bao nhiêu phí",
    ],
    "thu nhập": [
        "thu nhập", "lương", "làm kiếm được bao nhiêu",
        "收入", "佣金", "赚多少", "hoa hồng", "commission",
    ],
    "ký hợp đồng": [
        "ký hợp đồng", "ký hợp đồng 3 bên", "签合同", "签约",
        "hợp đồng", "sign contract", "ký",
    ],
    "đăng ký": [
        "đăng ký", "muốn đăng ký", "dang ky", "报名", "注册",
        "muốn tham gia", "tham gia", "register", "sign up", "apply",
    ],
    "giấy tờ": [
        "giấy tờ", "cần giấy tờ gì", "cần những gì",
        "资料", "要什么资料", "证件", "身份证", "营业执照",
        "cmnd", "cccd", "hộ chiếu", "passport", "giấy phép kinh doanh",
    ],
    "không biết chữ": ["không biết chữ", "不识字", "không biết đọc"],
    "ủy quyền": ["ủy quyền", "giấy ủy quyền", "授权", "委托书"],
    "thay đổi": ["thay đổi", "đổi", "cập nhật", "变更", "更换", "更改"],
    "momo": ["momo", "momo pay", "momoPay", "ví momo"],
    "zalopay": ["zalopay", "zalo pay", "ví zalo"],
    "quét mã": ["quét mã", "quét qr", "扫码", "scan"],
    "sử dụng": ["sử dụng", "dùng", "cách dùng", "怎么用", "如何使用", "how to use"],
    "lừa đảo": [
        "lừa đảo", "lừa", "骗人", "骗", "scam", "fake", "假的",
        "finviet có lừa đảo không",
    ],
    "tiền không về": [
        "tiền không về", "mất tiền", "钱不到账", "钱会不见吗",
        "tiền bị mất", "lo mất tiền",
    ],
    "rủi ro": ["rủi ro", "risk", "风险", "有什么风险"],
    "điều khoản": ["điều khoản", "条款", "合同条款", "terms"],
    "không có khách": [
        "không có khách", "chưa có khách", "没客人", "没有游客", "客人少",
    ],
    "cần gì": [
        "cần gì", "cần những gì", "phải làm gì",
        "需要什么", "要准备什么", "what do i need",
    ],
    "bán thời gian": [
        "bán thời gian", "part time", "parttime", "兼职",
        "chỉ làm ngoài giờ", "làm thêm", "làm buổi tối",
        "không làm toàn thời gian",
    ],
    "toàn thời gian": [
        "toàn thời gian", "full time", "fulltime", "全职",
        "làm chính thức", "nhân viên chính thức",
    ],
}


def faq_lookup(text: str, user_type: str = 'merchant') -> tuple:
    """查找 FAQ，返回 (answer, faq_key, match_type)
    user_type: 'merchant' | 'salesman'
    """
    text_lower = text.lower().strip()

    # 业务员专属：先查业务员 FAQ
    if user_type == 'salesman':
        for faq_key, keywords in SALESMAN_FAQ_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    ans = SALESMAN_FAQ_KB.get(faq_key)
                    if ans:
                        return ans, faq_key, 'salesman_faq'
        # 业务员也可以查商家FAQ（通用知识）

    # 后台动态 FAQ（Supabase）
    try:
        extra_faq = db_get_faq_extra()
        for kw, ans in extra_faq.items():
            if kw in text_lower:
                return ans, kw, 'extra_faq'
    except Exception:
        pass

    # 关键词扩展层
    for faq_key, keywords in FAQ_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                # faq_key 可能与 FAQ_KB 的 key 不完全一样，需做映射
                # FAQ_KEYWORDS 的 key 有时是描述性名称，FAQ_KB 的 key 是越南语
                # 这里直接用 faq_key 查 FAQ_KB，找不到则尝试去掉" là gì"等
                ans = FAQ_KB.get(faq_key)
                if ans:
                    return ans, faq_key, 'keyword_match'
                # 尝试从 FAQ_KB 中找第一个包含 faq_key 的 key
                for kb_key in FAQ_KB:
                    if faq_key.startswith(kb_key) or kb_key in faq_key:
                        return FAQ_KB[kb_key], kb_key, 'keyword_match'

    # 纯 FAQ_KB key 包含匹配（兜底）
    for faq_key, answer in FAQ_KB.items():
        if faq_key in text_lower:
            return answer, faq_key, 'direct_key_match'

    return None, None, None


# ════════════════════════════════════════════════════════════
# 菜单话术
# ════════════════════════════════════════════════════════════

# 商家开场白
SCRIPTS_MERCHANT = {
    'opening': """Chào bạn! 👋
Mình là Bong Bong - trợ lý của KINDLITE VIET NAM.

Bạn đang tìm hiểu giải pháp thanh toán quốc tế (WeChat Pay / Alipay / KakaoPay) cho cửa hàng, hay bạn là nhân viên kinh doanh của chúng tôi?

👆 Nếu bạn là CHỦ CỬA HÀNG muốn nhận tiền từ khách Trung/Hàn/Nhật:
1️⃣ Dịch vụ này là gì?
2️⃣ Phí và thu nhập như thế nào?
3️⃣ Cần chuẩn bị gì?
4️⃣ Đăng ký ngay

👆 Nếu bạn là NHÂN VIÊN KINH DOANH đã được đào tạo:
→ Nhắn: "Tôi là nhân viên" + họ tên + số điện thoại để đăng nhập chế độ nhân viên

Nhập số hoặc gõ câu hỏi nhé 😊""",

    '1': """Dịch vụ KINDLITE giúp cửa hàng bạn nhận tiền từ khách Trung Quốc, Hàn Quốc, Nhật Bản! 🌏

→ Khách quét mã QR bằng WeChat Pay / Alipay / KakaoPay
→ Tiền tự động về tài khoản ngân hàng VNĐ của bạn
→ 100% hợp pháp, có giấy phép từ Ngân hàng Nhà nước

✅ Không ảnh hưởng đến cách thu tiền hiện tại của bạn
✅ Chỉ bổ sung thêm một cách nhận tiền từ khách quốc tế

Bạn muốn hỏi thêm gì không? 😊""",

    '2': """Phí và thu nhập: 💰

💳 Phí giao dịch: chỉ 1.5% mỗi giao dịch thành công
❌ Không có phí đăng ký, phí bảo trì, hay phí ẩn

💡 Ví dụ: Khách Trung Quốc thanh toán 500元 (~1,885,000 VNĐ)
→ Bạn nhận ~1,857,000 VNĐ (đã trừ 1.5%)

Càng nhiều khách quốc tế → Thu nhập càng cao! 🫧""",

    '3': """Bạn chỉ cần chuẩn bị: 📋

✅ Giấy phép kinh doanh
✅ CMND / CCCD / Hộ chiếu (người đại diện)
✅ Số tài khoản ngân hàng

❌ Không cần đặt cọc
❌ Không cần công chứng phức tạp

Đăng ký đi, đội ngũ KINDLITE sẽ đến gặp bạn trực tiếp! Nhập 4️⃣ 😊""",

    '4': """Tuyệt vời! 🎉 Để đội ngũ KINDLITE liên hệ hỗ trợ, mình cần 3 thông tin:

👤 Họ tên của bạn:
📍 Thành phố (Hải Phòng / TP.HCM):
📱 Số điện thoại Zalo:

Gửi cả 3 thông tin một lần nhé, ví dụ:
「Nguyễn Văn A, TP.HCM, 0901234567」

Chúng tôi sẽ liên hệ trong vòng 24 giờ! 😊""",

    'thanks': """Cảm ơn bạn! ✅
Đội ngũ KINDLITE sẽ liên hệ với bạn trong vòng 24 giờ.

Nếu có thêm câu hỏi, cứ nhắn mình nhé 😊
Chúc bạn một ngày tốt lành! 🌟""",

    'default': """Xin lỗi, mình chưa hiểu câu hỏi của bạn 😅

Bạn có thể chọn:
1️⃣ Dịch vụ này là gì?
2️⃣ Phí và thu nhập?
3️⃣ Cần chuẩn bị gì?
4️⃣ Đăng ký ngay

Hoặc nhắn bất kỳ câu hỏi nào, mình sẽ cố gắng trả lời! 🫧"""
}

# 业务员开场白/专属脚本
SCRIPTS_SALESMAN = {
    'welcome': """Chào {name}! 👋 Đã xác nhận bạn là nhân viên kinh doanh KINDLITE.

Bạn có thể hỏi mình về:
• Quy trình báo cáo cửa hàng (báo cáo)
• Hoa hồng và KPI (hoa hồng / kpi)
• Hệ thống CRM (hệ thống)
• Tài liệu đào tạo (đào tạo)

Hoặc bất kỳ câu hỏi nào về sản phẩm, mình đều trả lời được! 😊""",

    'default': """Mình chưa tìm thấy thông tin cho câu hỏi này trong tài liệu nội bộ 😅

Bạn thử hỏi về:
• Quy trình / báo cáo / hoa hồng / kpi / hệ thống / đào tạo

Hoặc liên hệ trực tiếp với thành phố quản lý của bạn nhé! 🫧"""
}


# ════════════════════════════════════════════════════════════
# 解析留资信息（姓名 + 城市 + 电话）
# ════════════════════════════════════════════════════════════

import re

def parse_lead_info(text: str) -> dict | None:
    """尝试从文本中解析出 姓名、城市、电话
    格式：「姓名, 城市, 手机号」（逗号/空格/换行分隔）
    返回 {'name': ..., 'city': ..., 'phone': ...} 或 None
    """
    # 统一分隔符
    text = text.replace('，', ',').replace('、', ',').replace('\n', ',').replace('\t', ',')
    parts = [p.strip() for p in text.split(',') if p.strip()]

    if len(parts) < 3:
        return None

    # 找电话：包含至少 9 位数字
    phone = None
    phone_idx = -1
    for i, p in enumerate(parts):
        digits = re.sub(r'\D', '', p)
        if len(digits) >= 9:
            phone = p
            phone_idx = i
            break

    if not phone:
        return None

    # 找城市：含 "hải phòng" / "tp.hcm" / "hcm" / "sài gòn" / "海防" / "胡志明"
    city = None
    city_idx = -1
    city_keywords = ['hải phòng', 'hai phong', 'haiphong', 'tp.hcm', 'tp hcm', 'hcm',
                     'sài gòn', 'saigon', 'hồ chí minh', 'ho chi minh', '海防', '胡志明']
    for i, p in enumerate(parts):
        pl = p.lower()
        if any(c in pl for c in city_keywords):
            city = p
            city_idx = i
            break

    # 找名字：剩余的第一个长度 >= 2 的部分
    name = None
    for i, p in enumerate(parts):
        if i != phone_idx and i != city_idx and len(p) >= 2:
            name = p
            break

    if not name:
        # fallback: 取 parts[0] 作为名字
        name = parts[0] if parts else None

    if not city:
        # 没找到明确城市，取 parts 中非名字非电话的那个
        for i, p in enumerate(parts):
            if i != phone_idx and p != name:
                city = p
                break

    if name and city and phone:
        # 标准化城市名
        city_low = city.lower()
        if any(c in city_low for c in ['hải phòng', 'hai phong', 'haiphong', '海防']):
            city = 'Hải Phòng'
        elif any(c in city_low for c in ['hcm', 'hồ chí minh', 'sài gòn', 'saigon', '胡志明']):
            city = 'TP.HCM'
        return {'name': name, 'city': city, 'phone': phone}

    return None


def parse_salesman_registration(text: str) -> dict | None:
    """解析业务员报备信息
    格式：「tôi là nhân viên + 姓名 + 手机号」
    """
    text_lower = text.lower()
    triggers = ['tôi là nhân viên', 'tôi là nhan vien', '我是业务员', '我是员工',
                'nhân viên kindlite', 'nhan vien']
    triggered = any(t in text_lower for t in triggers)
    if not triggered:
        return None

    # 去掉触发词后解析剩余部分
    remaining = text
    for t in triggers:
        remaining = re.sub(re.escape(t), '', remaining, flags=re.IGNORECASE).strip()

    info = parse_lead_info(remaining)
    return info


# ════════════════════════════════════════════════════════════
# 内存缓存（Vercel 单实例生命周期内有效）
# ════════════════════════════════════════════════════════════
_state_cache: dict[str, dict] = {}


def get_user_state(user_id: str) -> dict:
    """先查内存缓存，再查 Supabase"""
    if user_id in _state_cache:
        return _state_cache[user_id]
    state = db_get_user_state(user_id)
    _state_cache[user_id] = state
    return state


def set_user_state(user_id: str, updates: dict):
    """同时更新内存缓存和 Supabase"""
    if user_id not in _state_cache:
        _state_cache[user_id] = {}
    _state_cache[user_id].update(updates)
    db_upsert_user_state(user_id, updates)


# ════════════════════════════════════════════════════════════
# GPT System Prompts
# ════════════════════════════════════════════════════════════

GPT_SYSTEM_MERCHANT = """Bạn là Bong Bong - trợ lý của KINDLITE VIET NAM.

## Vai trò:
- Trợ lý tư vấn cho CHỦ CỬA HÀNG muốn nhận tiền từ khách quốc tế
- Giải đáp về: WeChat Pay, Alipay, KakaoPay, NAPAS, phí 1.5%, ký hợp đồng, giấy tờ

## Về dự án:
- KINDLITE VIỆT NAM vận hành thương mại cho dự án thanh toán Finviet
- Hoạt động tại: Hải Phòng và TP.HCM
- Tỷ giá: 3770 VND = 1 CNY

## Nguyên tắc:
- KHÔNG tiết lộ cơ chế hoa hồng nội bộ (0.5%)
- Nếu không chắc → hẹn đội ngũ liên hệ trực tiếp
- Luôn kết thúc bằng emoji phù hợp
- Trả lời ngắn gọn, thân thiện"""

GPT_SYSTEM_SALESMAN = """Bạn là Bong Bong - trợ lý nội bộ của KINDLITE VIET NAM cho NHÂN VIÊN KINH DOANH.

## Vai trò:
- Hỗ trợ nhân viên kinh doanh về: quy trình, hoa hồng, KPI, hệ thống CRM, tài liệu

## Lưu ý:
- Đây là chat nội bộ với nhân viên đã được đào tạo
- Có thể đề cập đến quy trình nội bộ, nhưng KHÔNG tiết lộ số liệu hoa hồng cụ thể (0.5%)
- Nếu câu hỏi phức tạp → hướng dẫn liên hệ thành phố quản lý
- Luôn kết thúc bằng emoji"""


def ask_gpt(user_message: str, user_type: str = 'merchant') -> str | None:
    """调用 GPT-4 生成回复（最多等 1.5s，超时则返回 None 触发 fallback）"""
    if not openai_client:
        return None
    system = GPT_SYSTEM_MERCHANT if user_type != 'salesman' else GPT_SYSTEM_SALESMAN
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message}
            ],
            max_tokens=350,
            temperature=0.7,
            timeout=1.5,   # 最多等1.5秒，避免阻塞 webhook
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"GPT error: {e}")
        return None


# ════════════════════════════════════════════════════════════
# 核心回复逻辑
# ════════════════════════════════════════════════════════════

def get_reply(user_id: str, text: str) -> str:
    text = text.strip()
    text_lower = text.lower()

    state = get_user_state(user_id)
    user_type = state.get('user_type', 'merchant')   # merchant | salesman
    conv_state = state.get('conv_state', 'new')       # new | started | waiting_info | done

    # ── 1. 业务员注册检测（最高优先）──────────────────
    sal_info = parse_salesman_registration(text)
    if sal_info:
        set_user_state(user_id, {
            'user_type': 'salesman',
            'conv_state': 'started',
            'salesman_name': sal_info['name'],
            'salesman_phone': sal_info['phone'],
            'salesman_city': sal_info['city'],
        })
        # 记录业务员线索
        db_save_lead(user_id, sal_info['name'], sal_info['city'], sal_info['phone'], 'salesman')
        # 更新 Zalo 备注
        note = f"[业务员] {sal_info['name']} {sal_info['city']} {sal_info['phone']}"
        update_zalo_note(user_id, note)
        update_zalo_tag(user_id, '业务员')
        db_log_message(user_id, 'in', text, 'salesman_register', 'salesman_reg', 'salesman')
        return SCRIPTS_SALESMAN['welcome'].format(name=sal_info['name'])

    # ── 2. 等待留资（商家 waiting_info 状态）──────────
    if conv_state == 'waiting_info' and user_type == 'merchant':
        lead = parse_lead_info(text)
        if lead:
            set_user_state(user_id, {'conv_state': 'done'})
            # 保存线索
            db_save_lead(user_id, lead['name'], lead['city'], lead['phone'], 'merchant')
            # 更新 Zalo 备注
            note = f"[商家] {lead['name']} {lead['city']} {lead['phone']}"
            update_zalo_note(user_id, note)
            update_zalo_tag(user_id, '商家意向')
            db_log_message(user_id, 'in', text, 'lead_collected', 'lead', user_type)
            return SCRIPTS_MERCHANT['thanks']
        # 信息不完整，继续等待但给提示
        if ',' in text or '，' in text or len(text) > 8:
            # 有分隔符但解析失败，可能格式不对
            db_log_message(user_id, 'in', text, None, 'format_hint', user_type)
            return """Mình chưa nhận đủ thông tin 😅
Bạn gửi lại theo đúng định dạng nhé:

「Họ tên, Thành phố, Số điện thoại」

Ví dụ: 「Nguyễn Văn A, TP.HCM, 0901234567」"""

    # ── 3. 数字菜单 ────────────────────────────────────
    if text in ['1', '①']:
        set_user_state(user_id, {'conv_state': 'started'})
        db_log_message(user_id, 'in', text, 'menu_1', 'menu', user_type)
        return SCRIPTS_MERCHANT['1']
    if text in ['2', '②']:
        set_user_state(user_id, {'conv_state': 'started'})
        db_log_message(user_id, 'in', text, 'menu_2', 'menu', user_type)
        return SCRIPTS_MERCHANT['2']
    if text in ['3', '③']:
        set_user_state(user_id, {'conv_state': 'started'})
        db_log_message(user_id, 'in', text, 'menu_3', 'menu', user_type)
        return SCRIPTS_MERCHANT['3']
    if text in ['4', '④'] or any(kw in text_lower for kw in ['đăng ký ngay', 'dang ky', '注册', '报名', 'bắt đầu']):
        set_user_state(user_id, {'conv_state': 'waiting_info'})
        db_log_message(user_id, 'in', text, 'menu_4', 'menu', user_type)
        return SCRIPTS_MERCHANT['4']

    # ── 4. FAQ 匹配 ────────────────────────────────────
    faq_ans, faq_key, match_type = faq_lookup(text_lower, user_type)
    if faq_ans:
        set_user_state(user_id, {'conv_state': 'started'})
        db_log_message(user_id, 'in', text, faq_key, match_type, user_type)
        return faq_ans

    # ── 5. 新用户问候 ──────────────────────────────────
    if conv_state == 'new':
        greetings = ['xin chào', 'hello', 'hi', 'chào', 'bạn ơi', 'cảm ơn',
                     'good morning', 'good afternoon', 'good evening', '你好', '您好']
        is_pure_greeting = any(g in text_lower for g in greetings) and len(text_lower.split()) <= 4
        if is_pure_greeting:
            set_user_state(user_id, {'conv_state': 'started'})
            db_log_message(user_id, 'in', text, 'greeting', 'greeting', user_type)
            return SCRIPTS_MERCHANT['opening']
        # 新用户直接发问题
        set_user_state(user_id, {'conv_state': 'started'})

    # ── 6. 未命中，记录到后台 ──────────────────────────
    db_log_unmatched(user_id, text, user_type)

    # ── 7. GPT 兜底 ────────────────────────────────────
    gpt_reply = ask_gpt(text, user_type)
    if gpt_reply:
        db_log_message(user_id, 'in', text, 'gpt', 'gpt', user_type)
        return gpt_reply

    # ── 8. 最终 Fallback ───────────────────────────────
    db_log_message(user_id, 'in', text, 'default', 'fallback', user_type)
    if user_type == 'salesman':
        return SCRIPTS_SALESMAN['default']
    return SCRIPTS_MERCHANT['default']


# ════════════════════════════════════════════════════════════
# Zalo 发消息
# ════════════════════════════════════════════════════════════

def send_zalo_message(user_id: str, text: str):
    """发送消息到 Zalo OA"""
    if not ACCESS_TOKEN:
        log.warning("No ACCESS_TOKEN, skip sending")
        return False
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {'access_token': ACCESS_TOKEN, 'Content-Type': 'application/json'}
    payload = {
        "recipient": {"user_id": str(user_id)},
        "message": {"text": text}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        log.info(f"Send msg to {user_id}: {r.status_code} {r.text[:200]}")
        # 记录发出的消息
        state = get_user_state(user_id)
        db_log_message(user_id, 'out', text, None, None, state.get('user_type', 'merchant'))
        return r.status_code == 200
    except Exception as e:
        log.error(f"Send failed: {e}")
        return False


# ════════════════════════════════════════════════════════════
# 路由
# ════════════════════════════════════════════════════════════

@app.route('/', methods=['GET'])
def index():
    return jsonify({'status': 'Finviet Zalo Webhook v2.0 OK', 'time': datetime.now().isoformat()})


@app.route('/health', methods=['GET'])
def health():
    sb_ok = get_supabase() is not None
    return jsonify({'status': 'ok', 'supabase': sb_ok, 'time': datetime.now().isoformat()})


@app.route('/<path:verifier_path>', methods=['GET'])
def zalo_verify(verifier_path):
    """Zalo 域名归属验证"""
    if verifier_path.endswith('.html') and 'zalo_verifier' in verifier_path:
        token = verifier_path.replace('zalo_verifier', '').replace('.html', '')
        html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta property="zalo-platform-site-verification" content="{token}" />
</head>
<body>There Is No Limit To What You Can Accomplish Using Zalo!</body>
</html>'''
        return html_content, 200, {'Content-Type': 'text/html'}
    return 'Not Found', 404


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Zalo webhook 验证"""
    mode      = request.args.get('mode') or request.args.get('hub.mode')
    token     = request.args.get('VerifyToken') or request.args.get('hub.verify_token')
    challenge = request.args.get('challenge') or request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return jsonify(challenge), 200
    return 'Forbidden', 403


@app.route('/webhook', methods=['POST'])
def webhook_receive():
    """接收 Zalo 推送事件 - 同步生成回复，响应体内直接带消息内容"""
    try:
        raw_body = request.get_data()
        data = json.loads(raw_body)
        log.info(f"Event: {json.dumps(data)[:200]}")

        signature = request.headers.get('X-Zalo-Signature', '')
        if signature:
            log.info(f"MAC signature received: {signature[:20]}...")

        event_name = data.get('event_name', '')

        # ── follow：直接回复欢迎语 ─────────────────────────
        if event_name == 'follow':
            user_id = data.get('follower', {}).get('id', '')
            log.info(f"follow event: user_id={user_id}")
            if user_id:
                set_user_state(user_id, {'user_type': 'merchant', 'conv_state': 'new'})
                return jsonify({
                    'recipient': {'id': str(user_id)},
                    'message': {'text': SCRIPTS_MERCHANT['opening']}
                })

        # ── unfollow：只更新状态，不需要回复 ───────────────
        elif event_name == 'unfollow':
            user_id = data.get('follower', {}).get('id', '')
            log.info(f"unfollow: user_id={user_id}")
            if user_id:
                set_user_state(user_id, {'conv_state': 'unfollowed'})
            return jsonify({'status': 'ok'})

        # ── user_send_text：同步生成回复并返回 ─────────────
        elif event_name == 'user_send_text':
            user_id = data.get('sender', {}).get('id', '')
            text    = data.get('message', {}).get('text', '')
            log.info(f"user_send_text: user_id={user_id}, text={text[:50]}")
            if user_id and text:
                # 同步获取回复（GPT 有 1.5s 超时保护）
                reply = get_reply(user_id, text)
                log.info(f"Reply: {reply[:80]}")
                # 在响应体内直接带消息内容，Zalo 会自动推送
                return jsonify({
                    'recipient': {'id': str(user_id)},
                    'message': {'text': reply}
                })

    except Exception as e:
        log.error(f"Webhook error: {e}")
        import traceback
        log.error(traceback.format_exc())

    return jsonify({'status': 'ok'})


def _process_message(user_id: str, text: str):
    """后台线程处理消息（不阻塞 webhook 响应）"""
    try:
        reply = get_reply(user_id, text)
        log.info(f"[BG] Reply: {reply[:80]}")
        send_zalo_message(user_id, reply)
    except Exception as e:
        log.error(f"[BG] process_message error: {e}")


# ════════════════════════════════════════════════════════════
# 后台 API（供 finviet-crm 调用）
# ════════════════════════════════════════════════════════════

@app.route('/admin/leads', methods=['GET'])
def admin_leads():
    """获取线索列表（供后台调用，需要 token 鉴权）"""
    token = request.headers.get('X-Admin-Token', '')
    if token != os.environ.get('ADMIN_TOKEN', 'kindlite-admin-2026'):
        return jsonify({'error': 'Unauthorized'}), 401
    sb = get_supabase()
    if not sb:
        return jsonify({'error': 'Supabase not available'}), 503
    try:
        res = sb.table('zalo_leads').select('*').order('created_at', desc=True).limit(200).execute()
        return jsonify({'leads': res.data or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/unmatched', methods=['GET'])
def admin_unmatched():
    """获取未命中问题列表"""
    token = request.headers.get('X-Admin-Token', '')
    if token != os.environ.get('ADMIN_TOKEN', 'kindlite-admin-2026'):
        return jsonify({'error': 'Unauthorized'}), 401
    sb = get_supabase()
    if not sb:
        return jsonify({'error': 'Supabase not available'}), 503
    try:
        res = sb.table('zalo_unmatched_queries').select('*').eq('status', 'pending')\
              .order('created_at', desc=True).limit(200).execute()
        return jsonify({'unmatched': res.data or []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/faq', methods=['GET', 'POST'])
def admin_faq():
    """FAQ 动态管理"""
    token = request.headers.get('X-Admin-Token', '')
    if token != os.environ.get('ADMIN_TOKEN', 'kindlite-admin-2026'):
        return jsonify({'error': 'Unauthorized'}), 401
    sb = get_supabase()
    if not sb:
        return jsonify({'error': 'Supabase not available'}), 503
    if request.method == 'GET':
        res = sb.table('zalo_faq_extra').select('*').order('created_at', desc=True).execute()
        return jsonify({'faq': res.data or []})
    elif request.method == 'POST':
        body = request.get_json()
        sb.table('zalo_faq_extra').insert({
            'keyword': body['keyword'].lower(),
            'answer': body['answer'],
            'active': True,
            'created_at': datetime.utcnow().isoformat(),
        }).execute()
        return jsonify({'status': 'ok'})


@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    """消息统计"""
    token = request.headers.get('X-Admin-Token', '')
    if token != os.environ.get('ADMIN_TOKEN', 'kindlite-admin-2026'):
        return jsonify({'error': 'Unauthorized'}), 401
    sb = get_supabase()
    if not sb:
        return jsonify({'error': 'Supabase not available'}), 503
    try:
        # 总消息数
        total = sb.table('zalo_message_logs').select('id', count='exact').execute()
        # 线索数
        leads = sb.table('zalo_leads').select('id', count='exact').execute()
        # 未命中数
        unmatched = sb.table('zalo_unmatched_queries').select('id', count='exact').eq('status', 'pending').execute()
        # 业务员数
        salesmen = sb.table('zalo_leads').select('id', count='exact').eq('user_type', 'salesman').execute()
        return jsonify({
            'total_messages': total.count,
            'total_leads': leads.count,
            'unmatched_pending': unmatched.count,
            'salesmen': salesmen.count,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
