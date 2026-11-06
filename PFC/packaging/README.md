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

Extract the new ZIP alongside the existing package. Copy the old `data/`
directory into the new package, or keep using the same `data/` directory, so
application data is preserved across upgrades. Do not replace `data/` with an
empty folder during an upgrade.
