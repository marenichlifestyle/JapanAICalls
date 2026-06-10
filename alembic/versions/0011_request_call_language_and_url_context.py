"""request call language and url context

Revision ID: 0011_req_call_lang_ctx
Revises: 0010_request_call
Create Date: 2026-06-09
"""

from alembic import op


revision = "0011_req_call_lang_ctx"
down_revision = "0010_request_call"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_call_campaigns ADD COLUMN IF NOT EXISTS call_language VARCHAR(8)")
    op.execute("ALTER TABLE request_call_campaigns ADD COLUMN IF NOT EXISTS phone_region VARCHAR(8)")
    op.execute("ALTER TABLE request_call_campaigns ADD COLUMN IF NOT EXISTS source_urls_json JSON")
    op.execute("ALTER TABLE request_call_campaigns ADD COLUMN IF NOT EXISTS vehicle_context_json JSON")
    op.execute("ALTER TABLE dealer_call_targets ADD COLUMN IF NOT EXISTS phone_region VARCHAR(8)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_request_call_campaigns_call_language ON request_call_campaigns (call_language)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_request_call_campaigns_phone_region ON request_call_campaigns (phone_region)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_dealer_call_targets_phone_region ON dealer_call_targets (phone_region)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dealer_call_targets_phone_region")
    op.execute("DROP INDEX IF EXISTS ix_request_call_campaigns_phone_region")
    op.execute("DROP INDEX IF EXISTS ix_request_call_campaigns_call_language")
    op.execute("ALTER TABLE dealer_call_targets DROP COLUMN IF EXISTS phone_region")
    op.execute("ALTER TABLE request_call_campaigns DROP COLUMN IF EXISTS vehicle_context_json")
    op.execute("ALTER TABLE request_call_campaigns DROP COLUMN IF EXISTS source_urls_json")
    op.execute("ALTER TABLE request_call_campaigns DROP COLUMN IF EXISTS phone_region")
    op.execute("ALTER TABLE request_call_campaigns DROP COLUMN IF EXISTS call_language")
