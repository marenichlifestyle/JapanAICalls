"""dealer phone contexts

Revision ID: 0016_dealer_phone_contexts
Revises: 0015_req_call_owner_identity
Create Date: 2026-06-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_dealer_phone_contexts"
down_revision = "0015_req_call_owner_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dealer_phone_contexts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("phone_e164", sa.String(length=32), nullable=False),
        sa.Column("phone_region", sa.String(length=8), nullable=True),
        sa.Column("last_dealer_name", sa.Text(), nullable=True),
        sa.Column("last_campaign_id", sa.Integer(), nullable=True),
        sa.Column("last_target_id", sa.Integer(), nullable=True),
        sa.Column("last_report_id", sa.Integer(), nullable=True),
        sa.Column("last_called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("successful_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_items_json", sa.JSON(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dealer_phone_contexts_phone_e164", "dealer_phone_contexts", ["phone_e164"], unique=True)
    op.create_index("ix_dealer_phone_contexts_last_campaign_id", "dealer_phone_contexts", ["last_campaign_id"])
    op.create_index("ix_dealer_phone_contexts_last_target_id", "dealer_phone_contexts", ["last_target_id"])
    op.create_index("ix_dealer_phone_contexts_last_report_id", "dealer_phone_contexts", ["last_report_id"])


def downgrade() -> None:
    op.drop_index("ix_dealer_phone_contexts_last_report_id", table_name="dealer_phone_contexts")
    op.drop_index("ix_dealer_phone_contexts_last_target_id", table_name="dealer_phone_contexts")
    op.drop_index("ix_dealer_phone_contexts_last_campaign_id", table_name="dealer_phone_contexts")
    op.drop_index("ix_dealer_phone_contexts_phone_e164", table_name="dealer_phone_contexts")
    op.drop_table("dealer_phone_contexts")
