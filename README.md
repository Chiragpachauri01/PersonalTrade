# PersonalTrade

Personal AI trading research & execution platform for Indian markets (NSE via Upstox).
Deterministic quant core with an advisory-only AI layer. Paper trading first.

- Rules & conventions: [CLAUDE.md](CLAUDE.md)
- Plan & status: [docs/ROADMAP.md](docs/ROADMAP.md)
- Architecture: [docs/architecture/](docs/architecture/)

## Development

Requires [uv](https://docs.astral.sh/uv/) (manages Python 3.12 automatically).

```powershell
uv sync                  # create .venv and install all dependencies
uv run pt --version      # CLI smoke check
uv run pt config validate

uv run pytest            # tests
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # type check (strict)
```

Configuration: committed defaults in `config/default.yaml`, personal overrides in
`config/local.yaml` (git-ignored), secrets in `.env` (copy from `.env.example`).
