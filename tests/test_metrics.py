"""Deterministic checks for host arrival pairing and honest measurement reports."""

import json
import math
import unittest

from megumiu.metrics import ArrivalSynchronizer, FrameSample, FrameSet, percentile, summarize


def frame(camera, sequence, milliseconds, *, read_ms=1, pts=None):
    received = int((milliseconds + 1000) * 1_000_000)
    return FrameSample(camera, sequence, received - int(read_ms * 1_000_000), received, 640, 480, pts)


class ArrivalSynchronizerTests(unittest.TestCase):
    def test_three_cameras_pair_at_inclusive_boundary_in_configured_order(self):
        sync = ArrivalSynchronizer(["left", "right", "back"], max_skew_ms=12)
        self.assertEqual(sync.push(frame("back", 0, 12)), [])
        self.assertEqual(sync.push(frame("left", 0, 0)), [])
        sets = sync.push(frame("right", 0, 5))
        self.assertEqual(len(sets), 1)
        self.assertEqual([sample.camera_id for sample in sets[0].samples], ["left", "right", "back"])
        self.assertEqual(sets[0].skew_ms, 12)
        self.assertEqual(sync.pending_by_camera, {"left": 0, "right": 0, "back": 0})

    def test_frame_outside_window_is_discarded_not_reused(self):
        sync = ArrivalSynchronizer(["a", "b"], max_skew_ms=5)
        sync.push(frame("a", 0, 0))
        self.assertEqual(sync.push(frame("b", 0, 20)), [])
        self.assertEqual(sync.dropped_by_camera, {"a": 1, "b": 0})
        sets = sync.push(frame("a", 1, 21))
        self.assertEqual([sample.sequence for sample in sets[0].samples], [1, 0])
        self.assertEqual(sync.push(frame("b", 1, 22)), [])

    def test_queue_is_bounded_if_a_camera_stalls(self):
        sync = ArrivalSynchronizer(["a", "b"], max_queue=3)
        for index in range(20):
            self.assertEqual(sync.push(frame("a", index, index)), [])
            self.assertLessEqual(sync.pending_by_camera["a"], 3)
        self.assertEqual(sync.pending_by_camera, {"a": 3, "b": 0})
        self.assertEqual(sync.dropped_by_camera["a"], 17)
        snapshot = sync.pending_by_camera
        snapshot["a"] = 500
        self.assertEqual(sync.pending_by_camera["a"], 3)

    def test_different_rates_do_not_reuse_slow_camera_frames(self):
        sync = ArrivalSynchronizer(["fast", "slow"], max_skew_ms=2, max_queue=3)
        events = [frame("fast", index, index * 10) for index in range(11)]
        events += [frame("slow", index, index * 20 + 1) for index in range(6)]
        paired = []
        for sample in sorted(events, key=lambda sample: sample.host_received_ns):
            paired.extend(sync.push(sample))
        self.assertEqual(len(paired), 6)
        used = [(sample.camera_id, sample.sequence) for group in paired for sample in group.samples]
        self.assertEqual(len(used), len(set(used)))
        self.assertEqual(sync.dropped_by_camera, {"fast": 5, "slow": 0})
        self.assertTrue(all(group.skew_ms == 1 for group in paired))

    def test_source_pts_does_not_override_host_arrival(self):
        sync = ArrivalSynchronizer(["a", "b"], max_skew_ms=1)
        sync.push(frame("a", 0, 0, pts=500))
        self.assertEqual(sync.push(frame("b", 0, 50, pts=500)), [])
        sets = sync.push(frame("a", 1, 50, pts=9999))
        self.assertEqual(sets[0].skew_ms, 0)

    def test_invalid_push_does_not_poison_last_accepted_sample(self):
        sync = ArrivalSynchronizer(["a", "b"])
        sync.push(frame("a", 2, 10))
        for sample in [frame("a", 2, 11), frame("a", 1, 11), frame("a", 3, 9), frame("c", 0, 11)]:
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                sync.push(sample)
        sync.push(frame("a", 3, 10))
        self.assertEqual(sync.pending_by_camera["a"], 2)

    def test_zero_skew_and_single_camera_are_supported(self):
        sync = ArrivalSynchronizer(["a", "b"], max_skew_ms=0)
        sync.push(frame("a", 0, 0))
        self.assertEqual(len(sync.push(frame("b", 0, 0))), 1)
        self.assertEqual(ArrivalSynchronizer(["a"]).push(frame("a", 0, 0))[0].skew_ms, 0)

    def test_invalid_configuration_is_rejected(self):
        invalid = [
            ([], {}), (["a", "a"], {}), ([""], {}),
            (["a"], {"max_queue": 0}), (["a"], {"max_queue": 1.5}),
            (["a"], {"max_queue": True}), (["a"], {"max_skew_ms": -1}),
            (["a"], {"max_skew_ms": math.nan}), (["a"], {"max_skew_ms": math.inf}),
        ]
        for ids, options in invalid:
            with self.subTest(ids=ids, options=options), self.assertRaises(ValueError):
                ArrivalSynchronizer(ids, **options)


class MeasurementReportTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self):
        self.assertEqual(percentile([0, 10, 20, 30], 50), 15)
        self.assertAlmostEqual(percentile([0, 10, 20, 30], 95), 28.5)
        self.assertEqual(percentile([7], 99), 7)
        self.assertIsNone(percentile([], 50))
        for percent in [-1, 101, math.nan]:
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                percentile([1, 2], percent)

    def test_empty_report_keeps_unmeasured_durations_null(self):
        report = summarize([], [], ["a", "b"], 0)
        self.assertEqual(report["measurement_scope"], "host_arrival_only")
        self.assertFalse(report["exposure_sync_verified"])
        self.assertIsNone(report["end_to_end_latency_ms"])
        self.assertIsNone(report["paired_fps"])
        self.assertIsNone(report["host_arrival_skew_ms"]["p95"])
        for camera in report["cameras"].values():
            self.assertEqual(camera["received_frames"], 0)
            self.assertIsNone(camera["observed_fps"])
            self.assertIsNone(camera["interarrival_ms"]["p50"])
            self.assertIsNone(camera["read_duration_ms"]["p99"])
        json.dumps(report, allow_nan=False)

    def test_report_distinguishes_read_duration_arrival_skew_and_latency(self):
        samples = [
            frame("a", 0, 0, read_ms=2), frame("b", 0, 4, read_ms=5),
            frame("a", 1, 10, read_ms=4), frame("b", 1, 14, read_ms=7),
            frame("a", 2, 30, read_ms=6),
        ]
        sets = [FrameSet(tuple(samples[:2])), FrameSet(tuple(samples[2:4]))]
        report = summarize(samples, sets, ["a", "b"], 0.1, {"a": 1})
        a = report["cameras"]["a"]
        self.assertEqual(a["received_frames"], 3)
        self.assertAlmostEqual(a["observed_fps"], 2 / 0.03)
        self.assertEqual(a["interarrival_ms"]["p50"], 15)
        self.assertEqual(a["interarrival_ms"]["p95"], 19.5)
        self.assertEqual(a["read_duration_ms"]["p50"], 4)
        self.assertEqual(a["dropped_frames"], 1)
        self.assertEqual(a["unpaired_frames"], 1)
        self.assertEqual(report["paired_frame_sets"], 2)
        self.assertEqual(report["paired_fps"], 20)
        self.assertEqual(report["host_arrival_skew_ms"]["p95"], 4)
        self.assertIsNone(report["end_to_end_latency_ms"])

    def test_one_arrival_does_not_claim_a_framerate(self):
        sample = frame("a", 0, 0)
        report = summarize([sample], [], ["a"], 1)
        self.assertIsNone(report["cameras"]["a"]["observed_fps"])
        self.assertIsNone(report["cameras"]["a"]["interarrival_ms"]["p95"])
        self.assertEqual(report["cameras"]["a"]["read_duration_ms"]["p95"], 1)

    def test_report_rejects_missing_duplicate_and_reused_frames(self):
        sample = frame("a", 0, 0)
        group = FrameSet((sample,))
        for samples, sets in [([], [group]), ([sample, sample], []), ([sample], [group, group])]:
            with self.subTest(samples=samples, sets=sets), self.assertRaises(ValueError):
                summarize(samples, sets, ["a"], 1)

    def test_invalid_report_inputs_are_rejected(self):
        cases = [
            ([frame("other", 0, 0)], [], ["a"], 1, None),
            ([], [], ["a"], -1, None),
            ([], [], ["a"], math.inf, None),
            ([], [], ["a"], 1, {"a": -1}),
            ([], [], ["a"], 1, {"other": 1}),
            ([], [FrameSet((frame("a", 0, 0),))], ["a", "b"], 1, None),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                summarize(*args)

    def test_invalid_frame_metadata_is_rejected(self):
        for args in [
            ("a", -1, 0, 1, 640, 480), ("a", 0, 2, 1, 640, 480),
            ("a", 0, 0, 1, 0, 480), ("", 0, 0, 1, 640, 480),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                FrameSample(*args)
        with self.assertRaises(ValueError):
            FrameSet(())
        with self.assertRaises(ValueError):
            FrameSet((frame("a", 0, 0), frame("a", 1, 1)))


if __name__ == "__main__":
    unittest.main()
