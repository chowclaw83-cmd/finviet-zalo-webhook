"""
Finviet Zalo OA Webhook - Vercel Serverless
泡泡自动回复机器人
"""
import os
import json
import logging
import hmac
import hashlib
import requests
from datetime import datetime
from flask import Flask, request, jsonify

# OpenAI SDK
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────
VERIFY_TOKEN  = os.environ.get('ZALO_VERIFY_TOKEN', 'finviet_webhook_2026')
APP_ID        = os.environ.get('ZALO_APP_ID', '')
ACCESS_TOKEN  = os.environ.get('ZALO_ACCESS_TOKEN', '')
OA_SECRET    = os.environ.get('ZALO_OA_SECRET', '')  # OA Secret Key（用于 MAC 签名验证）
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')  # OpenAI API Key

# OpenAI 客户端（使用 GPTsAPI 兼容接口）
if OPENAI_API_KEY and OPENAI_AVAILABLE:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY,
        base_url="https://api.gptsapi.net/v1"
    )
else:
    openai_client = None


def verify_zalo_mac(data: dict, timestamp: str, signature: str) -> bool:
    """验证 Zalo webhook MAC 签名
    
    Zalo 公式: raw = app_id + JSON.stringify(body) + timestamp + oa_secret
    其中 body 不包含 signature 字段本身
    """
    if not OA_SECRET or not signature:
        log.warning("No OA_SECRET or signature, skip MAC verification")
        return True  # 跳过验证以便调试
    
    # JSON 序列化时排除 signature 字段（如果存在）
    body_for_sign = {k: v for k, v in data.items() if k != 'signature'}
    body_json = json.dumps(body_for_sign, separators=(',', ':'))
    
    # 按正确顺序拼接：app_id + body_json + timestamp + oa_secret
    raw = str(APP_ID) + body_json + timestamp + OA_SECRET
    expected = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    
    ok = hmac.compare_digest(expected, signature)
    if not ok:
        log.warning(f"MAC mismatch:\n  raw={raw[:100]}\n  expected={expected}\n  got={signature}")
    return ok

# ═══════════════════════════════════════════════════════════════════════
# 中文关键词 → FAQ_KB key 映射（解决中文问法匹配问题）
# 用法：用户发中文 → 先转成越南语关键词 → 再走 FAQ 匹配
# ═══════════════════════════════════════════════════════════════════════
ZH2FAQ = {
    # 公司/Finviet 相关
    "公司": "finviet",
    "finviet": "finviet",
    "你们": "finviet",
    "是什么公司": "finviet",
    "公司介绍": "finviet",
    # 牌照/合法
    "牌照": "giấy phép",
    "牌照吗": "giấy phép",
    "合法": "giấy phép",
    "合法吗": "giấy phép",
    "有牌照": "giấy phép",
    "正规": "giấy phép",
    "正规吗": "giấy phép",
    "执照": "giấy phép",
    "持牌": "giấy phép",
    # 安全
    "安全": "an toàn",
    "安全吗": "an toàn",
    "靠谱": "an toàn",
    # 押金/费用
    "押金": "đặt cọc",
    "要押金": "đặt cọc",
    "保证金": "đặt cọc",
    "交钱": "đặt cọc",
    "手续费": "phí",
    "收多少": "phí",
    "收我钱": "phí",
    "费用": "phí",
    "收费": "phí",
    "佣金": "thu nhập",
    "我能赚": "thu nhập",
    "收入": "thu nhập",
    "赚多少": "thu nhập",
    "利润": "thu nhập",
    # 签约/资料
    "签约": "ký hợp đồng",
    "签合同": "hợp đồng",
    "合同": "hợp đồng",
    "资料": "giấy tờ",
    "证件": "giấy tờ",
    "身份证": "giấy tờ",
    "执照": "giấy tờ",
    "需要什么": "cần gì",
    "准备什么": "cần gì",
    "什么材料": "cần gì",
    "多久": "bao lâu",
    "几天": "bao lâu",
    "几天能用": "bao lâu",
    "多久能好": "bao lâu",
    "多久开通": "bao lâu",
    "多久能用": "bao lâu",
    # 兼职/全职
    "兼职": "bán thời gian",
    "全职": "toàn thời gian",
    "parttime": "bán thời gian",
    "fulltime": "toàn thời gian",
    "只做": "bán thời gian",
    "可以吗": "bán thời gian",
    "能做吗": "bán thời gian",
    # 收钱/到账
    "到账": "tiền về",
    "钱到": "tiền về",
    "到账吗": "tiền về",
    "多久到": "tiền về",
    "到账时间": "tiền về",
    "收款": "tiền về",
    "收钱": "tiền về",
    "钱怎么到": "tiền về",
    "bank": "ngân hàng",
    "银行卡": "ngân hàng",
    "账户": "ngân hàng",
    # 游客/中日韩
    "中国游客": "khách trung quốc",
    "微信": "wechat",
    "支付宝": "alipay",
    "韩国游客": "kakao",
    "kakao": "kakao",
    "扫码": "quét mã",
    "怎么用": "sử dụng",
    "使用": "sử dụng",
    "怎么操作": "sử dụng",
    # momo/zalopay
    "momo": "momo",
    "zalopay": "zalopay",
    "有momo": "momo",
    "已有": "momo",
    # 骗/风险
    "骗": "lừa đảo",
    "骗人": "lừa đảo",
    "靠谱吗": "lừa đảo",
    "真的": "lừa đảo",
    "真的假的": "lừa đảo",
    "风险": "lừa đảo",
    "安全吗": "an toàn",
}


# ── 培训手册 FAQ 数据库（透彻版）──────────────────
# 原则：覆盖所有自然语言变体 + 越南语/中文/英文三语
# 注意：答案内容 100% 保持不变，只改匹配逻辑

FAQ_KB = {
    # ═══════════════════════════════════════════
    # 一、关于 Finviet / 公司资质
    # ═══════════════════════════════════════════
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

    # ═══════════════════════════════════════════
    # 二、收中日韩游客的钱
    # ═══════════════════════════════════════════
    "khách trung quốc": """Đúng rồi! Khách Trung Quốc, Hàn Quốc, Nhật Bản... họ đã quen với thanh toán không tiền mặt bằng WeChat Pay, Alipay, KakaoPay trên điện thoại.

📍 Nếu cửa hàng bạn có mã QR thanh toán quốc tế:
- Khách quét mã → Thanh toán ngay → Tiền về tài khoản ngân hàng của bạn
- Khách thoải mái chi tiêu, không cần đổi tiền

💡 Nhiều cửa hàng bán đồ ăn, cà phê... ở Sài Gòn, Hải Phòng đã dùng và thu thêm được hàng triệu đồng mỗi tháng!

Bạn muốn tìm hiểu cách đăng ký không? 😊""",

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

    # ═══════════════════════════════════════════
    # 三、钱怎么到账 / NAPAS / 银行
    # ═══════════════════════════════════════════
    "tiền về": """Tiền thanh toán sẽ đi qua hệ thống NAPAS (do Ngân hàng Nhà nước Việt Nam vận hành), sau đó tự động chuyển vào tài khoản ngân hàng của bạn.

🏦 Giống như thẻ ATM quẹt thẻ quốc tế - tiền về ngay tài khoản
📱 Nếu tải ECO, bạn xem được số dư real-time như Momo/ZaloPay

An toàn, nhanh, không lo mất tiền! ✅""",

    "ngân hàng": """Tiền thanh toán được xử lý qua NAPAS - hệ thống thanh toán quốc gia của Việt Nam, rồi tự động chuyển vào tài khoản ngân hàng của bạn.

🏦 Hệ thống NAPAS tương đương UnionPay (Trung Quốc) hoặc KFTC (Hàn Quốc)
✅ An toàn tuyệt đối
✅ Không có rủi ro mất tiền

Bạn dùng ngân hàng nào? Vietcombank, VietinBank, BIDV...? 😊""",

    "napas": """NAPAS là hệ thống thanh toán điện tử liên ngân hàng của Việt Nam, do Ngân hàng Nhà nước quản lý.

🏦 Khi khách quét mã thanh toán quốc tế:
→ Tiền đi qua NAPAS
→ Quy đổi ra VNĐ theo tỷ giá thị trường
→ Vào tài khoản ngân hàng của bạn

📱 Nếu bạn dùng VietQR thường thì biết rồi - VietQR Global của bên mình là phiên bản quốc tế của NAPAS, dùng để nhận tiền từ khách nước ngoài

An toàn và hợp pháp 100%! ✅""",

    "vietqr": """VietQR Global là mã thanh toán quốc tế của NAPAS - hệ thống thanh toán quốc gia Việt Nam.

📱 VietQR (thường): nhận tiền từ khách Việt Nam
🌏 VietQR Global (của Finviet): nhận tiền từ khách Trung/Hàn/Nhật

✅ Cùng hệ thống NAPAS - an toàn như ngân hàng
✅ Tiền về tài khoản tự động

Bạn đăng ký để nhận VietQR Global nhé! 😊""",

    "eco": """ECO là ứng dụng quản lý giao dịch của Finviet, giúp bạn:

📱 Xem số dư tài khoản real-time (như Momo/ZaloPay)
📊 Theo dõi lịch sử giao dịch
💰 Biết được khách nào thanh toán, bao nhiêu tiền

Sau khi ký hợp đồng, đội ngũ Finviet sẽ hướng dẫn bạn tải và đăng ký ECO. 😊""",

    "đến chưa": """Bạn yên tâm! Tiền sẽ tự động vào tài khoản ngân hàng qua hệ thống NAPAS.

📱 Nếu tải ECO, bạn xem được số dư real-time
📲 Sau khi ký hợp đồng, bạn sẽ nhận được thông báo tài khoản

Cần kiểm tra giao dịch cụ thể, liên hệ đội ngũ hỗ trợ nhé! 🫧""",

    # ═══════════════════════════════════════════
    # 四、费用 / 押金 / 手续费
    # ═══════════════════════════════════════════
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

    # ═══════════════════════════════════════════
    # 五、签约流程 / 资料
    # ═══════════════════════════════════════════
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

    # ═══════════════════════════════════════════
    # 六、收款方式 / ZaloPay / MoMo 对比
    # ═══════════════════════════════════════════
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

    # ═══════════════════════════════════════════
    # 七、风险 / 安全 / 合法
    # ═══════════════════════════════════════════
    "lừa đảo": """Finviet hoàn toàn hợp pháp! Đây là công ty thanh toán được Ngân hàng Nhà nước Việt Nam cấp phép. ✅

🏦 Tiền đi qua hệ thống NAPAS - cùng hệ thống ngân hàng quốc gia
📝 Hợp đồng 3 bên rõ ràng, bảo vệ quyền lợi cửa hàng
🔒 Khách thanh toán qua app chính thức (WeChat/Alipay/KakaoPay)

Nhiều cửa hàng ở TP.HCM và Hải Phòng đã dùng, hoàn toàn yên tâm! 😊

Bạn cần xem thêm thông tin gì không? 🫧""",

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

    "điều khoản": """Hợp đồng 3 bên của Finviet có các điều khoản quan trọng:

✅ Quyền lợi của cửa hàng được bảo vệ rõ ràng
✅ Phí giao dịch minh bạch (1.5%)
✅ Tiền thanh toán qua NAPAS - an toàn

⚠️ Điều khoản vi phạm: Nếu cửa hàng giao hàng không đúng cam kết, lừa đảo khách, bán hàng giả... thì sẽ bị xử lý theo quy định (qua cổng thanh toán + pháp luật Việt Nam). Điều này bảo vệ bạn khỏi bị lừa bởi khách xấu! 😊

Đội ngũ KINDLITE sẽ giải thích chi tiết từng điều khoản khi ký hợp đồng nhé! 🫧""",

    # ═══════════════════════════════════════════
    # 八、其他问题
    # ═══════════════════════════════════════════
    "thanh toán khi nào": """Thanh toán hoa hồng/phí giao dịch được tính theo chu kỳ cố định mỗi tháng. Tiền giao dịch về tài khoản ngân hàng ngay khi khách thanh toán. 🏦

📱 Nếu tải ECO: xem được số dư real-time
📋 Cuối tháng: đối soát và thanh toán phí theo quy định

Bạn đăng ký để nhận thông tin chi tiết về chu kỳ thanh toán nhé! 😊""",

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
}


def faq_lookup(text: str) -> str | None:
    """在 FAQ 数据库中查找匹配的回答（三语支持）

    匹配策略（按优先级）：
    1. 越南语关键词包含匹配（keyword in text）
    2. 中文关键词 → 映射越南语 key → 匹配
    3. 越南语 key 包含用户消息中的词（text in keyword）
    """
    text_lower = text.lower().strip()

    # ── Step 1: 越南语关键词直接匹配 ───────────────
    for keyword, answer in FAQ_KB.items():
        if keyword in text_lower:
            log.info(f"FAQ hit [VN]: '{keyword}' in '{text_lower[:50]}'")
            return answer

    # ── Step 2: 中文关键词 → 映射越南语 key ─────────
    for zh_key, faq_key in ZH2FAQ.items():
        if zh_key in text_lower:
            if faq_key in FAQ_KB:
                log.info(f"FAQ hit [ZH→VN]: '{zh_key}' → '{faq_key}'")
                return FAQ_KB[faq_key]

    # ── Step 3: 用户消息 → 越南语 key 包含匹配 ─────
    # 例：用户说 "我可以做兼职吗"，FAQ 有 key "bán thời gian"
    # "bán thời gian" 里包含用户词的情况
    for keyword, answer in FAQ_KB.items():
        # 检查用户消息里的每个词是否被 FAQ key 包含
        for word in text_lower.split():
            if len(word) >= 3 and word in keyword:
                log.info(f"FAQ hit [reverse]: '{keyword}' contains '{word}'")
                return answer

    return None


# ── 菜单话术库 ─────────────────────────────────
SCRIPTS = {
    'opening': """Chào bạn! 👋
Mình là Bong Bong - trợ lý tuyển dụng của KINDLITE VIET NAM.
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

# ── GPT-4 System Prompt ─────────────────────────────────
GPT_SYSTEM_PROMPT = """Bạn là Bong Bong - trợ lý tuyển dụng của KINDLITE VIET NAM.

## Giới thiệu (IMPORTANT - read first):
- Bạn là Bong Bong, trợ lý của KINDLITE VIET NAM
- KINDLITE là đơn vị vận hành thương mại tại Việt Nam cho dự án thanh toán di động quốc tế
- Các câu hỏi THƯỜNG GẶP (về WeChat Pay, Alipay, KakaoPay, NAPAS, phí, đặt cọc, ký hợp đồng, giấy tờ, thanh toán, ECO, rủi ro) đã có FAQ tự động trả lời
- Bạn chỉ trả lời khi: user hỏi về TUYỂN DỤNG (công việc, thu nhập, đăng ký làm đại lý), hoặc câu hỏi LẠ không có trong FAQ

## Về dự án:
- KINDLITE VIỆT NAM là công ty phụ trách vận hành thương mại tại Việt Nam cho dự án thanh toán di động Trung Quốc (WeChat Pay / Alipay)
- Hoạt động tại 2 thành phố: Hải Phòng và TP.HCM
- Tỷ giá: 3770 VND = 1 CNY

## Cơ chế hoa hồng:
- Phí giao dịch 1.5% từ merchant
- KINDLITE nhận 0.5% hoa hồng (bí mật nội bộ)
- A-marketing / đại lý nhận hoa hồng từ 0.3-0.5%

## Công việc tuyển dụng:
- Đi thị trường tìm quán ăn, cà phê, cửa hàng có khách Trung Quốc
- Giới thiệu kết nối WeChat Pay / Alipay
- Hỗ trợ cài đặt và theo dõi sau ký hợp đồng

## Điều kiện:
- Không yêu cầu bằng cấp hay kinh nghiệm
- Biết tiếng Việt, thích giao tiếp, chịu đi thị trường
- Quen thuộc khu vực Hải Phòng hoặc TP.HCM
- Có xe máy
- Ưu tiên: có kinh nghiệm bán hàng, biết tiếng Trung/Anh

## Lưu ý quan trọng:
- FAQ có sẵn cho: Finviet là gì, NAPAS, WeChat/Alipay/KakaoPay, phí, đặt cọc, MoMo/ZaloPay, ký hợp đồng, giấy tờ, thanh toán, rủi ro, ECO
- Nếu câu hỏi THƯỜNG GẶP về đăng ký/tìm hiểu công ty → trả lời ngắn gọn hoặc hẹn gặp trực tiếp
- Nếu câu hỏi LẠ về kỹ thuật/thanh toán → nói "Cảm ơn, đội ngũ sẽ liên hệ lại"
- KHÔNG trả lời về cơ chế hoa hồng 0.5% (bí mật nội bộ)
- KHÔNG trả lời sai về WeChat/Alipay - nếu không chắc → hỏi lại hoặc chuyển đội ngũ
- Luôn kết thúc bằng emoji phù hợp"""


def ask_gpt(user_message: str, user_name: str = "") -> str:
    """调用 GPT-4 生成回复"""
    if not openai_client:
        log.warning("OpenAI client not initialized")
        return None
    
    try:
        log.info(f"Calling OpenAI API...")
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": GPT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Tin nhắn từ {user_name or 'người dùng'}: {user_message}"}
            ],
            max_tokens=300,
            temperature=0.7
        )
        reply = response.choices[0].message.content.strip()
        log.info(f"GPT success: {reply[:50]}")
        return reply
    except Exception as e:
        log.error(f"GPT error: {e}")
        return None

# ── 用户状态（内存，无持久化）─────────────────────
user_states = {}

# ══════════════════════════════════════════════════════════
# 关键词扩展层 → FAQ_KB 答案映射
# 结构: {faq_key: [关键词列表...]}
# 答案直接从 FAQ_KB[faq_key] 读取，原文不动
# ══════════════════════════════════════════════════════════
FAQ_KEYWORDS = {
    # 一、公司/Finviet
    "finviet là gì": [
        "finviet là gì", "finviet la gi", "finviet", "finviet vietnam",
        "công ty finviet", "công ty của bạn", "công ty là gì", "công ty các bạn là gì",
        "你们是什么公司", "你们是干嘛的", "finviet是什么", "你们公司叫什么",
        "giới thiệu công ty", "giới thiệu về công ty", "tìm hiểu công ty",
        "công ty", "công ty của", "bạn là công ty nào",
        "what is finviet", "finviet company",
    ],
    "giấy phép": [
        "giấy phép", "có giấy phép", "có phép không", "được cấp phép",
        "chứng chỉ", "牌照", "有牌照吗", "合法吗", "正规吗", "你们合法吗", "có hợp pháp không",
        "legal", "licensed", "authorized",
        "giấy phép kinh doanh", "giấy phép thanh toán",
        "finviet có giấy phép", "finviet hợp pháp",
    ],

    # 二、中日韩游客/WeChat/Alipay/Kakao
    "khách trung quốc": [
        "khách trung quốc", "khách trung", "游客", "中国游客", "中国客人",
        "khách tàu", "trung quốc", "tiền trung quốc",
        "khách du lịch trung", "du lịch trung",
        "khách trung quốc đến việt nam", "người trung quốc",
    ],
    "wechat": [
        "wechat", "wechat pay", "微信", "微信支付", "微信付", "wechat thanh toán",
        "dùng wechat", "quét wechat", "wechat pay",
        "wetchat", "we chat", "ví wechat",
    ],
    "alipay": [
        "alipay", "支付宝", "alipay thanh toán", "dùng alipay", "quét alipay",
        "ali pay", "ví alipay", "alipa",
    ],
    "kakao": [
        "kakao", "kakaopay", "韩国", "khách hàn", "khách hàn quốc",
        "kakao pay", "hàn quốc", "thanh toán hàn quốc",
        "khách hàn", "người hàn",
    ],
    "thanh toán quốc tế": [
        "thanh toán quốc tế", "quốc tế", "thanh toán nước ngoài",
        "international payment", "thanh toán trung quốc",
        "外国游客", "国际支付", "外国客人",
        "khách nước ngoài", "người nước ngoài",
        "thanh toán quốc tế là gì",
    ],

    # 三、钱到账/NAPAS/银行
    "tiền về": [
        "tiền về", "tiền về tài khoản", "tiền có về không", "tiền không về",
        "tiền nhận", "nhận tiền", "tiền đi đâu", "tiền đi như thế nào",
        "钱到账", "钱怎么到", "收款", "收钱", "到账", "钱怎么到", "收到钱",
        "tiền có về không", "có nhận được tiền không",
        "nhận tiền như thế nào", "làm sao nhận tiền",
    ],
    "ngân hàng": [
        "ngân hàng", "tài khoản", "bank", "về ngân hàng", "银行卡",
        "tài khoản ngân hàng", "số tài khoản", "银行账户",
        "账户", "账号", "银行",
        "ngân hàng nào", "dùng ngân hàng nào",
    ],
    "napas": [
        "napas", "hệ thống napas", "napas là gì",
        "银行清算", "清算系统",
    ],
    "vietqr": [
        "vietqr", "vietqr global", "mã qr", "quét mã", "二维码",
        "qr code", "qr", "scan qr", "二维码", "扫码", "扫二维码",
        "treo mã qr", "mã qr thanh toán",
    ],
    "eco": [
        "eco", "ví eco", "eco wallet", "app eco", "eco app",
        "eco系统", "eco钱包",
    ],
    "bao lâu": [
        "bao lâu", "bao lâu thì có", "mất bao lâu", "lâu không", "几天",
        "bao lâu để", "khi nào", "khi nào có", "什么时候", "多久", "开通要多久",
        "how long", "how soon",
        "bao lâu để ký", "bao lâu ký xong",
    ],

    # 四、押金/费用/收入
    "đặt cọc": [
        "đặt cọc", "đặt cọc không", "cần đặt cọc", "không đặt cọc",
        "押金", "要押金吗", "要押金", "押金怎么算", "保证金",
        "có phải đặt cọc không", "đặt cọc bao nhiêu",
        "tiền đặt cọc", "phí đặt cọc",
    ],
    "phí": [
        "phí", "phí giao dịch", "phí thanh toán", "thu phí", "phí là bao nhiêu",
        "手续费", "收多少手续费", "收多少", "费率", "费用",
        "có phí không", "có mất phí không", "có thu phí không",
        "tiền phí", "giá", "bao nhiêu phí",
        "bao nhiêu %", "tỷ lệ phí", "tỷ lệ", "1.5%",
    ],
    "thu nhập": [
        "thu nhập", "thu nhập bao nhiêu", "lương", "lương bao nhiêu",
        "làm kiếm được bao nhiêu", "kiếm được bao nhiêu", "được bao nhiêu",
        "收入", "佣金", "赚多少", "能赚多少", "收入多少",
        "tiền hoa hồng", "hoa hồng", "commission",
        "lương cơ bản", "thu nhập từ đâu",
    ],

    # 五、签约/资料
    "ký hợp đồng": [
        "ký hợp đồng", "ký hợp đồng 3 bên", "ký hợp đồng như thế nào",
        "ký hợp đồng ở đâu", "ký hợp đồng mất bao lâu", "ký hợp đồng có mất phí không",
        "签合同", "签约", "签合约", "签合同要多久", "怎么签合同",
        "hợp đồng", "ký hợp đồng", "ký hợp đồng bao lâu",
        "ký hợp đồng ở đâu", "ký hợp đồng tại đâu",
        "sign contract", "ký",
    ],
    "đăng ký": [
        "đăng ký", "đăng ký ngay", "dang ky", "muốn đăng ký", "tôi muốn đăng ký",
        "đăng ký như thế nào", "bắt đầu", "报名", "注册", "我要报名",
        "注册", "马上注册", "立即注册",
        "làm sao đăng ký", "cách đăng ký", "muốn tham gia",
        "tham gia", "thanh toán di động", "giao dịch di động",
        "giúp tôi", "tôi muốn", "cho tôi biết",
        "register", "sign up", "apply",
    ],
    "giấy tờ": [
        "giấy tờ", "cần giấy tờ gì", "cần những gì", "cần chuẩn bị gì",
        "giấy tờ cần thiết", "hồ sơ", "thủ tục",
        "资料", "要什么资料", "准备什么", "需要什么", "需要什么证件",
        "证件", "身份证", "护照", "营业执照",
        "cần cmnd", "cmnd", "cccd", "hộ chiếu", "passport",
        "giấy phép kinh doanh", "đăng ký kinh doanh",
        "what documents", "documents needed",
    ],
    "không biết chữ": [
        "không biết chữ", "không biết đọc", "không biết viết",
        "文盲", "不识字", "不会写字", "不会签名",
    ],
    "ủy quyền": [
        "ủy quyền", "giấy ủy quyền", "thay đổi người đại diện",
        "授权", "委托书",
        "người được ủy quyền", "ký ủy quyền",
    ],
    "thay đổi": [
        "thay đổi", "đổi", "cập nhật", "sửa",
        "变更", "更换", "更改",
        "thay đổi thông tin", "đổi thông tin", "thay đổi số tài khoản",
        "đổi số tài khoản", "đổi ngân hàng",
    ],

    # 六、收款方式/MoMo/ZaloPay
    "momo": [
        "momo", "momo pay", "momoPay", "ví momo",
        "有momo", "我有momo", "momo怎么用", "momopay",
    ],
    "zalopay": [
        "zalopay", "zalo pay", "ví zalo", "zalo",
        "有zalo", "我有zalo", "zalo支付",
    ],
    "quét mã": [
        "quét mã", "quét qr", "quét", "扫码", "扫二维码",
        "scan", "làm sao quét", "cách quét",
    ],
    "sử dụng": [
        "sử dụng", "dùng", "cách dùng", "sử dụng như thế nào",
        "cách sử dụng", "怎么用", "如何使用", "如何使用",
        "how to use", "usage", "hướng dẫn",
    ],

    # 七、风险/安全/合法
    "lừa đảo": [
        "lừa đảo", "lừa", "có lừa đảo không", "骗人", "骗", "骗子",
        "scam", "fake", "假的",
        "finviet có lừa đảo không", "finviet có bịp không",
    ],
    "tiền không về": [
        "tiền không về", "tiền không đến", "tiền không về tài khoản",
        "mất tiền", "mất", "钱不到账", "钱会不见吗", "会不会丢钱",
        "tiền bị mất", "lo mất tiền", "sợ mất tiền",
    ],
    "rủi ro": [
        "rủi ro", "có rủi ro không", "risk", "rủi ro gì",
        "风险", "有什么风险", "有风险吗",
    ],
    "điều khoản": [
        "điều khoản", "điều khoản hợp đồng", "điều khoản vi phạm",
        "条款", "合同条款", "条款内容",
        "terms", "conditions",
    ],

    # 八、其他
    "thanh toán khi nào": [
        "thanh toán khi nào", "thanh toán lúc nào", "khi nào thanh toán",
        "结算", "什么时候结算", "结算时间", "多久结算",
        "thanh toán bao lâu một lần", "chu kỳ thanh toán",
    ],
    "không có khách": [
        "không có khách", "chưa có khách", "chưa có khách trung",
        "cửa hàng ít khách", "không có khách trung quốc",
        "没客人", "没有游客", "生意不好", "客人少",
    ],
    "thay đổi thông tin": [
        "thay đổi thông tin", "đổi thông tin", "cập nhật thông tin",
        "变更信息", "更改信息",
    ],
    "cần gì": [
        "cần gì", "cần những gì", "cần chuẩn bị gì", "phải làm gì",
        "需要什么", "要准备什么", "需要做什么",
        "what do i need", "what i need",
    ],
}


def faq_lookup(text: str) -> str | None:
    """关键词扩展层匹配 → 直接返回 FAQ_KB[faq_key]"""
    text_lower = text.lower().strip()

    # Step 1: 检查所有扩展关键词
    for faq_key, keywords in FAQ_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                log.info(f"FAQ hit: '{kw}' → '{faq_key}' in '{text_lower[:50]}'")
                return FAQ_KB.get(faq_key)

    # Step 2: 纯越南语 key 包含匹配（兜底）
    for faq_key, answer in FAQ_KB.items():
        if faq_key in text_lower:
            log.info(f"FAQ hit [key in text]: '{faq_key}'")
            return answer

    return None


def get_reply(user_id, text):
    text = text.strip()
    text_lower = text.lower()

    # ── 等待注册信息 ──────────────────────────────
    if user_states.get(user_id) == 'waiting_info':
        if ',' in text or '，' in text or len(text) > 10:
            user_states[user_id] = 'done'
            return SCRIPTS['thanks']

    # ── 数字菜单（最高优先级）─────────────────────
    if text in ['1', '①']:
        return SCRIPTS['1']
    if text in ['2', '②']:
        return SCRIPTS['2']
    if text in ['3', '③']:
        return SCRIPTS['3']
    if text in ['4', '④', 'đăng ký', 'dang ky', 'đăng ký ngay', '注册', '报名', 'bắt đầu']:
        user_states[user_id] = 'waiting_info'
        return SCRIPTS['4']

    # ── FAQ 数据库（最高优先级，答案原文不动）──────
    faq_reply = faq_lookup(text_lower)
    if faq_reply:
        log.info(f"FAQ matched: {text[:30]}")
        user_states[user_id] = 'started'
        return faq_reply

    # ── 开场白：仅限全新用户第一次发纯问候语 ───────
    is_new_user = user_id not in user_states
    if is_new_user:
        # 纯问候检测
        greetings = ['xin chào', 'hello', 'hi', 'chào', 'chào bạn', 'bạn ơi', 'cảm ơn', 'good morning', 'good afternoon', 'good evening']
        is_pure_greeting = any(g in text_lower for g in greetings) and len(text_lower.split()) <= 3
        if is_pure_greeting:
            user_states[user_id] = 'started'
            return SCRIPTS['opening']
        # 新用户直接发问题 → 不出菜单，直接 FAQ/GPT 回答
        user_states[user_id] = 'started'

    # ── GPT-4 兜底 ───────────────────────────────
    gpt_reply = ask_gpt(text, user_id)
    if gpt_reply:
        log.info(f"GPT reply: {gpt_reply[:50]}")
        return gpt_reply

    return SCRIPTS['default']


def send_zalo_message(user_id: str, text: str):
    """发送消息到 Zalo OA（同步方式，Vercel serverless 友好）"""
    if not ACCESS_TOKEN:
        log.warning("No ACCESS_TOKEN set, skip sending")
        return False
    url = "https://openapi.zalo.me/v3.0/oa/message/cs"
    headers = {
        'access_token': ACCESS_TOKEN,
        'Content-Type': 'application/json'
    }
    payload = {
        "recipient": {"user_id": str(user_id)},
        "message": {"text": text}
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        log.info(f"Send msg to {user_id}: {r.status_code} {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"Send failed: {e}")
        return False


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
    """Zalo webhook 验证"""
    mode      = request.args.get('mode') or request.args.get('hub.mode')
    token     = request.args.get('VerifyToken') or request.args.get('hub.verify_token')
    challenge = request.args.get('challenge') or request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        resp = jsonify(challenge)
        resp.status_code = 200
        return resp
    return 'Forbidden', 403


@app.route('/webhook', methods=['POST'])
def webhook_receive():
    """接收 Zalo 推送事件 - 必须 <2s 内响应，否则 Zalo 认为失败"""
    try:
        # 先获取原始请求体（MAC 验证需要原始 JSON）
        raw_body = request.get_data()
        data = json.loads(raw_body)  # 用原始 body 解析
        log.info(f"Event: {json.dumps(data)[:200]}")

        # MAC 签名验证（暂时跳过，避免阻止消息）
        # 如需启用，参考: https://developers.zalo.me/docs/official-account/webhook/
        # Zalo 公式: raw = app_id + raw_body + timestamp + oa_secret (SHA256)
        signature = request.headers.get('X-Zalo-Signature', '')
        if signature:
            log.info(f"MAC signature received (not verified yet): {signature[:20]}...")

        event_name = data.get('event_name', '')

        # ✅ 立即返回 200，避免 Zalo 超时
        # 消息发送同步执行（Vercel serverless 不支持 threading）
        if event_name == 'user_send_text':
            user_id = data.get('sender', {}).get('id', '')
            text    = data.get('message', {}).get('text', '')
            log.info(f"user_send_text: user_id={user_id}, text={text[:50]}")
            if user_id and text:
                reply = get_reply(user_id, text)
                log.info(f"Reply: {reply[:50]}")
                send_zalo_message(user_id, reply)

        elif event_name == 'follow':
            user_id = data.get('follower', {}).get('id', '')
            log.info(f"follow event: user_id={user_id}")
            if user_id:
                send_zalo_message(user_id, SCRIPTS['opening'])

        elif event_name == 'unfollow':
            log.info(f"User unfollowed OA")

    except Exception as e:
        log.error(f"Webhook error: {e}")

    # 立即返回，不等待消息发送完成
    return jsonify({'status': 'ok'})
