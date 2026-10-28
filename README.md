# Image identification speed benchmark

Compare Pillow (`Image.open()` followed by `verify()`), Defity, filetype-detector,
filetype, and python-magic against the files in `test_assets`.

## Run

Install Docker Desktop with Linux containers and Docker Compose. From this directory:

```powershell
docker compose run --rm --build benchmark
```

No host Python installation is required. The container has network access disabled
at runtime; building it downloads the base image and dependencies. Assets are
mounted read-only. Reports are written to `results/benchmark.json` and
`results/summary.csv`, overwriting previous reports.

For longer timing rounds:

```powershell
docker compose run --rm --build benchmark --labels /labels.json --rounds 11 --target-seconds 0.5
```

For a fixed number of complete corpus passes per round:

```powershell
docker compose run --rm benchmark --labels /labels.json --rounds 7 --iterations 1000
```

Run without labels by omitting `--labels` from the overridden command:

```powershell
docker compose run --rm benchmark --rounds 7
```

Use `docker compose run --rm benchmark --help` for all options. You can also run
`python benchmark.py --assets test_assets --labels labels.json --output results`
in a Python environment with the same Python and system dependencies.

## What is measured

- Each detector receives paths and performs its own file open/read/close operations.
  There is no shared header buffer and no forced common read size.
- Files are copied to extensionless numeric names on the container filesystem
  before timing. This prevents suffix hints and avoids timing Windows bind-mount
  access. It measures local container I/O, not your host filesystem's performance.
- An untimed audit compares the original filenames, extensionless names, and names
  ending in `.jpg`. Type, image decision, and exception class must agree. Diagnostic
  paths inside error messages are allowed to differ.
- A generated PNG and a plain text control must pass each detector before the
  corpus is benchmarked. This catches silently missing MIME databases.
- Imports, plugin discovery, construction, copying, audits, and report generation
  are excluded. `setup_seconds` is diagnostic, not an isolated cold-import benchmark:
  earlier imports can initialize shared dependencies.
- Two warmup corpus passes precede calibration. By default, the script calibrates
  each detector separately to roughly 0.2 seconds per round, then measures seven
  rounds. Detector order and file order are shuffled using a recorded seed.
- The ranking uses the median of round-average microseconds per file. Min/max and
  raw round durations are in JSON. These are throughput averages, not individual
  file latency percentiles. The common result wrapper and exception handling are
  included in all timings.
- This is a serial, warm-cache benchmark. It does not measure cold disk performance,
  concurrency, memory usage, or process startup. Avoid other heavy work while running.

## APIs and dependencies

| Option | Measured API / image decision |
| --- | --- |
| Pillow | `Image.open(path)` in a context manager, then `image.verify()` |
| Defity | `defity.from_file(path)`, MIME starts with `image/` |
| filetype-detector | `MagicInferencer().infer(path)`, then map the **inferred** extension to an image MIME type |
| filetype | `filetype.guess(path)`, returned MIME starts with `image/` |
| python-magic | Reused `magic.Magic(mime=True).from_file(path)`, MIME starts with `image/` |

filetype-detector's lexical mode uses filenames and is excluded. Its magic mode
wraps python-magic, so these two rows compare a wrapper with direct libmagic access.
Its Magika and cascading modes are different algorithms and are not included here.
See the [filetype-detector API](https://pypi.org/project/filetype-detector/).

The Docker image installs `libmagic1` for libmagic, `shared-mime-info` for Defity's
underlying MIME database, and `media-types` for Python's MIME-to-extension mapping.
Without the shared MIME database, Defity can silently report unknown for binary
files; the [underlying library documents its runtime database requirement](https://docs.rs/tree_magic_mini/latest/tree_magic_mini/).
Without `media-types`, filetype-detector can raise on MIME types missing from
Python's built-in extension table, including the supplied PowerPoint file.

The five compared Python packages are pinned in `requirements.txt`. The base image,
Debian packages, and transitive dependencies can change between fresh builds;
retain the built image when exact environment reuse matters. JSON records Python,
platform, the five package versions, file sizes and SHA-256 hashes, per-file
predictions/errors, filename audits, and timing settings.

## Correctness and interpretation

`labels.json` contains explicit image-format labels for the nine supplied fixtures
from [file/file-tests](https://github.com/file/file-tests). The five image labels
were checked against their content signatures (GIF, PNG, JPEG, TIFF, JPEG);
filenames are only keys for joining labels to files. Labels mean **image format**,
including an incomplete image, not proven decodability. Update this manifest when
adding, removing, or replacing files. Labels must cover exactly the corpus; they
are never inferred from filenames or another detector's output.

Signature recognition and structural verification answer different questions.
Pillow rejects the supplied `adhoc1.png` with `OSError: Truncated File Read`, while
signature detectors recognize its PNG header. [`verify()`](https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.verify)
also does not promise full pixel decoding or validation of every frame. Pillow's
supported formats differ from MIME detectors; for example, SVG is outside its
usual raster image decoding support.

The report separates speed from correctness:

- `fastest_without_errors` requires no exceptions, unchanged filename-audit results,
  and stable image/error totals across measured passes. Without labels it says
  nothing about accuracy.
- `fastest_matching_labels` additionally requires every supplied image/non-image
  label to match. With no labels this is `null`.
- TP/TN/FP/FN describe the image decisions. A failed detection yields a negative
  decision and is separately counted in `errors`; even if that negative matches
  its label, the detector is ineligible to win while errors remain.

Nine small files are enough for a reproducible comparison on this collection, but
not a universal speed or accuracy claim. Include representative large images,
additional formats, corrupt images, and ordinary non-image files before choosing
for a broader production workload. All methods here use content for detection;
the extension returned by filetype-detector comes from content inference.

## Measured result (2026-10-28)

On Docker Desktop's Linux/amd64 engine, Python 3.12.16, using the current nine files
(121,022 bytes), 11 rounds and a 0.5-second target per detector per round:

| Detector | Median microseconds/file | Images identified | Exceptions |
| --- | ---: | ---: | ---: |
| filetype | 10.21 | 5/5 | 0 |
| Defity | 10.88 | 5/5 | 0 |
| Pillow | 47.26 | 4/5 | 1 |
| python-magic | 53.87 | 5/5 | 0 |
| filetype-detector (magic) | 67.50 | 5/5 | 0 |

There were no false positives and all filename audits passed. **filetype was the
fastest detector matching all labels in the final run.** Defity and filetype were
close, and their order changed between runs; this is an observed ranking, not
proof of a significant or universal advantage. Pillow's exception is the truncated
PNG described above. Machine-readable evidence is in `results/benchmark.json`.

## Integration checks

After building, run generated PNG/text controls with intentionally wrong extensions,
label-coverage checks, empty-corpus handling, and missing-database preflight checks:

```powershell
docker compose run --rm --volume "${PWD}:/checks:ro" --entrypoint python benchmark /checks/test_benchmark.py -v
```

## AI Disclaimer

This repository was created using Codex/ChatGPT. I've included the [transcript](ai-transcript.md) for full transparency.