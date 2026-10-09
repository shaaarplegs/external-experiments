"""usage log, quotas, revocation and admin audit log

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 09:22:18.928389

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0002'
down_revision: str | Sequence[str] | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('admin_audit_log',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('admin_id', sa.Uuid(), nullable=True),
    sa.Column('admin_email', sa.String(length=320), nullable=False),
    sa.Column('action', sa.String(length=50), nullable=False),
    sa.Column('target_type', sa.String(length=20), nullable=True),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['admin_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_admin_audit_log_action'), 'admin_audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_admin_audit_log_admin_id'), 'admin_audit_log', ['admin_id'], unique=False)
    op.create_index(op.f('ix_admin_audit_log_created_at'), 'admin_audit_log', ['created_at'], unique=False)
    op.create_index(op.f('ix_admin_audit_log_target_id'), 'admin_audit_log', ['target_id'], unique=False)
    op.create_table('processing_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=True),
    sa.Column('meeting_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('failed_stage', sa.String(length=20), nullable=True),
    sa.Column('audio_seconds', sa.Float(), nullable=True),
    sa.Column('transcribed_seconds', sa.Float(), nullable=True),
    sa.Column('transcription_model', sa.String(length=200), nullable=True),
    sa.Column('llm_model', sa.String(length=200), nullable=True),
    sa.Column('llm_prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('llm_completion_tokens', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_processing_runs_created_at'), 'processing_runs', ['created_at'], unique=False)
    op.create_index(op.f('ix_processing_runs_meeting_id'), 'processing_runs', ['meeting_id'], unique=False)
    op.create_index(op.f('ix_processing_runs_user_id'), 'processing_runs', ['user_id'], unique=False)
    op.add_column('users', sa.Column('last_active_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('disabled_reason', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('quota_uploads_per_month', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('quota_audio_minutes_per_month', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_users_last_active_at'), 'users', ['last_active_at'], unique=False)
    op.execute("UPDATE users SET last_active_at = last_login_at")  # best guess for existing accounts


def downgrade() -> None:
    op.drop_index(op.f('ix_users_last_active_at'), table_name='users')
    op.drop_column('users', 'quota_audio_minutes_per_month')
    op.drop_column('users', 'quota_uploads_per_month')
    op.drop_column('users', 'disabled_at')
    op.drop_column('users', 'disabled_reason')
    op.drop_column('users', 'last_active_at')
    op.drop_index(op.f('ix_processing_runs_user_id'), table_name='processing_runs')
    op.drop_index(op.f('ix_processing_runs_meeting_id'), table_name='processing_runs')
    op.drop_index(op.f('ix_processing_runs_created_at'), table_name='processing_runs')
    op.drop_table('processing_runs')
    op.drop_index(op.f('ix_admin_audit_log_target_id'), table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_created_at'), table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_admin_id'), table_name='admin_audit_log')
    op.drop_index(op.f('ix_admin_audit_log_action'), table_name='admin_audit_log')
    op.drop_table('admin_audit_log')
