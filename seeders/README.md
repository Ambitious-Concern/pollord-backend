# Seeders

Domain-grouped seed data for local dev / pagination testing — real Ghanaian
names, real (verified) Unsplash images, spread over ~150 days so every date
range filter (Financial Stats, results, ticket sales) has something to show.

## Layout

```
seeders/
  config.py            volume knobs (N_ELECTIONS, N_EVENTS, N_VOTERS, ...) + fixed RNG seed
  data/
    names.py           Ghanaian first/last names, org names
    images.py           curated, verified Unsplash photo URLs
    copy_bank.py        election/event genres, titles, venues, ticket tiers
  domains/
    organizations.py    orgs + owner/editor accounts
    users.py             bulk voter pool + support agents
    elections.py         N_ELECTIONS elections w/ categories + candidates
    events.py             N_EVENTS events w/ ticket types (+ some hybrid voting)
    voting.py             free + paid votes, spread over time
    tickets.py             ticket sales (TicketTransaction + TicketPurchase/Ticket)
    promo_codes.py, templates.py, nominations.py, support.py, activity_log.py
  run.py                orchestrator — run this
```

## Running

Idempotent — safe to re-run. Tops up toward the configured counts rather
than duplicating anything; only genuinely new activity (e.g. after bumping
a count in `config.py`) gets added on a second run.

```bash
DATABASE_URL=postgresql+asyncpg://pollard:pollard@localhost:55432/pollard \
    .venv/Scripts/python.exe -m seeders.run
```

Or inside the backend container:

```bash
docker compose exec app python -m seeders.run
```

**Never point this at production.** It only writes to whatever
`DATABASE_URL` is set in its environment — there's no separate safety
switch, so double-check the URL before running.

## Changing volume

Edit the constants in `config.py` (not the domain files) and re-run.
