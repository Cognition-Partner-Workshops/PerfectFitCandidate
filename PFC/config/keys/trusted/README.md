# Analyst package-signing trust

Populate this directory before building or distributing the Analyst ZIP:

1. On the OCC Admin instance, export a settings package with the Admin's signing
   passphrase to create the encrypted signing key, if it does not already exist.
2. As Admin, call `GET /api/v1/occ/settings-package/signing-key`.
3. Base64-decode `publicKeyBase64` and save the resulting 32 raw Ed25519 public-key
   bytes as `<keyId>.pub` in `PFC/config/keys/trusted/`.
4. Verify the key's fingerprint with the Admin through a trusted channel before
   distributing it. `build_zip.py` already copies `PFC/config` into the ZIP.

At runtime this read-only directory is `<install>/config/keys/trusted/`.
`Settings.config_dir` resolves the bundled config directory; `PFC_CONFIG_DIR` can
point it at an explicitly provisioned alternative. The writable trust store at
`data_dir/keys/trusted/*.pub` is also loaded. Duplicate public keys are ignored.

Only public keys belong here. Do not copy the encrypted private key, Admin
passphrase, Analyst OTPs, or any other credentials. Package signatures establish
trust; receiving a package by inbox, email or Teams does not make its key trusted.
