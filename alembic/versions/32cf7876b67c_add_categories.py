"""add categories

Revision ID: 32cf7876b67c
Revises: e7b2c91d4a08
Create Date: 2026-08-30 00:00:00.000000

Adds a Category model that sits between Election/Event and Candidate/Vote, so one
election or event can run several positions/prizes ("President", "Best Dressed")
each with their own candidates and voting type, instead of one flat candidate list.

This migration also removes `multiple_choice` as an election_type value. It was
conflated with an unrelated payment concept (whether a paid voter can buy bulk
votes) — that's now governed solely by `allow_revoting`. Existing multiple_choice
elections are backfilled to single_choice; see the data-backfill step below.

Existing elections/candidates/votes/transactions are backfilled into one
auto-created "default" Category per election (named after the election, carrying
over its old election_type/allow_abstain) so they keep working unchanged. Events
have no prior categories to backfill — this is a clean, new capability for them.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '32cf7876b67c'
down_revision: Union[str, None] = 'e7b2c91d4a08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. New categories table ---
    op.create_table(
        'categories',
        sa.Column('category_id', sa.UUID(), nullable=False),
        sa.Column('election_id', sa.UUID(), nullable=True),
        sa.Column('event_id', sa.UUID(), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('election_type', sa.String(length=50), nullable=False),
        sa.Column('allow_abstain', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            '(election_id IS NOT NULL) != (event_id IS NOT NULL)',
            name='ck_category_exactly_one_parent',
        ),
        sa.ForeignKeyConstraint(['election_id'], ['elections.election_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['event_id'], ['events.event_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('category_id'),
    )
    op.create_index('ix_categories_election_id', 'categories', ['election_id'])
    op.create_index('ix_categories_event_id', 'categories', ['event_id'])

    # --- 2. Events gain the settings needed to run paid/bulk category voting ---
    op.add_column('events', sa.Column('vote_price', sa.Integer(), nullable=True))
    op.add_column(
        'events',
        sa.Column('allow_revoting', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )

    # --- 3. candidates / votes / transactions gain category_id + event_id,
    #        and their election_id becomes nullable (an event-owned row has no
    #        election_id at all) ---
    op.add_column('candidates', sa.Column('category_id', sa.UUID(), nullable=True))
    op.add_column('candidates', sa.Column('event_id', sa.UUID(), nullable=True))
    op.alter_column('candidates', 'election_id', existing_type=sa.UUID(), nullable=True)

    op.add_column('votes', sa.Column('category_id', sa.UUID(), nullable=True))
    op.add_column('votes', sa.Column('event_id', sa.UUID(), nullable=True))
    op.alter_column('votes', 'election_id', existing_type=sa.UUID(), nullable=True)

    op.add_column('transactions', sa.Column('category_id', sa.UUID(), nullable=True))
    op.add_column('transactions', sa.Column('event_id', sa.UUID(), nullable=True))
    op.alter_column('transactions', 'election_id', existing_type=sa.UUID(), nullable=True)

    # --- 4. Data backfill: one default Category per existing Election ---
    bind = op.get_bind()

    elections_t = sa.table(
        'elections',
        sa.column('election_id', sa.UUID()),
        sa.column('title', sa.String()),
        sa.column('election_type', sa.String()),
        sa.column('allow_abstain', sa.Boolean()),
    )
    categories_t = sa.table(
        'categories',
        sa.column('category_id', sa.UUID()),
        sa.column('election_id', sa.UUID()),
        sa.column('event_id', sa.UUID()),
        sa.column('name', sa.String()),
        sa.column('election_type', sa.String()),
        sa.column('allow_abstain', sa.Boolean()),
        sa.column('display_order', sa.Integer()),
        sa.column('created_at', sa.DateTime(timezone=True)),
        sa.column('updated_at', sa.DateTime(timezone=True)),
    )
    candidates_t = sa.table(
        'candidates',
        sa.column('election_id', sa.UUID()),
        sa.column('category_id', sa.UUID()),
    )
    votes_t = sa.table(
        'votes',
        sa.column('election_id', sa.UUID()),
        sa.column('category_id', sa.UUID()),
    )
    transactions_t = sa.table(
        'transactions',
        sa.column('election_id', sa.UUID()),
        sa.column('category_id', sa.UUID()),
    )

    elections = bind.execute(
        sa.select(
            elections_t.c.election_id,
            elections_t.c.title,
            elections_t.c.election_type,
            elections_t.c.allow_abstain,
        )
    ).fetchall()

    now = datetime.now(timezone.utc)
    for election_id, title, election_type, allow_abstain in elections:
        category_id = uuid.uuid4()
        # multiple_choice is being removed as a concept — fold it into single_choice.
        mapped_type = 'single_choice' if election_type == 'multiple_choice' else election_type

        bind.execute(
            categories_t.insert().values(
                category_id=category_id,
                election_id=election_id,
                event_id=None,
                name=title,
                election_type=mapped_type,
                allow_abstain=bool(allow_abstain),
                display_order=0,
                created_at=now,
                updated_at=now,
            )
        )
        bind.execute(
            candidates_t.update()
            .where(candidates_t.c.election_id == election_id)
            .values(category_id=category_id)
        )
        bind.execute(
            votes_t.update()
            .where(votes_t.c.election_id == election_id)
            .values(category_id=category_id)
        )
        bind.execute(
            transactions_t.update()
            .where(transactions_t.c.election_id == election_id)
            .values(category_id=category_id)
        )

    # --- 5. Tighten NOT NULL + swap constraints now that backfill is complete ---
    op.alter_column('candidates', 'category_id', existing_type=sa.UUID(), nullable=False)
    op.alter_column('votes', 'category_id', existing_type=sa.UUID(), nullable=False)
    op.alter_column('transactions', 'category_id', existing_type=sa.UUID(), nullable=False)

    op.create_index('ix_candidates_category_id', 'candidates', ['category_id'])
    op.create_index('ix_votes_category_id', 'votes', ['category_id'])
    op.create_index('ix_votes_event_id', 'votes', ['event_id'])
    op.create_index('ix_transactions_category_id', 'transactions', ['category_id'])
    op.create_index('ix_transactions_event_id', 'transactions', ['event_id'])

    op.create_foreign_key(
        'fk_candidates_category_id', 'candidates', 'categories',
        ['category_id'], ['category_id'], ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_candidates_event_id', 'candidates', 'events',
        ['event_id'], ['event_id'], ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_votes_category_id', 'votes', 'categories', ['category_id'], ['category_id'],
    )
    op.create_foreign_key(
        'fk_votes_event_id', 'votes', 'events', ['event_id'], ['event_id'],
    )

    op.drop_constraint('uq_vote_per_election', 'votes', type_='unique')
    op.create_unique_constraint('uq_vote_per_category', 'votes', ['category_id', 'voter_hash'])
    op.create_check_constraint(
        'ck_vote_exactly_one_parent',
        'votes',
        '(election_id IS NOT NULL) != (event_id IS NOT NULL)',
    )

    # --- 6. election_type/max_selections/allow_abstain now live on Category ---
    op.drop_column('elections', 'election_type')
    op.drop_column('elections', 'max_selections')
    op.drop_column('elections', 'allow_abstain')


def downgrade() -> None:
    # WARNING: lossy for elections that ended up with more than one category —
    # only the first category's (by display_order) election_type/allow_abstain
    # survive back onto the election, and any event-owned candidates/votes/
    # transactions have no home in the old schema at all. This is a dev/rollback
    # safety net, not a supported production path.
    op.add_column('elections', sa.Column('election_type', sa.String(length=50), nullable=True))
    op.add_column('elections', sa.Column('max_selections', sa.Integer(), nullable=True))
    op.add_column('elections', sa.Column('allow_abstain', sa.Boolean(), nullable=True))

    bind = op.get_bind()
    elections_t = sa.table(
        'elections',
        sa.column('election_id', sa.UUID()),
        sa.column('election_type', sa.String()),
        sa.column('allow_abstain', sa.Boolean()),
    )

    rows = bind.execute(
        sa.text(
            "SELECT DISTINCT ON (election_id) election_id, election_type, allow_abstain "
            "FROM categories WHERE election_id IS NOT NULL "
            "ORDER BY election_id, display_order ASC"
        )
    ).fetchall()
    for election_id, election_type, allow_abstain in rows:
        bind.execute(
            elections_t.update()
            .where(elections_t.c.election_id == election_id)
            .values(election_type=election_type, allow_abstain=allow_abstain)
        )

    op.alter_column('elections', 'election_type', existing_type=sa.String(length=50), nullable=False)
    op.alter_column(
        'elections', 'allow_abstain', existing_type=sa.Boolean(),
        nullable=False, server_default=sa.text('false'),
    )

    op.drop_index('ix_transactions_event_id', table_name='transactions')
    op.drop_index('ix_transactions_category_id', table_name='transactions')
    op.drop_index('ix_votes_event_id', table_name='votes')
    op.drop_index('ix_votes_category_id', table_name='votes')
    op.drop_index('ix_candidates_category_id', table_name='candidates')

    op.drop_constraint('ck_vote_exactly_one_parent', 'votes', type_='check')
    op.drop_constraint('uq_vote_per_category', 'votes', type_='unique')
    op.create_unique_constraint('uq_vote_per_election', 'votes', ['election_id', 'voter_hash'])

    op.drop_constraint('fk_votes_event_id', 'votes', type_='foreignkey')
    op.drop_constraint('fk_votes_category_id', 'votes', type_='foreignkey')
    op.drop_constraint('fk_candidates_event_id', 'candidates', type_='foreignkey')
    op.drop_constraint('fk_candidates_category_id', 'candidates', type_='foreignkey')

    # NOTE: only safe if no event-owned rows exist (their election_id is NULL).
    op.alter_column('transactions', 'election_id', existing_type=sa.UUID(), nullable=False)
    op.drop_column('transactions', 'event_id')
    op.drop_column('transactions', 'category_id')

    op.alter_column('votes', 'election_id', existing_type=sa.UUID(), nullable=False)
    op.drop_column('votes', 'event_id')
    op.drop_column('votes', 'category_id')

    op.alter_column('candidates', 'election_id', existing_type=sa.UUID(), nullable=False)
    op.drop_column('candidates', 'event_id')
    op.drop_column('candidates', 'category_id')

    op.drop_column('events', 'allow_revoting')
    op.drop_column('events', 'vote_price')

    op.drop_index('ix_categories_event_id', table_name='categories')
    op.drop_index('ix_categories_election_id', table_name='categories')
    op.drop_table('categories')
