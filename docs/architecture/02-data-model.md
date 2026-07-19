# 02 · Data Model

## Two-store strategy ([ADR-003](ADRS.md#adr-003))

| Store | Contents | Why |
|---|---|---|
| **SQLite** (SQLAlchemy + Alembic, WAL mode) | Transactional state: orders, trades, positions, signals, recommendations, risk events, news, backtest runs | ACID for money-adjacent rows; zero ops; trivially backed up |
| **Parquet + DuckDB** | OHLCV candles, backtest artifacts | Columnar scans over millions of rows; SQLite is the wrong tool for this |

Both sit behind repository interfaces, so either can be replaced (e.g., Postgres/Timescale) without
touching business logic. Money columns are `Decimal` (stored as integer paise or TEXT — decided at
M3), timestamps are UTC.

## ER diagram (SQLite)

```mermaid
erDiagram
    INSTRUMENT ||--o{ SIGNAL : "generates"
    INSTRUMENT ||--o{ ORDER_ : "traded via"
    INSTRUMENT ||--o{ POSITION : "held as"
    INSTRUMENT ||--o{ NEWS_ITEM : "tagged in"
    INSTRUMENT ||--o{ RECOMMENDATION : "subject of"
    STRATEGY_RUN ||--o{ SIGNAL : "produces"
    SIGNAL ||--o| ORDER_ : "may become"
    ORDER_ ||--o{ TRADE : "fills as"
    ORDER_ ||--o{ ORDER_EVENT : "audited by"
    TRADE }o--|| POSITION : "updates"
    AI_ANALYSIS ||--o{ RECOMMENDATION : "supports"
    NEWS_ITEM }o--o{ AI_ANALYSIS : "context for"
    BACKTEST_RUN ||--o{ BACKTEST_TRADE : "contains"

    INSTRUMENT {
        int id PK
        string symbol "e.g. RELIANCE"
        string exchange "NSE"
        string isin
        string instrument_key "Upstox key"
        decimal tick_size
        int lot_size
        bool active
    }
    SIGNAL {
        int id PK
        int instrument_id FK
        int strategy_run_id FK
        string direction "LONG|SHORT|EXIT"
        decimal ref_price
        json context "indicator values at emit time"
        datetime created_at
        string status "NEW|APPROVED|REJECTED|EXPIRED"
    }
    ORDER_ {
        int id PK
        string client_order_id UK "idempotency key"
        string broker_order_id "null until acked"
        int instrument_id FK
        int signal_id FK
        string side "BUY|SELL"
        string order_type "MARKET|LIMIT|SL"
        int qty
        int filled_qty
        decimal limit_price
        string state "state machine"
        string mode "PAPER|LIVE|BACKTEST"
        datetime created_at
        datetime updated_at
    }
    ORDER_EVENT {
        int id PK
        int order_id FK
        string from_state
        string to_state
        json payload
        datetime at
    }
    TRADE {
        int id PK
        int order_id FK
        decimal price
        int qty
        decimal brokerage
        decimal stt
        decimal stamp_duty
        decimal gst
        decimal exchange_charges
        decimal sebi_charges
        decimal net_amount
        datetime executed_at
    }
    POSITION {
        int id PK
        int instrument_id FK
        int qty "signed"
        decimal avg_price
        decimal realized_pnl
        string mode "PAPER|LIVE"
        datetime updated_at
    }
    RISK_EVENT {
        int id PK
        string kind "LIMIT_BREACH|KILL_SWITCH|REJECTION"
        json detail
        datetime at
    }
    STRATEGY_RUN {
        int id PK
        string strategy_name
        json params
        string mode
        datetime started_at
    }
    AI_ANALYSIS {
        int id PK
        int instrument_id FK
        string model "e.g. claude-opus-4-8"
        string prompt_hash "sha256 of full input"
        json input_snapshot "indicators+news ids shown"
        json output "validated AIAnalysis schema"
        int input_tokens
        int output_tokens
        decimal cost_usd
        datetime created_at
    }
    RECOMMENDATION {
        int id PK
        int instrument_id FK
        int signal_id FK "nullable"
        int ai_analysis_id FK "nullable"
        string action "BUY|SELL|HOLD|AVOID"
        int rank
        json rationale "merged deterministic + AI"
        datetime created_at
    }
    NEWS_ITEM {
        int id PK
        string source
        string url UK
        string title
        text body "untrusted input"
        datetime published_at
        datetime ingested_at
    }
    BACKTEST_RUN {
        int id PK
        string strategy_name
        json params
        json cost_model_version
        date from_date
        date to_date
        json metrics "sharpe, cagr, maxdd..."
        string data_fingerprint "reproducibility"
        datetime created_at
    }
    BACKTEST_TRADE {
        int id PK
        int backtest_run_id FK
        int instrument_id FK
        json detail
    }
```

## Parquet layout

```
data/candles/{exchange}/{symbol}/{interval}/year=YYYY/part.parquet
```

Columns: `ts_utc, open, high, low, close, volume, oi?`, plus `adjusted` flag and
`adjustment_factor`. DuckDB views expose these as one logical table per interval. A `manifest.json`
per dataset records source, sync time, and validation status.

## Key design decisions

- **`client_order_id` is generated before any broker call** — the idempotency key that makes crash
  recovery and reconciliation possible (see [04-trade-lifecycle.md](04-trade-lifecycle.md)).
- **`ORDER_EVENT` is append-only** — full audit of every state transition, required for debugging
  live incidents and for Rule 14 reconciliation.
- **Per-trade cost columns are explicit** (not a single "fees" blob) because Indian cost structure
  is the difference between a profitable and losing strategy; analytics must break costs down.
- **`AI_ANALYSIS.input_snapshot` + `prompt_hash`** make every AI recommendation reproducible and
  auditable (Rule 10): we can always answer "what did the model see when it said this?"
- **`mode` column everywhere it matters** — paper and live records never mix in aggregates.
