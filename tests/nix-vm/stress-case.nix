# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsPath,
  driverExePath,
  fixtureRootPath,
  profile ? "full",
  caseName ? "vod-seek",
  system ? builtins.currentSystem,
}:

assert builtins.isString nixpkgsPath;
assert builtins.match "^/nix/store/[^/]+-.*" nixpkgsPath != null;
assert builtins.isString driverExePath;
assert builtins.match "^/nix/store/[^/]+-.*" driverExePath != null;
assert builtins.isString fixtureRootPath;
assert builtins.match "^/nix/store/[^/]+-.*" fixtureRootPath != null;
assert builtins.isString caseName;

let
  pin = builtins.fromJSON (builtins.readFile ../media-engine-stress/fixtures/nixpkgs-pin.json);
  nixpkgsRoot = builtins.path {
    path = builtins.toPath nixpkgsPath;
    name = "source";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot { inherit system; config.allowUnfree = false; };
  inherit (pkgs) lib;
  driverExe = builtins.storePath driverExePath;
  fixtureRoot = builtins.storePath fixtureRootPath;
  profiles = {
    smoke = {
      forwardSeek = "6.0";
      forwardWait = "7.0";
      backwardSeek = "0.5";
      backwardWait = "1.5";
      redirectSeek = "6.0";
      redirectWait = "7.0";
      noRangeContinueWait = "2.5";
      noRangeRecoveryHoldMs = "1500";
      replacementWait = "2.0";
      eofSeek = "7.0";
      eofDuration = "8.0";
      startupWait = "0.5";
      initialWait = "1.0";
      scrubTargets = [ "4.25" "4.60" "4.95" "5.30" "5.65" "6.00" "6.35" "6.75" ];
      scrubFinalWait = "7.75";
      minPostSeekRangeResponses = "0";
      minTransferTailMs = "0";
      transportRole = "media-engine-diagnostic";
    };
    full = {
      forwardSeek = "83.0";
      forwardWait = "84.0";
      backwardSeek = "3.0";
      backwardWait = "4.0";
      redirectSeek = "70.0";
      redirectWait = "71.0";
      noRangeContinueWait = "15.0";
      noRangeRecoveryHoldMs = "3000";
      replacementWait = "3.0";
      eofSeek = "118.0";
      eofDuration = "120.0";
      startupWait = "1.0";
      initialWait = "12.0";
      scrubTargets = [ "30.0" "38.0" "46.0" "54.0" "62.0" "70.0" "78.0" "83.25" ];
      scrubFinalWait = "84.25";
      minPostSeekRangeResponses = "1";
      minTransferTailMs = "10000";
      transportRole = "streaming-first-qualification";
    };
  };
  selected = profiles.${profile};
  scrubFirst = builtins.elemAt selected.scrubTargets 0;
  scrubFinal = builtins.elemAt selected.scrubTargets 7;
  commonCodecInitialWait = "2.0";
  commonCodecScrubTargets = [
    "6.0"
    "7.5"
    "9.0"
    "10.5"
    "12.0"
    "13.5"
    "15.0"
    "16.25"
  ];
  commonCodecScrubFirst = builtins.elemAt commonCodecScrubTargets 0;
  commonCodecScrubFinal = builtins.elemAt commonCodecScrubTargets 7;
  commonCodecFinalWait = "17.25";
  commonCodecDuration = 20.0;
  number = value: builtins.fromJSON value;
  abs = value: if value < 0 then -value else value;
  outsideGeSynchronizationWindow = left: right:
    abs (number left - number right) > 3.0;
  minimumPostSeekAdvance = 0.75;

  identity = {
    expected_driver_sha256 = "@DRIVER_SHA256@";
    expected_exit_code = 0;
    expected_result = "pass";
    expected_scenario_sha256 = "@SCENARIO_SHA256@";
  };
  healthy = identity // {
    forbidden_events = [ "ERROR" "STREAMRENDERINGERROR" ];
    max_media_error_events = 0;
    max_time_regression = 0.1;
    max_timeout_snapshots = 0;
  };
  audioDelivery = {
    max_audio_nonzero_silence_ms = 750;
    max_audio_silence_ms = 750;
    min_audio_bytes_generation = 4096;
    min_audio_nonzero_units_generation = 20;
    min_audio_samples_generation = 10;
    required_audio_payload_formats = [ "pcm16" "pcm32" "float32" ];
  };

  vodControl = healthy // {
    event_order = [ "CANPLAY" "PLAYING" "SEEKING" "SEEKED" "PAUSE" "PLAYING" ];
    min_action_counts = { seek = 8; snapshot = 9; wait_event = 12; wait_time = 10; };
    min_playback_advance = 1.0;
    min_post_seek_advance = 0.75;
    min_source_generations = 1;
    max_source_generations = 1;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_seek_completion = true;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "seek" "pause" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" "SEEKED" "PAUSE" ];
    required_events_per_generation = [ "CANPLAY" ];
    required_seek_targets = lib.flatten (lib.replicate 4 [
      (number selected.forwardSeek)
      (number selected.backwardSeek)
    ]);
    seek_target_tolerance = 0.5;
  };
  vodInstrumented = vodControl // audioDelivery // {
    audio_check_labels = [ "post-seek-steady" ];
    min_post_seek_audio_bytes = 4096;
    min_post_seek_audio_samples = 10;
    min_post_seek_nonzero_units = 20;
  };

  redirectControl = healthy // {
    checkpoint_expectations = [{
      action = "snapshot";
      label = "redirect-post-seek-steady";
      min_matches = 1;
      max_matches = 1;
      min_time = number selected.redirectWait;
      max_time = number selected.redirectWait + 2.0;
      min_source_generation = 1;
      max_source_generation = 1;
      min_timeline_generation = 2;
      max_timeline_generation = 2;
      paused = false;
      seeking = false;
      ended = false;
      has_audio = true;
      has_video = true;
    }];
    event_order = [ "CANPLAY" "PLAYING" "SEEKING" "SEEKED" ];
    min_action_counts = { seek = 1; snapshot = 1; wait_event = 3; wait_time = 3; };
    min_playback_advance = 1.0;
    min_post_seek_advance = 0.75;
    min_source_generations = 1;
    max_source_generations = 1;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_seek_completion = true;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "seek" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" "SEEKED" ];
    required_events_per_generation = [ "CANPLAY" ];
    required_seek_targets = [ (number selected.redirectSeek) ];
    seek_target_tolerance = 0.5;
  };
  redirectInstrumented = redirectControl // audioDelivery // {
    audio_check_labels = [ "redirect-post-seek-steady" ];
    min_post_seek_audio_bytes = 4096;
    min_post_seek_audio_samples = 10;
    min_post_seek_nonzero_units = 20;
  };

  noRangeControl = healthy // {
    checkpoint_expectations = [{
      action = "snapshot";
      label = "no-range-sequential";
      min_matches = 1;
      max_matches = 1;
      min_time = number selected.noRangeContinueWait;
      max_time = number selected.noRangeContinueWait + 2.0;
      min_source_generation = 1;
      max_source_generation = 1;
      min_timeline_generation = 2;
      max_timeline_generation = 2;
      paused = false;
      seeking = false;
      ended = false;
      has_audio = true;
      has_video = true;
    }];
    event_order = [ "CANPLAY" "PLAYING" ];
    max_event_counts = { SEEKED = 0; };
    min_action_counts = { seek = 1; snapshot = 1; wait_event = 2; wait_ms = 1; wait_time = 2; };
    min_playback_advance = 1.0;
    min_source_generations = 1;
    max_source_generations = 1;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_seek_completion = false;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "seek" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" ];
    required_events_per_generation = [ "CANPLAY" ];
    required_seek_targets = [ (number selected.forwardSeek) ];
    seek_target_tolerance = 0.5;
  };
  noRangeInstrumented = noRangeControl // audioDelivery // {
    audio_check_labels = [ "no-range-sequential" ];
  };

  failedRangeControl = identity // {
    checkpoint_expectations = [
      {
        action = "snapshot";
        label = "pre-failure-steady";
        min_matches = 1;
        max_matches = 1;
        # wait_time accepts the requested target within 1 ms. Use the same
        # tolerance here so a valid frame-clock value such as 11.9998334 is
        # not rejected for the nominal 12-second checkpoint.
        min_time = number selected.initialWait - 0.001;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 1;
        max_timeline_generation = 1;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
      {
        action = "snapshot";
        label = "failed-range-terminal";
        min_matches = 1;
        max_matches = 1;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 2;
        max_timeline_generation = 2;
        seeking = false;
        ended = false;
        media_error = 2;
        media_error_hr = "0xc00d426a";
      }
    ];
    event_expectations = [{
      event = "ERROR";
      min_matches = 1;
      max_matches = 1;
      after_action = "seek";
      param1 = "0x2";
      param2 = "0xc00d426a";
      generation_current = true;
      media_error = 2;
      media_error_hr = "0xc00d426a";
      seeking = false;
      ended = false;
    }];
    event_order = [ "CANPLAY" "PLAYING" "ERROR" ];
    forbidden_events = [ "ENDED" "STREAMRENDERINGERROR" ];
    max_event_counts = { ENDED = 0; ERROR = 1; SEEKED = 0; STREAMRENDERINGERROR = 0; };
    max_media_error_events = 1;
    max_time_regression = 0.1;
    max_timeout_snapshots = 0;
    min_action_counts = { seek = 1; snapshot = 2; wait_event = 3; wait_time = 2; };
    min_playback_advance = 1.0;
    min_source_generations = 1;
    max_source_generations = 1;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_seek_completion = false;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "seek" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" "ERROR" ];
    required_events_per_generation = [ "CANPLAY" ];
    required_seek_targets = [ (number selected.forwardSeek) ];
    seek_target_tolerance = 0.5;
  };
  failedRangeInstrumented = failedRangeControl // audioDelivery // {
    audio_check_labels = [ "pre-failure-steady" ];
  };

  failedRangeRecoveryControl = identity // {
    checkpoint_expectations = [
      {
        action = "snapshot";
        label = "pre-failure-steady";
        min_matches = 1;
        max_matches = 1;
        min_time = number selected.initialWait - 0.001;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 1;
        max_timeline_generation = 1;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
      {
        action = "snapshot";
        label = "failed-range-terminal";
        min_matches = 1;
        max_matches = 1;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 2;
        max_timeline_generation = 2;
        seeking = false;
        ended = false;
        media_error = 2;
        media_error_hr = "0xc00d426a";
      }
      {
        action = "snapshot";
        label = "recovery-generation-steady";
        min_matches = 1;
        max_matches = 1;
        min_time = number selected.replacementWait - 0.001;
        max_time = number selected.replacementWait + 2.0;
        min_source_generation = 2;
        max_source_generation = 2;
        min_timeline_generation = 3;
        max_timeline_generation = 3;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
        media_error = 0;
        media_error_hr = "0x00000000";
      }
    ];
    event_expectations = [{
      event = "ERROR";
      min_matches = 1;
      max_matches = 1;
      after_action = "seek";
      param1 = "0x2";
      param2 = "0xc00d426a";
      generation_current = true;
      media_error = 2;
      media_error_hr = "0xc00d426a";
      seeking = false;
      ended = false;
    }];
    event_order = [ "CANPLAY" "PLAYING" "ERROR" "CANPLAY" "FIRSTFRAMEREADY" ];
    forbidden_events = [ "ENDED" "STREAMRENDERINGERROR" ];
    max_event_counts = {
      CANPLAY = 2;
      ENDED = 0;
      ERROR = 1;
      FIRSTFRAMEREADY = 2;
      PLAYING = 2;
      SEEKED = 0;
      STREAMRENDERINGERROR = 0;
    };
    max_media_error_events = 1;
    max_stale_events = 0;
    max_time_regression = 0.1;
    max_timeout_snapshots = 0;
    min_action_counts = {
      load = 1;
      play = 2;
      replace = 1;
      seek = 1;
      snapshot = 3;
      wait_event = 5;
      wait_time = 3;
    };
    min_playback_advance = 1.0;
    min_source_generations = 2;
    max_source_generations = 2;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_seek_completion = false;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "seek" "replace" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" "ERROR" "FIRSTFRAMEREADY" ];
    required_events_per_generation = [ "CANPLAY" ];
    required_seek_targets = [ (number selected.forwardSeek) ];
    seek_target_tolerance = 0.5;
  };
  failedRangeRecoveryInstrumented =
    failedRangeRecoveryControl // audioDelivery // {
      audio_check_labels = [
        "pre-failure-steady"
        "recovery-generation-steady"
      ];
    };

  finiteEofControl = healthy // {
    checkpoint_expectations = [
      {
        action = "snapshot";
        label = "pre-eof-steady";
        min_matches = 1;
        max_matches = 1;
        min_time = number selected.initialWait - 0.001;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 1;
        max_timeline_generation = 1;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
      {
        action = "snapshot";
        label = "finite-eof-ended";
        min_matches = 1;
        max_matches = 1;
        min_time = number selected.eofSeek;
        max_time = number selected.eofDuration + 0.05;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 2;
        max_timeline_generation = 2;
        seeking = false;
        ended = true;
        has_audio = true;
        has_video = true;
        media_error = 0;
        media_error_hr = "0x00000000";
      }
    ];
    event_expectations = [{
      event = "ENDED";
      min_matches = 1;
      max_matches = 1;
      after_action = "seek";
      param2 = "0x00000000";
      generation_current = true;
      media_error = 0;
      media_error_hr = "0x00000000";
      seeking = false;
      ended = true;
    }];
    event_order = [ "CANPLAY" "PLAYING" "SEEKING" "SEEKED" "ENDED" ];
    max_event_counts = {
      ENDED = 1;
      ERROR = 0;
      SEEKED = 1;
      STREAMRENDERINGERROR = 0;
    };
    max_stale_events = 0;
    min_action_counts = {
      load = 1;
      play = 1;
      seek = 1;
      snapshot = 2;
      wait_event = 4;
      wait_time = 2;
    };
    min_playback_advance = 1.0;
    min_post_seek_advance = 0.75;
    min_source_generations = 1;
    max_source_generations = 1;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_seek_completion = true;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "seek" "snapshot" "shutdown" ];
    required_completed_seek_targets = [ (number selected.eofSeek) ];
    required_events = [ "CANPLAY" "PLAYING" "SEEKED" "ENDED" ];
    required_events_per_generation = [ "CANPLAY" "PLAYING" "SEEKED" "ENDED" ];
    required_seek_targets = [ (number selected.eofSeek) ];
    seek_target_tolerance = 0.5;
  };
  finiteEofInstrumented = finiteEofControl // audioDelivery // {
    audio_check_labels = [ "pre-eof-steady" ];
  };

  replacementControl = healthy // {
    checkpoint_expectations = [{
      action = "snapshot";
      label = "replacement-generation-steady";
      min_matches = 1;
      max_matches = 1;
      # wait_time succeeds when the MediaEngine clock is within 1 ms of the
      # requested target. Keep the following snapshot on that same contract
      # instead of rejecting a valid 2.999xxx observation for a 3.0 target.
      min_time = number selected.replacementWait - 0.001;
      max_time = number selected.replacementWait + 2.0;
      min_source_generation = 2;
      max_source_generation = 2;
      min_timeline_generation = 3;
      max_timeline_generation = 3;
      paused = false;
      seeking = false;
      ended = false;
      has_audio = true;
      has_video = true;
    }];
    event_order = [ "CANPLAY" "PLAYING" ];
    max_event_counts = {
      CANPLAY = 1;
      ERROR = 0;
      PLAYING = 1;
      SEEKED = 0;
      STREAMRENDERINGERROR = 0;
    };
    max_stale_events = 0;
    min_action_counts = {
      load = 1;
      replace = 1;
      seek = 1;
      snapshot = 1;
      wait_event = 2;
      wait_time = 2;
    };
    min_playback_advance = 1.0;
    min_source_generations = 2;
    max_source_generations = 2;
    require_seek_completion = false;
    required_actions = [ "load" "seek" "replace" "play" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" ];
    required_seek_targets = [ (number selected.forwardSeek) ];
    seek_target_tolerance = 0.5;
  };
  replacementInstrumented = replacementControl // audioDelivery // {
    audio_check_labels = [ "replacement-generation-steady" ];
  };

  runningReplacementControl = healthy // {
    checkpoint_expectations = [
      {
        action = "snapshot";
        label = "http-running-g1";
        min_matches = 4;
        max_matches = 4;
        min_time_span = 2.5;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 1;
        max_timeline_generation = 1;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
      {
        action = "snapshot";
        label = "http-running-g2";
        min_matches = 4;
        max_matches = 4;
        min_time_span = 2.5;
        min_source_generation = 2;
        max_source_generation = 2;
        min_timeline_generation = 2;
        max_timeline_generation = 2;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
    ];
    min_action_counts = {
      load = 1;
      play = 2;
      replace = 1;
      snapshot = 8;
      wait_event = 4;
      wait_ms = 8;
      wait_time = 2;
    };
    min_playback_advance = 2.5;
    min_source_generations = 2;
    max_source_generations = 2;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_video_each_generation = true;
    required_actions = [ "load" "play" "replace" "snapshot" "shutdown" ];
    required_events = [ "CANPLAY" "PLAYING" ];
    required_events_per_generation = [ "CANPLAY" "PLAYING" ];
  };
  runningReplacementInstrumented = runningReplacementControl // audioDelivery // {
    audio_check_labels = [ "http-running-g1" "http-running-g2" ];
  };

  rapidScrubControl = healthy // {
    checkpoint_expectations = [
      {
        action = "snapshot";
        label = "rapid-scrub-settled";
        min_matches = 1;
        max_matches = 1;
        min_time = number scrubFinal - 0.5;
        max_time = number scrubFinal + 0.75;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 9;
        max_timeline_generation = 9;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
      {
        action = "snapshot";
        label = "rapid-scrub-post-settle";
        min_matches = 2;
        max_matches = 2;
        min_time = number selected.scrubFinalWait - 0.001;
        max_time = number selected.scrubFinalWait + 3.0;
        min_time_span = 0.75;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 9;
        max_timeline_generation = 9;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
    ];
    event_order = [ "CANPLAY" "PLAYING" "SEEKING" "SEEKED" ];
    max_monotonic_ms = 120000;
    max_seek_action_span_ms = 200;
    min_action_counts = {
      load = 1;
      play = 1;
      seek = 8;
      snapshot = 4;
      wait_event = 2;
      wait_ms = 1;
      wait_seek_settled = 1;
      wait_time = 3;
    };
    min_source_generations = 1;
    max_source_generations = 1;
    require_audio_each_generation = true;
    require_av_same_snapshot_each_generation = true;
    require_video_each_generation = true;
    required_actions = [
      "load"
      "play"
      "seek"
      "wait_seek_settled"
      "snapshot"
      "shutdown"
    ];
    required_completed_seek_targets = [ (number scrubFinal) ];
    required_events = [ "CANPLAY" "PLAYING" "SEEKED" ];
    required_events_per_generation = [ "CANPLAY" "PLAYING" ];
    required_seek_targets = map number selected.scrubTargets;
    seek_target_tolerance = 0.5;
  };
  rapidScrubInstrumented = rapidScrubControl // audioDelivery // {
    audio_check_labels = [ "rapid-scrub-post-settle" ];
  };
  rapidScrubReference = rapidScrubControl // {
    required_completed_seek_targets = [ (number scrubFirst) (number scrubFinal) ];
  };
  commonCodecRapidScrubControl = rapidScrubControl // {
    checkpoint_expectations = [
      {
        action = "snapshot";
        label = "rapid-scrub-common-settled";
        min_matches = 1;
        max_matches = 1;
        min_time = number commonCodecScrubFinal - 0.5;
        max_time = number commonCodecScrubFinal + 0.75;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 9;
        max_timeline_generation = 9;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
      {
        action = "snapshot";
        label = "rapid-scrub-common-post-settle";
        min_matches = 2;
        max_matches = 2;
        min_time = number commonCodecFinalWait - 0.001;
        max_time = number commonCodecFinalWait + 2.0;
        min_time_span = 0.75;
        min_source_generation = 1;
        max_source_generation = 1;
        min_timeline_generation = 9;
        max_timeline_generation = 9;
        paused = false;
        seeking = false;
        ended = false;
        has_audio = true;
        has_video = true;
      }
    ];
    required_completed_seek_targets = [ (number commonCodecScrubFinal) ];
    required_seek_targets = map number commonCodecScrubTargets;
  };
  commonCodecRapidScrubInstrumented =
    commonCodecRapidScrubControl // audioDelivery // {
      audio_check_labels = [ "rapid-scrub-common-post-settle" ];
    };
  commonCodecRapidScrubReference = commonCodecRapidScrubControl // {
    required_completed_seek_targets = [
      (number commonCodecScrubFirst)
      (number commonCodecScrubFinal)
    ];
  };

  cases = {
    "vod-seek" = {
      id = "vod-seek-loopback";
      mode = "range";
      scenario = ./examples/vod-seek-loopback.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        FORWARD_SEEK = selected.forwardSeek;
        FORWARD_WAIT = selected.forwardWait;
        BACKWARD_SEEK = selected.backwardSeek;
        BACKWARD_WAIT = selected.backwardWait;
      };
      controlOracle = vodControl;
      instrumentedOracle = vodInstrumented;
      maxErrors = 0;
      minPostSeek = number selected.minPostSeekRangeResponses;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = if selected.transportRole == "streaming-first-qualification"
        then "qualification" else "expected-pass";
      expectedOutcome = "progressive-range-seek-and-resume";
      limitations = [ ];
    };
    "progressive-fixed-redirect" = {
      id = "progressive-fixed-redirect";
      mode = "redirect";
      scenario = ./examples/progressive-fixed-redirect.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        REDIRECT_SEEK = selected.redirectSeek;
        REDIRECT_WAIT = selected.redirectWait;
      };
      controlOracle = redirectControl;
      instrumentedOracle = redirectInstrumented;
      maxErrors = 0;
      minPostSeek = number selected.minPostSeekRangeResponses;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = if selected.transportRole == "streaming-first-qualification"
        then "qualification" else "expected-pass";
      expectedOutcome = "redirect-target-progressive-and-seekable";
      limitations = [ ];
    };
    "progressive-no-range" = {
      id = "progressive-no-range";
      mode = "no-range";
      scenario = ./examples/progressive-no-range.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        UNSUPPORTED_SEEK = selected.forwardSeek;
        RECOVERY_HOLD_MS = selected.noRangeRecoveryHoldMs;
      };
      controlOracle = noRangeControl;
      instrumentedOracle = noRangeInstrumented;
      maxErrors = 0;
      minPostSeek = 0;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = if selected.transportRole == "streaming-first-qualification"
        then "qualification" else "expected-pass";
      expectedOutcome = "progressive-sequential-playback-without-seek-success";
      limitations = [
        "The current driver cannot query the native seekable-range property; it proves no SEEKED event, no 206 response, no duplicate full transfer, and continued linear time instead."
      ];
    };
    "http-failed-range-terminal" = {
      id = "http-failed-range-terminal";
      mode = "fail-post-open-range";
      scenario = ./examples/http-failed-range-terminal.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        FAILED_SEEK = selected.forwardSeek;
      };
      controlOracle = failedRangeControl;
      instrumentedOracle = failedRangeInstrumented;
      maxErrors = 1;
      minPostSeek = 0;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = "expected-pass";
      expectedOutcome = "one-terminal-media-error-and-no-range-retry";
      limitations = [ ];
    };
    "http-failed-range-recovery" = {
      id = "http-failed-range-recovery";
      mode = "fail-post-open-range-recovery";
      scenario = ./examples/http-failed-range-recovery.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        FAILED_SEEK = selected.forwardSeek;
        RECOVERY_WAIT = selected.replacementWait;
      };
      controlOracle = failedRangeRecoveryControl;
      instrumentedOracle = failedRangeRecoveryInstrumented;
      maxErrors = 1;
      minPostSeek = 0;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = "expected-pass";
      expectedOutcome =
        "one-terminal-media-error-then-clean-generation-two-recovery";
      limitations = [
        "The sanitized HTTP log deliberately omits URL paths. The hash-bound scenario selects /recovery, the service reserves that route for this mode, and a healthy response after the exact 503 plus advancing generation two jointly prove recovery."
      ];
    };
    "finite-eof-near-end" = {
      id = "finite-eof-near-end";
      mode = "range";
      scenario = ./examples/finite-eof-near-end.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        EOF_SEEK = selected.eofSeek;
      };
      controlOracle = finiteEofControl;
      instrumentedOracle = finiteEofInstrumented;
      maxErrors = 0;
      minPostSeek = number selected.minPostSeekRangeResponses;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = if selected.transportRole == "streaming-first-qualification"
        then "qualification" else "expected-pass";
      expectedOutcome =
        "near-end-seek-completes-and-true-finite-eof-emits-ended";
      limitations = [ ];
    };
    "source-replacement-generation" = {
      id = "source-replacement-generation";
      mode = "range";
      scenario = ./examples/source-replacement-generation.scenario.in;
      substitutions = {
        OLD_SEEK = selected.forwardSeek;
        STARTUP_WAIT = selected.startupWait;
        REPLACEMENT_WAIT = selected.replacementWait;
      };
      controlOracle = replacementControl;
      instrumentedOracle = replacementInstrumented;
      maxErrors = 0;
      minPostSeek = 0;
      minTransferTailMs = 0;
      role = "media-engine-diagnostic";
      headerDelaySeconds = 1.5;
      caseRole = "expected-pass";
      expectedOutcome = "replacement-generation-discards-old-pending-seek-and-plays-from-zero";
      limitations = [
        "Both generations use the same A/V member; this proves generation/state isolation but not old-versus-new frame provenance or topology replacement."
        "The shared header delay overlaps opens but does not guarantee the old resolver completes last."
      ];
    };
    "running-http-replacement" = {
      id = "running-http-replacement";
      mode = "range";
      scenario = ./examples/running-http-replacement.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
      };
      controlOracle = runningReplacementControl;
      instrumentedOracle = runningReplacementInstrumented;
      maxErrors = 0;
      minPostSeek = 0;
      minTransferTailMs = 0;
      role = "media-engine-diagnostic";
      headerDelaySeconds = 0.0;
      caseRole = "expected-pass";
      expectedOutcome = "running-source-replacement-starts-and-advances-the-new-topology";
      limitations = [
        "Both generations use the same A/V member; query tokens distinguish requests but not decoded frame content."
      ];
    };
    "rapid-scrub-parity" = {
      id = "rapid-scrub-parity";
      mode = "range";
      scenario = ./examples/rapid-scrub-parity.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = selected.initialWait;
        SCRUB_1 = builtins.elemAt selected.scrubTargets 0;
        SCRUB_2 = builtins.elemAt selected.scrubTargets 1;
        SCRUB_3 = builtins.elemAt selected.scrubTargets 2;
        SCRUB_4 = builtins.elemAt selected.scrubTargets 3;
        SCRUB_5 = builtins.elemAt selected.scrubTargets 4;
        SCRUB_6 = builtins.elemAt selected.scrubTargets 5;
        SCRUB_7 = builtins.elemAt selected.scrubTargets 6;
        SCRUB_8 = builtins.elemAt selected.scrubTargets 7;
        FINAL_WAIT = selected.scrubFinalWait;
      };
      controlOracle = rapidScrubControl;
      instrumentedOracle = rapidScrubInstrumented;
      referenceOracle = rapidScrubReference;
      maxErrors = 0;
      minPostSeek = number selected.minPostSeekRangeResponses;
      minTransferTailMs = number selected.minTransferTailMs;
      role = selected.transportRole;
      headerDelaySeconds = 0.0;
      caseRole = if selected.transportRole == "streaming-first-qualification"
        then "qualification" else "expected-pass";
      expectedOutcome = "rapid-scrub-health-plus-rtsp-leading-and-latest-completion-comparison";
      limitations = [
        "The sidecar oracle proves leading-versus-trailing seek scheduling and final settlement; it does not prove decoded-frame previews or a literal one-second completion cadence."
      ];
    };
    "rapid-scrub-common-codec" = {
      id = "rapid-scrub-common-codec";
      mode = "range";
      scenario = ./examples/rapid-scrub-common-codec.scenario.in;
      substitutions = {
        STARTUP_WAIT = selected.startupWait;
        INITIAL_WAIT = commonCodecInitialWait;
        SCRUB_1 = builtins.elemAt commonCodecScrubTargets 0;
        SCRUB_2 = builtins.elemAt commonCodecScrubTargets 1;
        SCRUB_3 = builtins.elemAt commonCodecScrubTargets 2;
        SCRUB_4 = builtins.elemAt commonCodecScrubTargets 3;
        SCRUB_5 = builtins.elemAt commonCodecScrubTargets 4;
        SCRUB_6 = builtins.elemAt commonCodecScrubTargets 5;
        SCRUB_7 = builtins.elemAt commonCodecScrubTargets 6;
        SCRUB_8 = builtins.elemAt commonCodecScrubTargets 7;
        FINAL_WAIT = commonCodecFinalWait;
      };
      controlOracle = commonCodecRapidScrubControl;
      instrumentedOracle = commonCodecRapidScrubInstrumented;
      referenceOracle = commonCodecRapidScrubReference;
      fixtureRelativePath = "discovery/codecs/vp9-opus.webm";
      maxErrors = 0;
      minPostSeek = 0;
      minTransferTailMs = 0;
      role = "media-engine-diagnostic";
      headerDelaySeconds = 0.0;
      caseRole = "expected-pass";
      expectedOutcome = "common-vp9-opus-rapid-scrub-health-plus-rtsp-leading-and-latest-completion-comparison";
      limitations = [
        "This full-profile-only comparison uses the 20-second VP9/Opus WebM discovery member so Alpha2's GStreamer path and A3.11 exercise a shared codec pair without host FFmpeg codec libraries."
        "The short 2.3 MiB member can be fully buffered at the declared fixture rate, so this case does not qualify download-while-playback behavior or post-seek Range transport; retain rapid-scrub-parity for the 120-second H.264/AAC transport case."
        "The sidecar oracle proves leading-versus-trailing seek scheduling and final settlement; it does not prove decoded-frame previews or a literal one-second completion cadence."
      ];
    };
  };
  selectedCase = builtins.getAttr caseName cases;
  scenarioArgs = lib.concatStringsSep " " (lib.mapAttrsToList
    (name: value: "--replace-fail '@${name}@' ${lib.escapeShellArg value}")
    selectedCase.substitutions);
  serviceConfig = pkgs.writeText "${caseName}-${profile}-service.json" (builtins.toJSON {
    bind = "127.0.0.1";
    caseId = selectedCase.id;
    chunkBytes = 65536;
    failCount = 1;
    failStatus = 503;
    fixtureRelativePath =
      selectedCase.fixtureRelativePath or "av/faststart.mp4";
    headerDelaySeconds = selectedCase.headerDelaySeconds;
    maxConcurrent = 4;
    maxErrorResponses = selectedCase.maxErrors;
    maxLogBytes = 131072;
    maxObservedRequests = 64;
    maxRangeRepeats = 8;
    maxRequests = 256;
    maxStartupMs = 5000;
    minPostSeekRangeResponses = selectedCase.minPostSeek;
    minTransferTailMs = selectedCase.minTransferTailMs;
    mode = selectedCase.mode;
    port = 18765;
    rateKiB = 1024;
    schema = 1;
    service = "progressive-http-v1";
    stallAfterBytes = 262144;
    stallSeconds = 2.0;
    transportRole = selectedCase.role;
    truncateAfterBytes = 262144;
  });
  controlTemplate = pkgs.writeText "${caseName}-${profile}-control-oracle.json"
    (builtins.toJSON selectedCase.controlOracle);
  instrumentedTemplate = pkgs.writeText "${caseName}-${profile}-instrumented-oracle.json"
    (builtins.toJSON selectedCase.instrumentedOracle);
  referenceTemplate = if selectedCase ? referenceOracle
    then pkgs.writeText "${caseName}-${profile}-rtsp-reference-oracle.json"
      (builtins.toJSON selectedCase.referenceOracle)
    else null;
  caseMetadata = pkgs.writeText "${caseName}-${profile}-metadata.json" (builtins.toJSON {
    schema = 1;
    caseId = selectedCase.id;
    inherit profile;
    caseRole = selectedCase.caseRole;
    transportRole = selectedCase.role;
    expectedOutcome = selectedCase.expectedOutcome;
    limitations = selectedCase.limitations;
    network = "ipv4-loopback-only";
    runtimeExecuted = false;
  });
in

assert lib.assertMsg (builtins.pathExists driverExe) "driverExePath does not exist";
assert lib.assertMsg (builtins.pathExists fixtureRoot) "fixtureRootPath does not exist";
assert lib.assertMsg (builtins.pathExists (fixtureRoot + "/provenance/manifest.json"))
  "fixtureRootPath has no provenance manifest";
assert lib.assertMsg (builtins.hasAttr profile profiles) "profile must be smoke or full";
assert lib.assertMsg (builtins.hasAttr caseName cases)
  "caseName must be vod-seek, progressive-no-range, progressive-fixed-redirect, http-failed-range-terminal, http-failed-range-recovery, finite-eof-near-end, source-replacement-generation, running-http-replacement, rapid-scrub-parity, or rapid-scrub-common-codec";
assert lib.assertMsg (caseName != "rapid-scrub-common-codec" || profile == "full")
  "rapid-scrub-common-codec requires the full fixture profile";
assert lib.assertMsg (builtins.length selected.scrubTargets == 8)
  "the rapid-scrub discriminator requires exactly eight dispatch targets";
assert lib.assertMsg
  (number (builtins.elemAt selected.scrubTargets 0) < number (builtins.elemAt selected.scrubTargets 1)
    && number (builtins.elemAt selected.scrubTargets 1) < number (builtins.elemAt selected.scrubTargets 2)
    && number (builtins.elemAt selected.scrubTargets 2) < number (builtins.elemAt selected.scrubTargets 3)
    && number (builtins.elemAt selected.scrubTargets 3) < number (builtins.elemAt selected.scrubTargets 4)
    && number (builtins.elemAt selected.scrubTargets 4) < number (builtins.elemAt selected.scrubTargets 5)
    && number (builtins.elemAt selected.scrubTargets 5) < number (builtins.elemAt selected.scrubTargets 6)
    && number (builtins.elemAt selected.scrubTargets 6) < number (builtins.elemAt selected.scrubTargets 7))
  "rapid-scrub targets must be strictly increasing";
assert lib.assertMsg
  (outsideGeSynchronizationWindow scrubFirst selected.initialWait
    && number selected.scrubFinalWait - number scrubFinal >= minimumPostSeekAdvance)
  "rapid-scrub targets do not distinguish a real leading seek and final advancement";
assert lib.assertMsg (builtins.length commonCodecScrubTargets == 8)
  "the common-codec rapid-scrub discriminator requires exactly eight dispatch targets";
assert lib.assertMsg
  (number (builtins.elemAt commonCodecScrubTargets 0) < number (builtins.elemAt commonCodecScrubTargets 1)
    && number (builtins.elemAt commonCodecScrubTargets 1) < number (builtins.elemAt commonCodecScrubTargets 2)
    && number (builtins.elemAt commonCodecScrubTargets 2) < number (builtins.elemAt commonCodecScrubTargets 3)
    && number (builtins.elemAt commonCodecScrubTargets 3) < number (builtins.elemAt commonCodecScrubTargets 4)
    && number (builtins.elemAt commonCodecScrubTargets 4) < number (builtins.elemAt commonCodecScrubTargets 5)
    && number (builtins.elemAt commonCodecScrubTargets 5) < number (builtins.elemAt commonCodecScrubTargets 6)
    && number (builtins.elemAt commonCodecScrubTargets 6) < number (builtins.elemAt commonCodecScrubTargets 7))
  "common-codec rapid-scrub targets must be strictly increasing";
assert lib.assertMsg
  (outsideGeSynchronizationWindow commonCodecScrubFirst commonCodecInitialWait
    && number commonCodecFinalWait - number commonCodecScrubFinal >= minimumPostSeekAdvance
    && number commonCodecFinalWait <= commonCodecDuration - 0.25)
  "common-codec rapid-scrub targets do not distinguish a leading seek, final advancement, and the fixture duration margin";
assert lib.assertMsg
  (number selected.startupWait >= 0.5
    && number selected.startupWait < number selected.initialWait)
  "the playback checkpoint must be observable and precede the uninterrupted pre-seek hold";
assert lib.assertMsg
  (outsideGeSynchronizationWindow selected.eofSeek selected.initialWait
    && number selected.eofDuration - number selected.eofSeek >= minimumPostSeekAdvance
    && number selected.eofSeek <= number selected.eofDuration - 0.25)
  "the finite-EOF seek does not leave a bounded post-seek playback interval";
assert lib.assertMsg
  (outsideGeSynchronizationWindow selected.forwardSeek selected.initialWait
    && outsideGeSynchronizationWindow selected.backwardSeek selected.forwardWait
    && outsideGeSynchronizationWindow selected.forwardSeek selected.backwardWait
    && outsideGeSynchronizationWindow selected.redirectSeek selected.initialWait)
  "a seek followed by wait_event SEEKED falls inside GE's three-second synchronization window";
assert lib.assertMsg
  (number selected.forwardWait - number selected.forwardSeek >= minimumPostSeekAdvance
    && number selected.backwardWait - number selected.backwardSeek >= minimumPostSeekAdvance
    && number selected.redirectWait - number selected.redirectSeek >= minimumPostSeekAdvance)
  "post-seek wait_time does not satisfy the oracle's minimum playback advance";
assert lib.assertMsg
  (number selected.noRangeContinueWait > number selected.initialWait
    && number selected.noRangeContinueWait + 3.0 < number selected.forwardSeek)
  "no-Range sequential checkpoint does not distinguish an ignored seek from a jump";
assert lib.assertMsg
  (abs ((number selected.initialWait + number selected.noRangeRecoveryHoldMs / 1000.0)
    - number selected.noRangeContinueWait) <= 0.001)
  "no-Range wall-clock hold does not reach the declared sequential checkpoint";

pkgs.runCommand "rtsp-media-stress-${caseName}-${profile}-case" {
  nativeBuildInputs = [ pkgs.coreutils pkgs.python3 ];
  strictDeps = true;
} ''
  install -d "$out"
  substitute ${selectedCase.scenario} "$out/scenario" ${scenarioArgs}
  install -m 0444 ${serviceConfig} "$out/service-config.json"
  install -m 0444 ${caseMetadata} "$out/case-metadata.json"

  driver_sha256=$(sha256sum ${lib.escapeShellArg (toString driverExe)} | cut -d ' ' -f 1)
  scenario_sha256=$(sha256sum "$out/scenario" | cut -d ' ' -f 1)
  substitute ${controlTemplate} "$out/control.oracle.json" \
    --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
    --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"
  substitute ${instrumentedTemplate} "$out/instrumented.oracle.json" \
    --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
    --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"
  ${lib.optionalString (selectedCase ? referenceOracle) ''
    substitute ${referenceTemplate} "$out/rtsp-reference.oracle.json" \
      --replace-fail '@DRIVER_SHA256@' "$driver_sha256" \
      --replace-fail '@SCENARIO_SHA256@' "$scenario_sha256"
    ${pkgs.python3}/bin/python3 -I - \
      "$out/rtsp-reference.oracle.json" \
      ${../media-engine-stress/driver/parse_results.py} <<'PY'
import importlib.util
import pathlib
import sys

oracle_path = pathlib.Path(sys.argv[1])
parser_path = pathlib.Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("media_stress_parser", parser_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
oracle = module.load_oracle(oracle_path)
module.validate_oracle(oracle)
PY
  ''}

  ${pkgs.python3}/bin/python3 ${./result_contract.py} check-stress-oracles \
    --control "$out/control.oracle.json" \
    --instrumented "$out/instrumented.oracle.json" \
    --driver-sha256 "$driver_sha256" \
    --scenario-sha256 "$scenario_sha256" \
    --media-kind av
  ${pkgs.python3}/bin/python3 ${./stress_fixture_service.py} check \
    --config "$out/service-config.json" \
    --fixture-root ${lib.escapeShellArg (toString fixtureRoot)} \
    --fixture-manifest ${lib.escapeShellArg (toString fixtureRoot + "/provenance/manifest.json")} \
    --scenario "$out/scenario" >/dev/null
  ${pkgs.python3}/bin/python3 -c \
    'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")); assert value["runtimeExecuted"] is False' \
    "$out/case-metadata.json"

  cd "$out"
  checksum_inputs=(
    scenario
    service-config.json
    control.oracle.json
    instrumented.oracle.json
    case-metadata.json
  )
  ${lib.optionalString (selectedCase ? referenceOracle) ''
    checksum_inputs+=(rtsp-reference.oracle.json)
  ''}
  sha256sum "''${checksum_inputs[@]}" > SHA256SUMS
''
