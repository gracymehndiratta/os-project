"""evaluate.py — CLI for the evaluation harness (design §9 / §10).

Usage:
    python evaluate.py                 # full experiment matrix
    python evaluate.py --quick         # shorter runs
    python evaluate.py --scenarios slow_ramp evading_ramp
    python evaluate.py --variants full plain_adaptive
"""
from __future__ import annotations

from agent.evaluation import main

if __name__ == "__main__":
    main()
