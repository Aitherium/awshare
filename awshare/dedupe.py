"""Keep many copies of a directory while paying for its bytes once.

WHY THIS EXISTS (measured 2026-10-03)

Every platform backup was a FULL copy: 35 GB a run, 28 GB of it dated training
files that never change after their day. The mirror of those backups on a 3.7 TB
node reached 5.2 MB free. Run over that mirror, content-verified hard-linking freed
1.17 TiB — 66,485 files that existed as identical byte-for-byte copies — and
deleted nothing. The bytes were never the problem; storing each of them forty
times was.

THREE TOOLS, ONE RULE

    link_tree(src, dest, link_dest)   copy a tree, hard-linking every file that is
                                      unchanged since `link_dest` (rsync --link-dest)
    dedupe_tree(roots)                hard-link identical files that already exist
                                      (content-verified, dry-run first)
    snapshot_tree / restore_tree      a content-addressed object store: each file
                                      stored once by its sha256, a snapshot is a
                                      manifest of names -> digests

The rule all three keep: **every copy stays complete.** A linked backup is still a
whole directory that restores, mirrors and diffs exactly like a full copy; a tree
snapshot restores every file and verifies every digest on the way out. Dedupe is a
storage decision, never a change in what a backup contains.

WHAT A HARD LINK CANNOT DO, SAID ONCE

Two names for one inode share one set of bytes: writing through either changes
both. That is correct for backups (never written after they land) and wrong for a
working tree. These tools are for trees that are written once and read many times.
`link_tree` links only files whose size matches and that have NOT been modified
since the previous copy was written — a file rewritten in place to the same size
is copied, not linked.

STDLIB ONLY. A backup tool that needs a dependency to restore is a backup tool that
fails on the machine that just lost its environment.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .store import ShareError, VerificationFailedError, atomic_write, digest_file, safe_member_path

TREE_MANIFEST_VERSION = 1
TREE_MANIFEST_SUFFIX = ".awtree.json"
_BUF = 1024 * 1024


# ----------------------------------------------------------------------------- link_tree
def _copy(src: str, dst: str) -> int:
    n = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            b = fin.read(_BUF)
            if not b:
                break
            fout.write(b)
            n += len(b)
    return n


def unchanged_since(src: os.stat_result, prev: os.stat_result) -> bool:
    """Is `prev` (written by an earlier copy) still a faithful copy of `src`?"""
    return src.st_size == prev.st_size and src.st_mtime <= prev.st_mtime


def link_tree(src: Path, dest: Path, link_dest: Optional[Path] = None) -> Dict[str, object]:
    """Copy `src` to `dest`, hard-linking files unchanged since `link_dest`.

    Returns {"files", "bytes", "linked", "linked_bytes", "link_failures"}. A link
    that the filesystem refuses falls back to a copy and is COUNTED, so "links
    unsupported here" shows up as a number rather than as a backup that quietly
    stopped being incremental.
    """
    src, dest = Path(src), Path(dest)
    if not src.is_dir():
        raise ShareError(f"link_tree source is not a directory: {src}")
    prev_root = Path(link_dest) if link_dest and Path(link_dest).is_dir() else None
    out = {"files": 0, "bytes": 0, "linked": 0, "linked_bytes": 0, "link_failures": 0}
    for root, dirs, names in os.walk(src, followlinks=False):
        dirs.sort()
        rel_root = os.path.relpath(root, src)
        for name in sorted(names):
            s = os.path.join(root, name)
            if os.path.islink(s) or not os.path.isfile(s):
                continue
            rel = os.path.normpath(os.path.join(rel_root, name))
            d = dest / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            st = os.stat(s)
            p = prev_root / rel if prev_root else None
            if p is not None and p.is_file() and unchanged_since(st, p.stat()):
                try:
                    os.link(p, d)
                    out["files"] += 1
                    out["bytes"] += st.st_size
                    out["linked"] += 1
                    out["linked_bytes"] += st.st_size
                    continue
                except OSError:
                    out["link_failures"] += 1
            out["bytes"] += _copy(s, str(d))
            out["files"] += 1
    return out


# ----------------------------------------------------------------------------- dedupe_tree
def _same_inode(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_ino == b.st_ino and a.st_dev == b.st_dev and a.st_ino != 0


def dedupe_tree(roots: Iterable[Path], *, dry_run: bool = False,
                min_size: int = 1) -> Dict[str, object]:
    """Hard-link byte-identical files under `roots` to one inode.

    Candidates are grouped by size, then by sha256; only files whose digests match
    are linked, and each replacement is made by linking to a temp name and renaming
    over the duplicate, so a crash mid-way leaves either the old file or the link,
    never neither. Files on different devices are never linked. Returns
    {"files", "linked", "saved_bytes", "dry_run", "errors"}.
    """
    by_size: Dict[int, List[Path]] = {}
    files = 0
    for r in roots:
        r = Path(r)
        if not r.is_dir():
            raise ShareError(f"dedupe root is not a directory: {r}")
        for root, dirs, names in os.walk(r, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                p = Path(root) / name
                if p.is_symlink() or not p.is_file():
                    continue
                files += 1
                size = p.stat().st_size
                if size >= min_size:
                    by_size.setdefault(size, []).append(p)
    linked, saved, errors = 0, 0, []
    for size, paths in sorted(by_size.items()):
        if len(paths) < 2:
            continue
        by_digest: Dict[str, List[Path]] = {}
        for p in paths:
            try:
                by_digest.setdefault(digest_file(p), []).append(p)
            except ShareError as exc:
                errors.append(str(exc))
        for group in by_digest.values():
            keep = group[0]
            kst = keep.stat()
            for dup in group[1:]:
                dst = dup.stat()
                if _same_inode(kst, dst) or kst.st_dev != dst.st_dev:
                    continue
                if not dry_run:
                    tmp = dup.with_name(f".awshare-link-{os.getpid()}-{dup.name}")
                    try:
                        os.link(keep, tmp)
                        os.replace(tmp, dup)
                    except OSError as exc:
                        errors.append(f"{dup}: {exc}")
                        try:
                            tmp.unlink()
                        except OSError:
                            errors.append(f"{tmp}: stranded temp link")
                        continue
                linked += 1
                saved += size
    return {"files": files, "linked": linked, "saved_bytes": saved,
            "dry_run": dry_run, "errors": errors}


# ----------------------------------------------------------------------------- object store
class ObjectStore:
    """Files stored once by sha256 under `root/objects/ab/<digest>`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"

    def path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ShareError(f"not a sha256 hex digest: {digest!r}")
        return self.objects / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def put(self, src: Path, digest: Optional[str] = None) -> tuple:
        """Store `src`; returns (digest, stored_new). Never overwrites an object."""
        digest = digest or digest_file(Path(src))
        target = self.path(digest)
        if target.is_file():
            return digest, False
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".awshare-obj-")
        os.close(fd)
        try:
            _copy(str(src), tmp)
            got = digest_file(Path(tmp))
            if got != digest:
                raise VerificationFailedError(
                    f"{src} changed while it was stored: expected {digest[:16]}…, "
                    f"got {got[:16]}…")
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest, True

    def materialize(self, digest: str, dst: Path, *, link: bool = False) -> None:
        """Put the object's bytes at `dst`, verified. `link=True` hard-links it
        (read-only trees only — see the module docstring)."""
        src = self.path(digest)
        if not src.is_file():
            raise ShareError(f"object {digest[:16]}… is missing from {self.objects}")
        if digest_file(src) != digest:
            raise VerificationFailedError(f"object {digest[:16]}… is corrupt in the store")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if link:
            try:
                os.link(src, dst)
                return
            except OSError:  # different device or no link support: copy it instead
                link = False
        _copy(str(src), str(dst))


def snapshot_tree(src: Path, store: Path, name: str, *,
                  previous: Optional[Dict[str, object]] = None,
                  meta: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Snapshot `src` into the object store at `store`; write `<name>.awtree.json`.

    A file whose size and mtime match its entry in `previous` reuses that digest
    without re-reading it (the rsync quick check); every other file is hashed. Only
    digests the store does not have yet take space. Returns the manifest, with
    "new_objects" and "new_bytes" saying what this snapshot actually cost.
    """
    src = Path(src)
    if not src.is_dir():
        raise ShareError(f"snapshot source is not a directory: {src}")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ShareError(f"refusing snapshot name {name!r}: it becomes a filename")
    os_ = ObjectStore(store)
    prev_files = dict((previous or {}).get("files") or {})
    files: Dict[str, Dict[str, object]] = {}
    new_objects = new_bytes = total = 0
    for root, dirs, names in os.walk(src, followlinks=False):
        dirs.sort()
        for fname in sorted(names):
            p = Path(root) / fname
            if p.is_symlink() or not p.is_file():
                continue
            rel = p.relative_to(src).as_posix()
            st = p.stat()
            old = prev_files.get(rel) or {}
            digest = old.get("sha256") if (old.get("size") == st.st_size
                                           and old.get("mtime") == st.st_mtime) else None
            if digest and os_.has(str(digest)):
                stored = False
            else:
                digest, stored = os_.put(p)
            files[rel] = {"sha256": digest, "size": st.st_size, "mtime": st.st_mtime,
                          "mode": st.st_mode & 0o777}
            total += st.st_size
            if stored:
                new_objects += 1
                new_bytes += st.st_size
    manifest = {"version": TREE_MANIFEST_VERSION, "name": name, "files": files,
                "total_bytes": total, "new_objects": new_objects, "new_bytes": new_bytes,
                "meta": dict(meta or {})}
    atomic_write(Path(store) / f"{name}{TREE_MANIFEST_SUFFIX}",
                 json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))
    return manifest


def load_tree_manifest(path: Path) -> Dict[str, object]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShareError(f"tree manifest {path} is unreadable: {exc}") from exc
    if d.get("version") != TREE_MANIFEST_VERSION:
        raise ShareError(f"{path} is tree manifest version {d.get('version')!r}, "
                         f"this is {TREE_MANIFEST_VERSION}")
    return d


def restore_tree(manifest: Dict[str, object], store: Path, dest: Path, *,
                 link: bool = False) -> Dict[str, object]:
    """Materialise every file of `manifest` under `dest`, each digest verified.

    Raises on the first missing or corrupt object: a restore that lands most files
    is not a restore. Names are contained with `safe_member_path`.
    """
    os_ = ObjectStore(store)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    n = modes_kept = 0
    for rel, ent in sorted(dict(manifest.get("files") or {}).items()):
        target = safe_member_path(dest, rel)
        os_.materialize(str(ent["sha256"]), target, link=link)
        try:
            os.chmod(target, int(ent.get("mode") or 0o644))
        except OSError:  # a filesystem without POSIX modes keeps its own
            modes_kept += 1
        n += 1
    return {"files": n, "dest": str(dest), "modes_not_applied": modes_kept}


def gc(store: Path, *, dry_run: bool = False) -> Dict[str, object]:
    """Delete objects no `*.awtree.json` in `store` references. Refuses when a
    manifest cannot be read — collecting against a partial view deletes live data."""
    store = Path(store)
    live = set()
    for m in sorted(store.glob(f"*{TREE_MANIFEST_SUFFIX}")):
        for ent in dict(load_tree_manifest(m).get("files") or {}).values():
            live.add(str(ent["sha256"]))
    os_ = ObjectStore(store)
    removed, freed = 0, 0
    if os_.objects.is_dir():
        for p in sorted(os_.objects.glob("*/*")):
            if p.name in live or p.name.startswith("."):
                continue
            freed += p.stat().st_size
            removed += 1
            if not dry_run:
                p.unlink()
    return {"live": len(live), "removed": removed, "freed_bytes": freed, "dry_run": dry_run}


def drop_tree(store: Path, name: str) -> None:
    """Remove one snapshot manifest and collect what only it referenced."""
    (Path(store) / f"{name}{TREE_MANIFEST_SUFFIX}").unlink(missing_ok=True)
    gc(store)


__all__ = ["ObjectStore", "TREE_MANIFEST_SUFFIX", "TREE_MANIFEST_VERSION", "dedupe_tree",
           "drop_tree", "gc", "link_tree", "load_tree_manifest", "restore_tree",
           "snapshot_tree", "unchanged_since"]

