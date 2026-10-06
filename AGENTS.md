# awshare for agents

Read this if you are an agent (or a human) editing this package. Short on
purpose: the commands, the traps that cost a session, and where the rest lives.
Nothing here is read at runtime — it is for you.

## What this is

PyPI distribution **`awshare`** (version in `pyproject.toml`), import package
`awshare`, Python >= 3.10. Publish a directory as a verifiable bundle and fetch
it back byte-checked; many backups while paying for unchanged bytes once
(dedupe, incremental snapshots).

This repository is a **synced mirror** of the AitherOS monorepo (lane
`.github/workflows/sync-awshare.yml`). Hand edits made here are overwritten on
the next sync — change the source and let the lane publish.

## Build, test, verify

```bash
python -m pytest tests -q        # the suite: 51 passed, 1 skipped at v0.2.2
pip install -e .                 # editable install for developing against it
```

The suite was run from a source checkout with no prior install. The publish
lane (`publish-brick.yml`) additionally builds the wheel, installs it and
imports it — a tree that tests green can still ship a broken wheel.

## Rules that keep this useful

- **The wire format is a contract, and it is tested as one.**
  `tests/test_awshare_contract.py` pins the bundle format; a format change and
  its contract test land in the same commit. Chunking and dedupe each carry
  their own suite (`test_chunk.py`, `test_dedupe.py`) because they are where a
  silent byte-level regression would hide.
- **"Byte-checked" is the product.** Anything that compares or copies bundle
  content without verifying hashes is a defect even when it is faster.
- **The registry drives the public surface.** This repo's README header,
  `llms.txt` and `aither-manifest.json` are generated from the ecosystem
  registry (one yaml in the AitherOS monorepo) and rewritten on every sync.
  Change the registry; do not hand-edit the generated blocks.
- **The install line is a measured claim.** `check_ecosystem_install_lines`
  asserts the advertised `pip install` channel is real and ours. A rename or
  a move lands with the registry entry in the same change.

## Read next

- `llms.txt` — the install/use card written for an agent to execute
- `README.md` — the human front door
- `docs/` — the generated docs site source
