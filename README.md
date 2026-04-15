# Finviet Zalo OA Webhook

泡泡 (Bong Bong) - KINDLITE VIET NAM Zalo 自动回复机器人

## 环境变量（在 Vercel 后台配置）

| 变量名 | 说明 |
|--------|------|
| ZALO_APP_ID | Zalo App ID |
| ZALO_ACCESS_TOKEN | Zalo Access Token |
| ZALO_VERIFY_TOKEN | Webhook 验证 Token（默认 finviet_webhook_2026） |

## 接口

- `GET /` — 状态检查
- `GET /health` — 健康检查
- `GET /webhook` — Zalo webhook 验证
- `POST /webhook` — 接收 Zalo 事件
- `GET /zalo_verifier_1501034389927564920.html` — 域名验证文件
