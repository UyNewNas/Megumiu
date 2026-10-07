"""Coarse common-flash arrival comparison, with explicit sampling limitations."""

from __future__ import annotations

import math

from .metrics import percentile
from .session import load_session


def flash_report(input_path: str, threshold: float = 128, match_window_ms: float = 100) -> dict:
    if not math.isfinite(threshold) or not 0 < threshold < 255:
        raise ValueError("threshold must be between 0 and 255")
    if not math.isfinite(match_window_ms) or not 0 < match_window_ms <= 1000:
        raise ValueError("match_window_ms must be in (0, 1000]")
    metadata, samples = load_session(input_path)
    ids = metadata["camera_ids"]
    edges: dict[str, list[int]] = {c: [] for c in ids}
    previous: dict[str, float] = {}
    last_sequence: dict[str, int] = {}
    for sample in samples:
        if sample.camera_id not in edges:
            raise ValueError("frame camera is absent from session metadata")
        value = sample.brightness
        camera = sample.camera_id
        # A discontinuity cannot identify which intervening frame crossed the threshold.
        continuous = sample.sequence == last_sequence.get(camera, -2) + 1
        if value is not None and continuous and camera in previous and previous[camera] < threshold <= value:
            edges[camera].append(sample.host_received_ns)
        last_sequence[camera] = sample.sequence
        if value is None:
            previous.pop(camera, None)
        else:
            previous[camera] = value
    reference = ids[0]
    comparisons = {}
    for camera in ids[1:]:
        available = list(edges[camera])
        deltas = []
        for instant in edges[reference]:
            candidates = [(abs(t - instant), i, t) for i, t in enumerate(available)
                          if abs(t - instant) <= match_window_ms * 1e6]
            if candidates:
                _, i, matched = min(candidates)
                available.pop(i)
                deltas.append((matched - instant) / 1e6)
        comparisons[camera] = {
            "matched_events": len(deltas), "unmatched_reference_events": len(edges[reference]) - len(deltas),
            "unmatched_camera_events": len(available), "arrival_delta_ms": deltas,
            "arrival_delta_p50_ms": percentile(deltas, 50),
            "arrival_delta_p95_ms": percentile(deltas, 95),
        }
    return {
        "schema_version": 1, "mode": metadata["mode"], "reference_camera": reference,
        "threshold": threshold, "event_counts": {c: len(v) for c, v in edges.items()},
        "comparisons": comparisons, "measurement_scope": "common_flash_host_arrival_comparison",
        "exposure_sync_verified": False, "end_to_end_latency_ms": None,
        "notes": [
            "Use the SAME isolated physical light pulse in every ROI, with fixed exposure/gain.",
            "First bright frame arrival includes exposure phase, sampling, readout and transfer.",
            "No hardware trigger time is available: these deltas are NOT absolute camera latency.",
            "Resolution is limited by frame intervals; no sub-frame accuracy is claimed.",
            "Keep pulses farther apart than twice match_window_ms; visually verify event matches.",
            "A recording or simulation does not establish live-camera performance.",
        ],
    }
