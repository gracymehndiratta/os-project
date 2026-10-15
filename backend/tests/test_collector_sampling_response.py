"""Unit tests for collector, sampling, and response packages."""
from __future__ import annotations

import unittest
import numpy as np

from agent.config import Config, N_FEATURES
from agent.data_model import (
    BaselineKey,
    GateState,
    RiskLevel,
    Sample,
    ScoreResult,
    Tier,
    FeatureContribution,
)
from agent.collector import ReplayCollector, PsutilCollector
from agent.sampling import SelfMeter, SamplingController
from agent.response import ResponseEngine, RollbackAction


class TestReplayCollector(unittest.TestCase):
    """Test suite for ReplayCollector."""

    def setUp(self):
        self.config = Config()
        self.traces = [
            {
                "app_id": "app_test_1",
                "exe_name": "worker.exe",
                "pid": 1234,
                "start_time": 1000.0,
                "parent_name": "bash",
                "username": "gracy",
                "generate": lambda tick: [10.0 + tick, 100.0, 50.0, 20.0, 1.0],
            },
            {
                "app_id": "app_test_2",
                "exe_name": "backup.exe",
                "pid": 5678,
                "start_time": 1000.0,
                "parent_name": "cron",
                "username": "root",
                "generate": lambda tick: None if tick < 2 else [5.0, 40.0, 10.0, 0.0, 0.0],
            },
        ]
        self.collector = ReplayCollector(self.traces, self.config)

    def test_default_tier_normal_collection(self):
        # Default tier is NORMAL (tier1_interval = 2)
        # At tick=0: 0 % 2 == 0 -> should collect worker.exe (backup.exe returns None at tick 0)
        samples = self.collector.collect(tick=0)
        self.assertEqual(len(samples), 1)
        s = samples[0]
        self.assertEqual(s.pid, 1234)
        self.assertEqual(s.tier, Tier.NORMAL)
        self.assertEqual(s.cost, self.config.sampling.tier1_cost)
        self.assertTrue(np.all(s.measured_mask))
        self.assertEqual(len(s.raw_features), N_FEATURES)
        self.assertEqual(s.raw_features[0], 10.0)

        # At tick=1: 1 % 2 != 0 -> should collect nothing
        samples1 = self.collector.collect(tick=1)
        self.assertEqual(len(samples1), 0)

        # At tick=2: both worker and backup are due and active
        samples2 = self.collector.collect(tick=2)
        self.assertEqual(len(samples2), 2)

    def test_tier_intervals_and_masking(self):
        # Assign worker to Tier 0 (MINIMAL) and backup to Tier 2 (DETAILED)
        tier_assignments = {
            "1234@1000": Tier.MINIMAL,
            "5678@1000": Tier.DETAILED,
        }
        # Tier 0 interval is 8. Tier 2 interval is 1.
        # At tick=1: worker (interval 8) not due. backup (interval 1) is due.
        samples = self.collector.collect(tick=1, tier_assignments=tier_assignments)
        self.assertEqual(len(samples), 0)  # backup returns None for tick < 2

        # At tick=2: backup is due (tick=2 % 1 == 0). worker is not due (2 % 8 != 0).
        samples = self.collector.collect(tick=2, tier_assignments=tier_assignments)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].pid, 5678)
        self.assertEqual(samples[0].tier, Tier.DETAILED)
        self.assertTrue(np.all(samples[0].measured_mask))

        # At tick=8: worker is due (8 % 8 == 0) and backup is due (8 % 1 == 0).
        samples = self.collector.collect(tick=8, tier_assignments=tier_assignments)
        self.assertEqual(len(samples), 2)
        worker_sample = next(s for s in samples if s.pid == 1234)
        self.assertEqual(worker_sample.tier, Tier.MINIMAL)
        self.assertEqual(worker_sample.cost, self.config.sampling.tier0_cost)
        # Check Tier 0 measured mask: only CPU and RAM
        np.testing.assert_array_equal(
            worker_sample.measured_mask,
            np.array([True, True, False, False, False]),
        )
        # Disk, Net, Child must be zeroed out
        self.assertEqual(worker_sample.raw_features[0], 18.0)
        self.assertEqual(worker_sample.raw_features[1], 100.0)
        self.assertEqual(worker_sample.raw_features[2], 0.0)
        self.assertEqual(worker_sample.raw_features[3], 0.0)
        self.assertEqual(worker_sample.raw_features[4], 0.0)


class TestPsutilCollector(unittest.TestCase):
    """Test suite for PsutilCollector."""

    def test_collect(self):
        config = Config()
        collector = PsutilCollector(config)
        samples = collector.collect(tick=0)
        self.assertIsInstance(samples, list)
        self.assertGreater(len(samples), 0)
        sample = samples[0]
        self.assertIsInstance(sample, Sample)
        self.assertEqual(len(sample.raw_features), N_FEATURES)
        self.assertEqual(len(sample.measured_mask), N_FEATURES)
        self.assertIn("@", sample.process_instance_id)
        self.assertEqual(len(sample.application_id), 16)


class TestSelfMeter(unittest.TestCase):
    """Test suite for SelfMeter."""

    def test_mock_measure_and_usage(self):
        meter = SelfMeter(window_ticks=3)
        meter.set_mock(cpu=1.5, rss=40.0)

        # Measure 1
        m1 = meter.measure(tick=0)
        self.assertAlmostEqual(m1["cpu_pct"], 1.5)
        self.assertAlmostEqual(m1["rss_mb"], 40.0)

        # Change mock
        meter.set_mock(cpu=2.5, rss=50.0)
        m2 = meter.measure(tick=1)
        # Average of 1.5 and 2.5 is 2.0
        self.assertAlmostEqual(m2["cpu_pct"], 2.0)
        self.assertAlmostEqual(m2["rss_mb"], 45.0)

        # Measure 3
        meter.set_mock(cpu=2.0, rss=60.0)
        m3 = meter.measure(tick=2)
        # Average of 1.5, 2.5, 2.0 is 2.0
        self.assertAlmostEqual(m3["cpu_pct"], 2.0)
        self.assertAlmostEqual(m3["rss_mb"], 50.0)

        # Window slides (window_ticks=3)
        meter.set_mock(cpu=5.0, rss=60.0)
        m4 = meter.measure(tick=3)
        # Window has: 2.5, 2.0, 5.0 -> avg = 9.5 / 3 = 3.16666...
        self.assertAlmostEqual(m4["cpu_pct"], 9.5 / 3.0)

        # Usage fraction against budget of 2.0%
        # current_cpu is 9.5 / 3 = ~3.1667; fraction = 3.1667 / 2.0 = ~1.5833
        fraction = meter.get_usage_fraction(budget_pct=2.0)
        self.assertAlmostEqual(fraction, (9.5 / 3.0) / 2.0)


class TestSamplingController(unittest.TestCase):
    """Test suite for SamplingController."""

    def setUp(self):
        self.config = Config()
        self.controller = SamplingController(self.config, initial_budget=10.0, min_budget=1.0, max_budget=50.0)

    def test_assign_tiers_greedy_and_flagged_priority(self):
        # 3 processes:
        # P1: desired Tier 2, unflagged
        # P2: desired Tier 2, FLAGGED
        # P3: desired Tier 1, unflagged
        #
        # Base cost (Tier 0 for 3 procs) = 3 * 0.125 = 0.375
        # If budget is 5.0:
        # Remaining budget = 5.0 - 0.375 = 4.625
        # Priority order:
        # 1. P2 (flagged, desired 2): needs upgrade 4.0 - 0.125 = 3.875 -> Granted Tier 2. Remaining = 0.75
        # 2. P1 (unflagged, desired 2): needs 3.875 (not enough) or 1.875 (not enough) -> Granted Tier 0.
        # 3. P3 (unflagged, desired 1): needs 1.875 (not enough) -> Granted Tier 0.
        ctrl = SamplingController(self.config, initial_budget=5.0)
        procs = [
            ("P1", 2, False),
            ("P2", 2, True),
            ("P3", 1, False),
        ]
        assigned = ctrl.assign_tiers(procs)
        self.assertEqual(assigned["P2"], Tier.DETAILED)
        self.assertEqual(assigned["P1"], Tier.MINIMAL)
        self.assertEqual(assigned["P3"], Tier.MINIMAL)
        self.assertEqual(ctrl.reduced_coverage_ticks, 1)

    def test_adjust_budget_shrink_and_grow(self):
        ctrl = SamplingController(
            self.config,
            initial_budget=10.0,
            min_budget=2.0,
            max_budget=20.0,
        )
        # target is config.sampling.cpu_budget_pct = 1.0
        # Over target: 1.5 > 1.0 -> shrink by 20%
        ctrl.adjust_budget(measured_cpu_pct=1.5)
        self.assertAlmostEqual(ctrl.budget, 8.0)
        self.assertEqual(ctrl.budget_violation_count, 1)
        self.assertEqual(ctrl.below_count, 0)

        # Under grow_threshold * target = 0.70 * 1.0 = 0.70
        # dwell_count is 3 windows
        ctrl.adjust_budget(measured_cpu_pct=0.5)  # window 1
        self.assertEqual(ctrl.below_count, 1)
        self.assertAlmostEqual(ctrl.budget, 8.0)

        ctrl.adjust_budget(measured_cpu_pct=0.4)  # window 2
        self.assertEqual(ctrl.below_count, 2)
        self.assertAlmostEqual(ctrl.budget, 8.0)

        ctrl.adjust_budget(measured_cpu_pct=0.6)  # window 3 -> grows by 15%!
        self.assertEqual(ctrl.below_count, 0)
        self.assertAlmostEqual(ctrl.budget, 8.0 * 1.15)


class TestResponseEngine(unittest.TestCase):
    """Test suite for ResponseEngine."""

    def setUp(self):
        self.config = Config(dry_run=True)
        self.engine = ResponseEngine(self.config)
        self.key = BaselineKey(application_id="app123", context="normal")
        self.sample = Sample(
            process_instance_id="999@1000",
            application_id="app123",
            parent_application="bash",
            tick=10,
            raw_features=np.array([50.0, 200.0, 100.0, 50.0, 2.0]),
            measured_mask=np.ones(N_FEATURES, dtype=bool),
            tier=Tier.NORMAL,
            cost=2.0,
            exe_name="test_service",
            pid=999,
        )

    def test_consecutive_scoring_and_alert(self):
        # ScoreResult with RiskLevel.THROTTLE
        contrib = FeatureContribution(
            name="cpu_pct",
            value=np.log1p(50.0),
            baseline_mean=2.0,
            baseline_std=0.5,
            z_score=3.8,
            contribution_pct=0.75,
        )
        score_high = ScoreResult(
            d_squared=25.0,
            risk=0.9998,
            level=RiskLevel.THROTTLE,
            contributions=[contrib],
            feature_vector=np.zeros(5),
        )
        score_normal = ScoreResult(
            d_squared=2.0,
            risk=0.5,
            level=RiskLevel.NORMAL,
            contributions=[],
            feature_vector=np.zeros(5),
        )

        # 1st high: count=1 < 3 -> no alert
        a1 = self.engine.evaluate(self.key, score_high, GateState.NORMAL, self.sample, tick=1)
        self.assertIsNone(a1)
        self.assertEqual(self.engine.alert_count(), 0)

        # 2nd high: count=2 < 3 -> no alert
        a2 = self.engine.evaluate(self.key, score_high, GateState.NORMAL, self.sample, tick=2)
        self.assertIsNone(a2)
        self.assertEqual(self.engine.alert_count(), 0)

        # 3rd high: count=3 >= 3 -> alert!
        a3 = self.engine.evaluate(self.key, score_high, GateState.NORMAL, self.sample, tick=3)
        self.assertIsNotNone(a3)
        self.assertEqual(self.engine.alert_count(), 1)
        self.assertEqual(a3.level, RiskLevel.THROTTLE)
        self.assertEqual(a3.consecutive_count, 3)
        self.assertEqual(a3.pid, 999)
        self.assertEqual(a3.process_name, "test_service")
        self.assertTrue(a3.dry_run)

        # Score normal -> resets consecutive count
        a4 = self.engine.evaluate(self.key, score_normal, GateState.NORMAL, self.sample, tick=4)
        self.assertIsNone(a4)
        self.assertEqual(self.engine._consecutive_counts[self.key], 0)

    def test_dry_run_and_rollback_action(self):
        alert = self.engine.evaluate(
            self.key,
            ScoreResult(
                d_squared=35.0,
                risk=0.99999,
                level=RiskLevel.SUSPEND,
                contributions=[],
                feature_vector=np.zeros(5),
            ),
            GateState.FROZEN,
            self.sample,
            tick=1,
        )
        # 1st evaluation -> None
        self.assertIsNone(alert)
        # Advance to 3
        self.engine.evaluate(self.key, ScoreResult(35.0, 0.99999, RiskLevel.SUSPEND, [], np.zeros(5)), GateState.FROZEN, self.sample, 2)
        alert = self.engine.evaluate(self.key, ScoreResult(35.0, 0.99999, RiskLevel.SUSPEND, [], np.zeros(5)), GateState.FROZEN, self.sample, 3)
        self.assertIsNotNone(alert)

        actions = self.engine.get_rollback_actions()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act.action_type, RiskLevel.SUSPEND)
        self.assertTrue(act.dry_run)
        self.assertFalse(act.executed)  # because dry_run=True

        # Rollback test
        ok = self.engine.rollback(act.action_id)
        self.assertTrue(ok)
        self.assertTrue(act.rolled_back)

    def test_non_dry_run_throttle_callback(self):
        throttled_pids = []

        def mock_throttle(pid, tier):
            throttled_pids.append((pid, tier))

        engine = ResponseEngine(
            Config(dry_run=False),
            on_throttle=mock_throttle,
        )
        engine._config.dry_run = False

        score_throttle = ScoreResult(
            d_squared=20.0,
            risk=0.9996,
            level=RiskLevel.THROTTLE,
            contributions=[],
            feature_vector=np.zeros(5),
        )

        for t in range(1, 4):
            engine.evaluate(self.key, score_throttle, GateState.NORMAL, self.sample, tick=t)

        self.assertEqual(len(throttled_pids), 1)
        self.assertEqual(throttled_pids[0], (999, Tier.DETAILED))


if __name__ == "__main__":
    unittest.main()
