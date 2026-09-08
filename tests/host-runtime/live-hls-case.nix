# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsPath,
  driverExePath,
  fixtureRootPath,
  port ? 18082,
  source ? "mux",
  instrumentation ? "audio-monitor",
  system ? builtins.currentSystem,
}:

assert builtins.isString nixpkgsPath;
assert builtins.match "^/nix/store/[^/]+-.*" nixpkgsPath != null;
assert builtins.isString driverExePath;
assert builtins.match "^/nix/store/[^/]+-.*" driverExePath != null;
assert builtins.isString fixtureRootPath;
assert builtins.match "^/nix/store/[^/]+-.*" fixtureRootPath != null;
assert builtins.isInt port && port >= 1024 && port <= 65535 && port != 9;
assert builtins.elem source [ "mux" "separate" ];
assert builtins.elem instrumentation [ "audio-monitor" "endpoint-monitor" ];

let
  pin = builtins.fromJSON (builtins.readFile ../media-engine-stress/fixtures/nixpkgs-pin.json);
  nixpkgsRoot = builtins.path {
    path = builtins.toPath nixpkgsPath;
    name = "source";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot { inherit system; config.allowUnfree = false; };
  driverExe = builtins.storePath driverExePath;
  fixtureRoot = builtins.storePath fixtureRootPath;
  scenarioTemplate =
    if source == "mux"
    then ./examples/live-hls-muxed.scenario.in
    else ./examples/live-hls-separate.scenario.in;
  serviceTemplate =
    if source == "mux"
    then ./examples/live-hls-muxed.service.json.in
    else ./examples/live-hls-separate.service.json.in;
  caseLabel = if source == "mux" then "muxed" else "separate";
in

assert pkgs.lib.assertMsg (builtins.pathExists driverExe) "driverExePath does not exist";
assert pkgs.lib.assertMsg (builtins.pathExists fixtureRoot) "fixtureRootPath does not exist";
assert pkgs.lib.assertMsg (builtins.pathExists (fixtureRoot + "/provenance/manifest.json"))
  "fixtureRootPath has no provenance manifest";

pkgs.runCommand "rtsp-media-stress-live-hls-${caseLabel}-case" {
  nativeBuildInputs = [ pkgs.coreutils pkgs.python3 ];
  strictDeps = true;
} ''
  install -d "$out"
  substitute ${scenarioTemplate} "$out/scenario" \
    --replace-fail '@PORT@' ${toString port}

  adapter_sha256=$(sha256sum ${./live_hls_case.py} | cut -d ' ' -f 1)
  publisher_sha256=$(sha256sum ${../media-engine-stress/live-fixture/live_fixture.py} | cut -d ' ' -f 1)
  substitute ${serviceTemplate} "$out/service-config.json" \
    --replace-fail '@PORT@' ${toString port} \
    --replace-fail '@INSTRUMENTATION@' ${instrumentation} \
    --replace-fail '@ADAPTER_SHA256@' "$adapter_sha256" \
    --replace-fail '@PUBLISHER_SHA256@' "$publisher_sha256"

  driver_sha256=$(sha256sum ${pkgs.lib.escapeShellArg (toString driverExe)} | cut -d ' ' -f 1)
  scenario_sha256=$(sha256sum "$out/scenario" | cut -d ' ' -f 1)
  substitute ${./examples/live-hls-muxed.control.oracle.json.in} "$out/control.oracle.json" \
    --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
    --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"
  substitute ${./examples/live-hls-muxed.instrumented.oracle.json.in} "$out/instrumented.oracle.json" \
    --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
    --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"

  ${pkgs.python3}/bin/python3 ${../nix-vm/result_contract.py} check-stress-oracles \
    --control "$out/control.oracle.json" \
    --instrumented "$out/instrumented.oracle.json" \
    --driver-sha256 "$driver_sha256" \
    --scenario-sha256 "$scenario_sha256" \
    --media-kind av
  RTSP_LIVE_FIXTURE_PATH=${../media-engine-stress/live-fixture/live_fixture.py} \
  ${pkgs.python3}/bin/python3 ${./live_hls_case.py} check \
    --config "$out/service-config.json" \
    --fixture-root ${pkgs.lib.escapeShellArg (toString fixtureRoot)} \
    --fixture-manifest ${pkgs.lib.escapeShellArg (toString fixtureRoot + "/provenance/manifest.json")} \
    --scenario "$out/scenario" >/dev/null

  cd "$out"
  sha256sum scenario service-config.json control.oracle.json instrumented.oracle.json > SHA256SUMS
''
