"""Standalone simulator – runs the monitoring agent with simulated telemetry.

Usage:
    python simulator.py --mode normal --duration 60
    python simulator.py --mode attack --duration 120
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from agent.monitor import MonitoringAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("simulator")

_running = True


def _handle_signal(signum: int, frame: object) -> None:
    global _running
    _running = False


def run_simulation(mode: str, duration_sec: float) -> None:
    """Run the monitoring agent with simulated telemetry."""
    agent = MonitoringAgent(
        simulation_mode=True,
        risk_threshold=4.0,
        collection_interval=1.0,
    )
    agent.start()

    start_time = time.time()
    cycle_count = 0

    logger.info("Starting %s simulation for %.0fs", mode, duration_sec)
    logger.info("The simulator includes attack processes (boiling_frog, spike, memory_leak, child_bomb)")

    try:
        while _running and (time.time() - start_time) < duration_sec:
            status = agent.get_status()
            cycle_count += 1

            if cycle_count % 10 == 0:
                elapsed = time.time() - start_time
                logger.info(
                    "t=%.0fs cycle=%d tier=%s baselines=%d frozen=%d alerts=%d quarantined=%d",
                    elapsed, cycle_count, status.current_tier.name,
                    status.active_baselines, status.frozen_baselines,
                    status.alerts_raised, status.quarantined_samples,
                )

            time.sleep(1.0)

    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
        status = agent.get_status()
        logger.info(
            "Simulation complete: %d cycles, %d alerts raised, "
            "%d frozen baselines, %d quarantined samples, %d budget violations",
            cycle_count, status.alerts_raised,
            status.frozen_baselines, status.quarantined_samples,
            status.budget_violations,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="OS Monitoring Agent Simulator")
    parser.add_argument("--mode", choices=["normal", "attack"],
                        default="normal", help="Simulation mode")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="Duration in seconds")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run_simulation(args.mode, args.duration)


if __name__ == "__main__":
    main()
