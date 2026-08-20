"""Publish a directory as one fetchable artifact, and fetch it back verified.

A bundle is a tar.gz plus a small JSON manifest naming its digest, its size and
the files inside it. The manifest is what a consumer fetches first and the only
thing they need to decide whether the archive they are about to open is the one
that was published.

WHY THIS PAIRS WITH awseal RATHER THAN REPLACING IT

They answer different questions and either alone is a half-answer:

    awshare  — are these the bytes that were published?   (integrity)
    awseal   — who published them?                        (provenance)

A digest proves an artifact was not corrupted in transit. It proves nothing
about who produced it, because whoever produced the bytes also produced the
digest. `publish(..., seal=True)` seals first and then bundles, so the seal
travels INSIDE the archive and a consumer can check both from one download.

WHAT IS DELIBERATELY NOT HERE

No upload transport. `publish` writes a bundle to a directory; whether that
directory is a local path, a mounted share, an S3 sync target or a Strata
namespace is the caller's business. Baking one transport in is how a portable
package acquires a dependency on our fleet, and the point of extracting this
was to remove exactly that.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import (
    ShareError,
    VerificationFailedError,
    atomic_write,
    digest_file,
    safe_member_path,
)

MANIFEST_VERSION = 1
MANIFEST_SUFFIX = ".awshare.json"
ARCHIVE_SUFFIX = ".tar.gz"

#: Refuse to unpack an archive that expands beyond this without an explicit
#: override. A 1 KB archive that expands to 100 GB is a zip bomb, and "the
#: download was small" is exactly why nobody notices until the disk is full.
DEFAULT_MAX_EXPANDED = 32 * 1024 * 1024 * 1024


@dataclass
class Manifest:
    version: int
    name: str
    digest: str
    size: int
    files: List[str]
    created: str
    sealed: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version, "name": self.name, "digest": self.digest,
            "size": self.size, "files": self.files, "created": self.created,
            "sealed": self.sealed, "meta": self.meta,
        }


def publish(root: Path, out_dir: Path, *, name: Optional[str] = None,
            seal: bool = False, key_path: Optional[Path] = None,
            meta: Optional[Dict[str, Any]] = None) -> Manifest:
    """Bundle `root` into `out_dir`. Returns the manifest that was written."""
    if not root.is_dir():
        raise ShareError(f"{root} is not a directory")
    label = name or root.name

    if seal:
        # Seal BEFORE archiving so the seal is inside the artifact. Sealing the
        # archive instead would let a consumer verify the tarball and still not
        # know whether its CONTENTS match what the publisher signed.
        try:
            import awseal
        except ImportError as exc:
            raise ShareError(
                "seal=True needs the `awseal` package. Refusing to publish an "
                "UNSEALED bundle under a request to seal it — that would be a "
                "silent downgrade of exactly the property being asked for"
            ) from exc
        s = awseal.sign(root, key_path=key_path, subject=label)
        awseal.write(s, root)

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{label}{ARCHIVE_SUFFIX}"

    files: List[str] = []
    # `filter` normalises ownership and timestamps. Without it the archive
    # carries the publisher's uid/gid and mtimes, which makes two builds of
    # identical content produce different bytes — and a content-addressed store
    # then treats them as different artifacts.
    def _norm(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mtime = 0
        return ti

    with tarfile.open(archive, "w:gz") as tf:
        for p in sorted(root.rglob("*"), key=lambda x: x.relative_to(root).as_posix()):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            files.append(rel)
            tf.add(p, arcname=rel, filter=_norm)
    if not files:
        archive.unlink(missing_ok=True)
        raise ShareError(f"{root} contains no files — refusing to publish an "
                         f"empty bundle, which fetches and verifies perfectly "
                         f"while containing nothing")

    manifest = Manifest(
        version=MANIFEST_VERSION, name=label, digest=digest_file(archive),
        size=archive.stat().st_size, files=files,
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sealed=bool(seal), meta=dict(meta or {}),
    )
    atomic_write(out_dir / f"{label}{MANIFEST_SUFFIX}",
                 json.dumps(manifest.to_dict(), indent=2,
                            sort_keys=True).encode("utf-8"))
    return manifest


def load_manifest(path: Path) -> Manifest:
    """Read a manifest, refusing any shape this version does not fully know."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShareError(f"cannot read manifest {path}: {exc}") from exc
    if d.get("version") != MANIFEST_VERSION:
        raise ShareError(
            f"{path} is manifest version {d.get('version')!r}, this is "
            f"{MANIFEST_VERSION}. Refusing rather than guessing at a shape whose "
            f"fields may not mean what they appear to")
    for req in ("name", "digest", "size", "files", "created"):
        if req not in d:
            raise ShareError(f"{path} is missing required field {req!r}")
    if not d["files"]:
        raise ShareError(f"{path} lists no files — refusing")
    return Manifest(version=d["version"], name=d["name"], digest=d["digest"],
                    size=int(d["size"]), files=list(d["files"]),
                    created=d["created"], sealed=bool(d.get("sealed")),
                    meta=d.get("meta") or {})


def fetch(manifest_path: Path, dest: Path, *,
          archive_path: Optional[Path] = None,
          expect_key: Optional[str] = None,
          max_expanded: int = DEFAULT_MAX_EXPANDED) -> Dict[str, Any]:
    """Fetch and unpack a bundle, verifying before anything is written.

    Order is the whole design: digest first, expansion budget second, member
    paths third, and only then any bytes on disk. Each check exists because the
    one before it cannot see the next failure.
    """
    manifest = load_manifest(manifest_path)
    archive = archive_path or manifest_path.with_name(
        manifest.name + ARCHIVE_SUFFIX)
    if not archive.is_file():
        raise ShareError(f"archive {archive} not found beside its manifest")

    got = digest_file(archive)
    if got != manifest.digest:
        raise VerificationFailedError(
            f"digest mismatch: manifest says {manifest.digest[:16]}…, archive "
            f"is {got[:16]}…. Refusing to unpack — this is the check, and "
            f"unpacking first to 'see what is in there' defeats it")

    dest.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    with tarfile.open(archive, "r:gz") as tf:
        total = 0
        members = tf.getmembers()
        for m in members:
            if m.isdir():
                continue
            if not m.isfile():
                # Symlinks, devices, fifos. A symlink inside an archive is the
                # classic escape: it passes every path check as a NAME and
                # points anywhere once created.
                raise ShareError(
                    f"refusing non-regular archive member {m.name!r} "
                    f"({'symlink' if m.issym() or m.islnk() else 'special'})")
            total += m.size
            if total > max_expanded:
                raise ShareError(
                    f"archive expands past {max_expanded} bytes — refusing. A "
                    f"small download that expands without bound fills the disk "
                    f"long before anyone reads a log line")
            target = safe_member_path(dest, m.name)
            src = tf.extractfile(m)
            if src is None:
                raise ShareError(f"cannot read member {m.name!r}")
            atomic_write(target, src.read())
            written.append(m.name)

    missing = sorted(set(manifest.files) - set(written))
    extra = sorted(set(written) - set(manifest.files))
    if missing or extra:
        raise VerificationFailedError(
            f"archive contents disagree with the manifest "
            f"(missing={missing[:5]}, unexpected={extra[:5]}). The digest "
            f"matched, so this is a manifest that describes a DIFFERENT set of "
            f"files to the one it names — worse than corruption, because every "
            f"integrity check passes")

    result: Dict[str, Any] = {"name": manifest.name, "files": len(written),
                              "sealed": manifest.sealed, "verified": True,
                              "seal": None}
    if manifest.sealed or expect_key is not None:
        try:
            import awseal
        except ImportError as exc:
            raise ShareError(
                "this bundle is sealed but `awseal` is not installed, so its "
                "provenance cannot be checked. Reporting it as fetched-and-fine "
                "would discard the property it was published with"
            ) from exc
        result["seal"] = awseal.verify(dest, expect_key=expect_key)
        if not result["seal"]["ok"]:
            raise VerificationFailedError(
                f"bundle unpacked but its SEAL failed: {result['seal']}. The "
                f"bytes arrived intact and are not from who you expected")
    return result
