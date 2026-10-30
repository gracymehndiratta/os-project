# Divergence-Gated Behavioral Baseline Learning with Overhead-Budgeted Adaptive Sampling

An operating-system monitoring agent that learns per-(application, context) behavioral
baselines, detects anomalous process behaviour with a joint multi-resource score, and
manages its own resource consumption through an adaptive sampling controller.

The prototype implements the design and implementation document
(*OS Behavior Monitor*, Oct 2026). It is a **per-tick pipeline** with two feedback loops:

- **Loop 1 — poisoning control (divergence gate).** The gate compares short-term,
  long-term and trusted-reference baselines, freezes the long-term baseline on suspicious
  drift, and holds new samples in a bounded quarantine buffer. Buffers are only merged
  after *independent* corroboration.
- **Loop 2 — overhead control (sampling controller).** A self-meter measures the agent's
  own CPU/RSS and the controller adjusts a per-tick cost budget to stay near the CPU
  target, assigning sampling tiers in priority order.
- **Interlock.** A baseline freeze flags the process and raises its desired sampling tier;
  a `THROTTLE` response escalates the tier through the response engine.

## Architecture

```
                       ┌──────────────────────────────────────────────┐
                       │              MonitorAgent (agent.py)          │
                       └──────────────────────────────────────────────┘
 Collector ──► Feature Builder ──► Identity/Context ──► Baseline Store
   ▲                                                        │
   │ sampling tier                    Joint Scorer ──► Explainer ──► Response Engine
   │                                                        │
 Sampling Controller ◄── freeze request ◄── Divergence Gate ◄─► Quarantine Buffer
   ▲                                                        │
 Self-Meter (agent CPU / RSS)                     Corroboration sources
```

Per-tick data flow (design §4):

1. the controller assigns a sampling tier to every process, in priority order, within budget;
2. the collector returns samples for processes due at that tier (tier 0 returns CPU + memory only);
3. each full sample becomes a log-scale feature vector, keyed by application identity and context;
4. the store picks the context-specific baseline if trusted, otherwise the coarse `*` baseline;
5. the scorer computes the joint deviation `D²` and a risk value;
6. the gate updates its drift measures and decides to learn / freeze / merge / discard;
7. alerts fire after several consecutive high-risk samples, carrying ranked contributions;
8. the self-meter reads the agent's own usage and the controller adjusts the budget.

During warm-up (`baseline.min_samples`) every sample is used for learning and nothing is
scored, so new baselines are not judged on a handful of samples.

## Module map

| Module | Responsibility | Claim step |
|--------|----------------|-----------|
| `agent/collector.py` | `ReplayCollector` (synthetic traces) and `PsutilCollector` (live sampling) | (a) telemetry |
| `agent/features.py` | Log-scale feature vector, stable application identity, time/parent context | (b)(c) |
| `agent/baseline.py` | `Gaussian` (incremental mean/cov + exact merge), `KeyState`, `BaselineStore` with coarse fallback + LRU | (b) baselines |
| `agent/scoring.py` | Regularized joint Mahalanobis `D²` + ranked per-feature contributions | (c)(g) |
| `agent/isolation.py` | Isolation-forest ablation (Review 1 §6 prior work); windowed refit, empirical-percentile risk | ablation |
| `agent/gate.py` | Mean-shift, CUSUM, reference drift; `NORMAL→FROZEN→MERGE/DISCARD`; quarantine; corroboration | (d)(e) |
| `agent/sampling.py` | `SelfMeter` (own usage) and `SamplingController` (tier assignment, budget hysteresis, reduced coverage) | (f)(g) |
| `agent/response.py` | Graduated responses (notify → throttle → suspend), dry-run by default, rollback | (g) |
| `agent/agent.py` | Wires the pipeline and runs the loops; interlocks | all |
| `agent/config.py` | All tunables from design §8 in one place | — |
| `agent/data_model.py` | `Sample`, `BaselineKey`, `Tier`, `GateState`, `RiskLevel`, `Alert`, … | — |
| `agent/scenarios.py` | Synthetic traces for the five experiment scenarios + live demo | §9 |
| `agent/evaluation.py` | Variants, scenario runner and metrics table | §9/§10 |
| `agent/api.py` | FastAPI REST under `/api` + WebSocket `/ws` | — |
| `agent/models.py` | Pydantic response models for the API boundary | — |
| `simulator.py` | Standalone demo / replay / live runner | — |
| `evaluate.py` | CLI for the evaluation harness | §9 |

## Quick start

### Backend

```bash
cd backend
pip install -r requirements.txt

# API server + dashboard backend (agent runs in simulation mode by default)
python main.py                     # http://localhost:8000  (docs at /docs)
# or: uvicorn agent.api:app --port 8000

# Live psutil collection instead of replay:
OSMON_LIVE=1 python main.py

# Quiet demo: one short attack burst that then settles
OSMON_SCENARIO=single_alert OSMON_TICK_INTERVAL=0.2 python main.py

# Standalone simulator
python simulator.py --scenario demo --ticks 600
python simulator.py --scenario slow_ramp --variant full --ticks 3000
python simulator.py --live --ticks 60

# Evaluation harness (design §9/§10)
python evaluate.py --quick
python evaluate.py --scenarios slow_ramp evading_ramp --variants full plain_adaptive
```

### Frontend dashboard

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000, proxies /api and /ws to :8000
npm run build      # production build into dist/
```

### Tests

There is no `pytest` dependency; use the standard library runner from `backend/`:

```bash
cd backend
python -m unittest discover -s tests
```

## Configuration

All parameters live in `agent/config.py` (design §8). The defaults below are **starting
points** — the design document says to choose final values from experiments, and
`evaluate.py` is the harness used to re-tune them.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `baseline.alpha_short` / `alpha_long` | `0.1` / `0.01` | EWMA rates of the two baselines |
| `baseline.min_samples` | `30` | Samples before a baseline is trusted |
| `baseline.ridge` | `0.02` | Covariance regularization (min tolerance in log units) |
| `baseline.max_entries` | `256` | LRU cap on stored (application, context) keys |
| `gate.shift_thr` | `1.5` | Short-vs-long standardized shift that triggers a freeze |
| `gate.cusum_k` / `cusum_h` | `0.5` / `12.0` | CUSUM allowance and alarm level |
| `gate.ref_thr` | `1.0` | Long-vs-reference shift that triggers a freeze |
| `gate.update_guard` | `3.0` | Per-sample score above which the long baseline does not learn |
| `gate.ttl_days` | `1.0` | Lifetime of an uncorroborated quarantine buffer |
| `gate.max_buffers` / `max_buffer_samples` | `64` / `512` | Buffer caps |
| `score.notify` / `throttle` / `suspend` | `0.995` / `0.9995` / `0.99995` | Chi-squared risk percentiles for response levels |
| `score.alert_consecutive` | `3` | Consecutive high-risk samples before an alert fires |
| `sampling.cpu_budget_pct` | `1.0` | Target agent CPU use (percent of one core) |
| `sampling.window_ticks` | `20` | Self-meter measurement window |
| `sampling.budget_shrink` / `budget_grow` | `0.20` / `0.15` | Budget change per over/under window |
| `sampling.grow_threshold` / `dwell_count` | `0.70` / `3` | Grow only after several under-target windows (hysteresis) |
| `dry_run` | `True` | Disruptive responses are logged, not executed |

**Tuned in practice.** Two knobs were adjusted from the design defaults after experiments
(design §8 says to tune): the diurnal amplitude in `scenarios._diurnal` is `0.08`, and the
gate's standardization ridge `_GATE_RIDGE` is `1e-3`. The gate's reference snapshot is
taken once the long baseline has converged (`2 * min_samples`), and drift measures are
standardized by the **trusted reference** covariance so a slow ramp cannot hide by
inflating the live long baseline's own variance.

## Features

Five canonical features (design §7): `cpu_pct`, `rss_mb`, `disk_io_kbps`, `net_kbps`,
`child_rate`. Live `psutil` collection has no per-process network byte counter, so the
live collector uses the **open-socket count as a proxy**; real byte counts need eBPF/ETW
(out of scope).

## Sampling tiers

| Tier | Interval | Features | Cost / tick |
|------|----------|----------|-------------|
| 0 — MINIMAL | every 8 ticks | CPU + memory only | 0.125 units |
| 1 — NORMAL | every 2 ticks | all five | 2 units |
| 2 — DETAILED | every tick | all five | 4 units |

When the budget is tight, processes are served in priority order: flagged and high-risk
processes first. Every process keeps at least tier 0, and each tick where a process
receives less than it wanted is counted as **reduced coverage**.

## Gate state machine

```
LEARNING ──(min_samples)──► NORMAL ──(mean shift | CUSUM | reference drift)──► FROZEN
                              ▲                                                  │
                              │                    independent corroboration ──► MERGE
                              │                    TTL expired ───────────────► DISCARD
                              └──────────────────────────────────────────────────┘
```

- **NORMAL** — short baseline always learns; long baseline learns only when the sample
  score is under the update guard.
- **FROZEN** — long baseline locked; samples go to quarantine; the key is flagged.
- **MERGE** — buffer summary merged into the long baseline; reference reset; CUSUM cleared.
- **DISCARD** — buffer dropped; short baseline reset to long; the gate may freeze again if
  the behaviour continues.

A return to normal behaviour is **never** treated as corroboration (malware can stop on
purpose). Corroboration comes only from independent sources: an administrator confirmation
or a verified application update.

## API endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Service info |
| `/api/status` | GET | Agent status (tick, tier, budgets, violations) |
| `/api/alerts` | GET | List alerts (`acknowledged`, `severity`, `limit`) |
| `/api/alerts/{id}/acknowledge` | POST | Acknowledge an alert |
| `/api/baselines` | GET | List all active baselines |
| `/api/baselines/pid/{pid}` | GET | Baselines for a process |
| `/api/baselines/pid/{pid}/reconcile` | POST | Merge quarantine on corroboration (`corroborated=true`) |
| `/api/baselines/pid/{pid}/dismiss` | POST | Discard quarantine (TTL-style) |
| `/api/processes` | GET | Monitored processes with risk + sampling tier |
| `/api/processes/{pid}/tier` | POST | Set sampling tier for a process |
| `/api/budget` | GET | Sampling budget status |
| `/api/rollback-actions` | GET | Recorded reversible actions |
| `/api/rollback/{action_id}` | POST | Roll back a recorded action |
| `/ws` | WebSocket | Real-time status stream (2 s updates) |

## Experiment plan and metrics

Variants (design §9): `static`, `plain_adaptive`, `gate_only`, `budget_only`, `full`, plus
`iforest` (the Review 1 §6 prior-work ablation).
`gate_only` and `budget_only` are the ablations that show each controller contributes.

Scenarios (design §9):

| Scenario | Simulates | Main measurements |
|----------|-----------|-------------------|
| `benign` | days of normal diurnal behaviour | false-positive episodes per process-day |
| `slow_ramp` | gradual multi-resource ramp over days | detection rate and delay |
| `evading_ramp` | ramp slow enough to stay under the short-vs-long gate | whether reference drift still catches it |
| `legit_step` | permanent benign step + administrator confirmation | alerts after the step, recovery time |
| `overhead` | many simulated processes under a CPU budget | modeled CPU, budget violations, reduced coverage |

Metrics reported by `format_results`: detection rate and delay (simulated days),
false-positive episodes per process per day, alerts remaining after a legitimate step and
the time until they stop, agent CPU, budget violations, reduced-coverage ticks and
p50/p95/p99 per-sample latency (design §10). Latency is wall-clock and machine-dependent,
so it is omitted from the fixed reference table below; `agent.status()` exposes it as
`latency_p50_ms` / `latency_p95_ms` / `latency_p99_ms` for live runs.

### Reference results (`python evaluate.py --quick`)

| scenario | variant | det | delay(d) | atk_alerts | FP/proc/day | freezes | cpu% | reduced | after | recov(d) |
|----------|---------|-----|----------|-----------|-------------|---------|------|---------|-------|----------|
| benign | static | - | - | 0 | 0.0000 | 0 | 0.02 | 0 | - | - |
| benign | plain_adaptive | - | - | 0 | 0.0000 | 0 | 0.02 | 0 | - | - |
| benign | gate_only | - | - | 0 | 0.0000 | 0 | 0.02 | 0 | - | - |
| benign | budget_only | - | - | 0 | 0.0000 | 0 | 0.02 | 0 | - | - |
| benign | full | - | - | 0 | 0.0000 | 0 | 0.02 | 0 | - | - |
| benign | iforest | - | - | 0 | 0.0000 | 0 | 0.02 | 0 | - | - |
| slow_ramp | static | yes | 0.92 | 240 | 0.0000 | 0 | 0.03 | 0 | - | - |
| slow_ramp | plain_adaptive | yes | 1.17 | 1 | 0.0000 | 0 | 0.03 | 0 | - | - |
| slow_ramp | gate_only | yes | 0.92 | 433 | 0.0000 | 7 | 0.04 | 0 | - | - |
| slow_ramp | budget_only | yes | 1.17 | 1 | 0.0000 | 0 | 0.03 | 0 | - | - |
| slow_ramp | full | yes | 0.92 | 433 | 0.0000 | 7 | 0.04 | 0 | - | - |
| slow_ramp | iforest | no | - | 0 | 0.0000 | 0 | 0.03 | 0 | - | - |
| evading_ramp | static | yes | 1.12 | 253 | 0.0000 | 0 | 0.03 | 0 | - | - |
| evading_ramp | plain_adaptive | no | - | 0 | 0.0000 | 0 | 0.03 | 0 | - | - |
| evading_ramp | gate_only | yes | 1.92 | 466 | 0.0000 | 8 | 0.04 | 0 | - | - |
| evading_ramp | budget_only | no | - | 0 | 0.0000 | 0 | 0.03 | 0 | - | - |
| evading_ramp | full | yes | 1.92 | 466 | 0.0000 | 8 | 0.04 | 0 | - | - |
| evading_ramp | iforest | no | - | 0 | 0.0000 | 0 | 0.03 | 0 | - | - |
| legit_step | static | - | - | 0 | - | 0 | 0.03 | 0 | 240 | 0.50 |
| legit_step | plain_adaptive | - | - | 0 | - | 0 | 0.03 | 0 | 4 | 0.25 |
| legit_step | gate_only | - | - | 0 | - | 4 | 0.03 | 0 | 480 | 0.50 |
| legit_step | budget_only | - | - | 0 | - | 0 | 0.03 | 0 | 4 | 0.25 |
| legit_step | full | - | - | 0 | - | 4 | 0.03 | 0 | 480 | 0.50 |
| legit_step | iforest | - | - | 0 | - | 0 | 0.03 | 0 | 0 | 0.00 |
| overhead | static | - | - | 0 | 0.0000 | 0 | 0.24 | 0 | - | - |
| overhead | plain_adaptive | - | - | 0 | 0.0000 | 0 | 0.24 | 0 | - | - |
| overhead | gate_only | - | - | 0 | 0.0000 | 0 | 0.24 | 0 | - | - |
| overhead | budget_only | - | - | 0 | 0.0000 | 0 | 0.23 | 15 | - | - |
| overhead | full | - | - | 0 | 0.0000 | 0 | 0.23 | 15 | - | - |
| overhead | iforest | - | - | 0 | 0.6400 | 0 | 0.24 | 0 | - | - |

What this shows:

- **No false-positive alerts** on the benign scenario for any variant (0 episodes per
  process-day), and no benign freezes for the gate variants.
- A **non-adapting baseline** (`static`) detects both ramps only because it never learns —
  but it cannot adapt to legitimate change and keeps alerting for 240 alerts after
  `legit_step`. It is not viable in practice.
- An **adaptive baseline without the gate** (`plain_adaptive`, `budget_only`) produces at
  most a single late alert on the gradual ramp (1.17 days) and none on the evading ramp,
  versus 433–466 sustained alerts for the gate variants. The evading ramp is caught by the
  gate through reference drift at ~1.92 days.
- On `legit_step`, `plain_adaptive` absorbs the benign step almost immediately (4 alerts
  after, ~0.25 days) — but that same fast adaptation is exactly why it is poisonable. The
  gate variants freeze on the step and only recover once the administrator corroborates
  (~0.50 days after confirmation), which is the honest tradeoff of design §11.3.
- The budget controller is active under load (`overhead`): it reduces coverage for 15 ticks
  and keeps modeled CPU near 0.23%, with no budget violations.
- The **isolation-forest ablation** (Review 1 §6 prior work) misses both ramps and raises no
  alerts on `legit_step`; it also produces 0.64 false positives per process-day on the
  heterogeneous `overhead` fleet and costs roughly an order of magnitude more per sample. A
  point-anomaly detector cannot see collective drift and has no per-application context.

Numbers are from the fast `--quick` run; `python evaluate.py` runs the longer configuration.
The harness rebuilds the scenario for every variant, so each sees the same noise stream.
These are synthetic-trace results only (design §11). Per-sample latency is reported by the
harness (p50 ≈ 0.18 ms, p95 ≈ 0.37 ms) but is wall-clock and machine dependent.

## Simulation mode

The replay collector ships with pre-configured benign processes (chrome, vscode, python3,
node, postgres, nginx) and, for the live demo, four attack patterns:

- `malware_sim` — slow multi-resource ramp (boiling frog)
- `exploit_py` — sudden CPU + I/O burst (spike)
- `leaky_app` — continuous memory growth (leak)
- `bash_fork` — rapid child-process spawning (fork bomb)

## Claim-to-code map (design §13)

| Draft claim step | Implemented in | Proved by |
|------------------|----------------|-----------|
| Collect multi-resource telemetry | `collector`, `features` | `TestReplayCollector`, `TestPsutilCollector`; replay runs |
| Context vector and context-specific baseline | `features`, `baseline` | `TestStore`; `benign` scenario false positives |
| Joint deviation score with correlations | `scoring.mahalanobis` | `TestScorer` |
| Divergence between short and long baselines, freeze | `gate` (`mean_shift`, `reference_drift`, CUSUM, state machine) | `TestGate`; `slow_ramp`, `evading_ramp` |
| Quarantine buffer and reconcile on independent corroboration | `gate` + `baseline.QuarantineBuffer` | `TestGate.test_corroboration_*`; `legit_step` |
| Own-overhead measurement and budgeted sampling | `sampling` (`SelfMeter`, `SamplingController`, `LatencyTracker`) | `TestSelfMeter`, `TestSamplingController`, `TestLatency`; `overhead` |
| Freeze raises sampling priority | `agent._priorities`, `agent._on_throttle` | `TestInterlock` |
| Ranked alert and graduated response | `scoring.explain`, `response` | `TestResponseEngine` |

## Design decisions

1. **Divergence gating with quarantine** — when drift is detected the long-term baseline is
   frozen and observations enter a bounded quarantine buffer, merged only after
   independent corroboration.
2. **CUSUM for slow poisoning** — an accumulated-drift detector catches persistent small
   changes that stay below the instantaneous threshold.
3. **Reference drift** — a trusted snapshot catches an attacker who moves both short and
   long baselines together.
4. **Interlock** — a freeze (and a `THROTTLE` response) raises the affected process's
   sampling tier, subject to budget availability.
5. **Joint multi-resource scoring** — Mahalanobis distance with per-feature variance
   prevents double-counting correlated features and detects combinations of moderate
   deviations.
6. **Explainable alerts** — each alert lists the top feature contributions with z-scores
   and direction.
7. **Graduated response** — actions escalate from logging to notification to sampling
   escalation to suspension, all recorded for rollback, in dry-run mode by default.

## Honest limitations (design §11)

- Attack traces are **synthetic**; no claim is made about performance on real attacks.
- Divergence gating **reduces but does not eliminate** slow-poisoning risk; reference drift
  narrows the gap but also freezes on legitimate slow change, which then needs
  administrator confirmation.
- Requiring independent corroboration means most buffers are discarded, so baselines can go
  stale after real workload changes and raise false positives (measured by `legit_step`).
- Load can push the monitor into degraded sampling; reduced coverage is counted and should
  be treated as a risk signal.
- A Python prototype **overstates overhead** because of interpreter cost.
- Automated suspension can disrupt legitimate processes; responses stay in dry-run mode
  unless explicitly enabled.
