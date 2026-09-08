# VRChat MediaEngine stress-suite contract

This directory describes the deterministic test surface for the RTSP-on-GE
foundation and its extended Proton-RTSP 11 parity branch.  The manifest is a
test contract, not a claim that every runner already exists.

Run the zero-network manifest checks with:

```bash
python3 -m unittest -v test_manifest.py
python3 validate_manifest.py manifest.json
```

Run the complete host-safe static gate from this directory with:

```bash
./check-static.sh
```

That command runs pure/model/parser/source checks only. Add `--loopback` for
the bounded literal-loopback integration suites, and optionally add
`--nixpkgs /nix/store/<hash>-source` for review-only Nix evaluation. It never
starts Wine, Proton, Steam, VRChat, or a virtual machine.

Without `--loopback`, it starts no network listener. All modes leave Steam,
compatdata, and host system configuration untouched.

`manifest.json` labels every scenario with one of these execution states:

- `implemented`: a reviewed path can execute and score the complete named
  operation;
- `compose`: existing fixture pieces exist, but an orchestration/result parser
  still has to join them;
- `planned`: new fixture or consumer work is required;
- `manual-vrchat`: the result depends on VRChat, Udon, a real player frontend,
  or two real clients and cannot be claimed by a standalone MediaEngine probe.

Fixture `availability` is separate from scenario status: `implemented` means
the declared bytes/service can be materialized now, `compose` means only a
partial input or helper exists, and `planned` means the fixture itself does not
yet exist. Tooling must never infer fixture existence from `kind` or from a
scenario which merely references its future contract.

Host-safe cases may use only repository data, temporary files, and
loopback-only listeners.  Any case that changes routing, packet delivery,
resolver behavior, a trust store, firewall state, or system services is
`vm-only`.  Those cases belong in the declared Nix VM lab; they must never
modify the workstation running Steam.

This directory retains the historical stress-suite infrastructure. Its
private planning matrix is not exported. Use the checked-in scenario
manifests and fixture scripts; [current testing scope](../../docs/TESTING.md)
distinguishes these checks from the focused MediaEngine clock regression.

At present, no media scenario is end-to-end implemented (0 of 76). Manifest
validation and its mutation tests run directly as static infrastructure; they
are not counted as a media scenario. The deterministic fixture derivation and
Windows scenario driver are validated building blocks, but their complete
fixture/Proton/scorer paths are not yet composed and therefore do not qualify
any media scenario or branch.

The status count covers only complete manifest rows. Separate guarded
host-contained slices already execute progressive HTTP, healthy HLS, and
literal RTSP. A full row may remain `vm-only` while such a bounded slice uses
immutable inputs, loopback only, private mutable state, and no system mutation;
the slice does not promote the larger soak/fault contract to `implemented`.

In particular, `running-http-replacement` is a `compose` Proton-probe row even
though its bounded immutable case has executed through the guarded host
adapter. Schema 1 has no separate guarded-host evidence layer: `vm-only`
describes the intended isolated full-case path, not a claim that the existing
diagnostic ran in a VM. The `aggressive-seek-latest-wins` scenario and oracle
likewise exist and issue all 301 calls, but no single runner yet owns its
fixture, Proton consumer, and scorer.

Any future full-case runner must fail closed: manifest metadata is not
executable authority, unimplemented cases may not be silently skipped, and
only reviewed immutable inputs may run. Disposable state must stay outside the
repository, Steam, and compatdata; spawned services or system/network mutation
remain VM-only. Runs need bounded cleanup and sanitized structured results.
Reference results must be frozen explicitly before comparison.
