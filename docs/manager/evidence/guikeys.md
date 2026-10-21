# GUIKEYS verification — 2026-10-21

**Verified:** on `feat/gui-keys`, setup explains PUP/APP key purpose, shared system
key identity, exact JSON fields and lengths, sources and next steps. File selection
validates on Finish. Hex entry uses the CLI over stdin and a 0600 private key file;
the GUI never writes library data. Missing targets explicitly produce format-only
results. Adoption validates against available source targets before publication.

**Verified:** RPCS3 source pin is `ffba2bf09ef3980638f9327b25240d81414034ab`.
The local reference files match that commit's Git blobs. File digests in
`anyps3_manager/keys.py` pin both downloads. The parser matches the pinned source's
named `constexpr u8` arrays and APP constructors. No keys or downloaded source are
stored in this checkout. APP type/revision comes from the selected SELF header;
without a target, revision 1 is the explicit Ratchet default.

One manual offline parser check (29 key sets: one PUP set and 28 APP revisions):

> parsed 29 keys, matched the existing local/ratchet key files' sha256: yes

Comparison hashes canonical JSON of the required key fields, excluding provenance
and formatting. The actual local EBOOT header selected type 4, revision 1.
Only that summary was printed; key values were neither printed nor saved.

**Verified:** final scoped tests: **75 passed, 4 skipped**, zero failures.
The skips are the four existing opt-in installer integration tests. Commands ran
from this worktree, after `local/ratchet/tools/quiet_wait.sh` in the primary checkout:

```bash
systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  -p AllowedCPUs=6-11,18-23 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
  python3 -m unittest discover -s tests/manager -v
# 65 tests, 4 opt-in installer skips

systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 \
  -p AllowedCPUs=6-11,18-23 \
  env SDL_AUDIODRIVER=dummy ANYPS3_AUDIO_SINK=null QT_QPA_PLATFORM=offscreen \
  python3 -m unittest tests.gui.test_gui tests.test_firmware tests.test_ingest.SelfTests -v
# 14 tests, no skips
```

`python3` used the existing installed application's Python environment for Qt and
cryptography dependencies (via PATH); package imports came from this worktree.
Test libraries and keys were temporary, separate from the user's installed library.
Logs: `/tmp/anyps3-guikeys-manager-final.log` and
`/tmp/anyps3-guikeys-format-gui-final.log`.
The 20 key tests also passed a final focused compatibility check preserving the
existing `keys set` response's `sha256` field (`/tmp/anyps3-guikeys-api-final.log`).

Tests generate synthetic encrypted PUP/SELF fixtures and synthetic key-vault C++.
They cover wrong ERK/RIV/HMAC/revision, malformed metadata, target selection,
format-only entry, sanitized errors, key permissions, stdin through a real QProcess,
confirmation/cancellation, download/hash/validation failure preservation, revision
selection and adoption failure/recovery. HTTP responses are monkeypatched; tests
perform no network requests. Existing native flag tests compile only a tiny synthetic
program. An offscreen synthetic wizard preview was also inspected at 800 × 650;
the key page scrolls, with controls and explanation visible. Preview:
`/tmp/anyps3-guikeys-wizard.png` (no key values).

**Limitations:** live HTTPS retrieval was not exercised. No real-game import,
build, launch or physical-controller test was performed. Setup's SELF metadata
check and PUP HMAC/package check do not verify ECDSA signatures or replace full
import verification. Runtime sources, profiles and `docs/knowledge/` are unchanged.
