"""request call owner identity

Revision ID: 0015_req_call_owner_identity
Revises: 0014_group_chats_seq
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_req_call_owner_identity"
down_revision = "0014_group_chats_seq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("request_call_campaigns") as batch_op:
        batch_op.add_column(sa.Column("telegram_username", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("telegram_user_display_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("request_call_campaigns") as batch_op:
        batch_op.drop_column("telegram_user_display_name")
        batch_op.drop_column("telegram_username")
