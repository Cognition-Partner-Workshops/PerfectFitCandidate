# PerfectFitCandidate (PFC)

Internal, local-first web application that matches bench resources to open demands, with explainable 0–100 % match scores, human-approved candidate emails and demand/bench analytics. Runs from a portable ZIP on a locked-down Windows 11 laptop, launched by a `.bat` / `.cmd` file. The default local embedding model is nomic-embed-text-v1.5, external network features require an active Settings flag, and the model process is limited to ≤8 GB.

> Status: **documentation only** — no application code yet.

## Documents

| Document | Purpose |
|---|---|
| [docs/REQUIREMENTS_BASELINE.md](docs/REQUIREMENTS_BASELINE.md) | Consolidated requirements (source of truth, requirement IDs, decision log) |
| [docs/BRD.md](docs/BRD.md) | Business requirements, stakeholders, scope, success metrics |
| [docs/FDD.md](docs/FDD.md) | Functional design per feature area |
| [docs/TDD.md](docs/TDD.md) | Technical design: components, SQLite schema, embedding pipeline, LLM layer, auth, packaging |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture and diagrams |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | End-to-end workflow: upload → parse → embed → match → review → approve → email → track → archive |
| [docs/GUI_OCC_DESIGN.md](docs/GUI_OCC_DESIGN.md) | Web GUI, dashboard, OCC admin panel, login |
| [docs/FEASIBILITY.md](docs/FEASIBILITY.md) | Corporate-laptop feasibility and distribution recommendation |
| [docs/PHASES.md](docs/PHASES.md) | Phased delivery plan |
| [docs/DASHBOARD_RESEARCH.md](docs/DASHBOARD_RESEARCH.md) | Dashboard chart research and recommendations |
| [docs/reference/](docs/reference/) | Original inputs: project brief (docx), DL/CT metadata and samples (xlsx), application design sketch (png) |
