"""RemoteObjectStore: the object-store contract over a blob target that is not this disk."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from awshare import RemoteObjectStore, restore_tree, snapshot_tree
from awshare.store import ShareError, VerificationFailedError


class FakeTarget:
    """An in-memory blob service: what put/stat/read would answer."""

    def __init__(self) -> None:
        self.blobs: dict = {}
        self.uploads: list = []
        self.stat_calls = 0
        self.down = False
        self.serve_rot = False
        self.lie_on_stat = False

    def upload_verified(self, rel, data, sha256, metadata=None):
        if self.down:
            raise RuntimeError("service down")
        self.blobs[rel] = bytes(data)
        self.uploads.append(rel)
        return {"path": rel, "bytes": len(data)}

    def stat_or_none(self, rel):
        self.stat_calls += 1
        if self.down:
            raise RuntimeError("service down")
        if rel not in self.blobs:
            return None
        data = self.blobs[rel]
        h = "0" * 64 if self.lie_on_stat else hashlib.sha256(data).hexdigest()
        return {"size": len(data), "hash": h}

    def get(self, rel):
        if rel not in self.blobs:
            raise RuntimeError("404")
        return (b"rot" if self.serve_rot else b"") + self.blobs[rel]


def _file(tmp_path: Path, data: bytes, name: str = "f.bin") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_put_is_idempotent_and_keys_are_content_addressed(tmp_path):
    t = FakeTarget()
    s = RemoteObjectStore(t, "__t__/acme/objects")
    sha = hashlib.sha256(b"hello").hexdigest()
    assert s.put(_file(tmp_path, b"hello")) == (sha, True)
    assert t.uploads == [f"__t__/acme/objects/{sha[:2]}/{sha}"]
    again = RemoteObjectStore(t, "__t__/acme/objects")  # a new process: no memo
    assert again.put(_file(tmp_path, b"hello", "g.bin")) == (sha, False)
    assert len(t.uploads) == 1


def test_put_refuses_a_file_that_is_not_the_digest_it_was_given(tmp_path):
    s = RemoteObjectStore(FakeTarget(), "p")
    with pytest.raises(VerificationFailedError):
        s.put(_file(tmp_path, b"x"), digest="0" * 64)


def test_has_is_false_when_absent_and_raises_on_an_outage():
    t = FakeTarget()
    s = RemoteObjectStore(t, "p")
    assert s.has("a" * 64) is False
    t.down = True
    with pytest.raises(RuntimeError):
        s.has("b" * 64)


def test_has_reports_a_key_holding_other_bytes_as_absent(tmp_path):
    t = FakeTarget()
    s = RemoteObjectStore(t, "p")
    sha, _ = s.put(_file(tmp_path, b"real"))
    t.lie_on_stat = True
    assert RemoteObjectStore(t, "p").has(sha) is False


def test_materialize_verifies_and_writes_nothing_on_a_mismatch(tmp_path):
    t = FakeTarget()
    s = RemoteObjectStore(t, "p")
    sha, _ = s.put(_file(tmp_path, b"payload"))
    out = tmp_path / "out" / "x.bin"
    s.materialize(sha, out)
    assert out.read_bytes() == b"payload"
    t.serve_rot = True
    bad = tmp_path / "out" / "y.bin"
    with pytest.raises(VerificationFailedError):
        s.materialize(sha, bad)
    assert not bad.exists()
    assert list((tmp_path / "out").glob(".awshare-remote-*")) == []


@pytest.mark.parametrize("prefix", ["", "/", "a/../b", "../x", "~root", "a//b", "c:/x"])
def test_a_prefix_that_could_escape_is_refused(prefix):
    with pytest.raises(ShareError):
        RemoteObjectStore(FakeTarget(), prefix)


def test_a_bad_digest_is_refused():
    with pytest.raises(ShareError):
        RemoteObjectStore(FakeTarget(), "p").key("../../etc")


def test_oversized_objects_are_refused_not_half_sent(tmp_path):
    t = FakeTarget()
    s = RemoteObjectStore(t, "p", max_object_bytes=4)
    with pytest.raises(ShareError):
        s.put(_file(tmp_path, b"12345"))
    assert t.uploads == []


def test_snapshot_tree_and_restore_tree_through_a_remote_store(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.txt").write_bytes(b"a" * 1000)
    (src / "b.txt").write_bytes(b"b")
    (src / "dup.txt").write_bytes(b"b")  # same bytes: one object
    t = FakeTarget()
    meta = tmp_path / "meta"
    m1 = snapshot_tree(src, meta, "s1", object_store=RemoteObjectStore(t, "p"))
    assert m1["new_objects"] == 2 and len(t.uploads) == 2
    assert not (meta / "objects").exists()
    m2 = snapshot_tree(src, meta, "s2", previous=m1, object_store=RemoteObjectStore(t, "p"))
    assert m2["new_objects"] == 0 and len(t.uploads) == 2  # dedupe preserved
    dest = tmp_path / "dest"
    restore_tree(m2, meta, dest, object_store=RemoteObjectStore(t, "p"))
    assert (dest / "sub" / "a.txt").read_bytes() == b"a" * 1000
    assert (dest / "dup.txt").read_bytes() == b"b"


def test_restore_tree_through_a_remote_store_refuses_rot(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"a")
    t = FakeTarget()
    m = snapshot_tree(src, tmp_path / "meta", "s1", object_store=RemoteObjectStore(t, "p"))
    t.serve_rot = True
    with pytest.raises(VerificationFailedError):
        restore_tree(m, tmp_path / "meta", tmp_path / "dest",
                     object_store=RemoteObjectStore(t, "p"))


def test_put_bytes_and_get_bytes_round_trip():
    t = FakeTarget()
    s = RemoteObjectStore(t, "p")
    sha, new = s.put_bytes(b'{"manifest": 1}')
    assert new and s.put_bytes(b'{"manifest": 1}') == (sha, False)
    assert s.get_bytes(sha) == b'{"manifest": 1}'
    t.serve_rot = True
    with pytest.raises(VerificationFailedError):
        RemoteObjectStore(t, "p").get_bytes(sha)
