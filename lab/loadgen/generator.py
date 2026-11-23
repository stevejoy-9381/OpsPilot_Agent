from __future__ import annotations

import bisect
import hashlib
import math
import random

from lab.loadgen.models import (
    BurstProfile,
    LoadProfile,
    RampProfile,
    ScheduledRequest,
    SteadyProfile,
)


class ZipfSampler:
    """Deterministic finite Zipf distribution sampler over N items.
    
    Probability mass function:
        P(k) = (1 / k^s) / sum_{j=1..N}(1 / j^s)
    """

    def __init__(self, n: int = 1000, s: float = 1.2) -> None:
        self.n = n
        self.s = s
        weights = [1.0 / (k**s) for k in range(1, n + 1)]
        total = sum(weights)
        self.cdf: list[float] = []
        running = 0.0
        for w in weights:
            running += w / total
            self.cdf.append(running)
        self.cdf[-1] = 1.0

    def sample(self, rng: random.Random) -> int:
        """Draws an item k in [1, N] according to Zipf distribution using rng."""
        u = rng.random()
        idx = bisect.bisect_left(self.cdf, u)
        return idx + 1


def sample_qty(rng: random.Random) -> int:
    """Draws order quantity deterministically from a realistic retail distribution.
    
    - 70% single unit (qty = 1)
    - 15% pairs (qty = 2)
    - 10% small bundle (qty = 3..5)
    - 5% bulk order (qty = 15..30) to exercise the 409 stock conflict path
    """
    roll = rng.random()
    if roll < 0.70:
        return 1
    elif roll < 0.85:
        return 2
    elif roll < 0.95:
        return rng.randint(3, 5)
    else:
        return rng.randint(15, 30)


def generate_schedule(
    profile: LoadProfile,
    duration_s: float,
    seed: int | None = None,
    catalog_size: int = 1000,
    zipf_s: float = 1.2,
) -> list[ScheduledRequest]:
    """Generates an open-loop deterministic request schedule for a profile.
    
    Same profile + seed => strictly identical request sequence (offsets, product_id, qty).
    """
    effective_seed = profile.seed if seed is None else seed
    rng = random.Random(effective_seed)
    zipf_sampler = ZipfSampler(n=catalog_size, s=zipf_s)

    offsets: list[float] = []

    if isinstance(profile, SteadyProfile):
        rps = profile.rps
        step = 1.0 / rps
        total_reqs = max(1, int(round(duration_s * rps)))
        offsets = [round(i * step, 6) for i in range(total_reqs)]

    elif isinstance(profile, BurstProfile):
        baseline_step = 1.0 / profile.baseline_rps
        spike_step = 1.0 / (profile.baseline_rps * profile.spike_multiplier)
        spike_start = profile.spike_start_s
        spike_end = profile.spike_start_s + profile.spike_duration_s

        t = 0.0
        while t < duration_s - 1e-7:
            offsets.append(round(t, 6))
            if spike_start <= t < spike_end:
                t += spike_step
            else:
                t += baseline_step
            if spike_start <= t < spike_end:
                t += spike_step
            else:
                t += baseline_step

    elif isinstance(profile, RampProfile):
        start_rps = profile.start_rps
        end_rps = profile.end_rps
        avg_rps = (start_rps + end_rps) / 2.0
        total_expected = max(1, int(math.floor(avg_rps * duration_s)))

        delta_r = end_rps - start_rps
        if abs(delta_r) < 1e-6:
            step = 1.0 / start_rps
            t = 0.0
            while t < duration_s:
                offsets.append(round(t, 6))
                t += step
        else:
            # Solve N(t) = start_rps * t + (delta_r / (2 * D)) * t^2 = i
            a = delta_r / (2.0 * duration_s)
            b = start_rps
            for i in range(total_expected):
                discriminant = b * b + 4.0 * a * i
                if discriminant >= 0:
                    t = (-b + math.sqrt(discriminant)) / (2.0 * a)
                    if t < duration_s:
                        offsets.append(round(t, 6))
    else:
        raise ValueError(f"Unsupported profile type: {type(profile)}")

    # Deterministic product and quantity generation
    requests: list[ScheduledRequest] = []
    for off in offsets:
        prod_id = zipf_sampler.sample(rng)
        qty = sample_qty(rng)
        requests.append(ScheduledRequest(offset_s=off, product_id=prod_id, qty=qty))

    return requests


def hash_request_sequence(requests: list[ScheduledRequest]) -> str:
    """Calculates SHA-256 hash of the canonical request sequence."""
    hasher = hashlib.sha256()
    for req in requests:
        record = f"{req.offset_s:.6f}:{req.product_id}:{req.qty}\n"
        hasher.update(record.encode("utf-8"))
    return hasher.hexdigest()
