-- schema.sql

-- ── 1. USERS ────────────────────────────────────────────────────────────────
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    phone_number    VARCHAR(20) UNIQUE NOT NULL,
    name            VARCHAR(100),
    is_registered   BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── 2. ACCOUNTS ─────────────────────────────────────────────────────────────
CREATE TABLE accounts (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    nickname         VARCHAR(100) NOT NULL,        -- "HDFC Salary", "Axis CC"
    bank_name        VARCHAR(100),                 -- "HDFC", "SBI", "Axis"

    -- Category: the group shown in WhatsApp list menu
    account_category VARCHAR(20) NOT NULL
                     CHECK (account_category IN ('bank', 'card', 'digital', 'cash')),

    -- Type: the specific type within that category
    account_type     VARCHAR(20) NOT NULL
                     CHECK (account_type IN (
                         -- bank
                         'savings', 'current', 'salary',
                         -- card
                         'debit_card', 'credit_card', 'prepaid_card',
                         -- digital
                         'wallet', 'upi',
                         -- cash
                         'cash'
                     )),

    balance          NUMERIC(12, 2) DEFAULT 0.00,  -- for bank/digital/cash
    credit_limit     NUMERIC(12, 2),               -- only for credit_card
    outstanding      NUMERIC(12, 2) DEFAULT 0.00,  -- only for credit_card (amount owed)

    is_default       BOOLEAN DEFAULT FALSE,         -- ⭐ shown first in pickers
    is_active        BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ DEFAULT NOW(),

    -- Validation: credit_limit only makes sense on credit cards
    CONSTRAINT credit_limit_only_for_cc
        CHECK (credit_limit IS NULL OR account_type = 'credit_card'),

    -- Validation: account_type must belong to account_category
    CONSTRAINT type_matches_category CHECK (
        (account_category = 'bank'    AND account_type IN ('savings','current','salary'))    OR
        (account_category = 'card'    AND account_type IN ('debit_card','credit_card','prepaid_card')) OR
        (account_category = 'digital' AND account_type IN ('wallet','upi'))                           OR
        (account_category = 'cash'    AND account_type = 'cash')
    )
);

-- Only one default account allowed per user
CREATE UNIQUE INDEX one_default_per_user
    ON accounts(user_id) WHERE is_default = TRUE;

-- Useful indexes for common bot queries
CREATE INDEX idx_accounts_user    ON accounts(user_id);
CREATE INDEX idx_accounts_default ON accounts(user_id, is_default);

-- ── 3. SUBSCRIPTIONS ────────────────────────────────────────────────────────
-- Created before transactions because transactions references it
CREATE TABLE subscriptions (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id          INTEGER REFERENCES accounts(id),
    service_name        VARCHAR(200) NOT NULL,
    category            VARCHAR(100),
    amount              NUMERIC(12, 2) NOT NULL,
    billing_day         SMALLINT CHECK (billing_day BETWEEN 1 AND 31),
    next_billing_date   DATE,
    status              VARCHAR(20) DEFAULT 'active'
                        CHECK (status IN ('active', 'paused', 'cancelled', 'trial')),
    trial_end_date      DATE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── 4. TRANSACTIONS ─────────────────────────────────────────────────────────
CREATE TABLE transactions (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id          INTEGER REFERENCES accounts(id),
    amount              NUMERIC(12, 2) NOT NULL,
    type                VARCHAR(20) NOT NULL
                        CHECK (type IN ('expense', 'income', 'transfer')),
    category            VARCHAR(100),
    sub_category        VARCHAR(100),
    merchant            VARCHAR(200),
    is_essential        BOOLEAN DEFAULT TRUE,
    notes               TEXT,
    transaction_date    TIMESTAMPTZ DEFAULT NOW(),
    to_account_id       INTEGER REFERENCES accounts(id),
    subscription_id     INTEGER REFERENCES subscriptions(id),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ── 5. BUDGETS ──────────────────────────────────────────────────────────────
CREATE TABLE budgets (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category        VARCHAR(100) NOT NULL,
    monthly_limit   NUMERIC(12, 2) NOT NULL,
    month_year      CHAR(7) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, category, month_year)
);

-- ── 6. INCOME DEDUCTIONS ────────────────────────────────────────────────────
CREATE TABLE income_deductions (
    id              SERIAL PRIMARY KEY,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    deduction_type  VARCHAR(50) NOT NULL,
    amount          NUMERIC(12, 2) NOT NULL
);

-- ── 7. WHATSAPP BOT SESSIONS ────────────────────────────────────────────────
CREATE TABLE whatsapp_bot_sessions (
    id              SERIAL PRIMARY KEY,
    phone_number    VARCHAR(20) UNIQUE NOT NULL,
    session_status  VARCHAR(50) DEFAULT 'idle',
    parsed_result   JSONB DEFAULT '{}',
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── 8. ANALYTICS CACHE ──────────────────────────────────────────────────────
CREATE TABLE analytics_cache (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    default_balance     NUMERIC(12, 2),
    budget_pct_used     SMALLINT,
    next_bill_text      VARCHAR(200),
    refreshed_at        TIMESTAMPTZ DEFAULT NOW()
);