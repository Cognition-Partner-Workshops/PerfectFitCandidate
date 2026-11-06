# Contributing

## Development setup

From the repository root, create and activate a Python 3.13 virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e "PFC/app[dev]"
```

On Windows, activate with `.venv\Scripts\activate`.

Install frontend dependencies:

```bash
cd PFC/frontend
npm ci
```

## Checks

Run backend checks from `PFC/app`:

```bash
ruff check pfc tests
ruff format --check pfc tests
mypy pfc
pytest
```

Run frontend checks from `PFC/frontend`:

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

## Conventions

- Use type hints for Python functions and add tests for new logic.
- Use Pydantic v2 for validation and settings.
- Keep API routes under `/api/v1`; return errors as RFC 9457 `application/problem+json`.
- Mermaid diagrams must not use styling or colors.
- Add dependencies only through `pyproject.toml` in your own PR; avoid editing other contributors' modules.

## Module ownership

| Area | Owned modules |
|---|---|
| Foundation | `pfc/main.py`, `pfc/config.py`, `pfc/db`, `pfc/security`, `pfc/jobs`, `pfc/launcher.py`, `pfc/api/routers/auth.py` |
| Domain | `pfc/domain`, `PFC/config` |
| Ingestion | `pfc/services/ingestion`, `pfc/adapters/sources` |
| Adapters | `pfc/adapters/{llm,vector,email}`, `pfc/security/package_signing.py` |
| Frontend | `PFC/frontend` |
