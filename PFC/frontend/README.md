# PFC frontend

Phase 1 single-page app for PerfectFitCandidate (React 19 + TypeScript + Vite), implementing
[GUI_OCC_DESIGN.md](../docs/GUI_OCC_DESIGN.md). Plain CSS design tokens (`src/styles/tokens.css`),
React Router (hash routes for deep links), TanStack Query and Apache ECharts (per
[DASHBOARD_RESEARCH.md](../docs/DASHBOARD_RESEARCH.md)).

## API mode

All data access goes through the typed `ApiClient` in `src/api/` whose methods map 1:1 to the
`/api/v1` routes in [TDD.md §12](../docs/TDD.md#12-api-surface).

The real backend is the default in every build; the in-browser mock is an explicit opt-in.

| Selection | Client | Notes |
|---|---|---|
| default (no flag) | `HttpApiClient` | Real `/api/v1` backend: same-origin cookie session (`pfc_session`), `X-CSRF-Token` on POST/PUT/DELETE (token from the login / `/auth/me` / `/auth/password` response), RFC 9457 `application/problem+json` errors shown as friendly messages, 401 → back to sign-in, 423 → lockout countdown. `VITE_PFC_API_BASE` overrides `/api/v1`. |
| `VITE_PFC_API=mock` | `MockApiClient` | Any build (vitest sets it). In-browser fixtures (fictitious people, `example.com`/`example.org` emails), persisted in `localStorage` key `pfc.mock.v3`. The first-run setup code is shown on the setup screen and in the console. |
| `?mock=1` on a `npm run dev` URL | `MockApiClient` | Dev server only; ignored in production builds. |
| `VITE_PFC_API=http` | `HttpApiClient` | Forces HTTP even with `?mock=1`. |

```bash
npm ci
npm run dev                        # real backend: proxies /api to 127.0.0.1:8765 (PFC_PORT overrides)
VITE_PFC_API=mock npm run dev      # mock mode (or open http://127.0.0.1:5173/?mock=1)
npm run build                      # dist/ is served by FastAPI from settings.frontend_dir (PFC_FRONTEND_DIR)
```

The dev proxy rewrites `Host` and `Origin` to the backend address, because the backend's
loopback guard rejects any other origin (REQ-AUTH-07).

### Endpoint mapping

`src/api/mappers.ts` converts the merged backend's snake_case Pydantic shapes (auth/setup, OCC,
settings, LLM, `/emails`) to the SPA DTOs. Endpoints that are not merged yet (ingestion,
matching, settings packages, analytics, DL/CT approvals) keep the TDD §12 client shapes.

## Checks

```bash
npm run lint && npm run typecheck && npm test && npm run build
```
