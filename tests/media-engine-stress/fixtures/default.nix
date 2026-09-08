# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsPath,
  system ? builtins.currentSystem,
  profile ? "smoke",
  observedRtspFixturePath ? null,
}:

assert builtins.isString nixpkgsPath;
assert builtins.match "^/nix/store/[^/]+-.*" nixpkgsPath != null;
assert observedRtspFixturePath == null || builtins.isString observedRtspFixturePath;

let
  pin = builtins.fromJSON (builtins.readFile ./nixpkgs-pin.json);
  suppliedNixpkgsRoot = builtins.toPath nixpkgsPath;
  # Verify the complete source tree before evaluating any expression from it.
  # The shell wrappers perform the same check for a clearer error, but this
  # binding prevents direct `nix-build -f default.nix` from bypassing the pin.
  nixpkgsRoot = builtins.path {
    path = suppliedNixpkgsRoot;
    name = "vrchat-media-pinned-nixpkgs";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot {
    inherit system;
    config.allowUnfree = false;
  };
  inherit (pkgs) lib;

  profilesDocument = builtins.fromJSON (builtins.readFile ./profiles.json);
  knownProfile = builtins.hasAttr profile profilesDocument.profiles;
  selected = profilesDocument.profiles.${profile};
  observedSpecification = profilesDocument.rtsp_reproduction_inputs.observed-heavy-v1;
  observedRtspFixture =
    if observedRtspFixturePath == null then null
    else builtins.path {
      path = builtins.toPath observedRtspFixturePath;
      name = "observed-heavy-v1.mp4";
    };
  ffmpegPackage = pkgs.ffmpeg;
  nixpkgsRevision = pin.revision;
  fixtureSource = pkgs.runCommand "vrchat-media-fixture-source" { } ''
    install -d "$out"
    install -m 0444 ${./profiles.json} "$out/profiles.json"
    install -m 0444 ${./nixpkgs-pin.json} "$out/nixpkgs-pin.json"
    install -m 0555 ${./generate-fixtures.sh} "$out/generate-fixtures.sh"
    install -m 0444 ${./generate_manifest.py} "$out/generate_manifest.py"
    install -m 0444 ${./make_live_schedule.py} "$out/make_live_schedule.py"
    install -m 0444 ${./pair_live_schedules.py} "$out/pair_live_schedules.py"
    install -m 0444 ${./align_live_renditions.py} "$out/align_live_renditions.py"
    install -m 0444 ${./validate_fixtures.py} "$out/validate_fixtures.py"
    install -m 0444 ${./test_fixture_contract.py} "$out/test_fixture_contract.py"
  '';
in

assert lib.assertMsg (builtins.pathExists nixpkgsRoot) "nixpkgsPath does not exist";
assert lib.assertMsg (pin.schema == 1) "unsupported nixpkgs pin schema";
assert lib.assertMsg (builtins.match "^[0-9a-f]{40}$" pin.revision != null)
  "nixpkgs pin revision must be a full commit";
assert lib.assertMsg (builtins.pathExists (nixpkgsRoot + "/default.nix"))
  "nixpkgsPath has no default.nix";
assert lib.assertMsg knownProfile "profile must be smoke or full";
assert lib.assertMsg (profilesDocument.schema == 1) "unsupported profiles schema";

pkgs.runCommand "vrchat-media-engine-fixtures-${profile}" {
  nativeBuildInputs = [
    ffmpegPackage
    pkgs.coreutils
    pkgs.dejavu_fonts
    pkgs.python3
  ];
  strictDeps = true;
  preferLocalBuild = true;

  FIXTURE_ROOT = builtins.placeholder "out";
  FFMPEG_BIN = "${ffmpegPackage}/bin/ffmpeg";
  FFPROBE_BIN = "${ffmpegPackage}/bin/ffprobe";
  PYTHON_BIN = "${pkgs.python3}/bin/python3";
  FONT_FILE = "${pkgs.dejavu_fonts}/share/fonts/truetype/DejaVuSansMono.ttf";
  PROFILE_NAME = profile;
  PRIMARY_DURATION = toString selected.primary_duration_seconds;
  TOPOLOGY_DURATION = toString selected.topology_duration_seconds;
  HLS_DURATION = toString selected.hls_duration_seconds;
  MATRIX_DURATION = toString selected.matrix_duration_seconds;
  WIDTH = toString selected.width;
  HEIGHT = toString selected.height;
  FPS = toString selected.frames_per_second;
  AUDIO_RATE = toString selected.audio_sample_rate;
  AUDIO_CHANNELS = toString selected.audio_channels;
  GOP_SECONDS = toString selected.gop_seconds;
  DELAY_SECONDS = toString selected.delayed_track_seconds;
  HLS_SEGMENT_SECONDS = toString selected.hls_segment_seconds;
  HLS_LIVE_WINDOW = toString selected.hls_live_window_segments;
  LIVE_SCHEDULE_TOOL = "${fixtureSource}/make_live_schedule.py";
  PAIRED_LIVE_SCHEDULE_TOOL = "${fixtureSource}/pair_live_schedules.py";
  ALIGN_LIVE_RENDITIONS_TOOL = "${fixtureSource}/align_live_renditions.py";
  OBSERVED_RTSP_FIXTURE =
    if observedRtspFixture == null then "" else toString observedRtspFixture;
  OBSERVED_RTSP_FIXTURE_SHA256 = observedSpecification.captured_reference_sha256;
} ''
  export LC_ALL=C
  export LANG=C
  export TZ=UTC
  export SOURCE_DATE_EPOCH=1
  export HOME="$TMPDIR/home"
  export PYTHONDONTWRITEBYTECODE=1
  mkdir -p "$HOME"
  umask 022

  cd ${fixtureSource}
  "$PYTHON_BIN" -m unittest -v test_fixture_contract.py

  cd "$TMPDIR"
  ${pkgs.bash}/bin/bash ${fixtureSource}/generate-fixtures.sh
  "$PYTHON_BIN" ${fixtureSource}/generate_manifest.py \
    --root "$FIXTURE_ROOT" \
    --profile "$PROFILE_NAME" \
    --profiles ${fixtureSource}/profiles.json \
    --nixpkgs-pin ${fixtureSource}/nixpkgs-pin.json \
    --ffmpeg "$FFMPEG_BIN" \
    --ffprobe "$FFPROBE_BIN" \
    --nixpkgs-path ${lib.escapeShellArg (toString nixpkgsRoot)} \
    --nixpkgs-revision ${lib.escapeShellArg nixpkgsRevision} \
    --nixpkgs-nar-hash ${lib.escapeShellArg pin.nar_hash_sri} \
    --nixpkgs-version ${lib.escapeShellArg lib.version}
  "$PYTHON_BIN" ${fixtureSource}/validate_fixtures.py \
    --root "$FIXTURE_ROOT" \
    --profiles ${fixtureSource}/profiles.json \
    --ffmpeg "$FFMPEG_BIN" \
    --ffprobe "$FFPROBE_BIN"
''
