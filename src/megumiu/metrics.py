"""Host-arrival measurements for the phase-one camera capture experiment.

These timestamps describe when a capture call returned on this computer. They do
not establish when the sensor exposed a frame. A small arrival skew therefore
does not demonstrate synchronized exposures or low end-to-end tracking latency.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Iterable


def _nonnegative_integer(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _finite_number(name: str, value: float, *, minimum: float | None = None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (minimum is not None and value < minimum)
    ):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise ValueError(f"{name} must be a finite number{suffix}")


def _validate_camera_ids(camera_ids: Iterable[str]) -> tuple[str, ...]:
    ids = tuple(camera_ids)
    if not ids or any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("camera_ids must contain nonempty strings")
    if len(set(ids)) != len(ids):
        raise ValueError("camera_ids must be unique")
    return ids


@dataclass(frozen=True, slots=True)
class FrameSample:
    """Metadata captured using a common monotonic host clock.

    ``read_started_ns`` precedes the capture read, and ``host_received_ns`` is
    recorded immediately after it returns. Their difference is a blocking read
    duration, not the age of the image. ``source_pts_ms`` is an optional file
    decode timestamp and is never used to synchronize live cameras.
    """

    camera_id: str
    sequence: int
    read_started_ns: int
    host_received_ns: int
    width: int
    height: int
    source_pts_ms: float | None = None
    brightness: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.camera_id, str) or not self.camera_id:
            raise ValueError("camera_id must be a nonempty string")
        for name in ("sequence", "read_started_ns", "host_received_ns", "width", "height"):
            _nonnegative_integer(name, getattr(self, name))
        if self.width == 0 or self.height == 0:
            raise ValueError("frame dimensions must be positive")
        if self.read_started_ns > self.host_received_ns:
            raise ValueError("read_started_ns must not follow host_received_ns")
        for name in ("source_pts_ms", "brightness"):
            value = getattr(self, name)
            if value is not None:
                _finite_number(name, value)


@dataclass(frozen=True, slots=True)
class FrameSet:
    """One unused frame from each participating camera, paired by arrival time."""

    samples: tuple[FrameSample, ...]

    def __post_init__(self) -> None:
        if not self.samples:
            raise ValueError("a frame set must contain at least one sample")
        if len({sample.camera_id for sample in self.samples}) != len(self.samples):
            raise ValueError("a frame set must contain distinct cameras")

    @property
    def skew_ms(self) -> float:
        arrivals = [sample.host_received_ns for sample in self.samples]
        return (max(arrivals) - min(arrivals)) / 1_000_000


class ArrivalSynchronizer:
    """Greedily pair bounded queues by host arrival, without reusing frames.

    This is an arrival aligner, not an exposure synchronizer. A queue overflow or
    a head frame too old to match the other heads counts as a dropped frame.
    Frames still waiting in queues are not counted as dropped.
    """

    def __init__(
        self,
        camera_ids: list[str],
        max_skew_ms: float = 12,
        max_queue: int = 3,
    ) -> None:
        self.camera_ids = _validate_camera_ids(camera_ids)
        _finite_number("max_skew_ms", max_skew_ms, minimum=0)
        _nonnegative_integer("max_queue", max_queue)
        if max_queue == 0:
            raise ValueError("max_queue must be positive")
        self.max_skew_ms = max_skew_ms
        self.max_queue = max_queue
        self._max_skew_ns = max_skew_ms * 1_000_000
        self._queues: dict[str, deque[FrameSample]] = {
            camera_id: deque() for camera_id in self.camera_ids
        }
        self._last_by_camera: dict[str, FrameSample] = {}
        self.dropped_by_camera = {camera_id: 0 for camera_id in self.camera_ids}

    @property
    def pending_by_camera(self) -> dict[str, int]:
        """Current queue sizes as a snapshot; callers cannot mutate the queues."""
        return {camera_id: len(queue) for camera_id, queue in self._queues.items()}

    def push(self, sample: FrameSample) -> list[FrameSet]:
        if sample.camera_id not in self._queues:
            raise ValueError(f"unknown camera: {sample.camera_id}")
        previous = self._last_by_camera.get(sample.camera_id)
        if previous is not None:
            if sample.sequence <= previous.sequence:
                raise ValueError("sequence must increase for each camera")
            if sample.host_received_ns < previous.host_received_ns:
                raise ValueError("host arrival time must not move backwards")
        self._last_by_camera[sample.camera_id] = sample

        queue = self._queues[sample.camera_id]
        queue.append(sample)
        if len(queue) > self.max_queue:
            queue.popleft()
            self.dropped_by_camera[sample.camera_id] += 1

        result = []
        while all(self._queues.values()):
            heads = [self._queues[camera_id][0] for camera_id in self.camera_ids]
            oldest = min(heads, key=lambda frame: frame.host_received_ns)
            newest = max(heads, key=lambda frame: frame.host_received_ns)
            if newest.host_received_ns - oldest.host_received_ns <= self._max_skew_ns:
                result.append(
                    FrameSet(tuple(self._queues[camera_id].popleft() for camera_id in self.camera_ids))
                )
            else:
                self._queues[oldest.camera_id].popleft()
                self.dropped_by_camera[oldest.camera_id] += 1
        return result


def percentile(values: Iterable[float], percent: float) -> float | None:
    """Return a percentile using linear interpolation at (n - 1) * p / 100.

    An empty sample returns ``None`` so reports do not suggest a measured zero.
    """
    _finite_number("percent", percent, minimum=0)
    if percent > 100:
        raise ValueError("percent must be <= 100")
    ordered = sorted(values)
    for value in ordered:
        _finite_number("sample", value)
    if not ordered:
        return None
    index = (len(ordered) - 1) * percent / 100
    lower = math.floor(index)
    upper = math.ceil(index)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _distribution(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "mean": math.fsum(values) / len(values) if values else None,
    }


def summarize(
    samples: list[FrameSample],
    frame_sets: list[FrameSet],
    camera_ids: list[str],
    elapsed_seconds: float,
    dropped_by_camera: dict[str, int] | None = None,
) -> dict:
    """Build a JSON-serializable report without claiming unmeasured latency.

    ``observed_fps`` measures the rate between each camera's first and last
    arrivals; at least two distinct arrival timestamps are required. Aggregate
    ``paired_fps`` counts pairs over the supplied full capture interval.
    """
    ids = _validate_camera_ids(camera_ids)
    _finite_number("elapsed_seconds", elapsed_seconds, minimum=0)
    grouped: dict[str, list[FrameSample]] = {camera_id: [] for camera_id in ids}
    sample_by_identity: dict[tuple[str, int], FrameSample] = {}
    for sample in samples:
        if sample.camera_id not in grouped:
            raise ValueError(f"unknown camera: {sample.camera_id}")
        identity = (sample.camera_id, sample.sequence)
        if identity in sample_by_identity:
            raise ValueError("report samples must have unique camera/sequence identities")
        sample_by_identity[identity] = sample
        grouped[sample.camera_id].append(sample)
    drops = {camera_id: 0 for camera_id in ids}
    for camera_id, count in (dropped_by_camera or {}).items():
        if camera_id not in drops:
            raise ValueError(f"unknown camera in dropped_by_camera: {camera_id}")
        _nonnegative_integer("dropped count", count)
        drops[camera_id] = count

    expected_ids = set(ids)
    paired_identities: set[tuple[str, int]] = set()
    for frame_set in frame_sets:
        if {sample.camera_id for sample in frame_set.samples} != expected_ids:
            raise ValueError("each frame set must contain exactly the configured cameras")
        for sample in frame_set.samples:
            identity = (sample.camera_id, sample.sequence)
            if sample_by_identity.get(identity) != sample:
                raise ValueError("paired frames must be present in the report samples")
            if identity in paired_identities:
                raise ValueError("a frame must not be reused across frame sets")
            paired_identities.add(identity)

    cameras = {}
    for camera_id, frames in grouped.items():
        frames = sorted(frames, key=lambda sample: sample.host_received_ns)
        intervals = [
            (right.host_received_ns - left.host_received_ns) / 1_000_000
            for left, right in zip(frames, frames[1:])
        ]
        span_ns = frames[-1].host_received_ns - frames[0].host_received_ns if frames else 0
        cameras[camera_id] = {
            "received_frames": len(frames),
            "observed_fps": (len(frames) - 1) * 1_000_000_000 / span_ns if span_ns else None,
            "interarrival_ms": _distribution(intervals),
            "read_duration_ms": _distribution(
                [(frame.host_received_ns - frame.read_started_ns) / 1_000_000 for frame in frames]
            ),
            "dropped_frames": drops[camera_id],
            "unpaired_frames": len(frames) - len(frame_sets),
        }

    return {
        "measurement_scope": "host_arrival_only",
        "exposure_sync_verified": False,
        "end_to_end_latency_ms": None,
        "elapsed_seconds": elapsed_seconds,
        "cameras": cameras,
        "paired_frame_sets": len(frame_sets),
        "paired_fps": len(frame_sets) / elapsed_seconds if elapsed_seconds else None,
        "host_arrival_skew_ms": _distribution([frame_set.skew_ms for frame_set in frame_sets]),
        "measurement_notes": [
            "Host arrival timestamps do not identify sensor exposure times.",
            "Read duration measures time spent in the capture call, not image age or tracking latency.",
            "Source PTS is file decode metadata and is not used for live camera alignment.",
            "Dropped frames count arrival-aligner discards, not camera or transport frame loss.",
            "Unpaired frames include discarded frames and frames still waiting for a match.",
        ],
    }
