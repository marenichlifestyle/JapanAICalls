"""request call service messages

Revision ID: 0013_req_call_service_msgs
Revises: 0012_ai_quality_score
Create Date: 2026-06-10
"""

from alembic import op


revision = "0013_req_call_service_msgs"
down_revision = "0012_ai_quality_score"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE request_call_campaigns ADD COLUMN IF NOT EXISTS telegram_service_message_ids JSON")


def downgrade() -> None:
    op.execute("ALTER TABLE request_call_campaigns DROP COLUMN IF EXISTS telegram_service_message_ids")
