"""Capture failure and shutdown checks without opening any physical camera.

The worker uses a fake cv2 module; controller tests use a deterministic process
context and virtual clock. Real driver, pipe and Windows timing remain hardware
validation work rather than claims made by these tests.
"""

from dataclasses import asdict
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from megumiu.capture import _camera_worker, capture
from megumiu.config import CameraConfig, CaptureConfig
from megumiu.metrics import FrameSample


class FakeFrame:
    shape = (240, 320, 3)

    def __getitem__(self, key):
        return self


class WorkerFrames(queue.Queue):
    def __init__(self, stop, maxsize=0):
        super().__init__(maxsize=maxsize)
        self.stop = stop
        self.lifecycle = []

    def put_nowait(self, item):
        try:
            super().put_nowait(item)
        finally:
            self.stop.set()

    def close(self):
        self.lifecycle.append("close")

    def join_thread(self):
        self.lifecycle.append("join_thread")


class FakeCamera:
    def __init__(self, opened=True, fail_read=None, accepts_settings=True):
        self.opened = opened
        self.fail_read = fail_read
        self.accepts_settings = accepts_settings
        self.reads = 0
        self.released = False
        self.settings = []

    def isOpened(self):
        return self.opened

    def set(self, property_id, value):
        self.settings.append((property_id, value))
        return self.accepts_settings

    def get(self, property_id):
        return 30.0

    def getBackendName(self):
        return "FAKE"

    def read(self):
        self.reads += 1
        return (False, None) if self.reads == self.fail_read else (True, FakeFrame())

    def release(self):
        self.released = True


def fake_cv2(camera):
    constants = {name: number for number, name in enumerate((
        "CAP_ANY", "CAP_DSHOW", "CAP_MSMF", "CAP_V4L2", "CAP_PROP_FOURCC",
        "CAP_PROP_FRAME_WIDTH", "CAP_PROP_FRAME_HEIGHT", "CAP_PROP_FPS",
        "CAP_PROP_BUFFERSIZE", "CAP_PROP_AUTO_EXPOSURE", "CAP_PROP_EXPOSURE",
        "COLOR_BGR2GRAY",
    ))}
    return SimpleNamespace(**constants, VideoCapture=lambda *args: camera,
                           VideoWriter_fourcc=lambda *args: 1234,
                           cvtColor=lambda image, mode: image,
                           mean=lambda image: (100.0, 0.0, 0.0, 0.0))


class CameraWorkerTests(unittest.TestCase):
    def run_worker(self, camera, config=None, full_queue=False):
        start, stop = threading.Event(), threading.Event()
        start.set()
        frames = WorkerFrames(stop, maxsize=1 if full_queue else 0)
        if full_queue:
            frames.put({"occupied": True})
        control = queue.Queue()
        with patch.dict(sys.modules, {"cv2": fake_cv2(camera)}):
            _camera_worker(config or CameraConfig("cam", 0), frames, control,
                           start, stop, None, 0)
        events = []
        while not control.empty():
            events.append(control.get_nowait())
        self.assertTrue(camera.released)
        self.assertEqual(frames.lifecycle, ["close", "join_thread"])
        self.assertEqual(events[-1]["kind"], "done")
        return frames, events

    def test_camera_open_failure_is_reported_and_released(self):
        _, events = self.run_worker(FakeCamera(opened=False))
        self.assertEqual([event["kind"] for event in events], ["error", "done"])
        self.assertIn("could not be opened", events[0]["message"])
        self.assertEqual(events[-1]["captured_frames"], 0)

    def test_warmup_and_acquisition_read_failures_are_reported(self):
        for failed_read, expected in [(1, "warmup frame"), (2, "stopped returning frames")]:
            with self.subTest(failed_read=failed_read):
                _, events = self.run_worker(FakeCamera(fail_read=failed_read))
                errors = [event for event in events if event["kind"] == "error"]
                self.assertEqual(len(errors), 1)
                self.assertIn(expected, errors[0]["message"])
                self.assertEqual(events[-1]["captured_frames"], 0)

    def test_declined_properties_are_recorded_without_claiming_requested_values(self):
        frames, events = self.run_worker(FakeCamera(accepts_settings=False))
        ready = next(event for event in events if event["kind"] == "ready")
        fps = ready["settings"]["fps"]
        self.assertFalse(fps["set_returned_true"])
        self.assertEqual(fps["requested"], 60)
        self.assertEqual(fps["reported"], 30)
        self.assertFalse(any(event["kind"] == "error" for event in events))
        sample = frames.get_nowait()
        self.assertEqual((sample["width"], sample["height"]), (320, 240))
        self.assertEqual(sample["brightness"], 100)
        self.assertEqual(events[-1]["captured_frames"], 1)

    def test_roi_is_checked_against_actual_frame_dimensions(self):
        config = CameraConfig("cam", 0, brightness_roi=(300, 0, 30, 20))
        _, events = self.run_worker(FakeCamera(), config)
        errors = [event for event in events if event["kind"] == "error"]
        self.assertIn("outside actual camera image", errors[0]["message"])
        self.assertEqual(events[-1]["captured_frames"], 0)

    def test_full_ipc_queue_counts_a_drop_and_flushes_on_exit(self):
        frames, events = self.run_worker(FakeCamera(), full_queue=True)
        self.assertEqual(events[-1]["captured_frames"], 1)
        self.assertEqual(events[-1]["ipc_dropped_frames"], 1)
        self.assertEqual(frames.qsize(), 1)
        self.assertFalse(any(event["kind"] == "error" for event in events))


class VirtualClock:
    def __init__(self):
        self.now = 100.0

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeEvent:
    def __init__(self, callback):
        self.callback = callback
        self.value = False

    def set(self):
        if not self.value:
            self.value = True
            self.callback()

    def is_set(self):
        return self.value


class ControllerQueue(queue.Queue):
    def __init__(self, owner, is_frames, maxsize=0):
        super().__init__(maxsize=maxsize)
        self.owner = owner
        self.is_frames = is_frames
        self.closed = False

    def get(self, block=True, timeout=None):
        if self.owner.terminated:
            raise AssertionError("must not read IPC after terminating a producer")
        if self.is_frames and self.owner.scenario == "interrupt" and not self.owner.interrupted:
            self.owner.interrupted = True
            raise KeyboardInterrupt
        try:
            return super().get(block=False)
        except queue.Empty:
            if self.is_frames and timeout:
                self.owner.clock.sleep(timeout)
                self.owner.finish_if_drained()
            raise

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, owner, args):
        self.owner = owner
        self.camera = args[0]
        self.exitcode = None
        self.alive = False
        self.join_calls = []

    def start(self):
        self.alive = True
        if self.owner.scenario != "startup_hang":
            self.owner.control.put({"kind": "ready", "camera_id": self.camera.id,
                                    "backend": "FAKE", "settings": {}})

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self.owner.operations.append(("join", self.owner.frames.qsize()))

    def terminate(self):
        self.alive = False
        self.exitcode = -1
        self.owner.terminated = True
        self.owner.operations.append(("terminate", self.camera.id))


class FakeProcessContext:
    def __init__(self, scenario, clock):
        self.scenario = scenario
        self.clock = clock
        self.frames = self.control = None
        self.events = []
        self.processes = []
        self.operations = []
        self.terminated = self.interrupted = False

    def Queue(self, maxsize=0):
        is_frames = self.frames is None
        result = ControllerQueue(self, is_frames, maxsize)
        if is_frames:
            self.frames = result
        else:
            self.control = result
        return result

    def Event(self):
        result = FakeEvent(self.on_start if not self.events else self.on_stop)
        self.events.append(result)
        return result

    def Process(self, *, target, args, name, daemon):
        result = FakeProcess(self, args)
        self.processes.append(result)
        return result

    def add_frame(self, camera, sequence):
        timestamp = 100_000_000_000 + sequence * 10_000_000
        self.frames.put(asdict(FrameSample(camera, sequence, timestamp - 1_000_000,
                                          timestamp, 320, 240)))

    def on_start(self):
        if self.scenario == "normal" and not self.events[1].is_set():
            for process in self.processes:
                self.add_frame(process.camera.id, 0)

    def on_stop(self):
        if self.scenario == "normal":
            for sequence in (1, 2):
                for process in self.processes:
                    self.add_frame(process.camera.id, sequence)

    def finish_if_drained(self):
        if self.events[1].is_set() and self.scenario in ("normal", "interrupt") and self.frames.empty():
            for process in self.processes:
                process.alive = False
                process.exitcode = 0


class CaptureControllerTests(unittest.TestCase):
    def run_capture(self, scenario, duration=0.1, timeout=0.2):
        clock = VirtualClock()
        context = FakeProcessContext(scenario, clock)
        config = CaptureConfig((CameraConfig("left", 0), CameraConfig("right", 1)))
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / "capture")
            with patch("megumiu.capture.mp.get_context", return_value=context) as factory, \
                 patch("megumiu.capture.time.perf_counter", side_effect=clock.perf_counter), \
                 patch("megumiu.capture.time.sleep", side_effect=clock.sleep):
                report = capture(config, output, duration, timeout, warmup=0)
            self.assertTrue((Path(output) / "report.json").exists())
            factory.assert_called_once_with("spawn")
        self.assertTrue(context.frames.closed)
        self.assertTrue(context.control.closed)
        self.assertTrue(all(not process.is_alive() for process in context.processes))
        return report, context

    def test_startup_timeout_terminates_workers_and_writes_error_report(self):
        report, context = self.run_capture("startup_hang", timeout=0.05)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("startup timeout" in message for message in report["errors"]))
        self.assertEqual(report["elapsed_seconds"], 0)
        self.assertTrue(context.terminated)

    def test_camera_stall_is_detected_and_blocked_workers_are_terminated(self):
        report, context = self.run_capture("capture_hang", duration=1, timeout=0.05)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("no frames received" in message for message in report["errors"]))
        self.assertTrue(any("terminated" in message for message in report["errors"]))
        self.assertTrue(context.terminated)

    def test_keyboard_interrupt_still_closes_workers_and_writes_report(self):
        report, context = self.run_capture("interrupt")
        self.assertEqual(report["status"], "error")
        self.assertTrue(report["details"]["interrupted"])
        self.assertIn("Capture interrupted by user", report["errors"])
        self.assertFalse(context.terminated)

    def test_normal_shutdown_drains_tail_frames_before_joining(self):
        report, context = self.run_capture("normal")
        self.assertEqual(report["status"], "ok")
        self.assertFalse(context.terminated)
        self.assertEqual(report["paired_frame_sets"], 3)
        for camera in ("left", "right"):
            self.assertEqual(report["cameras"][camera]["received_frames"], 3)
        self.assertTrue(context.operations)
        self.assertTrue(all(operation == ("join", 0) for operation in context.operations))


if __name__ == "__main__":
    unittest.main()
