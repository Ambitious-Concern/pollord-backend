"""add missing indexes for FK and filter columns

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-28
"""
from alembic import op

revision = 'e4f5a6b7c8d9'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None

_INDEXES = [
    # (index_name, table, columns)
    # support_tickets — user list, agent queue, channel/status filters
    ('ix_support_tickets_user_id',          'support_tickets',     'user_id'),
    ('ix_support_tickets_status',           'support_tickets',     'status'),
    ('ix_support_tickets_channel',          'support_tickets',     'channel'),
    ('ix_support_tickets_assigned_agent_id','support_tickets',     'assigned_agent_id'),
    # support_messages — every ticket read loads messages via ticket_id
    ('ix_support_messages_ticket_id',       'support_messages',    'ticket_id'),
    # organization_members — permission checks on every org-scoped endpoint
    ('ix_org_members_org_id',              'organization_members', 'org_id'),
    ('ix_org_members_user_id',             'organization_members', 'user_id'),
    # organizations — ownership checks, admin KYC queue
    ('ix_organizations_owner_id',          'organizations',        'owner_id'),
    ('ix_organizations_is_verified',       'organizations',        'is_verified'),
    # vote_receipts — user's receipt lookup, election-wide receipt queries
    ('ix_vote_receipts_user_id',           'vote_receipts',        'user_id'),
    ('ix_vote_receipts_election_id',       'vote_receipts',        'election_id'),
    # ticket_purchases — user purchase history, event sales aggregation
    ('ix_ticket_purchases_user_id',        'ticket_purchases',     'user_id'),
    ('ix_ticket_purchases_event_id',       'ticket_purchases',     'event_id'),
    # payout_requests — organizer's own requests, admin status queue
    ('ix_payout_requests_organizer_id',    'payout_requests',      'organizer_id'),
    ('ix_payout_requests_status',          'payout_requests',      'status'),
    # eligible_voters — user_id alone (unique constraint leads with election_id)
    ('ix_eligible_voters_user_id',         'eligible_voters',      'user_id'),
    # candidates — denormalized parent FKs used in election-scoped lookups
    ('ix_candidates_election_id',          'candidates',           'election_id'),
    ('ix_candidates_event_id',             'candidates',           'event_id'),
    # org_roles — always listed per org
    ('ix_org_roles_org_id',               'org_roles',             'org_id'),
]

# Composite index handled separately (multiple columns)
_COMPOSITE_INDEX = (
    'ix_support_tickets_channel_status',
    'support_tickets',
    'channel, status',
)


def upgrade() -> None:
    for idx_name, table, col in _INDEXES:
        op.execute(
            f'CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})'
        )
    name, table, cols = _COMPOSITE_INDEX
    op.execute(
        f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})'
    )


def downgrade() -> None:
    name, _, _ = _COMPOSITE_INDEX
    op.execute(f'DROP INDEX IF EXISTS {name}')
    for idx_name, _, _ in reversed(_INDEXES):
        op.execute(f'DROP INDEX IF EXISTS {idx_name}')
