"""request call campaigns

Revision ID: 0010_request_call
Revises: 0009_job_vin_stock
Create Date: 2026-05-26
"""

from alembic import op

revision = "0010_request_call"
down_revision = "0009_job_vin_stock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS request_call_campaigns (
            id SERIAL PRIMARY KEY,
            telegram_chat_id INTEGER NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            telegram_source_message_id INTEGER,
            mode VARCHAR(32) NOT NULL,
            raw_input TEXT,
            raw_user_goal TEXT,
            normalized_goal_summary TEXT,
            status VARCHAR(64) NOT NULL,
            total_numbers INTEGER NOT NULL,
            valid_numbers INTEGER NOT NULL,
            invalid_numbers INTEGER NOT NULL,
            rejected_phones_json JSON,
            goal_meta_json JSON,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_request_call_campaigns_status ON request_call_campaigns (status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_request_call_campaigns_telegram_chat_id "
        "ON request_call_campaigns (telegram_chat_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_request_call_campaigns_telegram_user_id "
        "ON request_call_campaigns (telegram_user_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dealer_call_targets (
            id SERIAL PRIMARY KEY,
            campaign_id INTEGER NOT NULL REFERENCES request_call_campaigns(id),
            dealer_name TEXT NOT NULL,
            city VARCHAR(128),
            phone_raw VARCHAR(64) NOT NULL,
            phone_e164 VARCHAR(32) NOT NULL,
            original_line TEXT NOT NULL,
            goal_ru TEXT,
            status VARCHAR(64) NOT NULL,
            attempt INTEGER NOT NULL,
            last_call_job_id INTEGER,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_dealer_call_targets_campaign_id ON dealer_call_targets (campaign_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dealer_call_targets_phone_e164 ON dealer_call_targets (phone_e164)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dealer_call_targets_status ON dealer_call_targets (status)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS call_reports (
            id SERIAL PRIMARY KEY,
            campaign_id INTEGER NOT NULL REFERENCES request_call_campaigns(id),
            target_id INTEGER NOT NULL REFERENCES dealer_call_targets(id),
            dealer_name TEXT NOT NULL,
            phone_e164 VARCHAR(32) NOT NULL,
            call_status VARCHAR(64) NOT NULL,
            reached_sales BOOLEAN,
            target_vehicle_or_task TEXT,
            summary TEXT,
            availability_result TEXT,
            incoming_result TEXT,
            price_result TEXT,
            configuration_result TEXT,
            vin_or_stock_result TEXT,
            payment_result TEXT,
            paperwork_result TEXT,
            important_notes TEXT,
            next_action TEXT,
            raw_report_json JSON,
            created_at TIMESTAMPTZ DEFAULT now() NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_call_reports_campaign_id ON call_reports (campaign_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_call_reports_call_status ON call_reports (call_status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_call_reports_target_id ON call_reports (target_id)")

    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS request_campaign_id INTEGER")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS request_target_id INTEGER")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS request_goal_ru TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_request_campaign_id ON jobs (request_campaign_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_request_target_id ON jobs (request_target_id)")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_jobs_request_campaign_id'
            ) THEN
                ALTER TABLE jobs
                ADD CONSTRAINT fk_jobs_request_campaign_id
                FOREIGN KEY (request_campaign_id) REFERENCES request_call_campaigns(id);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_jobs_request_target_id'
            ) THEN
                ALTER TABLE jobs
                ADD CONSTRAINT fk_jobs_request_target_id
                FOREIGN KEY (request_target_id) REFERENCES dealer_call_targets(id);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS fk_jobs_request_target_id")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS fk_jobs_request_campaign_id")
    op.execute("DROP INDEX IF EXISTS ix_jobs_request_target_id")
    op.execute("DROP INDEX IF EXISTS ix_jobs_request_campaign_id")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS request_goal_ru")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS request_target_id")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS request_campaign_id")
    op.execute("DROP TABLE IF EXISTS call_reports")
    op.execute("DROP TABLE IF EXISTS dealer_call_targets")
    op.execute("DROP TABLE IF EXISTS request_call_campaigns")
