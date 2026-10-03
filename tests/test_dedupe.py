"""awshare.dedupe: every copy stays complete, unchanged bytes are stored once."""

import os
import time
from pathlib import Path

import pytest

from awshare.store import ShareError, VerificationFailedError
from awshare import dedupe as dd


def _tree(root: Path, files: dict) -> Path:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _ino(p: Path) -> int:
    return os.stat(p).st_ino


# ----------------------------------------------------------------------------- link_tree
def test_link_tree_links_unchanged_and_copies_appended(tmp_path):
    src = _tree(tmp_path / "src", {"day1.jsonl": b"a" * 100, "today.jsonl": b"b" * 10})
    b1 = dd.link_tree(src, tmp_path / "b1")
    assert b1["linked"] == 0 and b1["files"] == 2
    time.sleep(0.05)
    with open(src / "today.jsonl", "ab") as f:
        f.write(b"b" * 5)
    b2 = dd.link_tree(src, tmp_path / "b2", link_dest=tmp_path / "b1")
    assert b2["linked"] == 1 and b2["linked_bytes"] == 100 and b2["files"] == 2
    assert _ino(tmp_path / "b2" / "day1.jsonl") == _ino(tmp_path / "b1" / "day1.jsonl")
    assert (tmp_path / "b2" / "today.jsonl").read_bytes() == b"b" * 15
    assert (tmp_path / "b1" / "today.jsonl").read_bytes() == b"b" * 10


def test_link_tree_never_links_a_same_size_rewrite(tmp_path):
    src = _tree(tmp_path / "s", {"x.bin": b"1" * 8})
    dd.link_tree(src, tmp_path / "b1")
    (src / "x.bin").write_bytes(b"2" * 8)
    later = os.stat(tmp_path / "b1" / "x.bin").st_mtime + 5
    os.utime(src / "x.bin", (later, later))
    r = dd.link_tree(src, tmp_path / "b2", link_dest=tmp_path / "b1")
    assert r["linked"] == 0 and (tmp_path / "b2" / "x.bin").read_bytes() == b"2" * 8


def test_link_tree_refuses_a_missing_source(tmp_path):
    with pytest.raises(ShareError):
        dd.link_tree(tmp_path / "nope", tmp_path / "d")


# ----------------------------------------------------------------------------- dedupe_tree
def test_dedupe_dry_run_reports_and_changes_nothing(tmp_path):
    a = _tree(tmp_path / "a", {"f": b"same" * 100, "g": b"diff1"})
    b = _tree(tmp_path / "b", {"f": b"same" * 100, "g": b"diff2"})
    r = dd.dedupe_tree([a, b], dry_run=True)
    assert r["linked"] == 1 and r["saved_bytes"] == 400 and r["dry_run"]
    assert _ino(a / "f") != _ino(b / "f")


def test_dedupe_links_identical_content_only(tmp_path):
    a = _tree(tmp_path / "a", {"f": b"same" * 100, "g": b"diff1"})
    b = _tree(tmp_path / "b", {"f": b"same" * 100, "g": b"diff2"})
    r = dd.dedupe_tree([a, b])
    assert r["linked"] == 1 and not r["errors"]
    assert _ino(a / "f") == _ino(b / "f")
    assert _ino(a / "g") != _ino(b / "g")
    assert (b / "g").read_bytes() == b"diff2"
    assert dd.dedupe_tree([a, b])["linked"] == 0  # idempotent


def test_dedupe_same_size_different_bytes_is_not_linked(tmp_path):
    a = _tree(tmp_path / "a", {"f": b"AAAA"})
    b = _tree(tmp_path / "b", {"f": b"BBBB"})
    assert dd.dedupe_tree([a, b])["linked"] == 0


# ----------------------------------------------------------------------------- object store
def test_snapshot_stores_unchanged_bytes_once_and_restores_verified(tmp_path):
    src = _tree(tmp_path / "src", {"old/day1": b"x" * 1000, "new": b"y" * 10})
    store = tmp_path / "store"
    m1 = dd.snapshot_tree(src, store, "s1")
    assert m1["new_objects"] == 2 and m1["new_bytes"] == 1010
    (src / "new").write_bytes(b"y" * 20)
    m2 = dd.snapshot_tree(src, store, "s2", previous=m1)
    assert m2["new_objects"] == 1 and m2["new_bytes"] == 20  # day1 not stored again
    out = dd.restore_tree(dd.load_tree_manifest(store / "s1.awtree.json"), store, tmp_path / "r1")
    assert out["files"] == 2
    assert (tmp_path / "r1" / "new").read_bytes() == b"y" * 10
    assert (tmp_path / "r1" / "old" / "day1").read_bytes() == b"x" * 1000


def test_restore_refuses_a_corrupt_object(tmp_path):
    src = _tree(tmp_path / "src", {"f": b"good"})
    store = tmp_path / "store"
    m = dd.snapshot_tree(src, store, "s1")
    obj = dd.ObjectStore(store).path(m["files"]["f"]["sha256"])
    obj.write_bytes(b"evil")
    with pytest.raises(VerificationFailedError):
        dd.restore_tree(m, store, tmp_path / "r")


def test_restore_refuses_a_traversal_name(tmp_path):
    src = _tree(tmp_path / "src", {"f": b"x"})
    store = tmp_path / "store"
    m = dd.snapshot_tree(src, store, "s1")
    m["files"] = {"../escape": m["files"]["f"]}
    with pytest.raises(ShareError):
        dd.restore_tree(m, store, tmp_path / "r")


def test_gc_keeps_shared_objects_and_drops_orphans(tmp_path):
    src = _tree(tmp_path / "src", {"keep": b"k" * 50, "gone": b"g" * 50})
    store = tmp_path / "store"
    dd.snapshot_tree(src, store, "s1")
    (src / "gone").unlink()
    dd.snapshot_tree(src, store, "s2")
    dd.drop_tree(store, "s1")
    m2 = dd.load_tree_manifest(store / "s2.awtree.json")
    assert dd.restore_tree(m2, store, tmp_path / "r")["files"] == 1
    assert len(list((store / "objects").glob("*/*"))) == 1


def test_gc_refuses_when_a_manifest_is_unreadable(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "bad.awtree.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ShareError):
        dd.gc(store)


def _unreadable(monkeypatch, bad_name):
    """Make one file raise like WinError 1920 (a reparse point the system cannot open)."""
    real_stat = Path.stat
    real_os_stat = os.stat

    def fake_stat(self, *a, **k):
        if self.name == bad_name:
            raise OSError(1920, "The file cannot be accessed by the system")
        return real_stat(self, *a, **k)

    def fake_os_stat(p, *a, **k):
        if os.path.basename(os.fspath(p)) == bad_name:
            raise OSError(1920, "The file cannot be accessed by the system")
        return real_os_stat(p, *a, **k)
    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(os, "stat", fake_os_stat)


def test_an_unreadable_file_is_counted_not_a_crash(tmp_path, monkeypatch):
    """2026-10-03: one node_modules reparse point killed a dry run over a whole drive."""
    a = _tree(tmp_path / "a", {"f": b"same" * 100, "bad": b"x"})
    b = _tree(tmp_path / "b", {"f": b"same" * 100})
    _unreadable(monkeypatch, "bad")
    # Whether the stat error surfaces or is_file() swallows it differs by Python
    # version (3.14 returns False); the guarantee is the same: no crash, the good
    # file is still handled, and the unreadable one is never copied or linked.
    r = dd.dedupe_tree([a, b], dry_run=True)
    assert r["linked"] == 1
    lt = dd.link_tree(a, tmp_path / "copy")
    assert lt["files"] == 1 and "bad" not in os.listdir(tmp_path / "copy")
    m = dd.snapshot_tree(a, tmp_path / "store", "s1")
    assert set(m["files"]) == {"f"}
