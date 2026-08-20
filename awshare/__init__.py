"""awshare — publish an artifact, fetch it back verified.

Extracted from AitherOS's workspace artifact spine. What generalises out of it
is the part that makes a fetched artifact safe to open: content addressing,
verification on RETRIEVAL rather than on publish, atomic materialisation, and
containment of attacker-controlled member names.

    import awshare
    from pathlib import Path

    m = awshare.publish(Path("my-adapter"), Path("dist"), seal=True)
    awshare.fetch(Path("dist/my-adapter.awshare.json"), Path("./here"),
                  expect_key=PUBLISHER_KEY)

It pairs with `awseal` and does not replace it:

    awshare — are these the bytes that were published?  (integrity)
    awseal  — who published them?                       (provenance)

A digest cannot answer the second, because whoever produced the bytes also
produced the digest. `publish(seal=True)` seals the directory before archiving
so the seal travels inside the artifact and one download answers both.

There is deliberately no upload transport here. `publish` writes a bundle to a
directory; whether that is a local path, a mounted share, an object-store sync
target or a Strata namespace is the caller's business. Baking one in is how a
portable package acquires a dependency on somebody's fleet.
"""

from __future__ import annotations

from .bundle import (
    ARCHIVE_SUFFIX,
    MANIFEST_SUFFIX,
    MANIFEST_VERSION,
    Manifest,
    fetch,
    load_manifest,
    publish,
)
from .store import (
    ShareError,
    already_have,
    atomic_write,
    digest_bytes,
    digest_file,
    fetch_verified,
    safe_member_path,
)

__version__ = "0.1.0"

__all__ = [
    "ARCHIVE_SUFFIX",
    "MANIFEST_SUFFIX",
    "MANIFEST_VERSION",
    "Manifest",
    "ShareError",
    "already_have",
    "atomic_write",
    "digest_bytes",
    "digest_file",
    "fetch",
    "fetch_verified",
    "load_manifest",
    "publish",
    "safe_member_path",
]
