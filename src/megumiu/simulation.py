"""Deterministic virtual-time input. Its output never proves hardware performance."""

from __future__ import annotations

import math
import random

from .config import positive_number
from .metrics import FrameSample
from .session import Session, load_session


def simulate(output: str, duration: float = 10, fps: float = 60,
             offsets_ms: tuple[float, ...] = (0, 2, 5), jitter_ms: float = 0.5,
             drop_every: int = 0, max_skew_ms: float = 12, seed: int = 7) -> dict:
    positive_number(duration, "duration", 300)
    positive_number(fps, "fps", 240)
    if not 1 <= len(offsets_ms) <= 8 or any(not math.isfinite(v) or abs(v) > 1000 for v in offsets_ms):
        raise ValueError("provide 1 to 8 finite offsets within +/-1000 ms")
    if not math.isfinite(jitter_ms) or jitter_ms < 0 or jitter_ms >= 500 / fps:
        raise ValueError("jitter_ms must be nonnegative and less than half a frame interval")
    if type(drop_every) is not int or drop_every < 0:
        raise ValueError("drop_every must be a nonnegative integer")
    ids = [f"cam{i}" for i in range(len(offsets_ms))]
    randomizer = random.Random(seed)
    frames = []
    count = max(1, math.floor(duration * fps))
    for index, offset in enumerate(offsets_ms):
        for sequence in range(count):
            if drop_every and index == len(ids) - 1 and (sequence + 1) % drop_every == 0:
                continue
            received = 2_000_000_000 + round((sequence / fps + offset / 1000 + randomizer.uniform(-jitter_ms, jitter_ms) / 1000) * 1e9)
            frames.append(FrameSample(ids[index], sequence, received - 1_000_000,
                                      received, 1280, 720, brightness=220.0 if sequence % max(2, round(fps)) < max(1, round(fps / 5)) else 20.0))
    frames.sort(key=lambda f: (f.host_received_ns, f.camera_id))
    session = Session(output, ids, "simulation", max_skew_ms, details={
        "virtual_time": True, "fps": fps, "offsets_ms": offsets_ms,
        "jitter_ms": jitter_ms, "drop_every_last_camera": drop_every, "seed": seed,
    })
    for frame in frames:
        session.add(frame)
    return session.finish(duration, extra={"hardware_test": False})


def replay(input_path: str, output: str, max_skew_ms: float | None = None) -> dict:
    metadata, frames = load_session(input_path)
    ids = metadata["camera_ids"]
    session = Session(output, ids, "replay", max_skew_ms if max_skew_ms is not None else metadata["max_arrival_skew_ms"],
                      metadata["max_queue"], {"original_mode": metadata["mode"], "virtual_time": True})
    for frame in frames:
        session.add(frame)
    span = (max(f.host_received_ns for f in frames) - min(f.host_received_ns for f in frames)) / 1e9 if len(frames) > 1 else 0
    elapsed = metadata.get("elapsed_seconds", span)
    return session.finish(elapsed, extra={"hardware_test": False,
        "elapsed_basis": "original_session" if "elapsed_seconds" in metadata else "recorded_first_to_last_arrival"})
