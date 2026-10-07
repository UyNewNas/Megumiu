"""One bounded, terminable process per camera; no camera is opened at import time."""

from __future__ import annotations

import math
import multiprocessing as mp
import queue
import time
from dataclasses import asdict
from pathlib import Path

from .config import CameraConfig, CaptureConfig, positive_number
from .metrics import FrameSample
from .session import Session


def _camera_worker(config: CameraConfig, frames, control, start, stop,
                   output: str | None, warmup: float) -> None:
    cap = writer = None
    captured = discarded = 0
    try:
        import cv2

        backend = {"auto": cv2.CAP_ANY, "dshow": cv2.CAP_DSHOW,
                   "msmf": cv2.CAP_MSMF, "v4l2": cv2.CAP_V4L2}[config.backend]
        cap = cv2.VideoCapture(config.index, backend)
        if not cap.isOpened():
            raise RuntimeError(f"camera index {config.index} could not be opened using {config.backend}")
        requests = [("fourcc", cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*config.fourcc)),
                    ("width", cv2.CAP_PROP_FRAME_WIDTH, config.width),
                    ("height", cv2.CAP_PROP_FRAME_HEIGHT, config.height),
                    ("fps", cv2.CAP_PROP_FPS, config.fps),
                    ("buffer_size", cv2.CAP_PROP_BUFFERSIZE, config.buffer_size)]
        if config.auto_exposure is not None:
            requests.append(("auto_exposure", cv2.CAP_PROP_AUTO_EXPOSURE, config.auto_exposure))
        if config.exposure is not None:
            requests.append(("exposure", cv2.CAP_PROP_EXPOSURE, config.exposure))
        settings = {}
        for name, prop, value in requests:
            accepted = bool(cap.set(prop, value))
            reported = cap.get(prop)
            settings[name] = {"requested": value, "set_returned_true": accepted,
                              "reported": reported if math.isfinite(reported) else None}
        until = time.perf_counter() + warmup
        frame = None
        while not stop.is_set() and (frame is None or time.perf_counter() < until):
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("camera opened but did not return a warmup frame")
        control.put({"kind": "ready", "camera_id": config.id,
                     "backend": cap.getBackendName(), "settings": settings})
        while not start.wait(0.1):
            if stop.is_set():
                return
        while not stop.is_set():
            before = time.perf_counter_ns()
            ok, frame = cap.read()
            received = time.perf_counter_ns()
            if not ok or frame is None:
                raise RuntimeError("camera stopped returning frames")
            if stop.is_set():
                break
            height, width = frame.shape[:2]
            region = frame
            if config.brightness_roi is not None:
                x, y, w, h = config.brightness_roi
                if x + w > width or y + h > height:
                    raise ValueError("brightness_roi extends outside actual camera image")
                region = frame[y:y+h, x:x+w]
            brightness = float(cv2.mean(cv2.cvtColor(region, cv2.COLOR_BGR2GRAY))[0])
            sample = FrameSample(config.id, captured, before, received, width, height, brightness=brightness)
            captured += 1
            try:
                frames.put_nowait(asdict(sample))
            except queue.Full:
                discarded += 1
            if output is not None:
                if writer is None:
                    writer = cv2.VideoWriter(str(Path(output) / f"{config.id}.avi"),
                                             cv2.VideoWriter_fourcc(*"MJPG"), config.fps, (width, height))
                    if not writer.isOpened():
                        raise RuntimeError("cannot open recording writer")
                writer.write(frame)
    except Exception as exc:
        control.put({"kind": "error", "camera_id": config.id, "message": str(exc)})
    finally:
        if writer is not None:
            writer.release()
        if cap is not None:
            cap.release()
        control.put({"kind": "done", "camera_id": config.id,
                     "captured_frames": captured, "ipc_dropped_frames": discarded})
        frames.close()
        frames.join_thread()


def capture(config: CaptureConfig, output: str, duration: float = 10,
            timeout: float = 5, warmup: float = 1, record: bool = False) -> dict:
    positive_number(duration, "duration", 3600)
    positive_number(timeout, "timeout", 120)
    if not math.isfinite(warmup) or warmup < 0 or warmup >= timeout:
        raise ValueError("warmup must be nonnegative and less than timeout")
    session = Session(output, [c.id for c in config.cameras], "camera", config.max_arrival_skew_ms,
                      config.max_queue, {"config": config.to_dict(), "record_video": record,
                                         "warmup_seconds": warmup, "images_saved": record})
    context = mp.get_context("spawn")
    frames = context.Queue(maxsize=256)
    control = context.Queue()
    start, stop = context.Event(), context.Event()
    processes = {}
    ready, done, errors = {}, {}, []
    acquisition_start = None
    ended = None
    last_arrival = {}
    interrupted = False

    def messages() -> None:
        while True:
            try:
                event = control.get_nowait()
            except queue.Empty:
                return
            camera_id = event["camera_id"]
            if event["kind"] == "ready":
                ready[camera_id] = event
            elif event["kind"] == "error":
                errors.append(f"{camera_id}: {event['message']}")
            elif event["kind"] == "done":
                done[camera_id] = event

    try:
        for camera in config.cameras:
            process = context.Process(target=_camera_worker,
                args=(camera, frames, control, start, stop, str(session.path) if record else None, warmup),
                name=f"megumiu-{camera.id}", daemon=True)
            process.start()
            processes[camera.id] = process
        deadline = time.perf_counter() + timeout
        while len(ready) < len(config.cameras) and not errors:
            messages()
            if time.perf_counter() > deadline:
                errors.append("Camera startup timeout: " + ", ".join(set(processes) - set(ready)))
                break
            for camera_id, process in processes.items():
                if process.exitcode is not None and camera_id not in ready and not errors:
                    errors.append(f"{camera_id}: worker exited before startup")
            time.sleep(0.01)
        if not errors:
            acquisition_start = time.perf_counter()
            start.set()
            while time.perf_counter() - acquisition_start < duration:
                messages()
                if errors:
                    break
                try:
                    sample = FrameSample(**frames.get(timeout=0.05))
                    session.add(sample)
                    last_arrival[sample.camera_id] = time.perf_counter()
                except queue.Empty:
                    pass
                now = time.perf_counter()
                for camera_id, process in processes.items():
                    if process.exitcode is not None:
                        errors.append(f"{camera_id}: worker exited during capture")
                    elif now - last_arrival.get(camera_id, acquisition_start) > timeout:
                        errors.append(f"{camera_id}: no frames received within {timeout:g}s")
                if errors:
                    break
    except KeyboardInterrupt:
        interrupted = True
        errors.append("Capture interrupted by user")
    except Exception as exc:
        errors.append(str(exc))
    finally:
        ended = time.perf_counter()
        stop.set()
        start.set()
        # Keep consuming while workers flush their queue feeders. Joining first
        # can deadlock a full pipe; cancel_join_thread would silently lose data.
        shutdown_deadline = time.perf_counter() + 2
        while time.perf_counter() < shutdown_deadline:
            try:
                messages()
                session.add(FrameSample(**frames.get(timeout=0.02)))
            except queue.Empty:
                if all(not p.is_alive() for p in processes.values()):
                    break
            except (EOFError, OSError, ValueError, TypeError) as exc:
                errors.append(f"Shutdown queue error: {exc}")
                break
        for camera_id, process in processes.items():
            process.join(timeout=0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
                errors.append(f"{camera_id}: blocked driver process terminated; recording may be incomplete")
        # Do not read a queue after terminating a stuck producer: multiprocessing
        # cannot guarantee that a pipe interrupted during a write is intact.
        frames.close()
        control.close()
    elapsed = ended - acquisition_start if acquisition_start is not None else 0
    return session.finish(elapsed, errors, {"camera_settings": ready, "worker_totals": done,
        "interrupted": interrupted, "recording_affects_timing": record,
        "hardware_exposure_timestamp_available": False})
