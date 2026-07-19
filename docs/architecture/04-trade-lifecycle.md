# 04 · Trade Lifecycle

## Order state machine

Every order — backtest, paper, or live — moves through the same states. Transitions are persisted
to `ORDER_EVENT` (append-only) before side effects where possible.

```mermaid
stateDiagram-v2
    [*] --> PENDING_RISK : Signal emitted
    PENDING_RISK --> REJECTED_RISK : limit breach / kill switch
    PENDING_RISK --> SUBMITTING : risk approved (ApprovedOrder)
    SUBMITTING --> SUBMITTED : broker ack (broker_order_id)
    SUBMITTING --> FAILED : transport error after retries
    SUBMITTED --> OPEN : accepted by exchange
    SUBMITTED --> REJECTED_BROKER : broker/exchange rejection
    OPEN --> PARTIALLY_FILLED : partial fill
    PARTIALLY_FILLED --> FILLED : remaining qty filled
    OPEN --> FILLED : full fill
    OPEN --> CANCELLED : cancel confirmed
    PARTIALLY_FILLED --> CANCELLED : cancel confirmed (partial kept)
    OPEN --> EXPIRED : session end / validity lapse
    FILLED --> [*]
    CANCELLED --> [*]
    EXPIRED --> [*]
    REJECTED_RISK --> [*]
    REJECTED_BROKER --> [*]
    FAILED --> [*]
```

**Idempotency:** `client_order_id` (UUID) is generated and persisted at `PENDING_RISK`. If the
process dies between `SUBMITTING` and the ack, restart-time reconciliation queries the broker by
`client_order_id` to learn the truth — we never double-submit.

## Happy-path sequence

```mermaid
sequenceDiagram
    participant Feed as Live Feed
    participant Orch as Orchestrator
    participant Strat as Strategy
    participant Risk as Risk Engine
    participant OSM as Order State Machine
    participant Brk as Broker (paper/live)
    participant DB as SQLite

    Feed->>Orch: CandleReceived
    Orch->>Strat: on_candle(ctx)
    Strat-->>Orch: Signal(LONG, ref_price, context)
    Orch->>DB: persist Signal
    Orch->>Risk: evaluate(Signal)
    Risk->>Risk: size position, check limits, check kill switch
    Risk-->>Orch: ApprovedOrder(qty, type, client_order_id)
    Orch->>OSM: create PENDING_RISK → SUBMITTING
    OSM->>DB: persist order + event
    OSM->>Brk: place_order(request)
    Brk-->>OSM: OrderAck(broker_order_id)
    OSM->>DB: SUBMITTED
    Brk-->>OSM: OrderUpdate(FILL price, qty, costs)
    OSM->>DB: FILLED + Trade row (full cost breakdown)
    OSM->>Orch: FillReceived
    Orch->>DB: update Position, journal entry
    Orch->>Orch: notify (M19)
```

## Reconciliation (startup + periodic)

Runs at process start, after any websocket gap, and every N minutes during market hours:

1. Fetch broker positions, funds, and order statuses.
2. Diff against local `ORDER_`/`POSITION` state.
3. Local order in non-terminal state, unknown to broker → mark `FAILED`, alert.
4. Broker fill missing locally → apply fill, alert (we missed an update).
5. Position quantity mismatch → **broker wins**; local corrected; `RISK_EVENT` logged; if the
   divergence exceeds a threshold, trip the kill switch (something is structurally wrong).

## Kill switch semantics

- Tripped by: max daily loss, max consecutive order errors, reconciliation divergence, manual
  dashboard/CLI action.
- Effect: `Risk.evaluate` rejects everything; open orders are cancelled (configurable); positions
  are optionally flattened (config, default off — human decides).
- Persisted in DB: survives restart; requires explicit human reset with a logged reason.

## Crash-safety invariants

1. Persist intent before action (order row before broker call).
2. Every broker mutation carries `client_order_id`.
3. Recovery = reconciliation, never replay of local intent.
4. Handlers are idempotent: applying the same `OrderUpdate` twice is a no-op (dedup on
   broker event id).
