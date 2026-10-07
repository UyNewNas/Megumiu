"""Small CLI for the first-stage acquisition experiment."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
from datetime import datetime
from multiprocessing import freeze_support

from . import __version__


def _output_default() -> str:
    return "captures/" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _print_report(report: dict, output: str) -> None:
    print(f"Session: {output}")
    print(f"Mode: {report['mode']} | Status: {report['status']}")
    for camera, stats in report["cameras"].items():
        fps = stats["observed_fps"]
        rate = f"{fps:.2f}" if fps is not None else "unmeasured"
        print(f"  {camera}: {stats['received_frames']} frames, {rate} observed FPS")
    skew = report["host_arrival_skew_ms"]["p95"]
    print(f"Paired sets: {report['paired_frame_sets']} | Host arrival skew P95: {skew} ms")
    print("Exposure synchronization: NOT VERIFIED | End-to-end latency: NOT MEASURED")
    for error in report["errors"]:
        print(f"ERROR: {error}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="megumiu", description="Megumiu phase 1: multi-camera acquisition diagnostics")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="report installed runtime without opening a camera")
    simulation = sub.add_parser("simulate", help="generate a deterministic virtual-time session; no camera needed")
    simulation.add_argument("--duration", type=float, default=10)
    simulation.add_argument("--fps", type=float, default=60)
    simulation.add_argument("--offset-ms", default="0,2,5", help="comma-separated arrival offsets, one per virtual camera")
    simulation.add_argument("--jitter-ms", type=float, default=0.5)
    simulation.add_argument("--drop-every", type=int, default=0, help="omit every Nth frame from the last virtual camera")
    simulation.add_argument("--max-skew-ms", type=float, default=12)
    simulation.add_argument("--seed", type=int, default=7)
    simulation.add_argument("--output", default=None)
    live = sub.add_parser("capture", help="capture the configured cameras; defaults to metadata only")
    live.add_argument("--config", required=True)
    live.add_argument("--duration", type=float, default=10, help="capture duration in seconds, max 3600")
    live.add_argument("--timeout", type=float, default=5, help="startup / camera stall timeout in seconds")
    live.add_argument("--warmup", type=float, default=1)
    live.add_argument("--record", action="store_true", help="also save local MJPEG AVI; recording changes timing")
    live.add_argument("--output", default=None)
    replay = sub.add_parser("replay", help="recompute metrics from a saved session; no camera needed")
    replay.add_argument("--input", required=True)
    replay.add_argument("--output", default=None)
    replay.add_argument("--max-skew-ms", type=float)
    optical = sub.add_parser("flash-report", help="compare common-flash arrivals, NOT exposure synchronization")
    optical.add_argument("--input", required=True)
    optical.add_argument("--threshold", type=float, default=128)
    optical.add_argument("--match-window-ms", type=float, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    freeze_support()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            dependencies = {}
            for name in ("numpy", "opencv-python"):
                try:
                    dependencies[name] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    dependencies[name] = None
            print(json.dumps({"megumiu": __version__, "python": platform.python_version(),
                              "os": platform.system(), "architecture": platform.machine(),
                              "dependencies": dependencies, "camera_opened": False,
                              "hardware_validated": False}, indent=2))
            return 0
        if args.command == "flash-report":
            from .optical import flash_report
            print(json.dumps(flash_report(args.input, args.threshold, args.match_window_ms), indent=2))
            return 0
        output = args.output or _output_default()
        if args.command == "simulate":
            from .simulation import simulate
            offsets = tuple(float(v.strip()) for v in args.offset_ms.split(","))
            report = simulate(output, args.duration, args.fps, offsets, args.jitter_ms,
                              args.drop_every, args.max_skew_ms, args.seed)
        elif args.command == "replay":
            from .simulation import replay
            report = replay(args.input, output, args.max_skew_ms)
        else:
            from .config import load_config
            from .capture import capture
            report = capture(load_config(args.config), output, args.duration, args.timeout, args.warmup, args.record)
        _print_report(report, output)
        return 0 if report["status"] == "ok" else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
