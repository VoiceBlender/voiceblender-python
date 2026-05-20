"""Generator output must be deterministic: two runs produce byte-identical files,
and committed output must match what the generator currently produces.

This is the Python equivalent of "regenerate and ``git diff`` is clean" — the
gate that prevents the generated files from drifting out of sync with the spec.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "voiceblender"
GENERATOR = ROOT / "tools" / "generate.py"
OPENAPI = ROOT.parent / "VoiceBlender" / "openapi.yaml"
ASYNCAPI = ROOT.parent / "VoiceBlender" / "asyncapi.yaml"

GENERATED_FILES = (
    "_models.py",
    "_requests.py",
    "_responses.py",
    "_events.py",
    "_legs.py",
    "_rooms.py",
    "_webrtc.py",
    "_vsi.py",
)


def _run_generator(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Copy hand-written modules into out_dir so the package is importable —
    # the generator never touches them, but ruff/format passes need them present.
    for f in (
        "__init__.py",
        "_errors.py",
        "_http.py",
        "_client.py",
        "_playback.py",
        "_responses_extra.py",
        "_hub.py",
        "_stream.py",
        "_sync_helpers.py",
        "py.typed",
    ):
        src_file = SRC / f
        if src_file.exists():
            shutil.copy2(src_file, out_dir / f)
    cmd = [
        sys.executable,
        str(GENERATOR),
        "--openapi",
        str(OPENAPI),
        "--asyncapi",
        str(ASYNCAPI),
        "--out",
        str(out_dir),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.skipif(not OPENAPI.exists() or not ASYNCAPI.exists(), reason="specs not available")
def test_generator_is_idempotent() -> None:
    """Running the generator twice into the same dir produces identical bytes."""
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        _run_generator(out)
        first = {f: (out / f).read_bytes() for f in GENERATED_FILES}
        _run_generator(out)
        second = {f: (out / f).read_bytes() for f in GENERATED_FILES}
        for f in GENERATED_FILES:
            assert first[f] == second[f], f"{f} changed on second run"


@pytest.mark.skipif(not OPENAPI.exists() or not ASYNCAPI.exists(), reason="specs not available")
def test_committed_output_matches_fresh_generation(tmp_path: Path) -> None:
    """The checked-in generated files must match what ``make generate`` produces.

    Failure means the generator was changed (or the spec changed) but
    ``make generate`` wasn't re-run — exactly the stale-codegen gate the Go
    SDK enforces via ``git diff``. The test reproduces the full Makefile
    pipeline: generate → ``ruff check --fix`` → ``ruff format``.
    """
    out = tmp_path / "voiceblender"
    _run_generator(out)
    _ruff_pipeline(out)
    for f in GENERATED_FILES:
        fresh = (out / f).read_text(encoding="utf-8")
        committed = (SRC / f).read_text(encoding="utf-8")
        assert fresh == committed, (
            f"{f}: committed output does not match a fresh generation — "
            "did you forget to run `make generate`?"
        )


def _ruff_pipeline(out_dir: Path) -> None:
    """Run the post-generate ruff steps that ``make generate`` runs."""
    venv_ruff = Path(sys.executable).with_name("ruff")
    ruff = str(venv_ruff) if venv_ruff.exists() else "ruff"
    subprocess.run([ruff, "check", "--fix", str(out_dir)], capture_output=True, check=False)
    subprocess.run([ruff, "format", str(out_dir)], capture_output=True, check=True)
