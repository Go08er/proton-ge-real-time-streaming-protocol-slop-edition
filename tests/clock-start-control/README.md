# Presentation-clock start control

These deterministic checks cover the narrow A3.6 clock-origin correction
derived from RTSP Wine commit `c1f40112915a6e58f631e0ef15a926c97a42710f`.

Media Foundation uses `PRESENTATION_CURRENT_POSITION` when a session resumes
without an explicit position.  That value is valid only after the presentation
clock has established a timeline.  On a clock's first start, A3.6 normalizes
the sentinel to zero.  Later resumes retain the current-position behavior.
Successful Stop and Close transitions clear the tracked started state.

Run the policy model with:

```sh
python3 tests/clock-start-control/test_model.py
```

After replaying the patch series into a prepared GE source tree, audit the
implemented control flow with:

```sh
python3 tests/clock-start-control/audit.py /path/to/src-wine
```

Neither command opens a network connection or launches Steam.
