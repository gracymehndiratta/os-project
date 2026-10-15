"""Central configuration for the OS Behavior Monitor.

Every tunable parameter from the design document §8 lives here.  Modules
import the singleton ``CFG`` or accept a ``Config`` instance to override
defaults in tests and experiments.

The defaults are *starting points* – the design document explicitly says to
choose final values from experiments, not by guesswork.  ``evaluation.py``
is the harness used to re-tune them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Canonical feature vector
# ---------------------------------------------------------------------------
# Design §7: CPU percent, resident memory (MB), disk I/O (KB/s), network
# (KB/s) and child-process rate.  The live psutil collector has no
# per-process network byte counter, so it uses an open-socket count as a
# proxy (see collector.PsutilCollector).
N_FEATURES = 5

FEATURE_NAMES: list[str] = [
    "cpu_pct",       # CPU percent
    "rss_mb",        # Resident memory in MB
    "disk_io_kbps",  # Combined disk read+write in KB/s
    "net_kbps",      # Combined network in KB/s (open-socket proxy when live)
    "child_rate",    # New child processes per second
]


@dataclass
class BaselineConfig:
    """Baseline learning parameters (§8)."""
    alpha_short: float = 0.1       # EWMA rate of the short-term baseline
    alpha_long: float = 0.01       # EWMA rate of the long-term baseline
    min_samples: int = 30          # Samples before a baseline is trusted
    ridge: float = 0.02            # Covariance regularization (min tolerance in log units)
    max_entries: int = 256         # LRU cap on stored (application, context) keys


@dataclass
class GateConfig:
    """Divergence gate parameters (§8)."""
    shift_thr: float = 1.5         # Short vs long standardized shift → freeze
    cusum_k: float = 0.5           # CUSUM allowance (slack, standardized units)
    cusum_h: float = 12.0          # CUSUM alarm level
    ref_thr: float = 1.0           # Long vs reference shift → freeze
    update_guard: float = 3.0      # Per-sample score above which long baseline skips learning
    ttl_days: float = 1.0          # Lifetime of an uncorroborated quarantine buffer
    max_buffers: int = 64          # Global cap on active quarantine buffers
    max_buffer_samples: int = 512  # Per-buffer sample cap


@dataclass
class ScoreConfig:
    """Scoring and alert parameters (§8).

    Risk thresholds are chi-squared CDF percentiles evaluated at runtime
    with ``scipy.stats.chi2.cdf(d2, df=N_FEATURES)``.
    """
    notify: float = 0.995          # Risk percentile for notification
    throttle: float = 0.9995       # Risk percentile for sampling escalation
    suspend: float = 0.99995       # Risk percentile for suspension consideration
    alert_consecutive: int = 3     # Consecutive high-risk samples before an alert fires


@dataclass
class SamplingConfig:
    """Overhead-budgeted sampling controller parameters (§6, §8)."""
    cpu_budget_pct: float = 1.0    # Target agent CPU use (percent of one core)
    window_ticks: int = 20         # Self-meter measurement window in ticks
    budget_shrink: float = 0.20    # Fraction to shrink budget when over target
    budget_grow: float = 0.15      # Fraction to grow budget when under target
    grow_threshold: float = 0.70   # Usage must stay below this fraction of target to grow
    dwell_count: int = 3           # Windows below threshold before budget grows
    initial_budget: float = 64.0   # Starting cost budget in units per tick
    min_budget: float = 4.0        # Floor for the cost budget
    max_budget: float = 512.0      # Ceiling for the cost budget

    # Tier definitions (§6): (interval_ticks, cost_per_tick)
    # Tier 0 – MINIMAL: every 8 ticks, CPU + memory only, cheapest
    tier0_interval: int = 8
    tier0_cost: float = 0.125
    # Tier 1 – NORMAL: every 2 ticks, all five features
    tier1_interval: int = 2
    tier1_cost: float = 2.0
    # Tier 2 – DETAILED: every tick, all five features, most expensive
    tier2_interval: int = 1
    tier2_cost: float = 4.0

    @property
    def max_tier(self) -> int:
        return 2

    def tier_interval(self, tier: int) -> int:
        """Sampling interval in ticks for a tier (0, 1 or 2)."""
        return {0: self.tier0_interval, 1: self.tier1_interval, 2: self.tier2_interval}.get(
            int(tier), self.tier1_interval
        )

    def tier_cost(self, tier: int) -> float:
        """Cost per tick in budget units for a tier (0, 1 or 2)."""
        return {0: self.tier0_cost, 1: self.tier1_cost, 2: self.tier2_cost}.get(
            int(tier), self.tier1_cost
        )

    def tier_features(self, tier: int) -> list[bool]:
        """Measured-feature mask for a tier.

        Tier 0 measures only CPU and memory; tiers 1 and 2 measure all five.
        """
        if int(tier) == 0:
            return [True, True, False, False, False]
        return [True] * N_FEATURES


@dataclass
class Config:
    """Top-level configuration aggregating all sub-configs."""
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    score: ScoreConfig = field(default_factory=ScoreConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    dry_run: bool = True          # All disruptive responses are logged, not executed
    warm_up_ticks: int = 0        # Extra ticks of learning-only (beyond min_samples)
    ticks_per_day: int = 1440     # Replay ticks that represent one simulated day


# Module-level default instance
CFG = Config()
