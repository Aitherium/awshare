# awshare

Publish an artifact, and fetch it back verified.

`awshare` bundles a directory into a `tar.gz` plus a small JSON manifest naming
its digest, its size and the files inside it. A consumer fetches the manifest
first and can decide, before opening anything, whether the archive is the one
that was published.

```bash
pip install awshare            # stdlib only
pip install "awshare[seal]"    # adds provenance via awseal

awshare publish ./my-adapter --out ./dist --seal
awshare fetch ./dist/my-adapter.awshare.json --dest ./here --key <publisher-key>
```

## It pairs with awseal; it does not replace it

```
awshare — are these the bytes that were published?   (integrity)
awseal  — who published them?                        (provenance)
```

A digest cannot answer the second, because whoever produced the bytes also
produced the digest. `publish --seal` seals the directory *before* archiving, so
the seal travels inside the artifact and one download answers both. Unsealed
publishes say so on every run rather than letting a clean-looking pass be
mistaken for provenance.

## Exit codes are three answers, not two

```
0   verified
1   checked, and it failed      (digest mismatch, wrong publisher, bad seal)
2   could not check at all      (missing archive, unknown manifest version)
```

Collapsing 1 and 2 is how "I could not check this" becomes "it checked out".
This package's own CLI got it wrong first: an artifact from the *wrong
publisher* — definitively judged and definitively rejected — exited 2, the code
reserved for not being able to tell.

## What it refuses

- **Path traversal**, in four flavours, because each defeats the previous
  defence: `..` segments, absolute paths (`Path("/a") / "/etc/passwd"` is
  `/etc/passwd`), Windows drive-relative names, and symlinks inside the
  destination that only escape *after* `resolve()`.
- **Non-regular archive members.** A symlink in a tarball passes every check
  applied to its name and points anywhere once created.
- **Unbounded expansion.** A small download that expands without limit fills the
  disk long before anyone reads a log line.
- **An empty tree.** It would fetch and verify perfectly while containing
  nothing.
- **Contents that disagree with the manifest**, even when the digest matches —
  that is a manifest describing a different set of files to the one it names,
  which is worse than corruption because every integrity check passes.

Archives normalise uid, gid and mtime, so two builds of identical content
produce identical bytes and a content-addressed store does not treat them as
different artifacts.

## Licence

Apache-2.0.

---

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| [awnode](https://github.com/Aitherium/awnode) | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| **awshare** _(you are here)_ | that the download is intact | content-addressed bundles, verified on fetch |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — a bootable, immutable Linux base for machines where software writes software.

<!-- aitherium-ecosystem:start -->
## Aitherium open-source ecosystem

This repo is one piece of a connected set. All public, MIT/BSL-licensed:

| repo | what it is | pages |
|---|---|---|
| [awrecover](https://github.com/Aitherium/awrecover) | Labelled snapshots with an all-or-nothing restore | [docs](https://aitherium.github.io/awrecover/) |
| [awshare](https://github.com/Aitherium/awshare) | Publish an artifact and fetch it back verified | [docs](https://aitherium.github.io/awshare/) |
| [awseal](https://github.com/Aitherium/awseal) | Sign an artifact so a stranger can verify it | [docs](https://aitherium.github.io/awseal/) |
| [awnode](https://github.com/Aitherium/awnode) | Lightweight local gateway — your apps to backends you chose | [docs](https://aitherium.github.io/awnode/) |
| [awnix](https://github.com/Aitherium/awnix) | A bootable, immutable Linux base for agent-run machines | [docs](https://aitherium.github.io/awnix/) |
| [awdk](https://github.com/Aitherium/awdk) | Build AI agent fleets — 3 lines, any backend | [docs](https://aitherium.github.io/awdk/) |
| [awskills](https://github.com/Aitherium/awskills) | Free agent skills, scripts & automations | [docs](https://aitherium.github.io/awskills/) |
| [AitherZero](https://github.com/Aitherium/AitherZero) | PowerShell 7+ automation framework | [docs](https://aitherium.github.io/AitherZero/) |
| [awgit](https://github.com/Aitherium/awgit) | Semantic version control on top of git | [docs](https://aitherium.github.io/awgit/) |
| [awgraph](https://github.com/Aitherium/awgraph) | Code knowledge graph for AI agents | [docs](https://aitherium.github.io/awgraph/) |
| [aitherkvcache](https://github.com/Aitherium/aitherkvcache) | Near-optimal KV cache quantization | [docs](https://aitherium.github.io/aitherkvcache/) |
| [awrelay](https://github.com/Aitherium/awrelay) | Agent-to-agent messaging over any chat server | [docs](https://aitherium.github.io/awrelay/) |
| [awm](https://github.com/Aitherium/awm) | A small world model (LeWM JEPA + MLP) to bootstrap your own | [docs](https://aitherium.github.io/awm/) |
| [AitherConnect](https://github.com/Aitherium/AitherConnect) | Browser extension: federated AI search & desktop bridge | — |
| [homebrew-tap](https://github.com/Aitherium/homebrew-tap) | `brew tap aitherium/tap` | — |

Built by [Aitherium](https://aitherium.com).
<!-- aitherium-ecosystem:end -->
