"""Deterministic randomness.

All randomness flows from a single master seed so that runs are reproducible:
repository ``i`` always receives the same derived RNG for a given seed,
independent of how many random draws earlier repositories made.
"""

from __future__ import annotations

import hashlib
import random


def derive_seed(master_seed: int, *labels: object) -> int:
    material = ":".join([str(master_seed), *(str(label) for label in labels)])
    digest = hashlib.sha256(material.encode()).digest()
    return int.from_bytes(digest[:8], "big")


def derive_rng(master_seed: int, *labels: object) -> random.Random:
    return random.Random(derive_seed(master_seed, *labels))
