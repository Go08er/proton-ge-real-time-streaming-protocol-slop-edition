# SAR audio-flush control

These deterministic tests preserve the failure-path coverage for the active
state-preserving SAR audio-flush patch.  A running audio client must stop,
reset, and restart as one seek-flush operation; paused and zero-rate clients
must not be spuriously restarted.  The model also checks error precedence and
the retry state used when restarting the audio client fails.

Run the policy model with:

```sh
python3 tests/sar-flush-control/test_model.py
```

This model is a regression guard for reviewed control flow.  It does not
replace compilation, the static Wine source audit, or VRChat runtime testing.
