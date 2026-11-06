# PFC frontend

Phase 1 single-page app for PerfectFitCandidate (React 19 + TypeScript + Vite), implementing
[GUI_OCC_DESIGN.md](../docs/GUI_OCC_DESIGN.md). Plain CSS design tokens (`src/styles/tokens.css`),
React Router (hash routes for deep links), TanStack Query and Apache ECharts (per
[DASHBOARD_RESEARCH.md](../docs/DASHBOARD_RESEARCH.md)).

## API mode

All data access goes through the typed `ApiClient` in `src/api/` whose methods map 1:1 to the
`/api/v1` routes in [TDD.md §12](../docs/TDD.md#12-api-surface).

| `VITE_PFC_API` | Client | Notes |
|---|---|---|
| `mock` (default) | `MockApiClient` | In-browser fixtures (fictitious people, `example.com`/`example.org` emails), persisted in `localStorage` key `pfc.mock.v1`. The first-run setup code is shown on the setup screen and in the console. |
| `http` | `HttpApiClient` | Real backend; cookie session + `X-CSRF-Token`, RFC 9457 errors. `VITE_PFC_API_BASE` overrides `/api/v1`. |

```bash
npm ci
npm run dev                      # mock mode
VITE_PFC_API=http npm run dev    # proxies /api to 127.0.0.1:8765
```

## Checks

```bash
npm run lint && npm run typecheck && npm test && npm run build
```
