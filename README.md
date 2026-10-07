# TestAgent

TestAgent is an AI-assisted test-engineering workspace. It helps teams turn
requirement documents and test-plan templates into reviewable test plans, while
keeping the origin of project and knowledge-base evidence visible to the user.

> **License notice:** this repository is source-available under the
> [PolyForm Noncommercial 1.0.0 license](LICENSE). It permits noncommercial
> use only. Commercial use requires separate permission from the copyright
> holder; this is not an OSI-approved open-source license.

## What it provides

- Document- and template-driven test-plan generation with review and export
  flows.
- Project workspaces that retain project documents and retrieve relevant
  project evidence for a task.
- An optional company knowledge-base connector for organization-specific
  rules, conventions, and testing constraints.
- A preparation flow that identifies material requirement gaps, searches
  configured knowledge sources, and asks for clarification when needed.
- A Vue frontend and FastAPI backend with PostgreSQL/Redis development
  infrastructure.

No company knowledge-base endpoint, API key, or corporate document is bundled
with this repository. Configure external services with your own credentials.

## Architecture

| Area | Main technology |
| --- | --- |
| Frontend | Vue 3, Vite, TypeScript |
| Backend | FastAPI, Python 3.11+ |
| Development services | PostgreSQL and Redis via Docker Compose |
| Optional integrations | LLM, embedding, reranking, object storage, and company RAG services |

## Quick start for local development

### 1. Prerequisites

- Python 3.11 or later
- Node.js 20 or later
- Docker Desktop with Docker Compose v2 (recommended for PostgreSQL and Redis)

### 2. Start local infrastructure

From the repository root:

```powershell
docker compose -f docker-compose.dev.yml up -d
```

This starts only the development PostgreSQL and Redis services. Inspect their
state with:

```powershell
docker compose -f docker-compose.dev.yml ps
```

### 3. Configure and start the backend

```powershell
cd backend
Copy-Item .env.example .env
# Edit .env and supply only the services you intend to use.
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Keep `.env` private. In particular, create a unique `SECRET_KEY` and provide
only your own API keys, passwords, and external-service URLs. The committed
template intentionally contains no usable credentials.

### 4. Start the frontend

In a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

The development server listens on <http://127.0.0.1:5318>.

## Configuration and data safety

- Commit only `*.example` templates; real `.env` files are ignored at every
  directory level.
- Do not upload production documents, credentials, or private customer data to
  public issue trackers or pull requests.
- Company RAG is optional and configured by each deployment under **Settings**.
  A missing company RAG configuration is handled as an unavailable evidence
  source, not as a bundled default service.
- Production Compose configuration expects deployment-specific secret and TLS
  material. Review it carefully before exposing any service to a network.

## Testing

Run checks from the component you changed:

```powershell
# Backend
cd backend
python -m pytest

# Frontend
cd frontend
npm test
npm run build
```

The root Playwright workflow currently contains a generic browser smoke test;
it is not a substitute for an application end-to-end test suite. Replace it
with product-specific, self-contained tests before using it as a release gate.

## Documentation

Curated technical and setup documentation is under [`docs_x/`](docs_x/), including:

- [Full setup guide](docs_x/setup/22_TestAgent_从Git到启动完整教程.md)
- [Environment-variable reference](docs_x/setup/21_TestAgent_env参数详解.md)
- [Troubleshooting guide](docs_x/setup/23_TestAgent_常见问题FAQ与故障排查.md)
- [Project technical overview](docs_x/02_TestAgent_项目总体技术方案.md)

## Security and contributions

- Report vulnerabilities according to [SECURITY.md](SECURITY.md). Never put
  secrets or exploit details in a public issue.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change.

## License

Copyright holders retain all rights not granted by the
[PolyForm Noncommercial 1.0.0 license](LICENSE). If you need a commercial
license or other permission, contact the repository owner before use.
