# Divergence-Gated Behavioral Baseline Learning with Overhead-Budgeted Adaptive Sampling

An operating-system monitoring agent that learns per-process behavioral baselines and detects anomalies while managing its own resource consumption through adaptive sampling.

## Architecture

```
Telemetry Collector → Context Builder → Baseline Store (EWM + CUSUM + Quarantine)
                                                        ↓
                                                  Anomaly Scorer (Mahalanobis)
                                                        ↓
                                              Risk Score + Feature Contributions
                                                        ↓
Response Controller ←→ Budgeted Sampler (budget-governed adaptive sampling)
```

## Components

| Module | Purpose |
|--------|---------|
| `agent/telemetry.py` | Process metrics collection (real psutil + simulation mode with attack patterns) |
| `agent/context.py` | Context keys (time bucket, session, parent) + ProcessIdentity (PID reuse safe) |
| `agent/baseline.py` | EWM baselines with CUSUM drift detection, quarantine buffer, and reconcile lifecycle |
| `agent/scorer.py` | Mahalanobis distance scorer with ranked per-feature contributions |
| `agent/sampler.py` | Budget-governed adaptive sampling with progressive degradation |
| `agent/response_controller.py` | Graduated response (log → notify → sampling escalation → suspend) with rollback |
| `agent/monitor.py` | Main orchestrator – runs the collection/processing loop |
| `agent/api.py` | FastAPI REST + WebSocket interface |
| `agent/models.py` | Pydantic models for API serialization |

## Quick Start

```bash
cd backend

# Install dependencies
pip install -r requirements.txt

# Start the API server (includes the monitoring agent in simulation mode)
python main.py
# Or: uvicorn agent.api:app --host 0.0.0.0 --port 8000

# Run the standalone simulator (in another terminal)
python simulator.py --mode normal --duration 60
python simulator.py --mode attack --duration 120
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Service info |
| `/status` | GET | Agent status (tier, budgets, violations) |
| `/alerts` | GET | List alerts (filter by `acknowledged`, `severity`) |
| `/alerts/{id}/acknowledge` | POST | Acknowledge an alert |
| `/baselines` | GET | List all active baselines |
| `/baselines/pid/{pid}` | GET | Baseline details for a process |
| `/baselines/pid/{pid}/reconcile` | POST | Merge quarantined samples (requires `corroborated=true`) |
| `/processes` | GET | List monitored processes with sampling tiers |
| `/processes/{pid}/tier?tier=N` | POST | Set sampling tier for a process |
| `/ws` | WebSocket | Real-time status stream (2s updates) |

## Simulation Mode

The telemetry collector includes a simulation mode with pre-configured processes:

- **Normal processes**: chrome, vscode, python3, node, postgres, nginx
- **Attack simulations**:
  - `malware_sim` – Boiling frog (gradual CPU ramp)
  - `exploit_py` – Spike attack (sudden CPU+IO burst)
  - `leaky_app` – Memory leak (continuous growth)
  - `bash_fork` – Child bomb (rapid process spawning)

## Baseline Lifecycle

```
LEARNING → NORMAL → FROZEN → RECONCILED
              ↑         ↓
              └─────────┘ (quarantine dismissed)
```

1. **LEARNING**: Accumulating initial observations (30 samples required)
2. **NORMAL**: Updating both short-term and long-term EWM baselines
3. **FROZEN**: Long-term baseline locked; samples go to quarantine buffer
4. **RECONCILED**: Quarantine merged after admin corroboration

## Sampling Tiers

| Tier | Interval | Use Case |
|------|----------|----------|
| MINIMAL | 5s | Background / trusted processes |
| LOW | 3s | Default tier |
| MEDIUM | 1s | Elevated risk |
| HIGH | 0.5s | Frozen baseline / high risk |

## Budget Configuration

The `BudgetConfig` model controls the agent's own resource limits:

```python
BudgetConfig(
    cpu_percent_max=5.0,
    memory_mib_max=256.0,
    io_bytes_per_sec_max=1_000_000.0,
    measurement_window_sec=30,
    include_kernel_overhead=True,
)
```

When the agent exceeds its budget, it progressively degrades:
`HIGH → MEDIUM → LOW → MINIMAL`

## Key Design Decisions

1. **Divergence gating with quarantine**: When CUSUM detects persistent drift, the long-term baseline is frozen and observations enter a bounded quarantine buffer. Samples are only merged after independent corroboration.

2. **CUSUM for slow poisoning**: An accumulated-drift detector catches persistent small changes that stay below the instantaneous divergence threshold.

3. **Interlock**: A baseline freeze triggers a sampling-tier increase for the affected process — subject to budget availability.

4. **Joint multi-resource scoring**: Mahalanobis distance with per-feature variance prevents double-counting correlated features and detects combinations of moderate deviations.

5. **Explainable alerts**: Each alert lists the top feature contributions with z-scores and direction.

6. **Graduated response**: Actions escalate from logging to process suspension, with all disruptive actions recorded for rollback.
