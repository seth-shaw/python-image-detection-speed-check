#!/usr/bin/env python3
"""Single-process, warm-cache image identification benchmark (including file I/O)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import mimetypes
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Detection:
    is_image: bool
    detail: str | None = None
    error: str | None = None


@dataclass
class Detector:
    name: str
    detect: Callable[[str], Detection]
    setup_seconds: float


def make_detector(name: str) -> Detector:
    start = time.perf_counter()
    if name == "Pillow":
        from PIL import Image, UnidentifiedImageError

        Image.init()  # Discover plugins once, outside steady-state timing.

        def detect(path: str) -> Detection:
            try:
                with Image.open(path) as image:
                    fmt = image.format
                    image.verify()
                return Detection(True, fmt)
            except UnidentifiedImageError:
                return Detection(False, "unidentified")
            except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
                return Detection(False, "verification rejected", f"{type(exc).__name__}: {exc}")

    elif name == "Defity":
        import defity

        def detect(path: str) -> Detection:
            mime = defity.from_file(path)
            return Detection(bool(mime and mime.startswith("image/")), mime)

    elif name == "filetype-detector":
        from filetype_detector.magic_inferencer import MagicInferencer

        inferencer = MagicInferencer()
        # This maps the CONTENT-INFERRED extension, never the input filename.
        mimetypes.init()

        def detect(path: str) -> Detection:
            extension = inferencer.infer(path)
            mime = mimetypes.guess_type("detected" + extension, strict=False)[0]
            return Detection(bool(mime and mime.startswith("image/")), extension)

    elif name == "filetype":
        import filetype

        def detect(path: str) -> Detection:
            kind = filetype.guess(path)
            mime = kind.mime if kind else None
            return Detection(bool(mime and mime.startswith("image/")), mime)

    elif name == "python-magic":
        import magic

        instance = magic.Magic(mime=True)

        def detect(path: str) -> Detection:
            mime = instance.from_file(path)
            return Detection(mime.startswith("image/"), mime)

    else:
        raise ValueError(name)

    def guarded(path: str) -> Detection:
        try:
            return detect(path)
        except Exception as exc:
            # Failed detections remain visible and cannot win the correctness ranking.
            return Detection(False, error=f"{type(exc).__name__}: {exc}")

    return Detector(name, guarded, time.perf_counter() - start)


NAMES = ["Pillow", "Defity", "filetype-detector", "filetype", "python-magic"]


def preflight(detectors: list[Detector]) -> None:
    """Fail early if a missing database makes a detector silently return unknown."""
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="image-preflight-") as temporary:
        image_path = Path(temporary) / "positive"
        text_path = Path(temporary) / "negative"
        Image.new("RGB", (2, 2), "red").save(image_path, format="PNG")
        text_path.write_text("This is a plain text control file.\n", encoding="utf-8")
        for detector in detectors:
            positive = detector.detect(str(image_path))
            negative = detector.detect(str(text_path))
            if not positive.is_image or negative.is_image or positive.error or negative.error:
                raise ValueError(f"{detector.name} failed PNG/text preflight: {positive}; {negative}. "
                                 "Check library dependencies and system MIME databases.")


def scan(detector: Detector, paths: list[str], iterations: int) -> tuple[int, int, int]:
    images = errors = 0
    start = time.perf_counter_ns()
    for _ in range(iterations):
        for path in paths:
            result = detector.detect(path)
            images += result.is_image
            errors += result.error is not None
    return time.perf_counter_ns() - start, images, errors


def load_labels(path: Path | None, files: list[Path], root: Path) -> dict[str, bool]:
    if path is None:
        return {}
    labels = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(labels, dict) or any(type(v) is not bool for v in labels.values()):
        raise ValueError("Labels must be a JSON object mapping relative POSIX paths to booleans")
    names = {p.relative_to(root).as_posix() for p in files}
    if set(labels) != names:
        raise ValueError(f"Labels must cover exactly the corpus. Missing: {sorted(names - set(labels))}; "
                         f"extra: {sorted(set(labels) - names)}")
    return labels


def run(args: argparse.Namespace) -> dict:
    root = args.assets.resolve()
    if not root.is_dir():
        raise ValueError(f"Assets directory does not exist: {root}")
    # Exclude symlinks so a mounted collection cannot pull in unrelated files.
    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and not any(q.is_symlink() for q in [p, *p.parents]))
    if not files:
        raise ValueError("No regular files found")
    labels = load_labels(args.labels, files, root)
    rng = random.Random(args.seed)
    detectors = [make_detector(name) for name in NAMES]
    preflight(detectors)
    rows = []
    corpus = []
    predictions: dict[str, list[Detection]] = {d.name: [] for d in detectors}
    audit_failures = {d.name: 0 for d in detectors}

    # All measured I/O uses identical extensionless files on the container filesystem.
    # Staging is outside the timer; this avoids Windows bind-mount overhead dominating.
    with tempfile.TemporaryDirectory(prefix="image-benchmark-") as temporary:
        staging = Path(temporary)
        (staging / "plain").mkdir()
        (staging / "misleading").mkdir()
        paths = []
        for index, source in enumerate(files):
            name = source.relative_to(root).as_posix()
            plain = staging / "plain" / f"{index:08d}"
            shutil.copyfile(source, plain)
            # Change both image and non-image names, with no original names exposed.
            misleading = staging / "misleading" / f"{index:08d}.jpg"
            shutil.copyfile(plain, misleading)
            paths.append(str(plain))
            with plain.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            corpus.append({"path": name, "bytes": plain.stat().st_size,
                           "sha256": digest,
                           "expected_image": labels.get(name)})
            file_row = {"path": name, "expected_image": labels.get(name), "detections": {}}
            for detector in detectors:
                result = detector.detect(str(plain))
                original = detector.detect(str(source))
                renamed = detector.detect(str(misleading))
                # Exception messages may embed the filename. Compare error classes,
                # not those strings, so a diagnostic path is not a false audit failure.
                def signature(value: Detection) -> tuple:
                    error_type = value.error.split(":", 1)[0] if value.error else None
                    return value.is_image, value.detail, error_type

                invariant = signature(result) == signature(original) == signature(renamed)
                audit_failures[detector.name] += not invariant
                predictions[detector.name].append(result)
                file_row["detections"][detector.name] = {
                    **asdict(result), "filename_invariant": invariant,
                    "original": asdict(original), "misleading_jpg": asdict(renamed),
                }
            rows.append(file_row)

        iterations = {}
        for detector in detectors:
            for _ in range(args.warmup):
                scan(detector, paths, 1)
            if args.iterations:
                iterations[detector.name] = args.iterations
            else:
                # Calibrate each round to at least roughly target_seconds.
                count = 1
                while True:
                    elapsed, _, _ = scan(detector, paths, count)
                    if elapsed >= args.target_seconds * 1e9 / 4 or count >= 1_000_000:
                        break
                    count *= 2
                iterations[detector.name] = max(1, min(1_000_000, round(
                    count * args.target_seconds * 1e9 / max(elapsed, 1))))

        timings: dict[str, list[int]] = {d.name: [] for d in detectors}
        unstable = {d.name: False for d in detectors}
        for _ in range(args.rounds):
            order = detectors.copy()
            rng.shuffle(order)
            rng.shuffle(paths)
            for detector in order:
                count = iterations[detector.name]
                elapsed, images, errors = scan(detector, paths, count)
                timings[detector.name].append(elapsed)
                expected_images = sum(r.is_image for r in predictions[detector.name]) * count
                expected_errors = sum(r.error is not None for r in predictions[detector.name]) * count
                unstable[detector.name] |= (images, errors) != (expected_images, expected_errors)

    metrics = []
    for detector in detectors:
        results = predictions[detector.name]
        calls = len(files) * iterations[detector.name]
        samples = [ns / calls / 1000 for ns in timings[detector.name]]
        median = statistics.median(samples)
        confusion = None
        if labels:
            confusion = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
            for item, result in zip(corpus, results):
                key = ("t" if result.is_image == item["expected_image"] else "f")
                key += "p" if result.is_image else "n"
                confusion[key] += 1
        metrics.append({
            "name": detector.name, "median_us_per_file": median,
            "min_us_per_file": min(samples), "max_us_per_file": max(samples),
            "files_per_second": 1_000_000 / median,
            "setup_seconds": detector.setup_seconds, "iterations_per_round": iterations[detector.name],
            "round_ns": timings[detector.name], "images": sum(r.is_image for r in results),
            "errors": sum(r.error is not None for r in results),
            "filename_audit_failures": audit_failures[detector.name],
            "unstable_counts": unstable[detector.name], "confusion": confusion,
        })
    metrics.sort(key=lambda m: m["median_us_per_file"])
    eligible = [m for m in metrics if m["errors"] == 0 and not m["unstable_counts"]
                and m["filename_audit_failures"] == 0]
    correct = [m for m in eligible if m["confusion"] is not None
               and m["confusion"]["fp"] == m["confusion"]["fn"] == 0]
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "packages": {name: importlib.metadata.version(name) for name in
                                     ["Pillow", "defity", "filetype-detector", "filetype", "python-magic"]}},
        "settings": {"rounds": args.rounds, "warmup": args.warmup,
                     "seed": args.seed, "target_seconds": args.target_seconds,
                     "timing": "single process; warm cache; extensionless staged paths; includes open/read/close"},
        "file_count": len(files), "total_bytes": sum(item["bytes"] for item in corpus),
        "corpus": corpus, "metrics": metrics, "files": rows,
        "fastest_without_errors": eligible[0]["name"] if eligible else None,
        "fastest_matching_labels": correct[0]["name"] if correct else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("/assets"))
    parser.add_argument("--labels", type=Path, help="Explicit image/non-image labels; never inferred from suffixes")
    parser.add_argument("--output", type=Path, default=Path("/results"))
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--warmup", type=int, default=2, help="Full corpus passes before calibration")
    parser.add_argument("--iterations", type=int, help="Corpus passes per round (default: auto-calibrate)")
    parser.add_argument("--target-seconds", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    import math

    if (args.rounds < 1 or args.warmup < 0 or not math.isfinite(args.target_seconds) or args.target_seconds <= 0
            or (args.iterations is not None and args.iterations < 1)):
        parser.error("rounds, iterations and target-seconds must be positive; warmup must be nonnegative")
    # Filter once so first-occurrence warning output does not bias timed loops.
    warnings.simplefilter("ignore")
    try:
        report = run(args)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(2, f"Benchmark failed: {exc}\n")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "benchmark.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    fields = ["name", "median_us_per_file", "min_us_per_file", "max_us_per_file",
              "files_per_second", "images", "errors", "filename_audit_failures", "unstable_counts"]
    with (args.output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["metrics"])
    print(f"\n{report['file_count']} files, {report['total_bytes']:,} bytes; {args.rounds} rounds")
    print("Warm-cache path detection; median of round averages (includes file I/O).\n")
    print(f"{'Detector':<20} {'us/file':>10} {'files/sec':>12} {'images':>7} {'errors':>7} {'TP/TN/FP/FN':>15}")
    for metric in report["metrics"]:
        confusion = metric["confusion"]
        counts = "/".join(str(confusion[k]) for k in ("tp", "tn", "fp", "fn")) if confusion else "unlabeled"
        print(f"{metric['name']:<20} {metric['median_us_per_file']:>10.2f} "
              f"{metric['files_per_second']:>12,.0f} {metric['images']:>7} "
              f"{metric['errors']:>7} {counts:>15}")
        if metric["filename_audit_failures"] or metric["unstable_counts"]:
            print(f"  CHECK: filename audit failures={metric['filename_audit_failures']}, "
                  f"unstable counts={metric['unstable_counts']}")
    print(f"\nFastest without errors, with invariant filenames: {report['fastest_without_errors'] or 'none'}")
    print(f"Fastest matching every supplied label: {report['fastest_matching_labels'] or 'none (or no labels supplied)'}")
    print(f"Reports: {args.output / 'benchmark.json'} and {args.output / 'summary.csv'}")
    print("Pillow verifies structure; signature detection does not establish image integrity.")


if __name__ == "__main__":
    main()
