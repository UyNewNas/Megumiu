"""Explicit capture settings; camera drivers may decline any requested property."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


def positive_number(value: object, name: str, maximum: float = 10000) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be finite and in (0, {maximum}]")
    return float(value)


@dataclass(frozen=True)
class CameraConfig:
    id: str
    index: int
    backend: str = "auto"
    width: int = 1280
    height: int = 720
    fps: float = 60
    fourcc: str = "MJPG"
    buffer_size: int = 1
    exposure: float | None = None
    auto_exposure: float | None = None
    brightness_roi: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or len(self.id) > 64:
            raise ValueError("camera id must contain 1 to 64 characters")
        if not all(c.isascii() and (c.isalnum() or c in "_-") for c in self.id):
            raise ValueError("camera id must use ASCII letters, digits, '_' or '-'")
        if type(self.index) is not int or not 0 <= self.index <= 64:
            raise ValueError("camera index must be an integer from 0 to 64")
        if self.backend not in ("auto", "dshow", "msmf", "v4l2"):
            raise ValueError("backend must be auto, dshow, msmf or v4l2")
        for name in ("width", "height", "buffer_size"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= (8192 if name != "buffer_size" else 64):
                raise ValueError(f"invalid camera {name}")
        positive_number(self.fps, "fps", 1000)
        if not isinstance(self.fourcc, str) or len(self.fourcc) != 4 or not self.fourcc.isascii():
            raise ValueError("fourcc must be four ASCII characters")
        for name in ("exposure", "auto_exposure"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError(f"{name} must be finite")
        roi = self.brightness_roi
        if roi is not None:
            if len(roi) != 4 or any(type(v) is not int for v in roi):
                raise ValueError("brightness_roi must be [x, y, width, height] in pixels")
            if roi[0] < 0 or roi[1] < 0 or roi[2] <= 0 or roi[3] <= 0:
                raise ValueError("brightness_roi has invalid dimensions")


@dataclass(frozen=True)
class CaptureConfig:
    cameras: tuple[CameraConfig, ...]
    max_arrival_skew_ms: float = 12
    max_queue: int = 3

    def __post_init__(self) -> None:
        if not 1 <= len(self.cameras) <= 8:
            raise ValueError("configure 1 to 8 cameras (the project baseline is 3)")
        if len({c.id for c in self.cameras}) != len(self.cameras):
            raise ValueError("camera ids must be unique")
        if len({c.index for c in self.cameras}) != len(self.cameras):
            raise ValueError("camera indices must be unique")
        positive_number(self.max_arrival_skew_ms, "max_arrival_skew_ms", 1000)
        if type(self.max_queue) is not int or not 1 <= self.max_queue <= 128:
            raise ValueError("max_queue must be an integer from 1 to 128")

    def to_dict(self) -> dict:
        return {"schema_version": 1, **asdict(self)}


def load_config(path: str | Path) -> CaptureConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("configuration requires schema_version 1")
    unknown = set(data) - {"schema_version", "cameras", "max_arrival_skew_ms", "max_queue"}
    if unknown:
        raise ValueError(f"unknown configuration fields: {sorted(unknown)}")
    if not isinstance(data.get("cameras"), list):
        raise ValueError("cameras must be a list")
    try:
        cameras = tuple(CameraConfig(**c) for c in data["cameras"])
        return CaptureConfig(cameras, data.get("max_arrival_skew_ms", 12), data.get("max_queue", 3))
    except TypeError as exc:
        raise ValueError(f"invalid camera configuration: {exc}") from exc
