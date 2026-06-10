"""ai quality score

Revision ID: 0012_ai_quality_score
Revises: 0011_req_call_lang_ctx
Create Date: 2026-06-09
"""

from alembic import op


revision = "0012_ai_quality_score"
down_revision = "0011_req_call_lang_ctx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS analysis_ai_quality_score INTEGER")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS analysis_ai_quality_reason TEXT")
    op.execute("ALTER TABLE call_reports ADD COLUMN IF NOT EXISTS ai_quality_score INTEGER")
    op.execute("ALTER TABLE call_reports ADD COLUMN IF NOT EXISTS ai_quality_reason TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE call_reports DROP COLUMN IF EXISTS ai_quality_reason")
    op.execute("ALTER TABLE call_reports DROP COLUMN IF EXISTS ai_quality_score")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS analysis_ai_quality_reason")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS analysis_ai_quality_score")
