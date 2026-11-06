# Portable Windows package

Build the Windows x64 ZIP from the repository root:

```sh
python PFC/packaging/build_zip.py --out PFC/packaging/dist
```

The build downloads only pinned, SHA-256-verified runtime assets and wheels.
`--with-models` also downloads the optional models in `MODELS.lock`; entries
without a hash are refused unless `--allow-unpinned` is explicitly supplied.
`--skip-frontend` skips npm commands and copies `PFC/frontend/dist` only if it
already exists.

The ZIP includes an embedded CPython runtime, vendored Windows wheels, the
llama.cpp CPU server, the app source, and a `.cmd` launcher. It does not need
administrator rights or network access at runtime.

## Upgrade

Application data no longer lives inside the extracted package. The default data
directory is the user profile's `%LOCALAPPDATA%\PFC\data` (REQ-AUTH-06). On
Windows, the launcher restricts it to the signed-in user and SYSTEM. `PFC_DATA_DIR`
overrides the location.

To upgrade, extract the new ZIP to a new folder and run its `PFC.cmd`.

**Automatic move from an older package.** On first start, if
`%LOCALAPPDATA%\PFC\data` does not exist yet and the package being started has
its own `data\` folder containing `db\pfc.db`, the launcher moves it:

1. It copies the folder to the profile location and verifies the copy (SHA-256 per
   file, row counts per database table).
2. It renames the original to `data.migrated` next to it, or `data.migrated-2` and
   so on if that name is taken. The original is never deleted.

The move is refused, with the original left untouched, if the old PFC is still
running. Close the old PFC first. An empty `data\` folder is ignored.

**A database inside a different, older ZIP is not found automatically.** The new
package only looks at its own `data\` folder. Before the new package's first start,
do one of the following:

- Close the old PFC, then copy the old package's whole `data\` folder into the new
  package folder (next to `PFC.cmd`). The first start then moves it as above.
- Or set `PFC_DATA_DIR` to the old `data\` folder before starting. For example, in
  a Command Prompt: `set PFC_DATA_DIR=C:\Tools\PFC-old\data` then `PFC.cmd`. With
  `PFC_DATA_DIR` set, nothing is moved and that folder is used in place.

If the new package already started once without the old data, it created a fresh
profile database. Close PFC, rename `%LOCALAPPDATA%\PFC\data` (for example to
`data.unused`), then follow one of the two options above.

When the new version works, `data.migrated` can be archived or deleted under your
backup and retention policy. It may contain plaintext credentials from before
REQ-AUTH-06, as may older files under `backups\`.

## Model-complete offline release (REQ-PKG-02)

Run **Package Windows portable ZIP** manually with the desired branch/ref, or
create a `v*` release tag. Main already declares `workflow_dispatch`:

```sh
gh workflow run package.yml --ref <branch-or-tag>
```

Only dispatch/tag runs build `pfc-windows-x64-with-models`; PR packaging remains
model-free. The release job uses:

```sh
python PFC/packaging/build_zip.py --with-models --models-cache <cache-dir> --out PFC/packaging/dist
```

All five `MODELS.lock` entries are included: pinned Qwen GGUF, nomic ONNX, tokenizer,
tokenizer config and special-token map. Downloads use the lock URLs. Cache objects
are addressed by SHA-256, and every cache hit and packaged copy is verified;
mismatches fail the build. The Actions cache key hashes the entire lock (including
all model hashes). Never use `--allow-unpinned` for a release.

A separate `windows-latest` job downloads and extracts that artifact before
blocking non-loopback IPv4/IPv6 egress for its Python and llama-server executables
with Windows Firewall. It also sets dead HTTP(S)/ALL proxies with loopback exempted.
The Actions agent remains online. A dedicated local **Users**, not Administrators,
account runs the packaged launcher function and real Uvicorn server. Its actual
process token must have `TokenElevation=false` and `IsUserAnAdmin=false`.

`offline_smoke.ps1` / `offline_smoke.py` check fresh-data setup, HTTP health and
setup page, real CPU ONNX loading and a completed one-owner re-embed job, one
8-token GGUF generation (512-token context, two threads), and lifecycle shutdown
including llama-server exit. CI explicitly exercises the pinned GGUF; it does not
change production model-approval defaults. First-run Admin setup and explicit
approval/selection of model candidates remain required for users.

The job summary records ZIP bytes, startup-to-health, total model-exercise time,
and 100 ms sampled aggregate launcher + llama-server RSS. TCP snapshots must find
no non-loopback app connections; this sampling is **not** proof of zero attempted
packets. Firewall rules and the temporary account are removed even on failure.
Evidence is uploaded as `offline-smoke-evidence`. GitHub's Windows CI/VM is **not
the target corporate laptop**; it does not close locked-down-laptop UAT.

Passing evidence: [dispatch 42203458140](https://github.com/Cognition-Partner-Workshops/PerfectFitCandidate/actions/runs/42203458140),
implementation `e6596e1`: ZIP 2,718,697,267 bytes; startup-to-health 0.548344 s;
generation + re-embed complete at 6.414126 s; sampled peak RSS 4,716,417,024 bytes.
