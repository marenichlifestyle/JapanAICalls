"""job vin and stock number

Revision ID: 0009_job_vin_stock
Revises: 0008_listing_fix
Create Date: 2026-05-21
"""

from alembic import op


revision = "0009_job_vin_stock"
down_revision = "0008_listing_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS vin VARCHAR(32)")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stock_number VARCHAR(128)")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS stock_number")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS vin")
