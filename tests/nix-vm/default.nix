# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsPath,
  system ? builtins.currentSystem,
  memoryMiB ? 2048,
  cores ? 2,
  protonToolPath ? "",
  steamRuntimePath ? "",
  probeExePath ? "",
  probeFixturePath ? "",
  probeParserPath ? "",
  runRuntimeProbe ? false,
  stressProtonToolPath ? "",
  stressSteamRuntimePath ? "",
  stressDriverExePath ? "",
  stressScenarioPath ? "",
  stressFixtureManifestPath ? "",
  stressFixtureBytesPath ? "",
  stressServiceConfigPath ? "",
  stressControlOraclePath ? "",
  stressInstrumentedOraclePath ? "",
  stressParserPath ? "",
  stressBuildRole ? "streaming-base-candidate",
  stressCaseRole ? "qualification",
  stressMediaKind ? "av",
  runStressDriver ? false,
}:

assert builtins.isString nixpkgsPath;
assert builtins.match "^/nix/store/[^/]+-.*" nixpkgsPath != null;

let
  pin = builtins.fromJSON (builtins.readFile ../media-engine-stress/fixtures/nixpkgs-pin.json);
  suppliedNixpkgsRoot = builtins.toPath nixpkgsPath;
  nixpkgsRoot = builtins.path {
    path = suppliedNixpkgsRoot;
    # The reviewed Nixpkgs fetch has the canonical store name "source".
    # Reusing it lets a matching already-materialized tree retain its valid
    # store identity while the NAR hash still rejects different bytes.
    name = "source";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot {
    inherit system;
    config.allowUnfree = false;
  };
  inherit (pkgs) lib;

  runtimeInputStrings = [
    protonToolPath
    steamRuntimePath
    probeExePath
    probeFixturePath
    probeParserPath
  ];
  runtimeInputCount = builtins.length (builtins.filter (value: value != "") runtimeInputStrings);
  runtimeBundleEnabled = runtimeInputCount == builtins.length runtimeInputStrings;
  noRuntimeInputs = runtimeInputCount == 0;

  stressInputStrings = [
    stressProtonToolPath
    stressSteamRuntimePath
    stressDriverExePath
    stressScenarioPath
    stressFixtureManifestPath
    stressFixtureBytesPath
    stressServiceConfigPath
    stressControlOraclePath
    stressInstrumentedOraclePath
    stressParserPath
  ];
  stressInputCount = builtins.length (builtins.filter (value: value != "") stressInputStrings);
  stressBundleEnabled = stressInputCount == builtins.length stressInputStrings;
  noStressInputs = stressInputCount == 0;

  assertStoreInput = label: value:
    assert lib.assertMsg (builtins.isString value) "${label} must be a string";
    assert lib.assertMsg (
      value == "" || builtins.match "^/nix/store/[^/]+-.*" value != null
    ) "${label} must be an immutable /nix/store path";
    value;

  checkedProtonToolPath = assertStoreInput "protonToolPath" protonToolPath;
  checkedSteamRuntimePath = assertStoreInput "steamRuntimePath" steamRuntimePath;
  checkedProbeExePath = assertStoreInput "probeExePath" probeExePath;
  checkedProbeFixturePath = assertStoreInput "probeFixturePath" probeFixturePath;
  checkedProbeParserPath = assertStoreInput "probeParserPath" probeParserPath;
  checkedStressProtonToolPath = assertStoreInput "stressProtonToolPath" stressProtonToolPath;
  checkedStressSteamRuntimePath = assertStoreInput "stressSteamRuntimePath" stressSteamRuntimePath;
  checkedStressDriverExePath = assertStoreInput "stressDriverExePath" stressDriverExePath;
  checkedStressScenarioPath = assertStoreInput "stressScenarioPath" stressScenarioPath;
  checkedStressFixtureManifestPath = assertStoreInput "stressFixtureManifestPath" stressFixtureManifestPath;
  checkedStressFixtureBytesPath = assertStoreInput "stressFixtureBytesPath" stressFixtureBytesPath;
  checkedStressServiceConfigPath = assertStoreInput "stressServiceConfigPath" stressServiceConfigPath;
  checkedStressControlOraclePath = assertStoreInput "stressControlOraclePath" stressControlOraclePath;
  checkedStressInstrumentedOraclePath = assertStoreInput "stressInstrumentedOraclePath" stressInstrumentedOraclePath;
  checkedStressParserPath = assertStoreInput "stressParserPath" stressParserPath;

  # storePath retains derivation context, ensuring every declared runtime input
  # is copied into the guest closure rather than merely named in runtime.env.
  protonTool = if runtimeBundleEnabled then builtins.storePath checkedProtonToolPath else null;
  steamRuntime = if runtimeBundleEnabled then builtins.storePath checkedSteamRuntimePath else null;
  probeExe = if runtimeBundleEnabled then builtins.storePath checkedProbeExePath else null;
  probeFixture = if runtimeBundleEnabled then builtins.storePath checkedProbeFixturePath else null;
  probeParser = if runtimeBundleEnabled then builtins.storePath checkedProbeParserPath else null;
  stressProtonTool = if stressBundleEnabled then builtins.storePath checkedStressProtonToolPath else null;
  stressSteamRuntime = if stressBundleEnabled then builtins.storePath checkedStressSteamRuntimePath else null;
  stressDriverExe = if stressBundleEnabled then builtins.storePath checkedStressDriverExePath else null;
  stressScenario = if stressBundleEnabled then builtins.storePath checkedStressScenarioPath else null;
  stressFixtureManifest = if stressBundleEnabled then builtins.storePath checkedStressFixtureManifestPath else null;
  stressFixtureBytes = if stressBundleEnabled then builtins.storePath checkedStressFixtureBytesPath else null;
  stressServiceConfig = if stressBundleEnabled then builtins.storePath checkedStressServiceConfigPath else null;
  stressControlOracle = if stressBundleEnabled then builtins.storePath checkedStressControlOraclePath else null;
  stressInstrumentedOracle = if stressBundleEnabled then builtins.storePath checkedStressInstrumentedOraclePath else null;
  stressParser = if stressBundleEnabled then builtins.storePath checkedStressParserPath else null;

  validStressBuildRoles = [
    "stock-ge-control"
    "rtsp-reference-control"
    "frozen-regression-control"
    "streaming-base-candidate"
    "full-parity-candidate"
  ];
  validStressCaseRoles = [ "expected-pass" "negative-control" "qualification" ];
  validStressMediaKinds = [ "av" "audio-only" "video-only" ];

  labManifest = pkgs.writeText "rtsp-media-lab-manifest.json" (builtins.toJSON {
    schema = 1;
    nixpkgs = {
      revision = pin.revision;
      narHash = pin.nar_hash_sri;
      version = lib.version;
    };
    isolation = {
      guestNetwork = "loopback-only";
      hostDirectoryShares = false;
      hostNixStoreMount = false;
      legacyVaLayout = false;
      mmapRandomizationBits = 31;
      rootState = "tmpfs-ephemeral";
      writableNixStore = false;
    };
    resources = {
      vcpus = cores;
      memoryMiB = memoryMiB;
    };
    runtimeBundle = runtimeBundleEnabled;
    runtimeProbeRequested = runRuntimeProbe;
    stressBundle = stressBundleEnabled;
    stressDriverRequested = runStressDriver;
    stressExecution = "paired-control-and-audio-monitor";
  });

  labAssets = pkgs.runCommand "rtsp-media-lab-assets" {
    nativeBuildInputs = [ pkgs.python3 ];
  } ''
    install -d "$out/bin" "$out/share/http-stream-fixture" "$out/share/media-config"
    install -m 0555 ${./guest-smoke.sh} "$out/bin/rtsp-media-lab-smoke"
    install -m 0555 ${./run-proton-probe.sh} "$out/bin/rtsp-media-lab-proton-probe"
    install -m 0555 ${./run-stress-driver.sh} "$out/bin/rtsp-media-lab-stress-driver"
    substituteInPlace \
      "$out/bin/rtsp-media-lab-smoke" \
      "$out/bin/rtsp-media-lab-proton-probe" \
      "$out/bin/rtsp-media-lab-stress-driver" \
      --replace-fail '#!/usr/bin/env bash' '#!${pkgs.bash}/bin/bash'
    install -m 0444 ${./result_contract.py} "$out/bin/result-contract"
    install -m 0444 ${./store_input_digest.py} "$out/bin/store-input-digest"
    install -m 0444 ${./failure_diagnostic.py} "$out/bin/failure-diagnostic"
    install -m 0444 ${./stress_fixture_service.py} "$out/bin/stress-fixture-service"
    install -m 0444 ${./stress_fixture_service.py} "$out/bin/stress_fixture_service.py"
    install -m 0444 ${./transport_oracle.py} "$out/bin/transport-oracle"
    install -m 0444 ${labManifest} "$out/share/manifest.json"
    install -m 0444 ${../http-stream-fixture/http_stream_fixture.py} \
      "$out/share/http-stream-fixture/http_stream_fixture.py"
    install -m 0444 ${../http-stream-fixture/test_http_stream_fixture.py} \
      "$out/share/http-stream-fixture/test_http_stream_fixture.py"
    install -m 0444 ${../http-stream-fixture/smoke_test.py} \
      "$out/share/http-stream-fixture/smoke_test.py"
    install -m 0444 ${../media/mediamtx.yml} "$out/share/media-config/mediamtx.yml"
    install -m 0444 ${../media/mediamtx-hls-live.yml} \
      "$out/share/media-config/mediamtx-hls-live.yml"
    test -f "$out/bin/stress-fixture-service"
    test -f "$out/bin/stress_fixture_service.py"
    test -f "$out/bin/transport-oracle"
    test -f "$out/bin/failure-diagnostic"
    grep -Fq 'CLOCK_BASIS = "linux-clock-monotonic-raw-v1"' \
      "$out/share/http-stream-fixture/http_stream_fixture.py"
    grep -Fq '"clock_basis": CLOCK_BASIS' \
      "$out/share/http-stream-fixture/http_stream_fixture.py"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$out/bin" \
      python3 "$out/bin/transport-oracle" --help >/dev/null
  '';

  runtimeEnvironment = {
    RTSP_LAB_RESULT_CONTRACT = "${labAssets}/bin/result-contract";
    RTSP_LAB_STORE_DIGEST = "${labAssets}/bin/store-input-digest";
    RTSP_LAB_STRESS_FIXTURE_SERVICE = "${labAssets}/bin/stress-fixture-service";
    RTSP_LAB_TRANSPORT_ORACLE = "${labAssets}/bin/transport-oracle";
    RTSP_LAB_FAILURE_DIAGNOSTIC = "${labAssets}/bin/failure-diagnostic";
    RTSP_LAB_HTTP_FIXTURE = "${labAssets}/share/http-stream-fixture/http_stream_fixture.py";
    RTSP_LAB_HARNESS_ROOT = toString labAssets;
  } // lib.optionalAttrs runtimeBundleEnabled {
    RTSP_LAB_PROTON_TOOL = toString protonTool;
    RTSP_LAB_STEAM_RUNTIME = toString steamRuntime;
    RTSP_LAB_PROBE_EXE = toString probeExe;
    RTSP_LAB_PROBE_FIXTURE = toString probeFixture;
    RTSP_LAB_PROBE_PARSER = toString probeParser;
  } // lib.optionalAttrs stressBundleEnabled {
    RTSP_LAB_STRESS_PROTON_TOOL = toString stressProtonTool;
    RTSP_LAB_STRESS_STEAM_RUNTIME = toString stressSteamRuntime;
    RTSP_LAB_STRESS_DRIVER_EXE = toString stressDriverExe;
    RTSP_LAB_STRESS_SCENARIO = toString stressScenario;
    RTSP_LAB_STRESS_FIXTURE_MANIFEST = toString stressFixtureManifest;
    RTSP_LAB_STRESS_FIXTURE_BYTES = toString stressFixtureBytes;
    RTSP_LAB_STRESS_SERVICE_CONFIG = toString stressServiceConfig;
    RTSP_LAB_STRESS_CONTROL_ORACLE = toString stressControlOracle;
    RTSP_LAB_STRESS_INSTRUMENTED_ORACLE = toString stressInstrumentedOracle;
    RTSP_LAB_STRESS_PARSER = toString stressParser;
    RTSP_LAB_STRESS_BUILD_ROLE = stressBuildRole;
    RTSP_LAB_STRESS_CASE_ROLE = stressCaseRole;
    RTSP_LAB_STRESS_MEDIA_KIND = stressMediaKind;
  };
in

assert lib.assertMsg (builtins.pathExists nixpkgsRoot) "nixpkgsPath does not exist";
assert lib.assertMsg (pin.schema == 1) "unsupported nixpkgs pin schema";
assert lib.assertMsg (builtins.match "^[0-9a-f]{40}$" pin.revision != null)
  "nixpkgs pin revision must be a full commit";
assert lib.assertMsg (memoryMiB >= 1024 && memoryMiB <= 8192)
  "memoryMiB must be between 1024 and 8192";
assert lib.assertMsg (!runStressDriver || memoryMiB >= 8192)
  "runStressDriver requires 8192 MiB for two fresh runtime/prefix experiments";
assert lib.assertMsg (cores >= 1 && cores <= 8) "cores must be between 1 and 8";
assert lib.assertMsg (noRuntimeInputs || runtimeBundleEnabled)
  "supply all five runtime inputs or none of them";
assert lib.assertMsg (!runRuntimeProbe || runtimeBundleEnabled)
  "runRuntimeProbe requires the complete runtime bundle";
assert lib.assertMsg (noStressInputs || stressBundleEnabled)
  "supply all ten stress-driver inputs or none of them";
assert lib.assertMsg (!runStressDriver || stressBundleEnabled)
  "runStressDriver requires the complete stress-driver bundle";
assert lib.assertMsg (!(runRuntimeProbe && runStressDriver))
  "runRuntimeProbe and runStressDriver are separate qualification VMs";
assert lib.assertMsg (builtins.elem stressBuildRole validStressBuildRoles)
  "stressBuildRole is not a declared build role";
assert lib.assertMsg (builtins.elem stressCaseRole validStressCaseRoles)
  "stressCaseRole is not a declared case role";
assert lib.assertMsg (builtins.elem stressMediaKind validStressMediaKinds)
  "stressMediaKind is not a declared media kind";
assert lib.assertMsg (
  !runtimeBundleEnabled || builtins.all builtins.pathExists [
    protonTool
    steamRuntime
    probeExe
    probeFixture
    probeParser
  ]
) "one or more runtime bundle paths do not exist";
assert lib.assertMsg (
  !stressBundleEnabled || builtins.all builtins.pathExists [
    stressProtonTool
    stressSteamRuntime
    stressDriverExe
    stressScenario
    stressFixtureManifest
    stressFixtureBytes
    stressServiceConfig
    stressControlOracle
    stressInstrumentedOracle
    stressParser
  ]
) "one or more stress-driver bundle paths do not exist";

pkgs.testers.runNixOSTest {
  name = "rtsp-media-lab-smoke";
  # Two independently cold runs each retain a 300-second process bound. Leave
  # bounded room for two verified Steam Runtime copies and VM setup/teardown.
  globalTimeout = if runStressDriver then 1200 else 300;

  nodes.machine = { pkgs, lib, ... }: {
    virtualisation = {
      inherit cores;
      memorySize = memoryMiB;

      # The writable root is RAM-backed and disappears with QEMU.  The store
      # closure is copied into a separate image instead of mounting the host
      # store.  Force away qemu-vm.nix's default xchg/shared 9p mounts too.
      diskImage = null;
      useNixStoreImage = true;
      mountHostNixStore = false;
      writableStore = false;
      sharedDirectories = lib.mkForce { };
      useHostCerts = false;

      vlans = [ ];
      restrictNetwork = true;
      forwardPorts = [ ];
      graphics = false;

      # qemu-vm.nix otherwise adds a restricted SLiRP NIC even when the NixOS
      # test VLAN list is empty.  The lab services communicate only over
      # loopback, so omit the emulated NIC altogether.
      qemu.networkingOptions = lib.mkForce [ "-nic none" ];
    };

    networking = {
      useDHCP = false;
      nameservers = [ ];
      firewall = {
        enable = true;
        allowedTCPPorts = [ ];
        allowedUDPPorts = [ ];
      };
    };

    services.resolved.enable = false;
    # NixOS otherwise raises x86-64 mmap entropy to the architectural maximum
    # (32 bits).  Wine-staging's inherited syscall-emulation filter treats
    # addresses below 0x700000000000 as Windows syscall sites; the extreme low
    # tail of 32-bit top-down randomization can place its static-PIE preloader
    # just below that boundary and SIGSYS its first native syscall.  Thirty-one
    # bits remains above the upstream kernel default while making Wine's native
    # address invariant deterministic.  This changes only the disposable test
    # guest, never the candidate Proton runtime or host.
    boot.kernel.sysctl = {
      "vm.legacy_va_layout" = 0;
      "vm.mmap_rnd_bits" = 31;
    };
    # mesa-demos supplies glxinfo, but the actual DRI/Gallium driver closure is
    # provided by the NixOS graphics module.  The x86_64 stress driver needs
    # only the native-width software renderer.
    hardware.graphics.enable = stressBundleEnabled;
    # The guest consumes an already-registered, read-only store image and
    # never performs Nix operations. Disabling Nix also suppresses QEMU VM's
    # register-nix-paths writer, which is incompatible with writableStore=false.
    nix.enable = false;
    security.sudo.enable = false;
    users.mutableUsers = false;
    users.groups.media-lab = { };
    users.users.media-lab = {
      isNormalUser = true;
      uid = 1000;
      group = "media-lab";
      home = "/var/lib/media-lab";
      createHome = true;
    };

    environment.systemPackages = [
      labAssets
      pkgs.bash
      pkgs.coreutils
      pkgs.findutils
      pkgs.gawk
      pkgs.gnugrep
      pkgs.gnused
      pkgs.iproute2
      pkgs.procps
      pkgs.python3
      pkgs.util-linux
    ]
    ++ lib.optionals (runtimeBundleEnabled || stressBundleEnabled) [ pkgs.steam-run-free ]
    ++ lib.optionals stressBundleEnabled [
      pkgs.mesa-demos
      pkgs.pulseaudio
      pkgs.xdpyinfo
      pkgs.xorg-server
    ];

    environment.etc."rtsp-media-lab/manifest.json".source = labManifest;
    environment.etc."rtsp-media-lab/runtime.env".text = lib.concatStringsSep "\n" (
      lib.mapAttrsToList (name: value: "${name}=${value}") runtimeEnvironment
    ) + "\n";

    systemd.tmpfiles.rules = [
      "d /var/lib/rtsp-media-lab 0750 media-lab media-lab -"
      "d /var/lib/rtsp-media-lab/results 0700 media-lab media-lab -"
      "d /var/lib/rtsp-media-lab/runs 0700 media-lab media-lab -"
    ];

    systemd.services.rtsp-media-lab-smoke = {
      description = "RTSP media lab isolation and fixture smoke gate";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" "systemd-tmpfiles-setup.service" ];
      path = [
        pkgs.bash
        pkgs.coreutils
        pkgs.findutils
        pkgs.gawk
        pkgs.gnugrep
        pkgs.iproute2
        pkgs.python3
        pkgs.util-linux
      ];
      environment = {
        RTSP_LAB_ASSET_ROOT = "${labAssets}/share";
        RTSP_LAB_RESULT_CONTRACT = "${labAssets}/bin/result-contract";
        RTSP_LAB_STATE_ROOT = "/var/lib/rtsp-media-lab";
        PYTHONDONTWRITEBYTECODE = "1";
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "media-lab";
        Group = "media-lab";
        ExecStart = "${labAssets}/bin/rtsp-media-lab-smoke";
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectHome = "read-only";
        ProtectSystem = "strict";
        ReadWritePaths = [ "/var/lib/rtsp-media-lab" ];
        # ip(8) needs read-only routing/link queries over netlink to prove
        # that loopback is the guest's only interface.
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
      };
    };
  };

  testScript = ''
    import hashlib
    import json
    import os
    from pathlib import Path
    import re

    evidence_dir = Path(os.environ["out"]) / "evidence"
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=False)
    exported = []

    def export_result(machine, source, filename, kind):
        raw = machine.succeed("cat -- " + source)
        assert len(raw.encode("utf-8")) <= 16384, "guest result is too large"
        value = json.loads(raw)
        assert type(value) is dict, "guest result must be an object"

        if kind == "smoke":
            assert value == {
                "case": "isolation-smoke",
                "hostShares": False,
                "network": "loopback-only",
                "runtimeProbe": False,
                "schema": 1,
                "status": "passed",
                "suite": "rtsp-media-lab",
            }
            assert type(value["schema"]) is int
        elif kind == "runtime":
            assert set(value) == {
                "case", "expected", "parserAccepted", "processExit",
                "schema", "status", "suite",
            }
            assert type(value["schema"]) is int and value["schema"] == 1
            assert value["suite"] == "rtsp-media-lab"
            assert value["case"] == "runtime-probe"
            assert value["status"] == "passed"
            assert value["expected"] in ("a3.7-failure", "candidate-pass")
            assert value["parserAccepted"] is True
            assert type(value["processExit"]) is int
            assert 0 <= value["processExit"] <= 255
        elif kind == "stress":
            assert set(value) == {
                "audioEndpoint", "buildRole", "case", "caseRole",
                "digestSchema", "driver", "fixtureService", "inputSha256",
                "instrumentation", "schema", "status", "suite", "videoSetup",
                "mediaKind",
                "oracleOutcome",
                "oracleFailureCodes",
                "transport",
            }
            assert type(value["schema"]) is int and value["schema"] == 1
            assert value["suite"] == "rtsp-media-lab"
            assert value["case"] == "media-engine-stress"
            assert value["status"] == "passed"
            assert value["digestSchema"] == "sha256-file-or-tree-v1"
            assert value["audioEndpoint"] == "pulseaudio-null-sink-s16le-48000-stereo"
            assert value["videoSetup"] == "xvfb-llvmpipe-headless-no-frame-oracle"
            assert value["fixtureService"] == "progressive-http-loopback-v1"
            assert value["buildRole"] in {
                "stock-ge-control", "rtsp-reference-control", "frozen-regression-control",
                "streaming-base-candidate", "full-parity-candidate",
            }
            assert value["caseRole"] in {"expected-pass", "negative-control", "qualification"}
            assert value["instrumentation"] in {"control", "audio-monitor"}
            assert value["mediaKind"] in {"av", "audio-only", "video-only"}
            transport = value["transport"]
            assert set(transport) == {
                "bodyResponseCount", "clockBasis", "errorResponseCount", "firstPlaybackObservationLagMs",
                "getRequestCount", "initialResponseBytesExpected", "initialResponseBytesSent",
                "initialResponseCompleted", "initialRequestRange", "initialRequestSequence",
                "maxAllowedErrorResponses", "maxRangeRepeatCount", "maxWatcherObservationLagMs",
                "minExpectedErrorResponses", "minExpectedPostSeekRangeResponses",
                "postSeekRangeResponseCount",
                "progressiveScored", "rangeResponseCount", "requestCount", "role", "schema",
                "startupAfterRequestMs", "status", "transferTailAfterPlaybackMs",
            }
            assert transport["schema"] == 1 and transport["status"] == "passed"
            assert transport["clockBasis"] == "linux-clock-monotonic-raw-v1"
            assert transport["role"] in {"media-engine-diagnostic", "streaming-first-qualification"}
            assert 1 <= transport["requestCount"] <= 64
            assert type(transport["minExpectedErrorResponses"]) is int
            assert type(transport["maxAllowedErrorResponses"]) is int
            assert 0 <= transport["minExpectedErrorResponses"] <= transport["errorResponseCount"]
            assert transport["errorResponseCount"] <= transport["maxAllowedErrorResponses"] <= 64
            assert type(transport["minExpectedPostSeekRangeResponses"]) is int
            assert transport["postSeekRangeResponseCount"] >= transport["minExpectedPostSeekRangeResponses"]
            assert transport["rangeResponseCount"] >= transport["postSeekRangeResponseCount"]
            if transport["errorResponseCount"]:
                assert value["caseRole"] == "expected-pass"
            assert transport["maxRangeRepeatCount"] <= 8
            assert -2 <= transport["firstPlaybackObservationLagMs"] <= 250
            assert 0 <= transport["maxWatcherObservationLagMs"] <= 250
            assert transport["maxWatcherObservationLagMs"] >= max(
                0, transport["firstPlaybackObservationLagMs"]
            )
            if transport["role"] == "streaming-first-qualification":
                assert transport["progressiveScored"] is True
                assert 0 <= transport["startupAfterRequestMs"] <= 5000
                assert transport["transferTailAfterPlaybackMs"] >= 10000
                assert type(transport["initialRequestSequence"]) is int
                assert 1 <= transport["initialRequestSequence"] <= transport["requestCount"]
                assert transport["initialRequestRange"] is None or (
                    type(transport["initialRequestRange"]) is str
                    and re.fullmatch(r"bytes=[0-9]+-[0-9]+", transport["initialRequestRange"])
                )
            else:
                assert transport["progressiveScored"] is False
                assert transport["startupAfterRequestMs"] is None
                assert transport["transferTailAfterPlaybackMs"] is None
                assert transport["initialRequestSequence"] is None
                assert transport["initialRequestRange"] is None
            assert value["oracleOutcome"] in {"accepted", "rejected-as-expected"}
            assert type(value["oracleFailureCodes"]) is list
            assert value["oracleFailureCodes"] == sorted(set(value["oracleFailureCodes"]))
            assert all(code in {
                "post-seek-audio-samples", "post-seek-audio-bytes", "post-seek-audio-nonzero",
                "checkpoint-audio-samples", "checkpoint-audio-bytes",
                "checkpoint-audio-payload-missing", "checkpoint-audio-format",
                "checkpoint-audio-nonzero", "checkpoint-audio-delivery-stale",
                "checkpoint-audio-nonzero-stale",
            } for code in value["oracleFailureCodes"])
            assert set(value["inputSha256"]) == {
                "controlOracle", "driverExe", "fixtureBytes", "fixtureManifest",
                "instrumentedOracle", "labHarness", "parser", "protonTool", "scenario",
                "serviceConfig", "steamRuntime",
            }
            assert all(
                type(digest) is str
                and len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
                for digest in value["inputSha256"].values()
            )
            driver = value["driver"]
            assert set(driver) == {
                "durationMs", "exitCode", "parserAccepted", "processExit",
                "records", "result", "sourceGenerations", "staleEvents",
            }
            assert type(driver["parserAccepted"]) is bool
            assert type(driver["records"]) is int and driver["records"] > 0
            assert type(driver["durationMs"]) is int and 0 <= driver["durationMs"] <= 3600000
            assert type(driver["sourceGenerations"]) is int and driver["sourceGenerations"] >= 0
            assert type(driver["staleEvents"]) is int and 0 <= driver["staleEvents"] <= driver["records"]
            assert type(driver["exitCode"]) is int and 0 <= driver["exitCode"] <= 255
            assert type(driver["processExit"]) is int and driver["processExit"] == driver["exitCode"]
            if value["caseRole"] == "negative-control":
                assert value["mediaKind"] != "video-only"
                assert driver["result"] == "pass" and driver["exitCode"] == 0
                if value["instrumentation"] == "control":
                    assert driver["parserAccepted"] is True
                    assert value["oracleOutcome"] == "accepted"
                    assert value["oracleFailureCodes"] == []
                else:
                    assert driver["parserAccepted"] is False
                    assert value["oracleOutcome"] == "rejected-as-expected"
                    assert value["oracleFailureCodes"]
            else:
                assert driver["parserAccepted"] is True
                assert value["oracleOutcome"] == "accepted"
                assert value["oracleFailureCodes"] == []
                assert driver["result"] == "pass" and driver["exitCode"] == 0
        else:
            raise AssertionError("unknown result kind")

        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        target = evidence_dir / filename
        target.write_bytes(canonical)
        digest = hashlib.sha256(canonical).hexdigest()
        exported.append((filename, digest))
        return value

    machine.start()
    machine.wait_for_unit("multi-user.target")
    machine.wait_for_unit("rtsp-media-lab-smoke.service")
    machine.succeed("systemctl is-active --quiet rtsp-media-lab-smoke.service")
    machine.succeed("test -f /var/lib/rtsp-media-lab/results/smoke.ok")
    machine.succeed("test -s /var/lib/rtsp-media-lab/results/smoke-summary.json")
    machine.succeed("test -r /etc/rtsp-media-lab/manifest.json")
    machine.succeed("test -z \"$(ip -o link show | grep -v ' lo:')\"")
    machine.succeed("test -z \"$(ip route show)\"")
    machine.fail("findmnt -rn -t 9p,virtiofs")
    machine.fail("test -e /var/lib/media-lab/.local/share/Steam")
    machine.fail("test -e /var/lib/media-lab/.steam")
    machine.succeed("grep -qx '0' /proc/sys/vm/legacy_va_layout")
    machine.succeed("grep -qx '31' /proc/sys/vm/mmap_rnd_bits")
    export_result(
        machine,
        "/var/lib/rtsp-media-lab/results/smoke-summary.json",
        "smoke-summary.json",
        "smoke",
    )
    ${lib.optionalString runtimeBundleEnabled ''
      machine.succeed("test -s /etc/rtsp-media-lab/runtime.env")
      machine.succeed("test -x ${labAssets}/bin/rtsp-media-lab-proton-probe")
    ''}
    ${lib.optionalString stressBundleEnabled ''
      machine.succeed("test -s /etc/rtsp-media-lab/runtime.env")
      machine.succeed("test -x ${labAssets}/bin/rtsp-media-lab-stress-driver")
    ''}
    ${lib.optionalString runRuntimeProbe ''
      machine.succeed(
        "runuser -u media-lab -- env RTSP_LAB_EXPECT=candidate-pass "
        + "RTSP_LAB_RESULT_CONTRACT=${labAssets}/bin/result-contract "
        + "${labAssets}/bin/rtsp-media-lab-proton-probe",
        timeout=180,
      )
      machine.succeed("test -f /var/lib/rtsp-media-lab/results/runtime-probe.ok")
      machine.succeed("test -s /var/lib/rtsp-media-lab/results/runtime-probe-summary.json")
      export_result(
          machine,
          "/var/lib/rtsp-media-lab/results/runtime-probe-summary.json",
          "runtime-probe-summary.json",
          "runtime",
      )
    ''}
    ${lib.optionalString runStressDriver ''
      machine.succeed(
          "runuser -u media-lab -- ${labAssets}/bin/rtsp-media-lab-stress-driver",
          # Below the test's 1200-second global bound, but above the two
          # independent 300-second process bounds plus two bounded fixture
          # startups and cold-runtime copy/verification work.
          timeout=1080,
      )
      machine.succeed("test -f /var/lib/rtsp-media-lab/results/stress-control.ok")
      machine.succeed("test -f /var/lib/rtsp-media-lab/results/stress-audio-monitor.ok")
      control = export_result(
          machine,
          "/var/lib/rtsp-media-lab/results/stress-control-summary.json",
          "stress-control-summary.json",
          "stress",
      )
      instrumented = export_result(
          machine,
          "/var/lib/rtsp-media-lab/results/stress-audio-monitor-summary.json",
          "stress-audio-monitor-summary.json",
          "stress",
      )
      assert control["instrumentation"] == "control"
      assert instrumented["instrumentation"] == "audio-monitor"
      assert control["inputSha256"] == instrumented["inputSha256"]
      assert control["buildRole"] == instrumented["buildRole"]
      assert control["caseRole"] == instrumented["caseRole"]
      assert control["mediaKind"] == instrumented["mediaKind"]
      assert control["transport"]["role"] == instrumented["transport"]["role"]
    ''}

    checksum_lines = [digest + "  " + filename for filename, digest in sorted(exported)]
    (evidence_dir / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="ascii")
  '';
}
