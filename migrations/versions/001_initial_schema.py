"""
Initial DeleGate schema

Revision ID: 001
Create Date: 2026-01-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plans table
    op.create_table(
        'plans',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('plan_id', sa.String(64), nullable=False),
        sa.Column('tenant_id', sa.String(64), nullable=False),
        sa.Column('delegate_id', sa.String(64), nullable=False),
        sa.Column('intent_summary', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(32), nullable=False, server_default='single_task'),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0.8'),
        sa.Column('steps', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('references', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('trust_policy', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('assumptions', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('status', sa.String(32), nullable=False, server_default='created'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # Indexes for plans
    op.create_index('ix_plans_tenant_plan', 'plans', ['tenant_id', 'plan_id'], unique=True)
    op.create_index('ix_plans_tenant_status', 'plans', ['tenant_id', 'status'])
    op.create_index('ix_plans_created_at', 'plans', ['created_at'])

    # Workers table (registry)
    op.create_table(
        'workers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('worker_id', sa.String(64), nullable=False),
        sa.Column('tenant_id', sa.String(64), nullable=False),
        sa.Column('worker_name', sa.String(256), nullable=False),
        sa.Column('version', sa.String(32), nullable=False, server_default='1.0.0'),
        sa.Column('trust_declared_tier', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('trust_verified_tier', sa.Integer(), nullable=True),
        sa.Column('trust_verification_status', sa.String(32), nullable=False, server_default='unknown'),
        sa.Column('trust_signature', sa.Text(), nullable=True),
        sa.Column('capabilities', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('constraints', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('availability_status', sa.String(32), nullable=False, server_default='ready'),
        sa.Column('availability_load', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('availability_max_concurrent', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    # Indexes for workers
    op.create_index('ix_workers_tenant_worker', 'workers', ['tenant_id', 'worker_id'], unique=True)
    op.create_index('ix_workers_availability', 'workers', ['availability_status'])
    op.create_index('ix_workers_trust_tier', 'workers', ['trust_verified_tier'])

    # Capability index table (for faster searches)
    op.create_table(
        'capability_index',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('tenant_id', sa.String(64), nullable=False),
        sa.Column('worker_id', sa.String(64), nullable=False),
        sa.Column('tool_name', sa.String(256), nullable=False),
        sa.Column('semantic_tags', postgresql.ARRAY(sa.String(64)), nullable=False, server_default='{}'),
        sa.Column('description_vector', postgresql.JSONB(), nullable=True),  # For future semantic search
        sa.PrimaryKeyConstraint('id'),
    )

    # Indexes for capability_index
    op.create_index('ix_capability_tenant_tool', 'capability_index', ['tenant_id', 'tool_name'])
    op.create_index('ix_capability_worker', 'capability_index', ['worker_id'])


def downgrade() -> None:
    op.drop_table('capability_index')
    op.drop_table('workers')
    op.drop_table('plans')
