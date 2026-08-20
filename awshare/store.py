"""Where a shared artifact lives, and what it costs to trust one.

Extracted from AitherOS's workspace artifact spine. The internal version is
tenant-scoped, Strata-backed and Redis-catalogued; what generalises is the part
that makes a fetched artifact safe to open:

- **content addressing** — an artifact is named by the digest of its bytes, so
  "did I get the right thing" is answerable without asking anyone;
- **verification ON RETRIEVAL, not on publish** — the publisher's word is not
  evidence, and the fetch is where the bytes could have changed;
- **atomic materialisation** — a half-written artifact must never look
  complete;
- **path containment** — a name inside an archive is attacker-controlled.

THE ONE THAT IS ALWAYS UNDERESTIMATED

`../../../.ssh/authorized_keys` is a perfectly ordinary string until something
joins it to a directory. The internal spine strips separators, resolves the
final path and checks it is still inside the cache — three separate defences,
because each alone has a known bypass. All three are here, and the self-test
carries the bypasses as cases rather than trusting the code to be obviously
right: a symlink that escapes, an absolute path, a Windows drive letter, and a
name that only escapes AFTER resolution.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import warnings
from pathlib import Path

_CHUNK = 1024 * 1024


class ShareError(RuntimeError):
    """Could not judge: a missing archive, an unknown version, a broken read.

    Distinct from `VerificationFailed` on purpose. "I could not check this" and
    "this failed the check" are different answers, and a caller that collapses
    them reads an unreadable artifact as a clean one. This module's own CLI got
    that wrong first: an artifact from the WRONG PUBLISHER — definitively
    judged, definitively rejected — exited 2, the code reserved for not being
    able to tell.
    """


class VerificationFailedError(ShareError):
    """Checked, and it failed. A digest mismatch, a bad seal, a wrong key.

    Subclasses ShareError so existing `except ShareError` handlers stay
    fail-closed; anything that wants the distinction catches this first.
    """


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while True:
                b = fh.read(_CHUNK)
                if not b:
                    break
                h.update(b)
    except OSError as exc:
        raise ShareError(f"cannot read {path}: {exc}") from exc
    return h.hexdigest()


def safe_member_path(root: Path, name: str) -> Path:
    """Resolve `name` inside `root`, or refuse.

    Every check here has defeated the previous one somewhere:

    - stripping `..` alone is defeated by an ABSOLUTE path, which `Path.joinpath`
      silently honours — `Path("/a") / "/etc/passwd"` is `/etc/passwd`;
    - rejecting absolute paths alone is defeated on Windows by a DRIVE-relative
      name like `C:evil`;
    - checking the string alone is defeated by a SYMLINK inside `root` pointing
      outward, which only appears after `resolve()`.

    So the answer is computed and then re-checked against the resolved root.
    """
    if not name or name in (".", ".."):
        raise ShareError(f"refusing empty or dot member name: {name!r}")
    if "\x00" in name:
        raise ShareError("member name contains a NUL byte")
    p = Path(name)
    if p.is_absolute() or p.drive or p.root:
        raise ShareError(
            f"refusing absolute member path {name!r}: joining it to a directory "
            f"silently discards the directory")
    if ".." in p.parts:
        raise ShareError(f"refusing member path with a parent reference: {name!r}")
    target = (root / p)
    try:
        resolved = target.resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        raise ShareError(f"cannot resolve {name!r}: {exc}") from exc
    # The final containment check. It catches the symlink case, which no amount
    # of string inspection can.
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ShareError(
            f"refusing {name!r}: it resolves to {resolved}, outside {root_resolved}")
    return target


def atomic_write(target: Path, data: bytes) -> Path:
    """Write `data` to `target` so a reader never sees a partial file.

    Writes to a temp file in the SAME directory and renames. Same directory
    matters: `os.replace` across filesystems is not atomic, and `/tmp` is
    routinely a different filesystem to the destination — a detail that turns a
    guarantee into a coin flip exactly when the artifact is large.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".awshare-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Clean up on ANY exit including KeyboardInterrupt: a leftover
        # `.awshare-*` file is not merely untidy, it is an artifact fragment
        # sitting next to real ones.
        try:
            os.unlink(tmp)
        except OSError as cleanup_exc:
            # The original failure is what the caller needs, so this must not
            # replace it — but a stranded `.awshare-*` file sits next to real
            # artifacts and is exactly the fragment this function exists to
            # prevent, so it does not get to be silent either.
            warnings.warn(
                f"could not remove partial file {tmp} ({cleanup_exc}); a "
                f"fragment may remain beside the real artifacts",
                RuntimeWarning, stacklevel=2)
        raise
    return target


def fetch_verified(src: Path, dst: Path, expect_digest: str) -> Path:
    """Copy `src` to `dst` only if its bytes hash to `expect_digest`.

    Verification happens BEFORE the file lands at its final name. Verifying
    afterwards leaves a window in which a wrong artifact exists under the right
    name, and something else may read it in that window — the reason the
    internal spine materialises atomically rather than copy-then-check.
    """
    if not expect_digest or len(expect_digest) != 64:
        raise ShareError(
            f"expected a sha256 hex digest, got {expect_digest!r}. Fetching "
            f"without one is not 'unverified', it is unverifiABLE")
    got = digest_file(src)
    if got != expect_digest:
        raise VerificationFailedError(
            f"digest mismatch for {src.name}: expected {expect_digest[:16]}…, "
            f"got {got[:16]}…. The bytes are not what the publisher described")
    return atomic_write(dst, src.read_bytes())


def already_have(path: Path, expect_digest: str) -> bool:
    """True when `path` already holds exactly these bytes.

    Makes a fetch idempotent and cheap to re-run. Compares the DIGEST, never the
    mtime or the size: both match for a truncated-then-padded file, and a fetch
    that trusts them re-uses corruption forever.
    """
    if not path.is_file():
        return False
    try:
        return digest_file(path) == expect_digest
    except ShareError:
        return False
