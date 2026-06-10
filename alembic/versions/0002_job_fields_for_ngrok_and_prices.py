"""add job fields for car and price split

Revision ID: 0002_job_fields
Revises: 0001_init
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_job_fields"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("car_full", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("car_short", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("price_total_source_text", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("vehicle_price_source_text", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("price_confidence", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("price_used_jpy", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("price_used_type", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("price_used_spoken_ru", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "price_used_spoken_ru")
    op.drop_column("jobs", "price_used_type")
    op.drop_column("jobs", "price_used_jpy")
    op.drop_column("jobs", "price_confidence")
    op.drop_column("jobs", "vehicle_price_source_text")
    op.drop_column("jobs", "price_total_source_text")
    op.drop_column("jobs", "car_short")
    op.drop_column("jobs", "car_full")
