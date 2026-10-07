# TestAgent Backend

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## API Documentation

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- Health check: http://localhost:8000/api/health

## Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI application entry point
│   ├── core/                 # Config, security, exceptions, response
│   ├── api/                  # API routes (v1)
│   ├── schemas/              # Pydantic request/response models
│   ├── models/               # SQLAlchemy ORM models
│   ├── repositories/         # Database access layer
│   ├── services/             # Business logic layer
│   ├── agent/                # Agent Orchestrator
│   ├── tools/                # Agent tool implementations
│   ├── integrations/         # External service clients
│   ├── storage/              # File storage abstraction
│   ├── db/                   # Database session & base
│   └── utils/                # Shared utilities
├── alembic/                  # Database migrations
├── data/                     # Local file storage
│   ├── uploads/
│   ├── artifacts/
│   └── temp/
├── tests/
├── requirements.txt
├── pyproject.toml
└── alembic.ini
```
