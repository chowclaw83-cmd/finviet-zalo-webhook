-- ============================================================
-- Finviet Zalo Bot - Supabase 数据库建表脚本
-- 在 Supabase 后台 SQL Editor 里执行一次即可
-- ============================================================

-- 1. 用户状态表（持久化对话状态）
CREATE TABLE IF NOT EXISTS zalo_user_states (
    user_id         TEXT PRIMARY KEY,
    user_type       TEXT DEFAULT 'merchant',  -- merchant | salesman
    conv_state      TEXT DEFAULT 'new',       -- new | started | waiting_info | done | unfollowed
    salesman_name   TEXT,
    salesman_phone  TEXT,
    salesman_city   TEXT,
    notes           TEXT,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 消息日志表
CREATE TABLE IF NOT EXISTS zalo_message_logs (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    direction       TEXT NOT NULL,        -- in | out
    text            TEXT,
    matched_faq     TEXT,                 -- 命中的 FAQ key
    matched_type    TEXT,                 -- keyword_match | direct_key_match | salesman_faq | extra_faq | gpt | fallback | menu | greeting | lead
    user_type       TEXT DEFAULT 'merchant',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_msg_user_id ON zalo_message_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_msg_created ON zalo_message_logs(created_at DESC);

-- 3. 线索表（商家/业务员留资）
CREATE TABLE IF NOT EXISTS zalo_leads (
    user_id         TEXT PRIMARY KEY,
    name            TEXT,
    city            TEXT,
    phone           TEXT,
    user_type       TEXT DEFAULT 'merchant',  -- merchant | salesman
    status          TEXT DEFAULT 'new',       -- new | contacted | signed | rejected
    admin_notes     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON zalo_leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_city ON zalo_leads(city);

-- 4. 未命中问题表（用于后台补充 FAQ）
CREATE TABLE IF NOT EXISTS zalo_unmatched_queries (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    text            TEXT NOT NULL,
    user_type       TEXT DEFAULT 'merchant',
    status          TEXT DEFAULT 'pending',  -- pending | converted | ignored
    faq_key         TEXT,                    -- 转为FAQ后填写
    admin_notes     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_unmatched_status ON zalo_unmatched_queries(status);

-- 5. 动态 FAQ 表（后台可增删改，机器人实时读取）
CREATE TABLE IF NOT EXISTS zalo_faq_extra (
    id              BIGSERIAL PRIMARY KEY,
    keyword         TEXT NOT NULL UNIQUE,   -- 触发关键词（全小写）
    answer          TEXT NOT NULL,           -- 回答内容
    user_type       TEXT DEFAULT 'all',      -- all | merchant | salesman
    active          BOOLEAN DEFAULT TRUE,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- Row Level Security（关闭，用 API Key 控制访问）
-- ============================================================
ALTER TABLE zalo_user_states    DISABLE ROW LEVEL SECURITY;
ALTER TABLE zalo_message_logs   DISABLE ROW LEVEL SECURITY;
ALTER TABLE zalo_leads          DISABLE ROW LEVEL SECURITY;
ALTER TABLE zalo_unmatched_queries DISABLE ROW LEVEL SECURITY;
ALTER TABLE zalo_faq_extra      DISABLE ROW LEVEL SECURITY;
