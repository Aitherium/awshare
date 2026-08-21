"""Chunking: the arithmetic, and the refusals that make a bad stitch impossible.

The failure this module exists to prevent is SILENT. A misaligned stitch does
not raise — it produces a file of exactly the right length whose content is
shuffled at a part boundary, so every length check passes and the consumer
discovers it much later as "corrupt", with nothing pointing at the split.

So the interesting tests are not "does it split". They are the boundary cases in
`resolve_range` and every path where a manifest and its parts disagree.
"""

from __future__ import annotations

import pytest
from awshare.chunk import (
    DEFAULT_PART_SIZE,
    ChunkManifest,
    load_manifest,
    missing_parts,
    plan_parts,
    resolve_range,
    split,
    stitch,
)
from awshare.store import ShareError, VerificationFailedError


# ── planning (pure arithmetic, at sizes no test could write to disk) ──────────
def test_parts_tile_the_file_exactly():
    parts = plan_parts(10_000_000_000, part_size=1_900_000_000)
    assert sum(p.size for p in parts) == 10_000_000_000
    assert parts[0].offset == 0
    for a, b in zip(parts, parts[1:]):
        assert b.offset == a.offset + a.size, "a gap or overlap between parts"


def test_an_exact_multiple_does_not_produce_an_empty_tail():
    parts = plan_parts(4000, part_size=1000)
    assert len(parts) == 4
    assert all(p.size == 1000 for p in parts)


def test_a_file_smaller_than_one_part_is_a_single_part():
    parts = plan_parts(10, part_size=1_900_000_000)
    assert len(parts) == 1 and parts[0].size == 10


def test_planning_refuses_nonsense():
    with pytest.raises(ShareError):
        plan_parts(0)
    with pytest.raises(ShareError):
        plan_parts(100, part_size=0)


def test_default_part_size_is_under_the_github_asset_cap():
    assert DEFAULT_PART_SIZE < 2 * 1024 ** 3


# ── range resolution (the piece most likely to be quietly wrong) ──────────────
def _manifest(total=1000, part=250):
    return ChunkManifest(version=1, name="f.bin", total_bytes=total,
                         part_size=part, parts=plan_parts(total, part, "f.bin"))


def test_a_range_inside_one_part():
    got = resolve_range(_manifest(), 10, 20)
    assert len(got) == 1
    p, s, e = got[0]
    assert (p.index, s, e) == (0, 10, 20)


def test_a_range_spanning_three_parts_covers_every_byte_once():
    m = _manifest()
    got = resolve_range(m, 200, 800)
    covered = sum(e - s + 1 for _p, s, e in got)
    assert covered == 601, "the sub-ranges do not sum to the requested length"
    assert [p.index for p, _s, _e in got] == [0, 1, 2, 3]


def test_the_exact_boundary_byte_belongs_to_one_part_only():
    """An off-by-one here yields a response of the right LENGTH with one byte of
    wrong content — Content-Length checks out and the file is ruined."""
    m = _manifest()
    at_end = resolve_range(m, 249, 249)
    at_start = resolve_range(m, 250, 250)
    assert len(at_end) == 1 and at_end[0][0].index == 0
    assert len(at_start) == 1 and at_start[0][0].index == 1
    assert at_end[0][1] == at_end[0][2] == 249
    assert at_start[0][1] == at_start[0][2] == 0


def test_the_whole_file_range_uses_every_part():
    m = _manifest()
    got = resolve_range(m, 0, 999)
    assert len(got) == len(m.parts)
    assert sum(e - s + 1 for _p, s, e in got) == 1000


def test_a_range_past_the_end_is_refused():
    m = _manifest()
    for bad in ((0, 1000), (-1, 5), (500, 499)):
        with pytest.raises(ShareError):
            resolve_range(m, *bad)


# ── manifest validation ───────────────────────────────────────────────────────
def test_a_manifest_whose_parts_do_not_sum_is_refused():
    m = _manifest()
    m.parts[-1].size -= 1
    with pytest.raises(ShareError) as ei:
        m.validate()
    assert "sum" in str(ei.value)


def test_a_manifest_with_a_gap_is_refused():
    m = _manifest()
    m.parts[1].offset += 5
    with pytest.raises(ShareError) as ei:
        m.validate()
    assert "tile" in str(ei.value)


def test_a_manifest_with_no_parts_is_refused():
    with pytest.raises(ShareError):
        ChunkManifest.from_json({"version": 1, "name": "x", "total_bytes": 0,
                                 "parts": []})


def test_an_unknown_manifest_version_is_refused():
    with pytest.raises(ShareError):
        ChunkManifest.from_json({"version": 99, "name": "x", "total_bytes": 1,
                                 "parts": [{"name": "x.part0", "index": 0,
                                            "offset": 0, "size": 1}]})


# ── split / stitch round trip ─────────────────────────────────────────────────
@pytest.fixture()
def big(tmp_path):
    src = tmp_path / "weights.gguf"
    src.write_bytes(bytes(range(256)) * 33 + b"tail")  # 8452 bytes, non-uniform
    return src


def test_round_trip_reproduces_the_file_byte_for_byte(big, tmp_path):
    m = split(big, tmp_path / "parts", part_size=1000)
    assert len(m.parts) == 9
    out = stitch(m, tmp_path / "parts", tmp_path / "rebuilt.gguf")
    assert out.read_bytes() == big.read_bytes()


def test_the_manifest_is_written_and_reloads(big, tmp_path):
    m = split(big, tmp_path / "parts", part_size=1000)
    again = load_manifest(tmp_path / "parts" / m.manifest_name())
    assert again.total_bytes == m.total_bytes
    assert [p.name for p in again.parts] == [p.name for p in m.parts]


def test_stitching_refuses_a_part_of_the_wrong_size(big, tmp_path):
    """The whole point. A short part must never be stitched into a file that
    then looks finished."""
    m = split(big, tmp_path / "parts", part_size=1000)
    (tmp_path / "parts" / m.parts[2].name).write_bytes(b"short")
    with pytest.raises(VerificationFailedError):
        stitch(m, tmp_path / "parts", tmp_path / "rebuilt.gguf")
    assert not (tmp_path / "rebuilt.gguf").exists(), "a bad stitch was published"


def test_stitching_refuses_a_corrupted_part_of_the_right_size(big, tmp_path):
    """Same length, different bytes — the case a size check cannot see."""
    m = split(big, tmp_path / "parts", part_size=1000)
    target = tmp_path / "parts" / m.parts[1].name
    target.write_bytes(b"\x00" * target.stat().st_size)
    with pytest.raises(VerificationFailedError):
        stitch(m, tmp_path / "parts", tmp_path / "rebuilt.gguf")
    assert not (tmp_path / "rebuilt.gguf").exists()


def test_stitching_refuses_a_missing_part(big, tmp_path):
    m = split(big, tmp_path / "parts", part_size=1000)
    (tmp_path / "parts" / m.parts[0].name).unlink()
    with pytest.raises(ShareError):
        stitch(m, tmp_path / "parts", tmp_path / "rebuilt.gguf")


def test_no_partial_file_is_left_behind_after_a_failed_stitch(big, tmp_path):
    m = split(big, tmp_path / "parts", part_size=1000)
    (tmp_path / "parts" / m.parts[3].name).unlink()
    with pytest.raises(ShareError):
        stitch(m, tmp_path / "parts", tmp_path / "out.gguf")
    assert list(tmp_path.glob("*.stitching")) == []


# ── resumability ──────────────────────────────────────────────────────────────
def test_missing_parts_from_a_listing():
    m = _manifest()
    have = {"f.bin.part0", "f.bin.part2"}
    assert [p.name for p in missing_parts(m, present=have)] == \
        ["f.bin.part1", "f.bin.part3"]


def test_missing_parts_from_a_directory(big, tmp_path):
    m = split(big, tmp_path / "parts", part_size=1000)
    (tmp_path / "parts" / m.parts[4].name).unlink()
    assert [p.name for p in missing_parts(m, part_dir=tmp_path / "parts")] == \
        [m.parts[4].name]


def test_nothing_missing_when_everything_is_there(big, tmp_path):
    m = split(big, tmp_path / "parts", part_size=1000)
    assert missing_parts(m, part_dir=tmp_path / "parts") == []


# ── the real-world manifest this replaces ─────────────────────────────────────
def test_it_reproduces_the_hand_written_manifest_it_is_meant_to_retire():
    """The served 27B weight file is 3803452480 bytes in three parts, and that
    part table was hand-written into a serving proxy. Deriving it must give the
    same answer, or migrating to this would silently re-slice a live artifact."""
    parts = plan_parts(3_803_452_480, part_size=1_900_000_000,
                       name="Bonsai-27B-Q1_0.gguf")
    assert [p.size for p in parts] == [1_900_000_000, 1_900_000_000, 3_452_480]
    assert [p.name for p in parts] == [
        "Bonsai-27B-Q1_0.gguf.part0",
        "Bonsai-27B-Q1_0.gguf.part1",
        "Bonsai-27B-Q1_0.gguf.part2",
    ]
