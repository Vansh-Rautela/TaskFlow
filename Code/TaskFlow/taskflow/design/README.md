# Design canvas — TaskFlow Console concept

A modern operations dashboard for TaskFlow, published as a Claude Design canvas.

**This replaces nothing.** `dashboard/app.py` is the working Streamlit operator console
and remains the supported way to action a review. What lives here is a design artefact:
it reads no live data and calls no API, and its rows are sample data shaped like the real
thing.

## What it is grounded in

The structure and vocabulary come from the running system, not from invention:

| On screen | Source of truth |
|---|---|
| Intent chips | `Intent` in `src/taskflow/domain/enums.py` |
| The eight gates | `GateId`, and `services/confidence/gates.py` |
| Reason codes | `services/confidence/scorer.decide` and the gate ids |
| Weighted-score rows | the `weights:` block in `config/thresholds.yaml` |
| Per-intent bars | the `thresholds:` block in `config/thresholds.yaml` |
| Stage timeline | the stages `pipeline/orchestrator.py` writes to the trace |
| Retrieval payload | the `retrieve` trace event in `pipeline/orchestrator.py` |

## Files

- `Main.dc.html` — the artboard source. **Edit this**, not the built bundle.
- `canvas.json` — canvas layout and the note shown beside the artboard.
- `taskflow-console.html` — the built bundle (git-ignored; 2 MB of editor payload).

## Rebuilding

Re-seed a fresh bundle from the sources, then republish it to the same artifact URL:

```bash
node "<claude design skill dir>/seed-canvas.mjs" \
  --template "<claude design skill dir>/payload.template.html" \
  --out taskflow-console.html \
  --title "TaskFlow Console" \
  --artboard Main.dc.html \
  --canvas canvas.json
```

## If this concept is taken forward

It is a prototype, and the gap to a real console is the data layer, not the pixels. The
API already exposes what the three screens need — `GET /metrics` (action, intent and
reason-code breakdowns, cost), `GET /reviews`, `GET /traces`, `GET /traces/{id}`, and the
`POST /reviews/{id}/{approve,edit,reject}` actions — all behind the bearer token in
`src/taskflow/api/app.py`. The screens were drawn against those payload shapes.
