# Cove Windows aria2 build

Cove's Windows packages use aria2 1.37.0 built from the immutable upstream
commit `02f2d0d8472b3c38c29b4dba8c75ebd5fdd2899a`.

The official Windows binary uses Schannel and treats an unreachable
certificate revocation service as a fatal TLS error. Cove applies
`wintls-best-effort-revocation.patch`, which adds only
`SCH_CRED_IGNORE_REVOCATION_OFFLINE` to the existing verified-client flags.
Certificate-chain, hostname, expiry, signature, and known-revocation failures
remain enforced. Revocation checks are still attempted and are ignored only
when their distribution point is missing or offline.

Build the binary and corresponding source archive from the repository root:

```text
bash scripts/build-aria2-windows.sh
```

The outputs are:

```text
build/aria2-win/aria2c.exe
build/aria2-win/aria2-1.37.0-cove.1-source.tar.gz
```

The source archive accompanies Windows release artifacts so the exact patched
GPLv2 source used to build the bundled executable remains available.
