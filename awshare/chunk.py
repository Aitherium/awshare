"""Publish a file that is larger than the place you are putting it.

Every artifact host has a per-object cap — a GitHub release asset stops at
2 GiB, GitHub Pages at 100 MB — and model weights, disk images and datasets go
straight past them. The usual answer is to split the file, upload the parts, and
then hand-maintain a list of part sizes wherever the file is served from.

THE HAND-MAINTAINED LIST IS THE BUG. Splitting is easy and reversible; the part
list is neither, because two copies of it have to agree forever. A re-split with
a different part size, or a re-upload of one slice, changes a number in one copy
and not the other — and the serving side then stitches the wrong byte ranges
together. That does not fail loudly. It produces a file of plausible length full
of misaligned content, which the consumer discovers as "corrupt" long after the
transfer, with nothing pointing at the split.

So: ONE producer. `split()` writes the parts AND the manifest that describes
them, `stitch()` and `resolve_range()` read only that manifest, and nothing
anywhere re-derives a part boundary from a remembered part size.

WHAT IS DELIBERATELY NOT HERE — the same rule as `bundle.py`. No upload
transport. `split()` writes parts to a directory; whether they then go to a
release, an object store or a mounted share is the caller's business. Baking one
transport in is how a portable package acquires a dependency on somebody's
fleet.

`resolve_range()` is here for the other side of that boundary: a server that
wants to answer a normal HTTP Range request over the virtual whole file needs to
know which parts to read and which sub-range of each. That is arithmetic over
the manifest, it is easy to get subtly wrong at the boundaries, and it should
exist once.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

from .store import ShareError, VerificationFailedError, atomic_write, digest_file

CHUNK_MANIFEST_VERSION = 1
CHUNK_MANIFEST_SUFFIX = ".awchunk.json"

#: Default part size. Just under GitHub's 2 GiB release-asset cap, with room for
#: the multipart overhead their uploader adds — 2 GiB exactly is rejected.
DEFAULT_PART_SIZE = 1_900_000_000

#: Read/write granularity. Parts are far larger than memory should ever hold.
IO_CHUNK = 8 * 1024 * 1024


@dataclass
class Part:
    name: str
    index: int
    offset: int
    size: int
    digest: str = ""

    def as_json(self) -> dict:
        return asdict(self)


@dataclass
class ChunkManifest:
    """What the parts are, in the only place that says so."""

    version: int
    name: str
    total_bytes: int
    part_size: int
    digest: str = ""          # of the WHOLE file, when it was computed
    parts: List[Part] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "total_bytes": self.total_bytes,
            "part_size": self.part_size,
            "digest": self.digest,
            "parts": [p.as_json() for p in self.parts],
        }

    @classmethod
    def from_json(cls, doc: dict) -> "ChunkManifest":
        if doc.get("version") != CHUNK_MANIFEST_VERSION:
            raise ShareError(
                f"unsupported chunk manifest version {doc.get('version')!r}")
        parts = [Part(**p) for p in doc.get("parts") or []]
        if not parts:
            raise ShareError("chunk manifest lists no parts")
        m = cls(version=doc["version"], name=doc["name"],
                total_bytes=int(doc["total_bytes"]),
                part_size=int(doc.get("part_size") or DEFAULT_PART_SIZE),
                digest=doc.get("digest") or "", parts=parts)
        m.validate()
        return m

    def validate(self) -> None:
        """Refuse a manifest that cannot describe a real file.

        Checked on every load rather than only at write time: the manifest
        travels separately from the parts, and a truncated or hand-edited one is
        exactly the failure this module exists to make impossible.
        """
        expected = 0
        for i, p in enumerate(self.parts):
            if p.index != i:
                raise ShareError(f"{self.name}: part {p.name} is out of order")
            if p.offset != expected:
                raise ShareError(
                    f"{self.name}: part {p.name} starts at {p.offset}, "
                    f"expected {expected} — the parts do not tile the file")
            if p.size <= 0:
                raise ShareError(f"{self.name}: part {p.name} has size {p.size}")
            expected += p.size
        if expected != self.total_bytes:
            raise ShareError(
                f"{self.name}: parts sum to {expected}, manifest says "
                f"{self.total_bytes} — stitching this would produce a wrong file")

    def manifest_name(self) -> str:
        return self.name + CHUNK_MANIFEST_SUFFIX


def plan_parts(total_bytes: int, part_size: int = DEFAULT_PART_SIZE,
               name: str = "file") -> List[Part]:
    """The part table for a file of this size. Pure — no I/O.

    Separated from `split()` so the arithmetic can be tested against sizes no
    test could ever write to disk, and so an uploader can plan its work (and
    skip parts already uploaded) without touching the source file.
    """
    if total_bytes <= 0:
        raise ShareError(f"cannot plan parts for a {total_bytes}-byte file")
    if part_size <= 0:
        raise ShareError(f"part_size must be positive, got {part_size}")
    parts: List[Part] = []
    offset = 0
    idx = 0
    while offset < total_bytes:
        size = min(part_size, total_bytes - offset)
        parts.append(Part(name=f"{name}.part{idx}", index=idx,
                          offset=offset, size=size))
        offset += size
        idx += 1
    return parts


def resolve_range(manifest: ChunkManifest, start: int, end: int) -> List[tuple]:
    """Which parts serve bytes [start, end], and which slice of each.

    Returns [(part, sub_start, sub_end)] with sub-ranges INCLUSIVE and relative
    to each part. This is what a range-serving proxy needs, and it is the piece
    most likely to be quietly wrong: an off-by-one at a part boundary yields a
    response of exactly the right length with one byte of the wrong content, so
    the Content-Length checks out and the file is ruined.
    """
    if start < 0 or end < start or end >= manifest.total_bytes:
        raise ShareError(
            f"range {start}-{end} is not inside 0-{manifest.total_bytes - 1}")
    out = []
    for p in manifest.parts:
        p_end = p.offset + p.size - 1
        if p_end < start or p.offset > end:
            continue
        out.append((p, max(0, start - p.offset), min(p.size - 1, end - p.offset)))
    return out


def split(path: Path, out_dir: Path, part_size: int = DEFAULT_PART_SIZE,
          compute_digests: bool = True) -> ChunkManifest:
    """Write `path` as parts in `out_dir`, plus the manifest describing them.

    The manifest is written LAST, after every part is on disk. A manifest that
    exists is therefore a promise that its parts do too — the reverse order
    would leave a window in which a consumer reads a manifest naming parts that
    are not there yet, which looks identical to parts that were lost.
    """
    path = Path(path)
    out_dir = Path(out_dir)
    if not path.is_file():
        raise ShareError(f"not a file: {path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    total = path.stat().st_size
    parts = plan_parts(total, part_size, name=path.name)

    with open(path, "rb") as src:
        for p in parts:
            dest = out_dir / p.name
            remaining = p.size
            with open(dest, "wb") as dst:
                while remaining:
                    buf = src.read(min(IO_CHUNK, remaining))
                    if not buf:
                        raise ShareError(
                            f"{path.name}: source ended {remaining} bytes early "
                            f"while writing {p.name} — the file changed under us")
                    dst.write(buf)
                    remaining -= len(buf)
            if compute_digests:
                p.digest = digest_file(dest)

    manifest = ChunkManifest(
        version=CHUNK_MANIFEST_VERSION, name=path.name, total_bytes=total,
        part_size=part_size,
        digest=digest_file(path) if compute_digests else "",
        parts=parts)
    manifest.validate()
    atomic_write(out_dir / manifest.manifest_name(),
                 json.dumps(manifest.as_json(), indent=2).encode("utf-8"))
    return manifest


def load_manifest(path: Path) -> ChunkManifest:
    return ChunkManifest.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def iter_stitched(manifest: ChunkManifest, part_dir: Path,
                  verify_parts: bool = True) -> Iterator[bytes]:
    """Yield the whole file's bytes, in order, from its parts.

    Streams. Never holds a part in memory — these are gigabytes, and a stitcher
    that buffers is one that works in a test and dies on the real artifact.
    """
    part_dir = Path(part_dir)
    for p in manifest.parts:
        src = part_dir / p.name
        if not src.is_file():
            raise ShareError(f"{manifest.name}: missing part {p.name}")
        actual = src.stat().st_size
        if actual != p.size:
            raise VerificationFailedError(
                f"{manifest.name}: part {p.name} is {actual} bytes, manifest "
                f"says {p.size} — refusing to stitch a misaligned file")
        if verify_parts and p.digest:
            got = digest_file(src)
            if got != p.digest:
                raise VerificationFailedError(
                    f"{manifest.name}: part {p.name} digest {got} != {p.digest}")
        with open(src, "rb") as f:
            while True:
                buf = f.read(IO_CHUNK)
                if not buf:
                    break
                yield buf


def stitch(manifest: ChunkManifest, part_dir: Path, dest: Path,
           verify_parts: bool = True) -> Path:
    """Reassemble the file and verify the result against the manifest.

    Writes to a temporary neighbour and renames only once the whole-file size —
    and digest, when the manifest carries one — check out. A partial or wrong
    stitch must never appear under the real name: something else will open it,
    and a file that exists is assumed to be finished.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".stitching")
    try:
        with open(tmp, "wb") as out:
            for buf in iter_stitched(manifest, part_dir, verify_parts=verify_parts):
                out.write(buf)
        got = tmp.stat().st_size
        if got != manifest.total_bytes:
            raise VerificationFailedError(
                f"{manifest.name}: stitched {got} bytes, manifest says "
                f"{manifest.total_bytes}")
        if manifest.digest:
            actual = digest_file(tmp)
            if actual != manifest.digest:
                raise VerificationFailedError(
                    f"{manifest.name}: stitched digest {actual} != {manifest.digest}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


def missing_parts(manifest: ChunkManifest, present: Optional[set] = None,
                  part_dir: Optional[Path] = None) -> List[Part]:
    """Parts not yet uploaded/downloaded — the resumable half.

    Takes either a set of names already at the destination (what a release
    listing gives you) or a directory to look in. An upload of a 90 GB artifact
    WILL be interrupted; without this the only options are to start again or to
    guess, and guessing at which slice is missing is how a hole gets published.
    """
    if present is None:
        if part_dir is None:
            raise ShareError("pass present= or part_dir=")
        d = Path(part_dir)
        present = {p.name for p in d.glob("*.part*")} if d.is_dir() else set()
    return [p for p in manifest.parts if p.name not in present]
