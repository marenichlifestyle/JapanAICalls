"""add dealer phone resolver fields

Revision ID: 0005_dealer_phone_resolver
Revises: 0004_job_call_language
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_dealer_phone_resolver"
down_revision = "0004_job_call_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dealer_business_hours", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("dealer_closed_days", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("phone_from_listing", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("listing_phone_raw", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("listing_phone_type", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("resolved_phone_raw", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("resolved_phone_e164", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("resolved_phone_source_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("resolved_phone_source_type", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("resolver_confidence_score", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("resolver_status", sa.String(length=32), nullable=True))
    op.add_column("jobs", sa.Column("resolver_error_reason", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("resolver_result_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "resolver_result_json")
    op.drop_column("jobs", "resolver_error_reason")
    op.drop_column("jobs", "resolver_status")
    op.drop_column("jobs", "resolver_confidence_score")
    op.drop_column("jobs", "resolved_phone_source_type")
    op.drop_column("jobs", "resolved_phone_source_url")
    op.drop_column("jobs", "resolved_phone_e164")
    op.drop_column("jobs", "resolved_phone_raw")
    op.drop_column("jobs", "listing_phone_type")
    op.drop_column("jobs", "listing_phone_raw")
    op.drop_column("jobs", "phone_from_listing")
    op.drop_column("jobs", "dealer_closed_days")
    op.drop_column("jobs", "dealer_business_hours")
