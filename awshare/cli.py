"""`awshare` — publish a directory, fetch it back verified.

    awshare publish <dir> --out <dir> [--seal]
    awshare fetch   <manifest.awshare.json> --dest <dir> [--key HEX]
    awshare inspect <manifest.awshare.json>
    awshare --self-test

`fetch` exits 1 when verification fails and 2 when it could not check at all —
a missing archive, an unknown manifest version, a sealed bundle with no awseal
installed. Collapsing those is how "I could not check" becomes "it checked out".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bundle as _bundle
from .store import ShareError, VerificationFailedError, safe_member_path


def _cmd_publish(a) -> int:
    m = _bundle.publish(Path(a.directory), Path(a.out), name=a.name,
                        seal=a.seal,
                        key_path=Path(a.key_path) if a.key_path else None)
    print(f"published {m.name}: {len(m.files)} file(s), {m.size} bytes")
    print(f"digest: {m.digest}")
    print(f"sealed: {m.sealed}")
    if not m.sealed:
        # Say it plainly. A digest answers "are these the published bytes",
        # never "who published them" — and whoever made the bytes made the
        # digest.
        print("NOTE: unsealed. The digest proves the bytes are intact in "
              "transit, NOT who produced them. Use --seal for provenance.")
    return 0


def _cmd_fetch(a) -> int:
    r = _bundle.fetch(Path(a.manifest), Path(a.dest), expect_key=a.key)
    print(f"fetched {r['name']}: {r['files']} file(s), verified={r['verified']}")
    if r["seal"] is not None:
        s = r["seal"]
        print(f"seal: signature_ok={s['signature_ok']} content_ok={s['content_ok']} "
              f"key_trusted={s['key_trusted']}")
    elif a.key is None:
        print("NOTE: no seal on this bundle. Integrity was checked; provenance "
              "was not, because there is nothing here that could establish it.")
    return 0


def _cmd_inspect(a) -> int:
    m = _bundle.load_manifest(Path(a.manifest))
    print(json.dumps(m.to_dict(), indent=2, sort_keys=True))
    return 0


def self_test() -> int:
    import tempfile
    ok = True

    def chk(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'PASS' if good else 'FAIL'}  {label} -> {got!r} (want {want!r})")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        src = d / "artifact"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("hello", encoding="utf-8")
        (src / "sub" / "b.bin").write_bytes(b"\x00\x01")

        out = d / "out"
        m = _bundle.publish(src, out, name="demo")
        chk("publish lists every file", sorted(m.files), ["a.txt", "sub/b.bin"])

        dest = d / "dest"
        r = _bundle.fetch(out / "demo.awshare.json", dest)
        chk("fetch verifies and unpacks", r["files"], 2)
        chk("  content survives the round trip",
            (dest / "sub" / "b.bin").read_bytes(), b"\x00\x01")

        # Idempotent: fetching twice must not fail or duplicate.
        chk("fetching twice is fine",
            _bundle.fetch(out / "demo.awshare.json", dest)["files"], 2)

        # TAMPER the archive. The digest is the check, and it must fire BEFORE
        # anything is unpacked.
        arch = out / "demo.tar.gz"
        arch.write_bytes(arch.read_bytes() + b"junk")
        try:
            _bundle.fetch(out / "demo.awshare.json", d / "dest2")
            chk("refuses a tampered archive", "no raise", "raise")
        except ShareError:
            chk("refuses a tampered archive", "raise", "raise")
        chk("  and nothing was written", (d / "dest2").exists(), False)

        # PATH TRAVERSAL — each variant defeats the previous defence.
        root = d / "unpack"
        root.mkdir()
        for name in ("../escape", "/etc/passwd", "a/../../escape", "..",
                     "sub/../../x"):
            try:
                safe_member_path(root, name)
                chk(f"refuses traversal {name!r}", "no raise", "raise")
            except ShareError:
                chk(f"refuses traversal {name!r}", "raise", "raise")
        # ...and an ordinary nested name must still be allowed, or the guard is
        # just "reject everything", which passes every traversal test.
        chk("allows an ordinary nested member",
            safe_member_path(root, "sub/ok.txt").name, "ok.txt")

        # A SYMLINK inside the destination that points outward is invisible to
        # string checks and only appears after resolve().
        outside = d / "outside"
        outside.mkdir()
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
            try:
                safe_member_path(root, "link/evil")
                chk("refuses a member behind an escaping symlink",
                    "no raise", "raise")
            except ShareError:
                chk("refuses a member behind an escaping symlink", "raise", "raise")
        except (OSError, NotImplementedError):
            print("  SKIP  symlink case (no privilege on this platform) — "
                  "the check is still compiled and covered on POSIX CI")

        # An empty directory must not publish. It would fetch and verify
        # perfectly while containing nothing.
        try:
            _bundle.publish(d / "emptydir", out, name="empty")
            chk("refuses to publish an empty tree", "no raise", "raise")
        except ShareError:
            chk("refuses to publish an empty tree", "raise", "raise")

        # fetch_verified() is PUBLIC API and nothing above calls it — which
        # is exactly how it shipped raising a NameError on its only failure
        # path. It survived every test because the tests drive bundle.fetch,
        # and ruff's F821 found what the self-test could not: the failure lives
        # on the path no happy-path probe runs.
        from .store import VerificationFailedError, fetch_verified
        src = d / "src.bin"
        src.write_bytes(b"real bytes")
        import hashlib
        real = hashlib.sha256(b"real bytes").hexdigest()
        chk("fetch_verified copies when the digest matches",
            fetch_verified(src, d / "copied.bin", real).exists(), True)
        try:
            fetch_verified(src, d / "nope.bin", "0" * 64)
            chk("fetch_verified refuses on a digest mismatch", "no raise", "raise")
        except VerificationFailedError:
            chk("fetch_verified refuses on a digest mismatch", "raise", "raise")
        chk("  and wrote nothing", (d / "nope.bin").exists(), False)
        try:
            fetch_verified(src, d / "n2.bin", "not-a-digest")
            chk("fetch_verified refuses a malformed expected digest",
                "no raise", "raise")
        except ShareError:
            chk("fetch_verified refuses a malformed expected digest",
                "raise", "raise")

        # An unknown manifest version must refuse rather than guess.
        mf = out / "demo.awshare.json"
        bad = json.loads(mf.read_text(encoding="utf-8"))
        bad["version"] = 99
        mf.write_text(json.dumps(bad), encoding="utf-8")
        try:
            _bundle.load_manifest(mf)
            chk("refuses an unknown manifest version", "no raise", "raise")
        except ShareError:
            chk("refuses an unknown manifest version", "raise", "raise")

    print("\nself-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="awshare", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("publish")
    p.add_argument("directory")
    p.add_argument("--out", required=True)
    p.add_argument("--name")
    p.add_argument("--seal", action="store_true")
    p.add_argument("--key-path")
    p.set_defaults(fn=_cmd_publish)

    f = sub.add_parser("fetch")
    f.add_argument("manifest")
    f.add_argument("--dest", required=True)
    f.add_argument("--key")
    f.set_defaults(fn=_cmd_fetch)

    i = sub.add_parser("inspect")
    i.add_argument("manifest")
    i.set_defaults(fn=_cmd_inspect)

    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except VerificationFailedError as exc:
        # Checked, and it failed. Exit 1.
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1
    except ShareError as exc:
        # Could not check at all. Exit 2 — a different answer, and a caller
        # that treats it as 1 will retry forever on a missing file.
        print(f"COULD NOT VERIFY: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
