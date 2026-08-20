"""awshare's published contract, asserted.

The package advertises "content-addressed bundles with verification on
retrieval, atomic writes and containment of attacker-controlled member names".
Until this file it shipped ZERO tests, so every one of those was a claim in a
description rather than a property anyone had checked — and the containment half
is a security control, which is the worst thing to publish unverified.

Two halves on purpose. `test_round_trip_*` are the POSITIVE assertions: a suite
that only proves refusals passes trivially against a package that refuses
everything, including one that is simply broken (security-review-patterns #5).
The rest prove each documented refusal actually refuses.
"""
from __future__ import annotations

import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awshare import (  # noqa: E402
    ARCHIVE_SUFFIX,
    MANIFEST_SUFFIX,
    ShareError,
    digest_file,
    fetch,
    load_manifest,
    publish,
    safe_member_path,
)


def _publish(src: Path, out: Path, name: str = "demo"):
    """publish() returns the Manifest it wrote; the FILES are derived from
    out_dir + the suffix constants. Deriving them here rather than in each test
    keeps the tests honest if the layout ever changes."""
    manifest = publish(src, out, name=name)
    return manifest, out / (name + MANIFEST_SUFFIX), out / (name + ARCHIVE_SUFFIX)


def _repoint(manifest_path: Path, archive: Path) -> None:
    """Make the manifest address a rebuilt archive, so the DIGEST check passes
    and whatever comes after it is what has to refuse."""
    import json
    d = json.loads(manifest_path.read_text(encoding="utf-8"))
    d["digest"] = digest_file(archive)
    d["size"] = archive.stat().st_size
    manifest_path.write_text(json.dumps(d), encoding="utf-8")


def _tree(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "pkg" / "sub").mkdir()
    (root / "pkg" / "sub" / "b.txt").write_text("beta", encoding="utf-8")
    return root / "pkg"


# ── the happy path actually works ────────────────────────────────────────────

def test_round_trip_restores_every_file(tmp_path):
    src = _tree(tmp_path / "src")
    out = tmp_path / "out"
    _, manifest_path, _ = _publish(src, out)

    dest = tmp_path / "dest"
    fetch(manifest_path, dest)

    assert (dest / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (dest / "sub" / "b.txt").read_text(encoding="utf-8") == "beta"


def test_round_trip_manifest_describes_what_was_published(tmp_path):
    src = _tree(tmp_path / "src")
    out = tmp_path / "out"
    _, manifest_path, archive = _publish(src, out)

    m = load_manifest(manifest_path)
    assert m.name == "demo"
    # `files` is the LIST of archived names, so assert the names rather than a
    # count: a count of 2 would also pass if the nested file were recorded as a
    # second top-level entry, which is the mistake worth catching.
    assert sorted(m.files) == ["a.txt", "sub/b.txt"], (
        "the manifest must record nested members at their real relative paths"
    )
    assert m.digest == digest_file(archive), (
        "the manifest digest must address the archive it describes — that is the "
        "whole content-addressing claim"
    )


# ── verification on retrieval ────────────────────────────────────────────────

def test_fetch_refuses_a_corrupted_archive(tmp_path):
    src = _tree(tmp_path / "src")
    out = tmp_path / "out"
    _, manifest_path, archive = _publish(src, out)

    archive.write_bytes(archive.read_bytes() + b"tamper")

    with pytest.raises(ShareError):
        fetch(manifest_path, tmp_path / "dest")


def test_fetch_leaves_nothing_behind_when_the_digest_fails(tmp_path):
    """A refusal that half-extracts is worse than no check: the caller sees an
    error and a populated directory, and may well use it."""
    src = _tree(tmp_path / "src")
    out = tmp_path / "out"
    _, manifest_path, archive = _publish(src, out)
    archive.write_bytes(archive.read_bytes() + b"tamper")

    dest = tmp_path / "dest"
    with pytest.raises(ShareError):
        fetch(manifest_path, dest)
    assert not any(dest.rglob("*")) if dest.exists() else True


# ── containment of attacker-controlled member names ──────────────────────────
#
# These assert the OUTCOME (a hostile name is refused), not which layer refuses
# it, and that is deliberate — the guards are redundant by design. Verified by
# mutation: disabling the `..` check alone leaves all 15 passing, because the
# resolve()-against-root check still catches it. Removing containment entirely
# fails 9 of them, including the end-to-end archive case. So a single-layer
# mutation surviving is defence in depth doing its job, not a weak test.

@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "sub/../../escape.txt",
        "/etc/passwd",
        "",
        ".",
        "..",
        "with\x00nul.txt",
    ],
)
def test_safe_member_path_refuses_hostile_names(tmp_path, name):
    with pytest.raises(ShareError):
        safe_member_path(tmp_path, name)


@pytest.mark.skipif(os.name != "nt", reason="drive-relative names are a Windows shape")
def test_safe_member_path_refuses_drive_relative_name(tmp_path):
    """`C:evil` is neither absolute nor does it contain `..`, so the two obvious
    guards both pass it. This is the case the docstring says defeats them."""
    with pytest.raises(ShareError):
        safe_member_path(tmp_path, "C:evil.txt")


def test_safe_member_path_accepts_an_ordinary_nested_name(tmp_path):
    """The don't-refuse-everything half: containment that rejects valid members
    is indistinguishable from a broken extractor."""
    got = safe_member_path(tmp_path, "sub/ok.txt")
    assert got == (tmp_path / "sub" / "ok.txt")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_safe_member_path_refuses_escape_through_a_symlink(tmp_path):
    """A name with no `..` and no drive can still leave the root if a directory
    inside it is a symlink pointing out — visible only after resolve()."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(ShareError):
        safe_member_path(root, "link/escape.txt")


def test_fetch_refuses_an_archive_carrying_a_traversal_member(tmp_path):
    """End to end, not just the helper: a real archive built to escape."""
    src = _tree(tmp_path / "src")
    out = tmp_path / "out"
    _, manifest_path, archive = _publish(src, out)

    evil = tmp_path / "evil.txt"
    evil.write_text("owned", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(evil, arcname="../escaped.txt")

    _repoint(manifest_path, archive)

    with pytest.raises(ShareError):
        fetch(manifest_path, tmp_path / "dest")
    assert not (tmp_path / "escaped.txt").exists(), "member escaped the destination"


def test_fetch_refuses_a_non_regular_member(tmp_path):
    src = _tree(tmp_path / "src")
    out = tmp_path / "out"
    _, manifest_path, archive = _publish(src, out)

    with tarfile.open(archive, "w:gz") as tf:
        ti = tarfile.TarInfo("dev-node")
        ti.type = tarfile.CHRTYPE
        tf.addfile(ti)

    _repoint(manifest_path, archive)

    with pytest.raises(ShareError):
        fetch(manifest_path, tmp_path / "dest")
