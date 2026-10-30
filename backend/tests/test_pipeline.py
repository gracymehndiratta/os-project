"""Pipeline unit tests — evidence for the design document claim-to-code map.

Covers baseline statistics, the store fallback, the joint scorer, the
divergence gate + quarantine + corroboration, the freeze/sampling interlock
and the end-to-end agent behaviour.
"""
from __future__ import annotations

import threading
import unittest

import numpy as np

from agent.agent import MonitorAgent
from agent.baseline import BaselineStore, Gaussian, KeyState
from agent.config import Config, FEATURE_NAMES, N_FEATURES
from agent.data_model import BaselineKey, GateState, RiskLevel, Tier
from agent.evaluation import evaluate
from agent.gate import CorroborationSource, DivergenceGate, is_independent, mean_shift
from agent.isolation import IsolationScorer
from agent.scenarios import benign_scenario, demo_scenario, slow_ramp_scenario
from agent.scoring import Scorer
from agent.sampling import LatencyTracker


def _rng(seed=0):
    return np.random.default_rng(seed)


class TestGaussian(unittest.TestCase):
    def test_exact_merge_matches_batch(self):
        rng = _rng(1)
        a = rng.normal(0.0, 1.0, (40, N_FEATURES))
        b = rng.normal(3.0, 2.0, (60, N_FEATURES))

        g1 = Gaussian()
        for x in a:
            g1.update(x)
        g2 = Gaussian()
        for x in b:
            g2.update(x)
        g1.merge(g2)

        batch = Gaussian()
        for x in np.vstack([a, b]):
            batch.update(x)

        np.testing.assert_allclose(g1.mean, batch.mean, atol=1e-9)
        # The exact parallel merge matches the batch covariance up to the
        # small initial ridge (1e-3) applied to the first observation, which
        # decays as O(1/n); 1e-4 is a safe bound for n=100.
        np.testing.assert_allclose(g1.cov, batch.cov, atol=1e-4)
        self.assertEqual(g1.n, 100)

    def test_ewma_update(self):
        g = Gaussian()
        for x in np.zeros((20, N_FEATURES)):
            g.update(x, alpha=0.1)
        np.testing.assert_allclose(g.mean, np.zeros(N_FEATURES), atol=1e-9)
        self.assertEqual(g.n, 20)


class TestStore(unittest.TestCase):
    def test_context_fallback_to_coarse(self):
        cfg = Config()
        cfg.baseline.min_samples = 5
        store = BaselineStore(cfg)
        key = BaselineKey("app", "morning|user|bash")
        exact = store.get_or_create(key)
        coarse = store.get_or_create(key.coarse())

        # Trust only the coarse baseline.
        for i in range(10):
            store.learn(coarse, np.zeros(N_FEATURES), i)

        self.assertEqual(exact.gate_state, GateState.LEARNING)
        self.assertEqual(coarse.gate_state, GateState.NORMAL)
        self.assertIs(store.resolve(key), coarse)

    def test_lru_eviction(self):
        cfg = Config()
        cfg.baseline.max_entries = 4
        store = BaselineStore(cfg)
        for i in range(10):
            store.get_or_create(BaselineKey(f"app{i}", "*"))
        self.assertLessEqual(len(store.all_states()), 4)

    def test_snapshot_is_safe_during_mutation(self):
        """Readers must not crash while the monitoring loop mutates the store."""
        store = BaselineStore(Config())
        errors: list = []

        def writer() -> None:
            try:
                for i in range(5000):
                    store.get_or_create(BaselineKey(f"app{i % 64}", "*"))
            except Exception as exc:  # pragma: no cover - regression guard
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        for _ in range(5000):
            store.all_states()
            store.frozen_states()
            store.quarantine_count()
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


class TestScorer(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.state = KeyState(key=BaselineKey("app", "*"))
        rng = _rng(2)
        for x in rng.normal(0.0, 1.0, (300, N_FEATURES)):
            self.state.long.update(x, alpha=0.05)
        self.state.reference = self.state.long.copy()
        self.scorer = Scorer(self.cfg)

    def test_contributions_rank_and_sum(self):
        x = np.zeros(N_FEATURES)
        x[0] = 6.0  # cpu is the big deviation
        score = self.scorer.score(self.state, x)
        self.assertGreater(score.d_squared, 10.0)
        self.assertEqual(score.contributions[0].name, "cpu_pct")
        total = sum(c.contribution_pct for c in score.contributions)
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_risk_levels_monotonic(self):
        near = self.scorer.score(self.state, np.zeros(N_FEATURES))
        far = self.scorer.score(self.state, np.full(N_FEATURES, 8.0))
        self.assertEqual(near.level, RiskLevel.NORMAL)
        self.assertIn(far.level, (RiskLevel.THROTTLE, RiskLevel.SUSPEND))


class TestGate(unittest.TestCase):
    def _trusted_state(self, cfg):
        store = BaselineStore(cfg)
        state = store.get_or_create(BaselineKey("app", "*"), "app", 42)
        rng = _rng(3)
        for i in range(60):
            store.learn(state, rng.normal(0.0, 0.05, N_FEATURES), i)
        return store, state

    def test_freezes_on_mean_shift_and_quarantines(self):
        cfg = Config()
        cfg.baseline.min_samples = 10
        store, state = self._trusted_state(cfg)
        gate = DivergenceGate(cfg)
        scorer = Scorer(cfg)

        shifted = np.ones(N_FEATURES)  # far from the ~0 baseline
        for tick in range(100, 110):
            score = scorer.score(state, shifted)
            gate.observe(state, score, shifted, tick)

        self.assertEqual(state.gate_state, GateState.FROZEN)
        self.assertGreater(state.quarantine_size, 0)
        self.assertTrue(state.flagged)

    def test_corroboration_merges_only_when_independent(self):
        cfg = Config()
        cfg.baseline.min_samples = 10
        store, state = self._trusted_state(cfg)
        gate = DivergenceGate(cfg)
        scorer = Scorer(cfg)
        shifted = np.ones(N_FEATURES)
        for tick in range(100, 108):
            gate.observe(state, scorer.score(state, shifted), shifted, tick)
        self.assertEqual(state.gate_state, GateState.FROZEN)
        buffered = state.quarantine_size
        self.assertGreater(buffered, 0)

        # Non-independent evidence is rejected.
        self.assertFalse(is_independent(None))
        self.assertEqual(gate.corroborate(store, 42, 200, source=None), 0)
        self.assertEqual(state.gate_state, GateState.FROZEN)

        # Independent corroboration merges and returns to NORMAL.
        merged = gate.corroborate(store, 42, 210, source=CorroborationSource.ADMIN)
        self.assertEqual(merged, buffered)
        self.assertEqual(state.gate_state, GateState.NORMAL)
        self.assertEqual(state.quarantine_size, 0)

    def test_ttl_discards_buffer(self):
        cfg = Config()
        cfg.baseline.min_samples = 10
        cfg.ticks_per_day = 10
        cfg.gate.ttl_days = 1.0  # 10 ticks
        store, state = self._trusted_state(cfg)
        gate = DivergenceGate(cfg)
        scorer = Scorer(cfg)
        shifted = np.ones(N_FEATURES)
        for tick in range(100, 104):
            gate.observe(state, scorer.score(state, shifted), shifted, tick)
        self.assertEqual(state.gate_state, GateState.FROZEN)

        # Advance past the TTL with no corroboration; normal samples resume.
        normal = np.zeros(N_FEATURES)
        for tick in range(120, 126):
            gate.observe(state, scorer.score(state, normal), normal, tick)
        self.assertEqual(state.gate_state, GateState.NORMAL)
        self.assertEqual(state.quarantine_size, 0)


class TestInterlock(unittest.TestCase):
    def test_freeze_raises_desired_tier(self):
        agent = MonitorAgent(
            config=Config(), scenario=demo_scenario(), model_overhead=True,
            enable_gate=True,
        )
        agent.run(700)
        # At least one frozen process was flagged and escalated to DETAILED.
        self.assertTrue(any(agent._flagged.values()))
        self.assertTrue(any(t == int(Tier.DETAILED) for t in agent._desired.values()))


class TestLatency(unittest.TestCase):
    def test_percentiles_and_empty(self):
        tracker = LatencyTracker()
        self.assertEqual(tracker.percentiles(), {"p50": 0.0, "p95": 0.0, "p99": 0.0})
        for v in range(1, 101):
            tracker.record(v / 1000.0)  # 1..100 ms
        p = tracker.percentiles()
        self.assertAlmostEqual(p["p50"], 0.0505, places=3)
        self.assertAlmostEqual(p["p95"], 0.09505, places=3)
        self.assertAlmostEqual(p["p99"], 0.09901, places=3)
        self.assertEqual(tracker.count, 100)


class TestAgentBehaviour(unittest.TestCase):
    def test_benign_has_no_false_positives(self):
        agent = MonitorAgent(
            config=Config(), scenario=benign_scenario(2 * 1440), model_overhead=True,
        )
        agent.run(2 * 1440)
        self.assertEqual(len(agent.alert_log), 0)
        self.assertEqual(len(agent.freeze_log), 0)

    def test_gate_catches_slow_ramp_but_plain_adaptive_does_not(self):
        scenario = slow_ramp_scenario(2 * 1440)

        full = evaluate(scenario, "full")
        self.assertTrue(full.detected)
        self.assertIsNotNone(full.detection_delay_days)

        plain = evaluate(scenario, "plain_adaptive")
        self.assertFalse(plain.detected)

    def test_status_shape_matches_dashboard(self):
        agent = MonitorAgent(config=Config(), scenario=benign_scenario(600), model_overhead=True)
        agent.run(200)
        status = agent.status()
        for field in (
            "running", "current_tier", "cpu_usage_percent", "memory_usage_mib",
            "io_bytes_per_sec", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
            "budget_violations", "active_baselines",
            "frozen_baselines", "quarantined_samples", "alerts_raised",
            "processes_monitored", "within_budget", "cpu_budget_max",
            "memory_budget_max", "io_budget_max",
        ):
            self.assertIn(field, status)


class TestIsolationScorer(unittest.TestCase):
    """The isolation-forest ablation (Review 1 §6 prior work)."""

    def _scorer(self):
        cfg = Config()
        cfg.isolation.n_estimators = 60
        cfg.isolation.max_samples = 256
        cfg.isolation.min_fit = 128
        cfg.isolation.refit_every = 10_000
        iso = IsolationScorer(cfg)
        key = BaselineKey("app-iforest", "*")
        rng = _rng(7)
        for i in range(300):
            iso.observe(key, rng.normal(0.0, 0.05, N_FEATURES), tick=i)
        return iso, key

    def test_no_score_before_min_fit(self):
        cfg = Config()
        cfg.isolation.n_estimators = 20
        cfg.isolation.refit_every = 10_000
        iso = IsolationScorer(cfg)
        key = BaselineKey("cold", "*")
        rng = _rng(1)
        for i in range(10):
            iso.observe(key, rng.normal(0.0, 0.05, N_FEATURES), tick=i)
        self.assertIsNone(iso.score(key, np.zeros(N_FEATURES), tick=10))
        self.assertEqual(iso.fitted_count(), 0)

    def test_outlier_scores_higher_than_in_distribution(self):
        iso, key = self._scorer()
        normal = iso.score(key, np.zeros(N_FEATURES), tick=100)
        outlier = iso.score(key, np.full(N_FEATURES, 2.0), tick=101)
        self.assertIsNotNone(normal)
        self.assertIsNotNone(outlier)
        self.assertGreater(outlier.risk, normal.risk)
        self.assertGreater(outlier.d_squared, normal.d_squared)
        self.assertGreaterEqual(outlier.risk, 0.0)
        self.assertLessEqual(outlier.risk, 1.0)

    def test_single_axis_shift_is_not_detected(self):
        """Documented limitation: iForest misses one-feature deviations."""
        iso, key = self._scorer()
        x = np.zeros(N_FEATURES)
        x[2] = 3.0
        score = iso.score(key, x, tick=102)
        self.assertIsNotNone(score)
        self.assertEqual(score.level, RiskLevel.NORMAL)

    def test_contributions_rank_and_sum(self):
        iso, key = self._scorer()
        x = np.full(N_FEATURES, 1.0)
        x[2] = 3.0
        score = iso.score(key, x, tick=103)
        self.assertIsNotNone(score)
        self.assertEqual(len(score.contributions), N_FEATURES)
        self.assertAlmostEqual(
            sum(abs(c.contribution_pct) for c in score.contributions), 1.0, places=6
        )
        self.assertEqual(score.contributions[0].name, FEATURE_NAMES[2])

    def test_agent_runs_with_iforest_detector(self):
        cfg = Config()
        cfg.isolation.n_estimators = 20
        cfg.isolation.refit_every = 200
        agent = MonitorAgent(
            config=cfg, scenario=benign_scenario(700), detector="iforest"
        )
        agent.run(700)
        self.assertEqual(agent.tick, 700)
        self.assertIsNotNone(agent.isolation)
        self.assertGreater(agent.isolation.fitted_count(), 0)


class TestEvaluationHarness(unittest.TestCase):
    def test_scenario_is_rebuilt_for_each_variant(self):
        """Every variant must see an identical (fresh) noise stream."""
        import agent.evaluation as ev

        calls = []
        real = ev._scenario_builders

        def spy(quick):
            builders = real(quick)

            def wrap(name):
                def build():
                    calls.append(name)
                    return builders[name]()

                return build

            return {k: wrap(k) for k in builders}

        ev._scenario_builders = spy
        try:
            ev.run_all(
                variants=["static", "plain_adaptive"],
                scenarios=["benign"],
                quick=True,
            )
        finally:
            ev._scenario_builders = real
        self.assertEqual(calls, ["benign", "benign"])


if __name__ == "__main__":
    unittest.main()
