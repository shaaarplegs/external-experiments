"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-09 08:28:20.925987

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0001'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=True),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_role'), 'users', ['role'], unique=False)
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('categories',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('color', sa.String(length=9), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'name', name='uq_categories_owner_name')
    )
    op.create_index(op.f('ix_categories_owner_id'), 'categories', ['owner_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)
    op.create_table('meetings',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('owner_id', sa.Uuid(), nullable=False),
    sa.Column('category_id', sa.Uuid(), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=True),
    sa.Column('meeting_date', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_seconds', sa.Integer(), nullable=True),
    sa.Column('language', sa.String(length=10), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_filename', sa.String(length=255), nullable=True),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_meetings_category_id'), 'meetings', ['category_id'], unique=False)
    op.create_index(op.f('ix_meetings_meeting_date'), 'meetings', ['meeting_date'], unique=False)
    op.create_index(op.f('ix_meetings_owner_id'), 'meetings', ['owner_id'], unique=False)
    op.create_index(op.f('ix_meetings_status'), 'meetings', ['status'], unique=False)
    op.create_table('action_items',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('assignee', sa.String(length=200), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('due_text', sa.String(length=200), nullable=True),
    sa.Column('priority', sa.String(length=10), nullable=True),
    sa.Column('is_done', sa.Boolean(), nullable=False),
    sa.Column('source', sa.String(length=10), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_action_items_meeting_id'), 'action_items', ['meeting_id'], unique=False)
    op.create_table('meeting_attendees',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('meeting_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('role', sa.String(length=200), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_meeting_attendees_meeting_id'), 'meeting_attendees', ['meeting_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_meeting_attendees_meeting_id'), table_name='meeting_attendees')
    op.drop_table('meeting_attendees')
    op.drop_index(op.f('ix_action_items_meeting_id'), table_name='action_items')
    op.drop_table('action_items')
    op.drop_index(op.f('ix_meetings_status'), table_name='meetings')
    op.drop_index(op.f('ix_meetings_owner_id'), table_name='meetings')
    op.drop_index(op.f('ix_meetings_meeting_date'), table_name='meetings')
    op.drop_index(op.f('ix_meetings_category_id'), table_name='meetings')
    op.drop_table('meetings')
    op.drop_index(op.f('ix_refresh_tokens_user_id'), table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_index(op.f('ix_categories_owner_id'), table_name='categories')
    op.drop_table('categories')
    op.drop_table('app_settings')
    op.drop_index(op.f('ix_users_role'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
