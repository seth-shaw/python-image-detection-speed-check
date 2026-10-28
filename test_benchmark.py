"""Integration checks using generated controls, independent of the supplied fixtures."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

import benchmark


class BenchmarkTests(unittest.TestCase):
    def test_real_detectors_with_wrong_extensions_and_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            Image.new("RGB", (4, 4), "blue").save(assets / "picture.txt", format="PNG")
            (assets / "text.jpg").write_text("Plain text, despite the filename.\n")
            labels = root / "labels.json"
            labels.write_text(json.dumps({"picture.txt": True, "text.jpg": False}))
            args = argparse.Namespace(assets=assets, labels=labels, seed=1, warmup=1,
                                      iterations=2, target_seconds=0.01, rounds=2)
            report = benchmark.run(args)
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(len(report["metrics"]), 5)
            self.assertIsNotNone(report["fastest_matching_labels"])
            for metric in report["metrics"]:
                self.assertEqual(metric["confusion"], {"tp": 1, "tn": 1, "fp": 0, "fn": 0})
                self.assertEqual(metric["filename_audit_failures"], 0)
                self.assertEqual(metric["errors"], 0)
                self.assertFalse(metric["unstable_counts"])
                self.assertEqual(len(metric["round_ns"]), 2)
                self.assertGreater(metric["median_us_per_file"], 0)
            args.labels = None
            report = benchmark.run(args)
            self.assertIsNone(report["fastest_matching_labels"])

    def test_missing_labels_cannot_silently_score_partial_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "file"
            fixture.write_bytes(b"content")
            labels = root / "labels.json"
            labels.write_text("{}")
            with self.assertRaisesRegex(ValueError, "Missing"):
                benchmark.load_labels(labels, [fixture], root)

    def test_preflight_rejects_silent_unknown_detector(self):
        broken = benchmark.Detector("broken", lambda _: benchmark.Detection(False, "unknown"), 0)
        with self.assertRaisesRegex(ValueError, "failed PNG/text preflight"):
            benchmark.preflight([broken])

    def test_empty_corpus_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, benchmark.__file__, "--assets", temporary],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("No regular files", result.stderr)


if __name__ == "__main__":
    unittest.main()
