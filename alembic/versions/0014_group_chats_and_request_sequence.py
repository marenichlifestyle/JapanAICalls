"""group chats and request call sequence mode

Revision ID: 0014_group_chats_seq
Revises: 0013_req_call_service_msgs
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_group_chats_seq"
down_revision = "0013_req_call_service_msgs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "telegram_chat_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "telegram_user_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )

    with op.batch_alter_table("request_call_campaigns") as batch_op:
        batch_op.alter_column(
            "telegram_chat_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "telegram_user_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )
        batch_op.add_column(
            sa.Column(
                "call_sequence_mode",
                sa.String(length=16),
                nullable=False,
                server_default="manual",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("request_call_campaigns") as batch_op:
        batch_op.drop_column("call_sequence_mode")
        batch_op.alter_column(
            "telegram_user_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "telegram_chat_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "telegram_user_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "telegram_chat_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
