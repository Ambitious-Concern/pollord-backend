"""Volume + reproducibility knobs for the whole seeding run — change these,
not the domain files, when you want more/less data.

SEED is fixed so a run is deterministic (same titles, same counts) — makes
it obvious in a diff whether a re-run actually changed anything versus just
re-randomized. Because every domain tops up rather than replaces, actually
shrinking a dataset that's already bigger than these numbers requires an
`alembic downgrade base && alembic upgrade head` before re-running
seeders.run — see its module docstring."""
import random

SEED = 20260917
rng = random.Random(SEED)

# --- Primary test identity — fixed values, not generated from the name
# banks, since these are the exact login credentials used to demo/test the
# v2 dashboard. ---
ADMIN_EMAIL = "pollorg@gmail.com"
ADMIN_PASSWORD = "Pollord256"
ADMIN_NAME = "Pollord Admin"
ORG_NAME = "Pollord Platform Org"

N_TEAM_MEMBERS = 5  # includes the owner (Pollord Admin)
N_PENDING_INVITES = 2
N_ADMIN_SESSIONS = 2
N_ADMIN_LOGIN_ACTIVITIES = 2

N_ORGANIZATIONS = 1
N_VOTERS = 400
N_SUPPORT_AGENTS = 4

N_ELECTIONS = 12
N_EVENTS = 12

N_CATEGORIES_TOTAL = 24  # distributed across the 12 elections, varying per election
MIN_CANDIDATES_PER_CATEGORY = 3
MAX_CANDIDATES_PER_CATEGORY = 8

N_TICKET_TYPES_TOTAL = 10  # distributed across the 12 events — some get 0 (voting-only)

# Soft ceilings, not exact targets — "cap nominees at 800, 100 per events"
# reads as an upper bound. The category/candidate ranges above naturally
# land well under both; these exist as an assertion, not a fill target.
N_NOMINEES_CAP = 800
N_NOMINEES_EVENT_CAP = 100

N_NOMINATIONS_TOTAL = 60
N_ELECTIONS_WITH_FORMS = 5

N_ATTENDEES_TOTAL = 500

# ~600 combined Transaction (paid votes) + TicketTransaction (paid tickets)
# rows. Only the vote side is a fixed draw — the ticket side is whatever it
# organically takes to fulfill exactly N_ATTENDEES_TOTAL successful
# purchases at the ~85% success rate below (see tickets.py), so the
# combined total lands close to but not pinned exactly to 600.
N_VOTE_TRANSACTIONS = 360

# How many of the 12 elections + 12 events are deliberately given real paid
# revenue (a non-null price and at least one successful transaction) — this
# directly controls how many rows GET /payouts/sources returns, since a
# source only appears there once gross_revenue > 0. Split explicitly rather
# than derived, so elections.py and events.py can each pick their own
# subset independently without coordinating at runtime.
N_PAYOUT_SOURCE_ELECTIONS = 3
N_PAYOUT_SOURCE_EVENTS = 2
N_PAYOUT_HISTORY = 8

# How far back financial/vote/ticket-sale activity is spread — matches
# FinancialStatsService's date-range filters (this-month/last-30/last-90/
# all-time), so every range actually has something to show.
DATA_SPREAD_DAYS = 150

N_PROMO_CODES = 8
N_TEMPLATES = 12
N_SUPPORT_EMAIL_TICKETS = 35
N_SUPPORT_CHATS = 20
N_ACTIVITY_LOG = 30

SEED_PASSWORD = "SeedUser@1234"  # matches scripts/seed_v2_mock.py's existing convention
