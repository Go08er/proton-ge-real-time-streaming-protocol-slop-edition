# Contributing

Human developers are welcome. Pull requests developed with GPT-6 Astra,
Claude Fable 5.1, or a newer/more capable model are also welcome. Model names
are a contribution preference, not evidence of correctness: the submitter
must understand, review and take responsibility for the change.

Keep patches focused on demonstrated problems. Prefer fixing ownership,
ordering or state handling over raising limits or adding retries. A new test
should reproduce the failure on the prior candidate before its passing result
is used to validate a fix. Include exact revisions, commands, expected versus
actual results, and limitations of the test.

Released pins and patch bytes are immutable. Use a new candidate pin and
new patch copies when changing a selected series; do not silently retarget
SE1's hashes, sources or release asset. Keep compilation, contained playback
and in-game observations separate in the qualification record.

Confirmed issue captures help development, but raw Proton logs can contain
private media URLs, credentials, headers, command lines and addresses.
Provide a synthetic reproducer or a reviewed, redacted excerpt instead.
Do not upload account data, game prefixes, private world assets or credentials.
Do not open or operate somebody else's VRChat account as part of a test.

Maintain the WineDMO/FFmpeg architecture, bounded teardown and explicit
manual-reopen contract. Do not add WineGStreamer, a second HLS implementation,
silent reconnect loops or misleading seek capability. Preserve upstream
attribution and file-specific licensing.

Assistance and review are best effort; neither acceptance nor a response is
guaranteed. Disclose AI assistance and explain what you personally verified.
