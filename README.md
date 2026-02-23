# Pollard Backend API

E-Voting and Ticketing Platform backend built with FastAPI, SQLAlchemy 2.0, and PostgreSQL.

## Prerequisites

- Python 3.11+
- PostgreSQL 15+
- Redis 7+
- Docker & Docker Compose (optional, for containerized setup)

---

## Option 1: Run with Docker Compose (Recommended)

This starts PostgreSQL, Redis, the FastAPI app, Celery worker, and Celery beat all at once.

```bash
# 1. Clone and enter the project
cd pollard_backend

# 2. Create your .env file from the template
cp .env.example .env

# 3. Build and start all services
docker-compose up --build

# 4. In a separate terminal, run database migrations
docker-compose exec app alembic upgrade head
```

The API will be available at **http://localhost:8000**.

To stop all services:

```bash
docker-compose down
```

To stop and remove all data (volumes):

```bash
docker-compose down -v
```

---

## Option 2: Run Locally with Uvicorn

### 1. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate     # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up PostgreSQL

Make sure PostgreSQL is running locally, then create the database:

```bash
psql -U postgres
```

```sql
CREATE USER pollard WITH PASSWORD 'pollard';
CREATE DATABASE pollard OWNER pollard;
CREATE DATABASE pollard_test OWNER pollard;  -- for tests
\q
```

### 4. Set up Redis

Make sure Redis is running locally on the default port (6379).

```bash
# macOS (Homebrew)
brew services start redis

# Or run directly
redis-server
```

### 5. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and update the hostnames from Docker service names to `localhost`:

```
DATABASE_URL=postgresql+asyncpg://pollard:pollard@localhost:5432/pollard
REDIS_URL=redis://localhost:6379/0
```

### 6. Run database migrations

```bash
alembic upgrade head
```

If no migrations exist yet, generate the initial migration first:

```bash
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

### 7. Start the application

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at **http://localhost:8000**.

### 8. Start Celery workers (optional, for background tasks)

In separate terminal windows:

```bash
# Worker
celery -A app.tasks.celery_app worker --loglevel=info

# Beat scheduler (for periodic tasks)
celery -A app.tasks.celery_app beat --loglevel=info
```

---

## API Documentation

Once the server is running, visit:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc
- **Health Check**: http://localhost:8000/health

---

## Running Tests

### Unit tests (no database required)

```bash
pytest tests/unit -v
```

### Integration tests (requires PostgreSQL)

Make sure the test database exists:

```bash
psql -U postgres -c "CREATE DATABASE pollard_test OWNER pollard;"
```

Then run:

```bash
pytest tests/integration -v
```

### All tests

```bash
pytest -v
```

---

## Project Structure

```
pollard_backend/
├── app/
│   ├── api/
│   │   ├── v1/
│   │   │   ├── endpoints/    # REST API endpoints
│   │   │   └── router.py     # Route aggregation
│   │   └── websocket.py      # WebSocket endpoints
│   ├── core/
│   │   ├── config.py         # Settings (env vars)
│   │   ├── dependencies.py   # Auth dependencies (RBAC)
│   │   └── security.py       # JWT, bcrypt, AES, HMAC
│   ├── db/
│   │   ├── base.py           # Async engine & session
│   │   └── init_db.py        # Seed default roles
│   ├── middleware/            # Rate limiting, logging
│   ├── models/               # SQLAlchemy 2.0 models
│   ├── repositories/         # Data access layer
│   ├── schemas/              # Pydantic request/response models
│   ├── services/             # Business logic
│   ├── tasks/                # Celery background tasks
│   ├── utils/                # QR code, PDF, validators
│   └── main.py               # FastAPI app entry point
├── alembic/                   # Database migrations
├── tests/
│   ├── unit/                  # Unit tests (no DB)
│   └── integration/           # Integration tests (requires DB)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## API Modules

| Module | Prefix | Description |
|--------|--------|-------------|
| Auth | `/api/v1/auth` | Register, login, refresh, password reset, email verification |
| Users | `/api/v1/users` | Profile management, password change, activity log |
| Elections | `/api/v1/elections` | CRUD, candidate management, eligible voters |
| Voting | `/api/v1/voting` | Cast vote, get ballot, view receipt, election results |
| Events | `/api/v1/events` | CRUD, publish/cancel events |
| Tickets | `/api/v1/tickets` | Ticket types, purchase, validate, download |
| Analytics | `/api/v1/analytics` | Election/event/system statistics (admin) |
| Admin | `/api/v1/admin` | User management, role assignment, audit logs |

---

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://pollard:pollard@db:5432/pollard` |
| `REDIS_URL` | Redis connection string | `redis://redis:6379/0` |
| `JWT_SECRET_KEY` | Secret for signing JWTs | Must change in production |
| `AES_ENCRYPTION_KEY` | Key for vote encryption (AES-256-GCM) | Must change in production |
| `HMAC_SECRET_KEY` | Key for voter anonymization | Must change in production |
| `DEBUG` | Enable debug mode | `false` |
