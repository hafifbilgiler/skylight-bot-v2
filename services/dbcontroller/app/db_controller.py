# db_controller.py - COMPLETE VERSION WITH ITERATIVE IMAGE GENERATION
"""
═══════════════════════════════════════════════════════════════
ONE-BUNE AI Platform - Database Controller Service
═══════════════════════════════════════════════════════════════
COMPLETE VERSION - All Tables + Iterative Image Generation

Version: 2.1.0
New: Iterative image generation support (modification tracking)
Purpose: Manage PostgreSQL schema only (no data seeding)
═══════════════════════════════════════════════════════════════
"""

import os
import sys
import time
import traceback
import datetime
from typing import Dict, Any

import psycopg2


# =====================================================
# LOGGING
# =====================================================

def get_log_time() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_info(message: str):
    print(f"[{get_log_time()}] [INFO] {message}", flush=True)


def log_success(message: str):
    print(f"[{get_log_time()}] [SUCCESS] ✅ {message}", flush=True)


def log_warning(message: str):
    print(f"[{get_log_time()}] [WARNING] ⚠️ {message}", flush=True)


def log_error(message: str):
    print(f"[{get_log_time()}] [ERROR] ❌ {message}", flush=True)


# =====================================================
# DATABASE CONNECTION
# =====================================================

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "postgres"),
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            port=os.getenv("DB_PORT", "5432"),
            connect_timeout=10,
        )
        return conn
    except psycopg2.OperationalError as e:
        log_error(f"Database connection failed: {e}")
        raise
    except Exception as e:
        log_error(f"Unexpected connection error: {e}")
        raise


def test_connection() -> bool:
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        log_success("Database connection test OK")
        return True
    except Exception as e:
        log_error(f"Connection test failed: {e}")
        return False


# =====================================================
# HELPERS
# =====================================================

def ensure_column(cur, table_name: str, column_name: str, column_def: str):
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = %s
        """,
        (table_name, column_name),
    )
    exists = cur.fetchone()

    if not exists:
        log_info(f"Adding missing column: {table_name}.{column_name}")
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def};")


def ensure_index(cur, index_name: str, index_sql: str):
    log_info(f"Ensuring index: {index_name}")
    cur.execute(index_sql)


def ensure_extension(cur, extension_name: str):
    log_info(f"Ensuring extension: {extension_name}")
    cur.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension_name}";')


def ensure_constraint(cur, table_name: str, constraint_name: str, constraint_sql: str):
    """Add constraint if it doesn't exist."""
    cur.execute(
        """
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_schema = 'public'
          AND table_name = %s
          AND constraint_name = %s
        """,
        (table_name, constraint_name),
    )
    exists = cur.fetchone()

    if not exists:
        log_info(f"Adding constraint: {table_name}.{constraint_name}")
        cur.execute(f"ALTER TABLE {table_name} ADD {constraint_sql};")


# =====================================================
# SCHEMA INITIALIZATION
# =====================================================

def init_database_schema() -> bool:
    log_info("=" * 70)
    log_info("DATABASE SCHEMA INITIALIZATION STARTED")
    log_info("Version: 2.1.0 - With Iterative Image Generation")
    log_info("=" * 70)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        log_success("PostgreSQL connection established")

        # =================================================
        # EXTENSIONS
        # =================================================
        try:
            ensure_extension(cur, "pgcrypto")
            log_success("Extensions OK")
        except Exception as e:
            log_warning(f"Extension setup warning: {e}")

        # =================================================
        # 1. USERS
        # =================================================
        log_info("Creating/checking USERS table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id              SERIAL PRIMARY KEY,
                google_id       VARCHAR(255) UNIQUE,
                email           VARCHAR(255) UNIQUE NOT NULL,
                password        VARCHAR(255),
                name            VARCHAR(255),
                picture         TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                last_login      TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        ensure_column(cur, "users", "password", "VARCHAR(255)")
        ensure_column(cur, "users", "name", "VARCHAR(255)")
        ensure_column(cur, "users", "picture", "TEXT")
        ensure_column(cur, "users", "created_at", "TIMESTAMPTZ DEFAULT NOW()")
        ensure_column(cur, "users", "last_login", "TIMESTAMPTZ DEFAULT NOW()")
        log_success("USERS table OK")

        # =================================================
        # 2. OTP_CODES
        # =================================================
        log_info("Creating/checking OTP_CODES table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS otp_codes (
                email           VARCHAR(255) PRIMARY KEY,
                code            VARCHAR(6) NOT NULL,
                expire_at       TIMESTAMPTZ NOT NULL,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        ensure_column(cur, "otp_codes", "code", "VARCHAR(6) NOT NULL")
        ensure_column(cur, "otp_codes", "expire_at", "TIMESTAMPTZ NOT NULL")
        ensure_column(cur, "otp_codes", "created_at", "TIMESTAMPTZ DEFAULT NOW()")

        ensure_index(
            cur,
            "idx_otp_codes_email_unique",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_otp_codes_email_unique
            ON otp_codes(email);
            """
        )

        log_success("OTP_CODES table OK")

        # =================================================
        # 3. CONVERSATIONS
        # =================================================
        log_info("Creating/checking CONVERSATIONS table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id                 INTEGER REFERENCES users(id) ON DELETE CASCADE,
                title                   VARCHAR(255) DEFAULT 'Yeni Sohbet',
                created_at              TIMESTAMPTZ DEFAULT NOW(),
                updated_at              TIMESTAMPTZ DEFAULT NOW(),
                is_pinned               BOOLEAN DEFAULT FALSE,
                is_archived             BOOLEAN DEFAULT FALSE,
                metadata                JSONB DEFAULT '{}'::jsonb,
                compaction_count        INTEGER DEFAULT 0,
                last_compaction_at      TIMESTAMPTZ,
                summary                 TEXT,
                total_tokens_used       INTEGER DEFAULT 0,
                context_window_size     INTEGER DEFAULT 8000
            );
            """
        )
        ensure_column(cur, "conversations", "title", "VARCHAR(255) DEFAULT 'Yeni Sohbet'")
        ensure_column(cur, "conversations", "created_at", "TIMESTAMPTZ DEFAULT NOW()")
        ensure_column(cur, "conversations", "updated_at", "TIMESTAMPTZ DEFAULT NOW()")
        ensure_column(cur, "conversations", "is_pinned", "BOOLEAN DEFAULT FALSE")
        ensure_column(cur, "conversations", "is_archived", "BOOLEAN DEFAULT FALSE")
        ensure_column(cur, "conversations", "metadata", "JSONB DEFAULT '{}'::jsonb")
        ensure_column(cur, "conversations", "compaction_count", "INTEGER DEFAULT 0")
        ensure_column(cur, "conversations", "last_compaction_at", "TIMESTAMPTZ")
        ensure_column(cur, "conversations", "summary", "TEXT")
        ensure_column(cur, "conversations", "total_tokens_used", "INTEGER DEFAULT 0")
        ensure_column(cur, "conversations", "context_window_size", "INTEGER DEFAULT 8000")
        log_success("CONVERSATIONS table OK")

        # =================================================
        # 4. MESSAGES
        # =================================================
        log_info("Creating/checking MESSAGES table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id     UUID REFERENCES conversations(id) ON DELETE CASCADE,
                role                VARCHAR(20) NOT NULL,
                content             TEXT NOT NULL,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                edited_at           TIMESTAMPTZ,
                is_edited           BOOLEAN DEFAULT FALSE,
                token_count         INTEGER,
                model_used          VARCHAR(100),
                metadata            JSONB DEFAULT '{}'::jsonb,
                mode                VARCHAR(50),
                has_image           BOOLEAN DEFAULT FALSE,
                intent              VARCHAR(100)
            );
            """
        )
        ensure_column(cur, "messages", "edited_at", "TIMESTAMPTZ")
        ensure_column(cur, "messages", "is_edited", "BOOLEAN DEFAULT FALSE")
        ensure_column(cur, "messages", "token_count", "INTEGER")
        ensure_column(cur, "messages", "model_used", "VARCHAR(100)")
        ensure_column(cur, "messages", "metadata", "JSONB DEFAULT '{}'::jsonb")
        ensure_column(cur, "messages", "mode", "VARCHAR(50)")
        ensure_column(cur, "messages", "has_image", "BOOLEAN DEFAULT FALSE")
        ensure_column(cur, "messages", "intent", "VARCHAR(100)")
        log_success("MESSAGES table OK")

        # =================================================
        # 5. CONVERSATION_SUMMARIES
        # =================================================
        log_info("Creating/checking CONVERSATION_SUMMARIES table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id         UUID REFERENCES conversations(id) ON DELETE CASCADE,
                summary_text            TEXT NOT NULL,
                messages_summarized     INTEGER NOT NULL,
                start_message_id        UUID,
                end_message_id          UUID,
                created_at              TIMESTAMPTZ DEFAULT NOW(),
                token_count             INTEGER,
                model_used              VARCHAR(100)
            );
            """
        )
        log_success("CONVERSATION_SUMMARIES table OK")

        # =================================================
        # 6. USER_INTENTS
        # =================================================
        log_info("Creating/checking USER_INTENTS table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_intents (
                id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                message_id          UUID REFERENCES messages(id) ON DELETE CASCADE,
                conversation_id     UUID REFERENCES conversations(id) ON DELETE CASCADE,
                user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE,
                detected_intent     VARCHAR(100),
                confidence_score    FLOAT,
                suggested_mode      VARCHAR(50),
                actual_mode         VARCHAR(50),
                created_at          TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        log_success("USER_INTENTS table OK")

        # =================================================
        # 7. USER_PREFERENCES
        # =================================================
        log_info("Creating/checking USER_PREFERENCES table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                theme               VARCHAR(20) DEFAULT 'dark',
                language            VARCHAR(10) DEFAULT 'tr',
                enable_rag          BOOLEAN DEFAULT TRUE,
                enable_smart_tools  BOOLEAN DEFAULT TRUE,
                auto_save           BOOLEAN DEFAULT TRUE,
                preferences         JSONB DEFAULT '{}'::jsonb,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        log_success("USER_PREFERENCES table OK")

        # =================================================
        # 8. USER_PROFILES
        # =================================================
        log_info("Creating/checking USER_PROFILES table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id                 INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                
                interests               JSONB NOT NULL DEFAULT '[]',
                dislikes                JSONB NOT NULL DEFAULT '[]',
                expertise_areas         JSONB NOT NULL DEFAULT '[]',
                follow_topics           JSONB NOT NULL DEFAULT '[]',
                conversation_style      VARCHAR(50) DEFAULT 'balanced',
                preferred_language      VARCHAR(10) DEFAULT 'tr',
                profession              VARCHAR(200),
                
                topics                  JSONB NOT NULL DEFAULT '[]',
                preferences             JSONB NOT NULL DEFAULT '{}',
                summary                 TEXT NOT NULL DEFAULT '',
                positive_topics         JSONB NOT NULL DEFAULT '[]',
                negative_topics         JSONB NOT NULL DEFAULT '[]',
                response_style          JSONB NOT NULL DEFAULT '{"detail_level": "medium", "language": "tr", "emoji": true}',
                
                total_likes             INTEGER NOT NULL DEFAULT 0,
                total_dislikes          INTEGER NOT NULL DEFAULT 0,
                
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        
        ensure_column(cur, "user_profiles", "interests", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(cur, "user_profiles", "dislikes", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(cur, "user_profiles", "expertise_areas", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(cur, "user_profiles", "follow_topics", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(cur, "user_profiles", "conversation_style", "VARCHAR(50) DEFAULT 'balanced'")
        ensure_column(cur, "user_profiles", "preferred_language", "VARCHAR(10) DEFAULT 'tr'")
        ensure_column(cur, "user_profiles", "profession", "VARCHAR(200)")
        ensure_column(cur, "user_profiles", "created_at", "TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        ensure_column(cur, "user_profiles", "topics", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(cur, "user_profiles", "preferences", "JSONB NOT NULL DEFAULT '{}'")
        ensure_column(cur, "user_profiles", "summary", "TEXT NOT NULL DEFAULT ''")
        ensure_column(cur, "user_profiles", "positive_topics", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(cur, "user_profiles", "negative_topics", "JSONB NOT NULL DEFAULT '[]'")
        ensure_column(
            cur,
            "user_profiles",
            "response_style",
            """JSONB NOT NULL DEFAULT '{"detail_level": "medium", "language": "tr", "emoji": true}'"""
        )
        ensure_column(cur, "user_profiles", "total_likes", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(cur, "user_profiles", "total_dislikes", "INTEGER NOT NULL DEFAULT 0")
        
        log_success("USER_PROFILES table OK")

        # =================================================
        # 9. FEEDBACK
        # =================================================
        log_info("Creating/checking FEEDBACK table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id                      SERIAL PRIMARY KEY,
                user_id                 INTEGER REFERENCES users(id) ON DELETE CASCADE,
                conversation_id         TEXT,
                user_query              TEXT NOT NULL DEFAULT '',
                assistant_response      TEXT NOT NULL DEFAULT '',
                rating                  SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
                intent                  VARCHAR(50) DEFAULT '',
                context_source          VARCHAR(50) DEFAULT '',
                comment                 TEXT DEFAULT '',
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        log_success("FEEDBACK table OK")

        # =================================================
        # 10. USER_LEARNING
        # =================================================
        log_info("Creating/checking USER_LEARNING table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_learning (
                id                  SERIAL PRIMARY KEY,
                user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE,
                topic               VARCHAR(100) NOT NULL,
                pattern_type        VARCHAR(50) NOT NULL DEFAULT 'topic_preference',
                pattern_value       TEXT NOT NULL DEFAULT '',
                score               FLOAT NOT NULL DEFAULT 0.0,
                sample_count        INTEGER NOT NULL DEFAULT 1,
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        log_success("USER_LEARNING table OK")

        # =================================================
        # 11. QUERY_LOGS
        # =================================================
        log_info("Creating/checking QUERY_LOGS table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS query_logs (
                id                  SERIAL PRIMARY KEY,
                user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
                conversation_id     TEXT,
                query               TEXT NOT NULL,
                intent              VARCHAR(50),
                context_source      VARCHAR(50),
                response_preview    TEXT DEFAULT '',
                response_length     INTEGER DEFAULT 0,
                duration_ms         INTEGER DEFAULT 0,
                model_used          VARCHAR(100) DEFAULT 'llama-3.1-8b',
                is_identity         BOOLEAN DEFAULT FALSE,
                is_weather          BOOLEAN DEFAULT FALSE,
                is_realtime         BOOLEAN DEFAULT FALSE,
                city_detected       VARCHAR(100) DEFAULT '',
                ip_address          VARCHAR(50) DEFAULT '',
                user_agent          TEXT DEFAULT '',
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        log_success("QUERY_LOGS table OK")

        # =================================================
        # 12. SUBSCRIPTION_PLANS
        # =================================================
        log_info("Creating/checking SUBSCRIPTION_PLANS table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id              VARCHAR(50) PRIMARY KEY,
                name            VARCHAR(100) NOT NULL,
                description     TEXT DEFAULT '',
                price_monthly   DECIMAL(10,2) NOT NULL DEFAULT 0,
                price_yearly    DECIMAL(10,2) NOT NULL DEFAULT 0,
                currency        VARCHAR(3) NOT NULL DEFAULT 'TRY',
                features        JSONB NOT NULL DEFAULT '{}',
                limits          JSONB NOT NULL DEFAULT '{}',
                is_active       BOOLEAN NOT NULL DEFAULT TRUE,
                sort_order      INTEGER NOT NULL DEFAULT 0,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        log_success("SUBSCRIPTION_PLANS table OK")

        # =================================================
        # 13. USER_SUBSCRIPTIONS
        # =================================================
        log_info("Creating/checking USER_SUBSCRIPTIONS table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id                          SERIAL PRIMARY KEY,
                user_id                     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                plan_id                     VARCHAR(50) NOT NULL REFERENCES subscription_plans(id),
                status                      VARCHAR(20) NOT NULL DEFAULT 'active'
                                            CHECK (status IN ('active', 'cancelled', 'expired', 'past_due', 'trialing')),
                billing_period              VARCHAR(10) NOT NULL DEFAULT 'monthly'
                                            CHECK (billing_period IN ('monthly', 'yearly', 'lifetime', 'free')),
                current_period_start        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                current_period_end          TIMESTAMPTZ,
                cancel_at_period_end        BOOLEAN NOT NULL DEFAULT FALSE,
                iyzico_subscription_ref     VARCHAR(255),
                iyzico_customer_ref         VARCHAR(255),
                iyzico_payment_method       VARCHAR(50),
                trial_end                   TIMESTAMPTZ,
                cancelled_at                TIMESTAMPTZ,
                metadata                    JSONB NOT NULL DEFAULT '{}',
                created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        ensure_index(
            cur,
            "idx_user_subscriptions_active",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_subscriptions_active
            ON user_subscriptions(user_id)
            WHERE status IN ('active', 'trialing');
            """
        )
        log_success("USER_SUBSCRIPTIONS table OK")

        # =================================================
        # 14. PAYMENT_HISTORY
        # =================================================
        log_info("Creating/checking PAYMENT_HISTORY table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_history (
                id                      SERIAL PRIMARY KEY,
                user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subscription_id         INTEGER REFERENCES user_subscriptions(id) ON DELETE SET NULL,
                plan_id                 VARCHAR(50) NOT NULL,
                amount                  DECIMAL(10,2) NOT NULL,
                currency                VARCHAR(3) NOT NULL DEFAULT 'TRY',
                status                  VARCHAR(20) NOT NULL DEFAULT 'pending'
                                        CHECK (status IN ('pending', 'completed', 'failed', 'refunded', 'cancelled')),
                payment_method          VARCHAR(50) DEFAULT '',
                iyzico_payment_id       VARCHAR(255),
                iyzico_conversation_id  VARCHAR(255),
                iyzico_fraud_status     VARCHAR(20),
                iyzico_raw_result       JSONB DEFAULT '{}',
                invoice_number          VARCHAR(50),
                billing_name            VARCHAR(255),
                billing_email           VARCHAR(255),
                billing_address         TEXT DEFAULT '',
                description             TEXT DEFAULT '',
                error_message           TEXT DEFAULT '',
                metadata                JSONB NOT NULL DEFAULT '{}',
                paid_at                 TIMESTAMPTZ,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        log_success("PAYMENT_HISTORY table OK")

        # =================================================
        # 15. USAGE_TRACKING
        # =================================================
        log_info("Creating/checking USAGE_TRACKING table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_tracking (
                id                  SERIAL PRIMARY KEY,
                user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                usage_date          DATE NOT NULL DEFAULT CURRENT_DATE,
                messages_sent       INTEGER NOT NULL DEFAULT 0,
                modes_used          JSONB NOT NULL DEFAULT '{}',
                tokens_used         INTEGER NOT NULL DEFAULT 0,
                files_uploaded      INTEGER NOT NULL DEFAULT 0,
                images_generated    INTEGER NOT NULL DEFAULT 0,
                web_searches        INTEGER NOT NULL DEFAULT 0,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        ensure_index(
            cur,
            "idx_usage_tracking_user_date",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_tracking_user_date
            ON usage_tracking(user_id, usage_date);
            """
        )
        log_success("USAGE_TRACKING table OK")

        # =================================================
        # 16. GENERATED_IMAGES (WITH ITERATIVE GENERATION SUPPORT)
        # =================================================
        log_info("Creating/checking GENERATED_IMAGES table...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_images (
                id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                conversation_id         UUID REFERENCES conversations(id) ON DELETE CASCADE,
                
                -- Prompts (for iterative generation)
                prompt_turkish          TEXT NOT NULL,
                prompt_english          TEXT NOT NULL,
                user_prompt             TEXT,
                generated_prompt        TEXT,
                modification_of         UUID,
                
                -- Image Data
                image_url               TEXT,
                image_b64               TEXT,
                image_hash              VARCHAR(64),
                image_size_bytes        INTEGER DEFAULT 0,
                
                -- Metadata
                model_used              VARCHAR(100) DEFAULT 'FLUX-2-max',
                generation_cost         DECIMAL(10,4) DEFAULT 0,
                generation_time_ms      INTEGER DEFAULT 0,
                
                -- Expiration (3 days)
                expires_at              TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '3 days'),
                is_deleted              BOOLEAN DEFAULT FALSE,
                deleted_at              TIMESTAMPTZ,
                
                -- Timestamps
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        
        # Ensure new columns for iterative generation
        ensure_column(cur, "generated_images", "user_prompt", "TEXT")
        ensure_column(cur, "generated_images", "generated_prompt", "TEXT")
        ensure_column(cur, "generated_images", "modification_of", "UUID")
        
        # Add foreign key constraint for modification_of (if not exists)
        try:
            ensure_constraint(
                cur,
                "generated_images",
                "fk_generated_images_modification",
                """
                CONSTRAINT fk_generated_images_modification 
                FOREIGN KEY (modification_of) 
                REFERENCES generated_images(id) 
                ON DELETE SET NULL
                """
            )
        except Exception as e:
            log_warning(f"Constraint warning (may already exist): {e}")
        
        # Indexes for performance
        ensure_index(
            cur,
            "idx_generated_images_user_id",
            "CREATE INDEX IF NOT EXISTS idx_generated_images_user_id ON generated_images(user_id);"
        )
        ensure_index(
            cur,
            "idx_generated_images_conversation_id",
            "CREATE INDEX IF NOT EXISTS idx_generated_images_conversation_id ON generated_images(conversation_id);"
        )
        ensure_index(
            cur,
            "idx_generated_images_expires_at",
            "CREATE INDEX IF NOT EXISTS idx_generated_images_expires_at ON generated_images(expires_at) WHERE is_deleted = FALSE;"
        )
        ensure_index(
            cur,
            "idx_generated_images_hash",
            "CREATE INDEX IF NOT EXISTS idx_generated_images_hash ON generated_images(image_hash);"
        )
        ensure_index(
            cur,
            "idx_generated_images_modification",
            "CREATE INDEX IF NOT EXISTS idx_generated_images_modification ON generated_images(modification_of);"
        )
        ensure_index(
            cur,
            "idx_generated_images_created_at",
            "CREATE INDEX IF NOT EXISTS idx_generated_images_created_at ON generated_images(created_at DESC);"
        )
        
        log_success("GENERATED_IMAGES table OK (with iterative generation support)")

        # =================================================
        # 17. CHAT_LOGS (LEGACY)
        # =================================================
        log_info("Creating/checking CHAT_LOGS table (legacy)...")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_logs (
                id              SERIAL PRIMARY KEY,
                user_id         VARCHAR(255),
                prompt          TEXT,
                response        TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        log_success("CHAT_LOGS table OK (legacy)")

        # =================================================
        # INDEXES
        # =================================================
        log_info("Creating indexes...")

        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);",
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);",

            "CREATE INDEX IF NOT EXISTS idx_otp_email ON otp_codes(email);",

            "CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_conversations_pinned ON conversations(is_pinned) WHERE is_pinned = TRUE;",
            "CREATE INDEX IF NOT EXISTS idx_conversations_compaction ON conversations(compaction_count, last_compaction_at);",

            "CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);",
            "CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(role);",
            "CREATE INDEX IF NOT EXISTS idx_messages_mode ON messages(mode);",

            "CREATE INDEX IF NOT EXISTS idx_conv_summaries_conv_id ON conversation_summaries(conversation_id);",
            "CREATE INDEX IF NOT EXISTS idx_conv_summaries_created ON conversation_summaries(created_at DESC);",

            "CREATE INDEX IF NOT EXISTS idx_user_intents_conv_id ON user_intents(conversation_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_intents_user_id ON user_intents(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_intents_intent ON user_intents(detected_intent);",

            "CREATE INDEX IF NOT EXISTS idx_user_profiles_updated ON user_profiles(updated_at DESC);",

            "CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback(rating);",
            "CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC);",

            "CREATE INDEX IF NOT EXISTS idx_user_learning_user_id ON user_learning(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_learning_topic ON user_learning(topic);",

            "CREATE INDEX IF NOT EXISTS idx_query_logs_user_id ON query_logs(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_query_logs_created_at ON query_logs(created_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_query_logs_intent ON query_logs(intent);",

            "CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_subscriptions_status ON user_subscriptions(status);",
            "CREATE INDEX IF NOT EXISTS idx_user_subscriptions_plan ON user_subscriptions(plan_id);",
            "CREATE INDEX IF NOT EXISTS idx_user_subscriptions_period_end ON user_subscriptions(current_period_end);",

            "CREATE INDEX IF NOT EXISTS idx_payment_history_user ON payment_history(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_payment_history_status ON payment_history(status);",
            "CREATE INDEX IF NOT EXISTS idx_payment_history_created ON payment_history(created_at DESC);",
            "CREATE INDEX IF NOT EXISTS idx_payment_history_iyzico ON payment_history(iyzico_payment_id);",

            "CREATE INDEX IF NOT EXISTS idx_usage_tracking_user ON usage_tracking(user_id);",
            "CREATE INDEX IF NOT EXISTS idx_usage_tracking_date ON usage_tracking(usage_date);",
        ]

        for stmt in index_statements:
            cur.execute(stmt)

        # GIN indexes for JSONB
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_content_search
            ON messages USING gin(to_tsvector('simple', content));
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_profiles_interests
            ON user_profiles USING gin(interests);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_profiles_expertise
            ON user_profiles USING gin(expertise_areas);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_profiles_topics
            ON user_profiles USING gin(topics);
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_learning_unique
            ON user_learning(user_id, topic, pattern_type);
            """
        )

        log_success("Indexes OK")

        # =================================================
        # TRIGGERS
        # =================================================
        log_info("Creating triggers...")

        cur.execute(
            """
            CREATE OR REPLACE FUNCTION update_updated_at_column()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = NOW();
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )

        trigger_tables = [
            "conversations",
            "user_profiles",
            "user_learning",
            "user_subscriptions",
            "usage_tracking",
            "user_preferences",
            "subscription_plans",
        ]

        for tbl in trigger_tables:
            cur.execute(f"DROP TRIGGER IF EXISTS update_{tbl}_updated_at ON {tbl};")
            cur.execute(
                f"""
                CREATE TRIGGER update_{tbl}_updated_at
                BEFORE UPDATE ON {tbl}
                FOR EACH ROW
                EXECUTE FUNCTION update_updated_at_column();
                """
            )

        log_success("Triggers OK")

        conn.commit()
        cur.close()
        conn.close()

        log_success("Schema initialization completed successfully")
        log_info("=" * 70)
        return True

    except psycopg2.Error as e:
        log_error(f"PostgreSQL error: {e}")
        traceback.print_exc()
        return False
    except Exception as e:
        log_error(f"Unexpected error: {e}")
        traceback.print_exc()
        return False


# =====================================================
# HEALTH CHECK
# =====================================================

def health_check() -> Dict[str, Any]:
    status = {
        "status": "unknown",
        "timestamp": datetime.datetime.now().isoformat(),
        "checks": {}
    }

    try:
        status["checks"]["database_connection"] = test_connection()
    except Exception as e:
        status["checks"]["database_connection"] = False
        status["error"] = str(e)

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        required_tables = [
            "users",
            "otp_codes",
            "conversations",
            "messages",
            "conversation_summaries",
            "user_intents",
            "user_profiles",
            "query_logs",
            "subscription_plans",
            "user_subscriptions",
            "usage_tracking",
            "generated_images",
        ]

        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            """
        )
        existing_tables = [r[0] for r in cur.fetchall()]
        missing_tables = set(required_tables) - set(existing_tables)

        status["checks"]["required_tables"] = len(missing_tables) == 0
        if missing_tables:
            status["missing_tables"] = list(missing_tables)

        cur.close()
        conn.close()

    except Exception as e:
        status["checks"]["required_tables"] = False
        status["error"] = str(e)

    status["status"] = "healthy" if all(status["checks"].values()) else "unhealthy"
    return status


# =====================================================
# MAIN
# =====================================================

def main():
    log_info("🚀 ONE-BUNE DATABASE CONTROLLER SERVICE STARTING...")
    log_info("📦 Version: 2.1.0 - With Iterative Image Generation")
    log_info(f"🐘 PostgreSQL Host: {os.getenv('DB_HOST', 'postgres')}")
    log_info(f"🗄️ Database Name: {os.getenv('DB_NAME', 'N/A')}")
    log_info("=" * 70)

    max_retries = 30
    retry_delay = 2

    for attempt in range(1, max_retries + 1):
        log_info(f"Attempt {attempt}/{max_retries}: Testing database connection...")

        if test_connection():
            log_success("Database is ready!")
            break

        if attempt < max_retries:
            log_warning(f"Connection failed. Retrying in {retry_delay}s...")
            time.sleep(retry_delay)
        else:
            log_error("Max retries reached. Database not available.")
            sys.exit(1)

    log_info("Initializing database schema...")
    success = init_database_schema()

    if not success:
        log_error("Schema initialization failed!")
        sys.exit(1)

    log_success("Schema initialization complete!")

    health = health_check()
    log_info(f"Health status: {health['status']}")

    if health["status"] != "healthy":
        log_error(f"Health check failed: {health}")
        sys.exit(1)

    check_interval = int(os.getenv("CHECK_INTERVAL", "3600"))
    log_info(f"⏰ Starting periodic health checks (every {check_interval}s)")
    log_info("=" * 70)

    while True:
        try:
            time.sleep(check_interval)

            log_info("🔄 Running periodic health check...")
            health = health_check()

            if health["status"] == "healthy":
                log_success("✅ All systems operational")
            else:
                log_warning(f"⚠️ Health check issues: {health}")
                log_info("Attempting schema re-check...")
                init_database_schema()

        except KeyboardInterrupt:
            log_info("Shutdown signal received. Exiting gracefully...")
            break
        except Exception as e:
            log_error(f"Periodic check error: {e}")
            traceback.print_exc()
            time.sleep(60)


if __name__ == "__main__":
    main()