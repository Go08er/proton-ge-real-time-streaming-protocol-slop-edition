# SPDX-License-Identifier: BSD-3-Clause
"""Source guards for the host runner's non-Steam boundary."""

from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from containment_preflight import beneath


ROOT = Path(__file__).resolve().parent
RUNNER_PATH = ROOT / "run-host-stress.sh"
OUTER_PATH = ROOT / "run-contained-host-stress.sh"
PREFLIGHT_PATH = ROOT / "containment_preflight.py"
README_PATH = ROOT / "README.md"
RUNNER = RUNNER_PATH.read_text(encoding="utf-8")
OUTER = OUTER_PATH.read_text(encoding="utf-8")
PREFLIGHT = PREFLIGHT_PATH.read_text(encoding="utf-8")
README = README_PATH.read_text(encoding="utf-8")


class HostRunnerSourceTests(unittest.TestCase):
    def test_outer_runner_is_the_executable_public_entrypoint(self) -> None:
        self.assertTrue(OUTER_PATH.stat().st_mode & stat.S_IXUSR)
        self.assertIn("direct execution is forbidden; use run-contained-host-stress.sh", RUNNER)
        self.assertNotIn("tests/host-runtime/run-host-stress.sh \\\n", README)
        self.assertIn("tests/host-runtime/run-contained-host-stress.sh \\\n", README)

    def test_outer_boundary_unshares_network_process_and_mount_context(self) -> None:
        for token in (
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--ro-bind / /",
            "--clearenv",
            "--tmpfs /home",
            "--tmpfs /root",
            "--tmpfs /run",
            "--tmpfs /tmp",
            '--bind "$outer_root" /var/tmp',
            '--ro-bind "$x_socket" "$x_socket"',
        ):
            self.assertIn(token, OUTER)
        self.assertIn('interfaces != [(1, "lo")]', PREFLIGHT)
        self.assertIn('len(route_lines) != 1', PREFLIGHT)
        self.assertIn('listener.bind(("127.0.0.1", 0))', PREFLIGHT)
        for namespace in ("MNTNS", "NETNS", "PIDNS", "USERNS", "IPCNS", "UTSNS"):
            self.assertIn(f"RTSP_HOST_{namespace}", OUTER)
        self.assertIn('os.uname().nodename != "rtsp-media-test"', PREFLIGHT)
        for forbidden in ("/dev/input", "/dev/snd", "/dev/kfd"):
            self.assertIn(f'Path("{forbidden}")', PREFLIGHT)
            self.assertNotIn(f"--dev-bind {forbidden}", OUTER)
        self.assertIn("for mount_root in /mnt /media", OUTER)
        self.assertIn('mask_arguments+=(--tmpfs "$mount_root")', OUTER)
        self.assertIn("for link_name in current-system opengl-driver opengl-driver-32", OUTER)
        self.assertIn('runtime_link_arguments+=(--symlink "$link_target" "$link_path")', OUTER)
        self.assertIn('os.walk("/run", followlinks=False)', PREFLIGHT)
        self.assertIn("host service socket survived private /run", PREFLIGHT)

    def test_outer_binds_individual_graphics_nodes_only(self) -> None:
        self.assertIn("/dev/dri/card[0-9]* /dev/dri/renderD[0-9]*", OUTER)
        self.assertIn('[[ -c $node ]]', OUTER)
        self.assertNotIn("--dev-bind /dev/dri /dev/dri", OUTER)
        self.assertNotIn("--dev-bind /dev/nvidia-caps /dev/nvidia-caps", OUTER)
        self.assertIn('"major": os.major(info.st_rdev)', PREFLIGHT)
        self.assertIn('"minor": os.minor(info.st_rdev)', PREFLIGHT)

    def test_preflight_is_standalone_and_uses_the_selected_python(self) -> None:
        self.assertIn(
            '"$python_bin" -I "$script_dir/containment_preflight.py" \\\n'
            '        "$display" /var/tmp/outer-containment.json',
            OUTER,
        )
        self.assertNotIn("<<'PY'", OUTER)
        self.assertNotIn('"$python_bin" -I -', OUTER)
        self.assertIn('raise SystemExit(f"contained host preflight: {message}")', PREFLIGHT)
        self.assertIn('if __name__ == "__main__":\n    main()', PREFLIGHT)

    def test_preflight_writable_prefix_matching_is_component_safe(self) -> None:
        self.assertTrue(beneath("/var/tmp", "/var/tmp"))
        self.assertTrue(beneath("/var/tmp/run", "/var/tmp"))
        self.assertFalse(beneath("/var/tmp-other", "/var/tmp"))
        self.assertFalse(beneath("/var", "/var/tmp"))

    def test_preflight_only_exits_before_inner_runtime(self) -> None:
        self.assertIn("--containment-preflight-only", OUTER)
        self.assertIn("RTSP_HOST_PREFLIGHT_ONLY", OUTER)
        preflight_branch = OUTER.index("if [[ ${RTSP_HOST_PREFLIGHT_ONLY:-0} == 1 ]]")
        inner_exec = OUTER.index('exec "$inner_runner" "$@"')
        self.assertLess(preflight_branch, inner_exec)

    def test_results_discovery_matches_the_actual_outer_tree_depth(self) -> None:
        self.assertIn(
            'find "$outer_root" -mindepth 2 -maxdepth 2 -type d '
            "-path '*/rtsp-media-host.*/results'",
            OUTER,
        )

    def test_inner_runner_rejects_direct_execution_before_input_access(self) -> None:
        arguments = [
            "/run/current-system/sw/bin/bash",
            str(RUNNER_PATH),
            "--proton-tool", "/not-a-store-input",
            "--steam-runtime", "/not-a-store-input",
            "--driver-exe", "/not-a-store-input",
            "--fixture-root", "/not-a-store-input",
            "--case-dir", "/not-a-store-input",
            "--python-bin", "/not-a-store-input",
            "--app-id", "999999",
            "--instrumentation", "control",
            "--build-role", "streaming-base-candidate",
            "--case-role", "qualification",
            "--media-kind", "av",
            "--display", ":0",
            "--host-graphics-consent", "I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY",
        ]
        completed = subprocess.run(
            arguments,
            cwd=ROOT,
            env={"PATH": os.environ.get("PATH", "")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("direct execution is forbidden", completed.stderr)
        self.assertNotIn("must be an explicit immutable", completed.stderr)

    def test_explicit_host_graphics_consent_and_synthetic_appid(self) -> None:
        self.assertIn("I_UNDERSTAND_THIS_USES_MY_CURRENT_DISPLAY", RUNNER)
        self.assertIn("app_id >= 990000 && app_id <= 999999", RUNNER)
        self.assertIn("app_id != 438100", RUNNER)
        self.assertLess(RUNNER.index("app_id != 438100"), RUNNER.index("run_root=$(mktemp"))

    def test_all_mutable_compatibility_state_is_fresh_var_tmp(self) -> None:
        self.assertIn("mktemp -d --tmpdir=/var/tmp rtsp-media-host.XXXXXXXX", RUNNER)
        self.assertIn('STEAM_COMPAT_DATA_PATH="$run_root/compatdata"', RUNNER)
        self.assertIn('STEAM_COMPAT_CLIENT_INSTALL_PATH="$run_root/fake-steam"', RUNNER)
        self.assertIn('PRESSURE_VESSEL_VARIABLE_DIR="$runtime_variable_dir"', RUNNER)
        self.assertIn('cd "$run_root"', RUNNER)
        self.assertNotIn(".local/share/Steam", RUNNER)
        self.assertNotIn("steamapps/compatdata", RUNNER)

    def test_pressure_vessel_is_defensively_scoped_inside_outer_boundary(self) -> None:
        self.assertIn("PRESSURE_VESSEL_SHARE_HOME=0", RUNNER)
        self.assertIn('PRESSURE_VESSEL_HOME="$run_root/home"', RUNNER)
        self.assertIn("PRESSURE_VESSEL_SYSTEMD_SCOPE=0", RUNNER)
        self.assertIn('PRESSURE_VESSEL_FILESYSTEMS_RO=/nix/store', RUNNER)
        self.assertIn('PRESSURE_VESSEL_FILESYSTEMS_RW="$run_root"', RUNNER)
        self.assertIn('install -m 600 -- "$containment_manifest"', RUNNER)

    def test_runner_uses_exact_modern_python_everywhere(self) -> None:
        self.assertIn("sys.version_info < (3, 11)", RUNNER)
        self.assertIn('"$python_bin" "$proton_tool/proton"', RUNNER)
        self.assertNotIn("python3 ", RUNNER)

    def test_media_driver_bypasses_the_builtin_steam_wrapper(self) -> None:
        self.assertEqual(
            RUNNER.count('"$steam_run_bin" "$runtime_entry" --verb=waitforexitandrun --'),
            2,
        )
        self.assertIn(
            '"$python_bin" "$proton_tool/proton" getcompatpath "$driver_exe"',
            RUNNER,
        )
        self.assertIn(
            'env -i "${runtime_environment[@]}" PROTON_LOG=0',
            RUNNER,
        )
        self.assertIn(
            '"$python_bin" "$proton_tool/proton" runinprefix',
            RUNNER,
        )
        self.assertNotIn(
            '"$python_bin" "$proton_tool/proton" waitforexitandrun',
            RUNNER,
        )

    def test_media_and_unrelated_download_boundaries_are_explicit(self) -> None:
        self.assertIn('"$fixture_adapter" check', RUNNER)
        self.assertIn(
            'service not in {"progressive-http-v1", "live-hls-v1", "rtsp-live-v1"}',
            RUNNER,
        )
        self.assertIn("the live HLS qualification requires an audio observation mode", RUNNER)
        self.assertIn("the healthy live HLS adapter is qualification-only", RUNNER)
        self.assertIn("the live RTSP qualification requires an audio observation mode", RUNNER)
        self.assertIn("rtsp-live-tcp-blackhole-cancel", RUNNER)
        self.assertIn("rtsp-live-tcp-drain-diagnostics", RUNNER)
        self.assertIn(
            "the RTSP blackhole cancellation gate requires audio-monitor instrumentation",
            RUNNER,
        )
        self.assertIn("fixture_network=rtsp-interleaved-tcp-ipv4-loopback-only", RUNNER)
        self.assertIn("fixture_adapter=$rtsp_live_case", RUNNER)
        self.assertIn(
            "live-hls-v1|rtsp-live-v1) watch_config_argument=(--config \"$service_config\")",
            RUNNER,
        )
        self.assertNotIn("classify-negative", RUNNER)
        self.assertNotIn("declared A3.7 signature", RUNNER)
        self.assertIn("fail-post-open-range|fail-post-open-range-recovery", RUNNER)
        self.assertIn("failed-Range modes are bounded expected-pass error cases", RUNNER)
        self.assertIn("$case_role == expected-pass", RUNNER)
        self.assertIn('case_directory_sha256=$(digest_tree "$case_dir")', RUNNER)
        self.assertIn('containment_manifest_sha256=$(sha256sum -- "$containment_manifest"', RUNNER)
        self.assertIn("declared case role differs from immutable metadata", RUNNER)
        self.assertIn("declared case role differs from reviewed RTSP role", RUNNER)
        for helper in (
            '"$store_digest"', '"$result_contract"', '"$fixture_service"',
            '"$transport_oracle"', '"$http_fixture"', '"$parser"',
            '"$live_hls_case"', '"$live_fixture"', '"$rtsp_live_case"',
            '"$endpoint_audio_oracle"',
        ):
            self.assertIn(helper, RUNNER)
        for proxy_name in (
            "http_proxy=", "https_proxy=", "HTTP_PROXY=", "HTTPS_PROXY=",
            "all_proxy=", "ALL_PROXY=", "no_proxy=", "NO_PROXY=",
        ):
            self.assertNotIn(proxy_name, RUNNER)
        self.assertIn('exec env -i "${runtime_environment[@]}"', RUNNER)

    def test_rtsp_drain_diagnostics_is_a_closed_exact_opt_in(self) -> None:
        exact = (
            "-all,+timestamp,+pid,+tid,+rtspdrain,"
            "warn+dmo,err+dmo,warn+mfplat,err+mfplat"
        )
        self.assertIn("--rtsp-drain-diagnostics", RUNNER)
        self.assertIn("rtsp_drain_diagnostics=0", RUNNER)
        self.assertIn("rtsp_drain_diagnostics=1", RUNNER)
        self.assertIn("wine_debug=-all", RUNNER)
        self.assertIn(f"wine_debug='{exact}'", RUNNER)
        self.assertIn('WINEDEBUG="$wine_debug"', RUNNER)
        self.assertIn(
            "--rtsp-drain-diagnostics is accepted only for the reviewed RTSP "
            "drain-diagnostics case",
            RUNNER,
        )
        self.assertIn(
            "the RTSP drain-diagnostics case requires --rtsp-drain-diagnostics",
            RUNNER,
        )
        self.assertNotIn("--winedebug", RUNNER.lower())

    def test_scheduler_pressure_is_exact_bounded_and_case_scoped(self) -> None:
        for token in (
            "none|scheduler-pressure-v1",
            "scheduler_pressure_helper=\"$script_dir/scheduler_pressure.py\"",
            "--scheduler-pressure-profile \"$scheduler_pressure_profile\"",
            "--scheduler-pressure-helper \"$scheduler_pressure_helper_sha256\"",
            "$case_id == rtsp-live-tcp-reopen",
            "$source_scheme == rtspt",
            'source_scheme = "-" if source_scheme is None else source_scheme',
            'fixture_sha256 = "-" if fixture_sha256 is None else fixture_sha256',
            "8e2a8df84d9f257d3d0d3c24372ec644d9366cd9d734b284fe50d3b6a6346bde",
            "requires the reviewed captured-input RTSPT qualification",
            'exec --role fixture --',
            'exec --role application --',
            'child_pids+=("$scheduler_pressure_pid")',
            'kill -TERM "$scheduler_pressure_pid"',
            'forget_child_pid "$scheduler_pressure_pid"',
            "scheduler-pressure workers exited before driver completion",
            "scheduler-pressure evidence did not meet its calibrated floor",
            '--output "$results_root/scheduler-pressure-summary.json"',
        ):
            self.assertIn(token, RUNNER)
        fixture_start = RUNNER.index('exec --role fixture --')
        load_start = RUNNER.index('"$scheduler_pressure_helper" run')
        driver_start = RUNNER.index('exec --role application --')
        load_stop = RUNNER.index('kill -TERM "$scheduler_pressure_pid"')
        seal = RUNNER.index('"$scheduler_pressure_helper" seal')
        self.assertLess(fixture_start, load_start)
        self.assertLess(load_start, driver_start)
        self.assertLess(driver_start, load_stop)
        self.assertLess(load_stop, seal)
        self.assertIn("wine_debug=-all", RUNNER)
        self.assertNotIn("source_scheme or '-'", RUNNER)
        self.assertNotIn("fixture_sha256 or '-'", RUNNER)
        self.assertNotIn("trace+mfplat", RUNNER)
        self.assertNotIn("trace+dmo", RUNNER)

    def test_audio_and_process_cleanup_are_run_private(self) -> None:
        self.assertIn("module-null-sink sink_name=rtsp_host_null", RUNNER)
        self.assertIn('PULSE_SERVER="unix:$pulse_socket"', RUNNER)
        self.assertIn("cleanup_children", RUNNER)
        self.assertIn("cleanup_survivors", RUNNER)
        self.assertIn("run_root_survivors", RUNNER)
        self.assertIn("rm -rf --", RUNNER)

    def test_endpoint_monitor_uses_only_the_private_null_sink_monitor(self) -> None:
        self.assertIn('parec_bin=$(resolve_command "$parec_bin" parec)', RUNNER)
        self.assertIn('pactl_executable_sha256=$(digest_file "$pactl_bin")', RUNNER)
        self.assertIn('pulseaudio_executable_sha256=$(digest_file "$pulseaudio_bin")', RUNNER)
        self.assertIn('"$parec_bin" \\\n        --record', RUNNER)
        self.assertIn("--device=rtsp_host_null.monitor", RUNNER)
        self.assertIn("--format=s16le", RUNNER)
        self.assertIn("--rate=48000", RUNNER)
        self.assertIn("--channels=2", RUNNER)
        self.assertIn("--channel-map=front-left,front-right", RUNNER)
        self.assertIn("--no-remix", RUNNER)
        self.assertIn("--no-remap", RUNNER)
        self.assertIn('PULSE_SERVER="unix:$pulse_socket"', RUNNER)
        self.assertNotIn("--device=@DEFAULT_SOURCE@", RUNNER)
        self.assertNotIn("/dev/snd", RUNNER)
        self.assertIn('child_pids+=("$endpoint_capture_pid")', RUNNER)
        self.assertIn("stop_endpoint_capture", RUNNER)
        self.assertIn('kill -TERM "$endpoint_capture_pid"', RUNNER)
        self.assertIn('kill -KILL "$endpoint_capture_pid"', RUNNER)
        self.assertIn('wait "$endpoint_capture_pid"', RUNNER)
        self.assertIn("endpoint-monitor is invalid for video-only media", RUNNER)
        self.assertIn('"$endpoint_audio_oracle" anchor', RUNNER)
        self.assertIn("--capture-anchor-frame", RUNNER)
        self.assertIn("--capture-anchor-ns", RUNNER)
        self.assertIn("private endpoint recorder produced no PCM within five seconds", RUNNER)
        self.assertIn('child_has_exited "$endpoint_capture_pid"', RUNNER)
        self.assertIn('"$endpoint_audio_oracle" score', RUNNER)
        self.assertIn('selected_oracle=$control_oracle', RUNNER)
        self.assertIn('monitor_argument=(--audio-monitor)', RUNNER)
        self.assertIn('audio-monitor|endpoint-monitor', RUNNER)
        recorder = RUNNER.index('"$parec_bin" \\\n        --record')
        readiness = RUNNER.index('"$endpoint_audio_oracle" anchor')
        driver = RUNNER.index("driver_pid=$!")
        scorer = RUNNER.index('"$endpoint_audio_oracle" score')
        self.assertLess(recorder, readiness)
        self.assertLess(readiness, driver)
        self.assertLess(driver, scorer)

    def test_oracles_and_path_free_manifest_are_reused(self) -> None:
        for token in (
            '"$result_contract" check-stress-oracles',
            '"$selected_transport_oracle" watch',
            '"$selected_transport_oracle" score',
            '"$parser" "$results_root/driver.jsonl"',
            '"$contract" write-plan',
            '"$contract" write-evidence',
        ):
            self.assertIn(token, RUNNER)

    def test_transport_oracle_resolves_only_its_declared_sibling_directory(self) -> None:
        oracle = ROOT.parent / "nix-vm" / "transport_oracle.py"
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-B", "-E", "-s", str(oracle), "--help"],
                cwd=directory,
                env={"PATH": os.environ.get("PATH", "")},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Join flushed MediaEngine records", completed.stdout)
        self.assertEqual(RUNNER.count('"$python_bin" -B -E -s "$selected_transport_oracle"'), 2)
        self.assertNotIn('"$python_bin" -I "$transport_oracle"', RUNNER)


if __name__ == "__main__":
    unittest.main()
