"""The object store, kept somewhere other than this disk.

`ObjectStore` keeps each file once, by its sha256, under a local directory. That
is the right shape for a backup and the wrong place for one: the disk that holds
the only copy is the disk you are protecting against. `RemoteObjectStore` keeps
the same three-call contract -- `has`, `put`, `materialize` -- over any blob
target that can write, stat and read a key, so `snapshot_tree` and
`restore_tree` work unchanged against a storage service.

There is still no transport in this package. The caller hands in the target; this
module only decides what a key is called and what has to be PROVEN before a call
is allowed to return:

- **put proves the write.** The target's `upload_verified` reads the object's
  size (and its sha256, when the service reports one) back before `put` returns.
  "The request returned 200" is a claim the service made, not evidence the bytes
  landed.
- **materialize verifies on the way out.** The fetched bytes are hashed and must
  equal the digest that names them, or nothing is written. Whoever stored the
  object -- and whoever can rewrite it on the far side -- is not trusted to have
  kept it intact.
- **has() is never a guess.** A missing key is `False`; a target that cannot be
  asked raises. A store that answered `False` on an outage would re-upload
  everything, and one that answered `True` would skip bytes it never stored.

The key of an object is `<prefix>/<ab>/<sha256>`. The prefix is the caller's
namespace; a service that isolates tenants by a path segment puts that segment in
the prefix, and this module refuses a prefix that could climb out of it.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Tuple

from .store import ShareError, VerificationFailedError

#: Bytes read into memory for one object. A JSON-over-HTTP blob API carries the
#: whole object in one request, so an unbounded object is an unbounded allocation
#: on both ends. Larger files are refused loudly rather than half-sent; split them
#: with `awshare.split` first.
DEFAULT_MAX_OBJECT_BYTES = 256 * 1024 * 1024

_HEX = frozenset("0123456789abcdef")


class BlobTarget(Protocol):
    """What a remote object store needs from its transport."""

    def upload_verified(self, rel: str, data: bytes, sha256: str,
                        metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Write `data` at `rel` and prove it landed (size, and sha256 if known)."""

    def stat_or_none(self, rel: str) -> Optional[Dict[str, Any]]:
        """The object's metadata, None when it does not exist; raise otherwise."""

    def get(self, rel: str) -> bytes:
        """The object's bytes; raise when it cannot be read."""


def _check_digest(digest: str) -> str:
    d = str(digest or "")
    if len(d) != 64 or any(c not in _HEX for c in d):
        raise ShareError(f"not a sha256 hex digest: {digest!r}")
    return d


def _check_prefix(prefix: str) -> str:
    p = str(prefix or "").replace("\\", "/").strip("/")
    if not p:
        raise ShareError("a remote object store needs a non-empty key prefix")
    for seg in p.split("/"):
        if seg in ("", ".", "..") or seg.startswith("~") or "\x00" in seg or ":" in seg:
            raise ShareError(f"refusing key prefix {prefix!r}: segment {seg!r} could "
                             "escape the namespace it names")
    return p


class RemoteObjectStore:
    """Files stored once by sha256 at `<prefix>/<ab>/<digest>` on a blob target."""

    def __init__(self, target: BlobTarget, prefix: str, *,
                 max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
                 describe: str = "") -> None:
        self.target = target
        self.prefix = _check_prefix(prefix)
        self.max_object_bytes = int(max_object_bytes)
        self._describe = describe or self.prefix
        # Digests this process has proven present: has() on an unchanged file
        # costs one round trip the first time and none after.
        self._known: set = set()

    def __repr__(self) -> str:
        return f"RemoteObjectStore({self._describe})"

    @property
    def location(self) -> str:
        return self._describe

    def key(self, digest: str) -> str:
        d = _check_digest(digest)
        return f"{self.prefix}/{d[:2]}/{d}"

    def has(self, digest: str) -> bool:
        d = _check_digest(digest)
        if d in self._known:
            return True
        st = self.target.stat_or_none(self.key(d))
        if st is None:
            return False
        remote = str(st.get("hash") or st.get("content_hash") or st.get("sha256") or "")
        if remote and remote.lower() != d:
            # The key exists and holds other bytes: report absent so put() rewrites
            # it, never present (that would let a snapshot reference a bad object).
            return False
        self._known.add(d)
        return True

    def put(self, src: Path, digest: Optional[str] = None) -> Tuple[str, bool]:
        """Store `src`; returns (digest, stored_new). An object already present is
        never re-sent."""
        src = Path(src)
        size = src.stat().st_size
        if size > self.max_object_bytes:
            raise ShareError(
                f"{src} is {size} bytes; one remote object is capped at "
                f"{self.max_object_bytes}. Split it (awshare.split) before storing")
        data = src.read_bytes()
        got = hashlib.sha256(data).hexdigest()
        if digest is not None and _check_digest(digest) != got:
            raise VerificationFailedError(
                f"{src} changed while it was stored: expected {digest[:16]}…, "
                f"got {got[:16]}…")
        if self.has(got):
            return got, False
        self.target.upload_verified(self.key(got), data, got,
                                    metadata={"sha256": got, "awshare": "object"})
        self._known.add(got)
        return got, True

    def put_bytes(self, data: bytes) -> Tuple[str, bool]:
        """Store bytes held in memory (a manifest, a seal); same contract as put."""
        if len(data) > self.max_object_bytes:
            raise ShareError(f"{len(data)} bytes exceeds the {self.max_object_bytes} cap")
        got = hashlib.sha256(data).hexdigest()
        if self.has(got):
            return got, False
        self.target.upload_verified(self.key(got), data, got,
                                    metadata={"sha256": got, "awshare": "object"})
        self._known.add(got)
        return got, True

    def get_bytes(self, digest: str) -> bytes:
        """The object's bytes, verified against the digest that names them."""
        d = _check_digest(digest)
        data = self.target.get(self.key(d))
        got = hashlib.sha256(data).hexdigest()
        if got != d:
            self._known.discard(d)
            raise VerificationFailedError(
                f"object {d[:16]}… came back as {got[:16]}… from {self._describe}; "
                "refusing to use it")
        return data

    def materialize(self, digest: str, dst: Path, *, link: bool = False) -> None:
        """Write the object's bytes at `dst`, verified, atomically. `link` is
        accepted for interface parity and ignored: a remote object has no inode."""
        del link
        data = self.get_bytes(digest)
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".awshare-remote-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dst)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


__all__ = ["BlobTarget", "DEFAULT_MAX_OBJECT_BYTES", "RemoteObjectStore"]
