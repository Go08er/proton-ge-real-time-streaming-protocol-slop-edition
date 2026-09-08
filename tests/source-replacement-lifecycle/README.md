# MediaEngine and MediaSession running-source replacement control

Patch 14 fixes one ownership error at the boundary between asynchronous
MediaEngine source loading and an already-started MediaSession. The current
generation stages a `VT_I8` start position, but A3.8's whole-presentation clear
erases it while detaching the old source. Wine MF then sees
`STARTED + VT_EMPTY`, takes its keep-position early return, and never starts the
new topology's source. A3.9 proved that preserving `VT_I8(0)` reaches the
positioned MediaSession restart path, but also exposed a second invariant:
the replacement source has not yet been subscribed when that path calls
`Stop()`. Its `MESourceStopped` event is therefore orphaned and the command
queue remains in `RESTARTING_SOURCES`.

Patch 15 detects that the installed replacement presentation is already fully
stopped and routes it through MediaSession's existing fresh-start block. That
block subscribes the current sources and calls `Start()` directly. Ordinary
running seeks still use the existing asynchronous stop/restart path. The same
branch clears the stale `clock_started` marker so the established clock-start
repair resets a positionless fresh start to zero.

The Patch 14 source audit remains the A3.9 compatibility gate:

```sh
python3 tests/source-replacement-lifecycle/audit.py /path/to/src-wine
```

The separate Patch 15 source audit is required only for A3.10 and later:

```sh
python3 tests/source-replacement-lifecycle/audit_a310.py /path/to/src-wine
```

The deterministic ownership model and patch-scope checks run without Wine,
Proton, Steam, or network access:

```sh
python3 tests/source-replacement-lifecycle/test_model.py
```

The checks require Patch 14 to detach only the old refcounted source,
presentation descriptor, and frame sink. They reject clearing or restoring
the value across the old source's lock-dropping `Shutdown()`, because a
snapshot restore could overwrite a same-generation pre-ready seek or a newer
reentrant source request. They also require Patch 15 to bypass the clock-only
and running-restart paths only when every current source/source-stream node is
already stopped, then reuse the existing subscription and direct-start block.
They also require the stale presentation-clock marker to be cleared on that
same branch.

These are source/model gates, not runtime evidence. Promotion still requires
the running RTSP replacement control, the pause-before-replace discriminator,
and the running progressive-HTTP replacement control on a fresh A3.10 build.
