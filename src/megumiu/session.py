"""Versioned, local-only measurement sessions. No images are saved by default."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .metrics import ArrivalSynchronizer, FrameSample, summarize


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


class Session:
    def __init__(self, output: str | Path, camera_ids: list[str], mode: str,
                 max_skew_ms: float = 12, max_queue: int = 3, details: dict | None = None):
        self.path = Path(output)
        self.sync = ArrivalSynchronizer(camera_ids, max_skew_ms, max_queue)
        self.path.mkdir(parents=True, exist_ok=False)
        self.camera_ids = camera_ids
        self.samples: list[FrameSample] = []
        self.frame_sets = []
        self.metadata = {
            "schema_version": 1, "megumiu_version": __version__, "mode": mode,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "camera_ids": camera_ids, "max_arrival_skew_ms": max_skew_ms,
            "max_queue": max_queue, "clock": "host_monotonic_perf_counter_ns",
            "timestamp_meaning": "Immediately after read returns; NOT exposure time",
            "details": details or {},
        }
        write_json(self.path / "session.json", self.metadata)
        self.trace = (self.path / "frames.jsonl").open("w", encoding="utf-8")

    def add(self, sample: FrameSample) -> None:
        paired = self.sync.push(sample)
        self.samples.append(sample)
        self.frame_sets.extend(paired)
        self.trace.write(json.dumps(asdict(sample), allow_nan=False) + "\n")

    def finish(self, elapsed: float, errors: list[str] | None = None,
               extra: dict | None = None) -> dict:
        self.trace.close()
        self.metadata["elapsed_seconds"] = elapsed
        write_json(self.path / "session.json", self.metadata)
        report = summarize(self.samples, self.frame_sets, self.camera_ids,
                           elapsed, self.sync.dropped_by_camera)
        missing = [c for c in self.camera_ids if not any(s.camera_id == c for s in self.samples)]
        problems = list(errors or [])
        if missing:
            problems.append("No frames from: " + ", ".join(missing))
        report.update({
            "schema_version": 1, "mode": self.metadata["mode"],
            "status": "error" if problems else "ok", "errors": problems,
            "elapsed_seconds": elapsed, "max_arrival_skew_ms": self.metadata["max_arrival_skew_ms"],
            "pending_by_camera": self.sync.pending_by_camera,
            "notes": [
                "Arrival matching is a scheduling diagnostic, not exposure synchronization.",
                "Read duration includes blocking and decoding; it is not camera latency.",
                "Host arrival matching discards frames outside the configured tolerance.",
                "Camera transport drops cannot be inferred without hardware frame counters.",
                "No SteamVR or motion-to-photon latency has been measured.",
            ],
            "details": extra or {},
        })
        write_json(self.path / "report.json", report)
        with (self.path / "pairs.jsonl").open("w", encoding="utf-8") as stream:
            for group in self.frame_sets:
                stream.write(json.dumps({"host_arrival_skew_ms": group.skew_ms,
                                         "frames": {s.camera_id: s.sequence for s in group.samples}}) + "\n")
        return report


def load_session(path: str | Path) -> tuple[dict, list[FrameSample]]:
    directory = Path(path)
    metadata = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 1:
        raise ValueError("unsupported session schema")
    samples = []
    with (directory / "frames.jsonl").open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    samples.append(FrameSample(**json.loads(line)))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid frame at line {number}: {exc}") from exc
    return metadata, samples
