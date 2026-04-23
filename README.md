# Finviet Zalo OA Webhook

泡泡 (Bong Bong) - KINDLITE VIET NAM Zalo 自动回复机器人

## 环境变量（在 Vercel 后台配置）

| 变量名 | 说明 |
|--------|------|
| ZALO_APP_ID | Zalo App ID |
| ZALO_ACCESS_TOKEN | Zalo Access Token |
| ZALO_VERIFY_TOKEN | Webhook 验证 Token（默认 finviet_webhook_2026） |
| CRMAPIBASE | CRM 主系统 Production URL，如 `https://merchant-visit-mvp.vercel.app` |
| CRMSERVICEKEY | 与 CRM 主系统约定的共享密钥（须与 CRM 侧 `ZALO_SERVICE_KEY` 一致） |

### CRM 接入（集成 revision `5550908`）

CRM 主系统 handoff 文档：`merchant-visit-mvp/exports/zalo-integration-handoff.md`

联调顺序：
1. `GET /api/zalo/health`（确认 CRM 服务在线）
2. `GET /api/zalo/crm/list`（查客户列表）
3. `POST /api/zalo/crm/collision-check`（防撞预检）
4. `POST /api/zalo/crm/report`（提交报备）

## 接口

- `GET /` — 状态检查
- `GET /health` — 健康检查
- `GET /webhook` — Zalo webhook 验证
- `POST /webhook` — 接收 Zalo 事件
- `GET /zalo_verifier_1501034389927564920.html` — 域名验证文件
