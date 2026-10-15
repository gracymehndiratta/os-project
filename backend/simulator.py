"""simulator.py — Standalone simulator / demo runner.

Usage:
    python simulator.py --scenario demo --ticks 600
    python simulator.py --scenario slow_ramp --variant full --ticks 3000
    python simulator.py --live --ticks 60          # real psutil collection

Scenarios: demo, benign, slow_ramp, evading_ramp, legit_step, overhead.
Variants:  static, plain_adaptive, gate_only, budget_only, full.
"""
from __future__ import annotations

import argparse
import logging
import time

from agent.agent import MonitorAgent
from agent.config import Config
from agent.evaluation import VARIANTS
from agent.scenarios import get_scenario

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("simulator")


def run(scenario_name: str, variant: str, ticks: int, live: bool, tick_interval: float) -> None:
    opts = VARIANTS.get(variant, VARIANTS["full"])
    scenario = None if live else get_scenario(scenario_name)

    agent = MonitorAgent(
        config=Config(),
        scenario=scenario,
        simulation=not live,
        model_overhead=not live,
        tick_interval=tick_interval,
        **opts,
    )

    logger.info(
        "Starting simulation scenario=%s variant=%s ticks=%d live=%s",
        scenario_name, variant, ticks, live,
    )
    agent.start()

    try:
        while agent.running and agent.tick < ticks:
            if agent.tick % 50 == 0:
                s = agent.status()
                logger.info(
                    "tick=%d tier=%s baselines=%d frozen=%d alerts=%d quarantined=%d budget=%.1f viol=%d reduced=%d",
                    s["tick"], s["current_tier"], s["active_baselines"],
                    s["frozen_baselines"], s["alerts_raised"], s["quarantined_samples"],
                    s["budget"], s["budget_violations"], s["reduced_coverage_ticks"],
                )
            time.sleep(tick_interval)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
        s = agent.status()
        logger.info(
            "Complete: %d ticks, %d alerts, %d frozen, %d quarantined, %d budget violations, %d reduced-coverage ticks",
            s["tick"], s["alerts_raised"], s["frozen_baselines"],
            s["quarantined_samples"], s["budget_violations"], s["reduced_coverage_ticks"],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="OS Behavior Monitor simulator")
    parser.add_argument("--scenario", default="demo",
                        choices=["demo", "benign", "slow_ramp", "evading_ramp", "legit_step", "overhead"])
    parser.add_argument("--variant", default="full", choices=list(VARIANTS))
    parser.add_argument("--ticks", type=int, default=600)
    parser.add_argument("--live", action="store_true", help="use real psutil collection")
    parser.add_argument("--interval", type=float, default=0.2, help="seconds per tick")
    args = parser.parse_args()

    run(args.scenario, args.variant, args.ticks, args.live, args.interval)


if __name__ == "__main__":
    main()
