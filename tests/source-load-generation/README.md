# Source-load generation structural check

This source-only audit covers the quarantined candidate
`patches/candidates/0002-mfmediaengine-track-source-load-generations.patch`.
It does not build or run Wine.

The JSON fixture fixes the two-file candidate scope, excludes the deferred
error-handling tokens, names every supported Begin/End pair, and records the
four completion states that the generation gate must distinguish. The audit
verifies that:

- all Begin calls retain the explicit load context;
- callback state selects the matching End call before shutdown/stale checks;
- stale and shutdown results shut down and release a returned media source;
- the callback copies its generation before releasing callback state;
- old presentation state is detached before source shutdown drops the lock;
- PURGE, Begin, LOADSTART, old-source shutdown, and topology installation are
  protected by checks of the captured generation;
- only the current generation can reach topology installation;
- the latest Play/Pause request is preserved while a load is pending;
- a null source clears source/play pending flags;
- byte-stream and callback-state ownership is balanced structurally;
- URL duplication failure returns `E_OUTOFMEMORY`.

The audit also rejects the previously proposed broad `MEError` handling. That
behavior is intentionally deferred rather than bundled with this lifecycle
fix.

Run it directly against the candidate patch:

```sh
python3 tests/source-load-generation/audit.py \
  --patch patches/candidates/0002-mfmediaengine-track-source-load-generations.patch
```

After applying the candidate to a Wine tree, audit the complete sources with:

```sh
python3 tests/source-load-generation/audit.py --wine-tree /path/to/wine
```

Passing this check is structural evidence only. It cannot prove provider COM
behavior, callback timing, shutdown latency, or runtime cancellation.

Run the positive and mutation-negative audit self-test with:

```sh
python3 tests/source-load-generation/selftest.py
```

The self-test audits the intact patch, then proves that the audit rejects
twelve independent mutations: presentation attachment across the lock drop,
lost callback generation lifetime, missing reentrancy checks, topology
generation loss, pending-state leaks, stale Play/Pause intent, and reintroduced
broad `MEError` forwarding.
