import json
from pathlib import Path
import tempfile
import unittest

from megumiu.cli import main
from megumiu.config import CameraConfig, CaptureConfig, load_config
from megumiu.optical import flash_report
from megumiu.simulation import replay, simulate


class SessionTests(unittest.TestCase):
    def test_single_frame_replay_preserves_original_elapsed_time(self):
        with tempfile.TemporaryDirectory() as folder:
            original = str(Path(folder) / "single")
            output = str(Path(folder) / "replayed")
            report = simulate(original, duration=0.1, fps=1, offsets_ms=(0,), jitter_ms=0)
            replayed = replay(original, output)
            self.assertEqual(report["paired_frame_sets"], 1)
            self.assertEqual(replayed["elapsed_seconds"], report["elapsed_seconds"])
            self.assertEqual(replayed["paired_fps"], report["paired_fps"])
            self.assertIsNone(replayed["cameras"]["cam0"]["observed_fps"])
            self.assertEqual(replayed["details"]["elapsed_basis"], "original_session")

    def test_simulate_replay_reproduces_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            first = str(Path(folder) / "original")
            second = str(Path(folder) / "replay")
            report = simulate(first, duration=2, jitter_ms=0, drop_every=7)
            replayed = replay(first, second)
            self.assertEqual(report["paired_frame_sets"], replayed["paired_frame_sets"])
            self.assertEqual(report["cameras"], replayed["cameras"])
            self.assertEqual(report["host_arrival_skew_ms"], replayed["host_arrival_skew_ms"])
            self.assertFalse(report["exposure_sync_verified"])
            self.assertIsNone(report["end_to_end_latency_ms"])
            self.assertEqual(report["mode"], "simulation")
            with self.assertRaises(FileExistsError):
                simulate(first)

    def test_flash_detects_known_arrival_offsets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "run")
            simulate(path, duration=3, jitter_ms=0, offsets_ms=(0, 2, 5))
            report = flash_report(path)
            self.assertEqual(report["comparisons"]["cam1"]["arrival_delta_p50_ms"], 2)
            self.assertEqual(report["comparisons"]["cam2"]["arrival_delta_p50_ms"], 5)
            self.assertFalse(report["exposure_sync_verified"])

    def test_invalid_simulation_never_creates_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "invalid")
            for kwargs in ({"duration": 0}, {"fps": float("nan")}, {"jitter_ms": 50}, {"offsets_ms": ()}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    simulate(path, **kwargs)
                self.assertFalse(Path(path).exists())

    def test_missing_and_corrupt_trace_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run"
            simulate(str(path), duration=0.1)
            with (path / "frames.jsonl").open("a") as stream:
                stream.write("not-json\n")
            with self.assertRaisesRegex(ValueError, "Invalid frame"):
                replay(str(path), str(Path(folder) / "replayed"))

    def test_config_rejects_duplicate_device_and_unknown_fields(self):
        with self.assertRaises(ValueError):
            CaptureConfig((CameraConfig("a", 0), CameraConfig("b", 0)))
        with self.assertRaises(ValueError):
            CameraConfig("../bad", 0)
        with self.assertRaises(ValueError):
            CameraConfig("a", 0, fps=float("inf"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"schema_version": 1, "cameras": [], "fps_typo": 60}))
            with self.assertRaisesRegex(ValueError, "unknown"):
                load_config(path)

    def test_cli_bad_arguments_return_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main(["simulate", "--duration", "-1", "--output", str(Path(folder) / "bad")]), 2)


if __name__ == "__main__":
    unittest.main()
