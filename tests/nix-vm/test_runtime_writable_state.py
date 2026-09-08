# SPDX-License-Identifier: BSD-3-Clause
"""Source-level guards for immutable Steam Runtime inputs."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class RuntimeWritableStateTests(unittest.TestCase):
    def test_stress_launcher_checks_the_wine_preloader_stack_limit(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn('"legacyVaLayout":false', text)
        self.assertIn('"mmapRandomizationBits":31', text)
        self.assertIn("[[ $(ulimit -S -s) == 8192 ]]", text)
        self.assertIn("exec env -i", text)
        self.assertGreater(
            text.index("[[ $(ulimit -S -s) == 8192 ]]"),
            text.index("run_one()"),
        )
        self.assertLess(
            text.index("[[ $(ulimit -S -s) == 8192 ]]"),
            text.index("exec env -i"),
        )

    def test_vm_pins_wine_compatible_high_entropy_address_layout(self) -> None:
        text = (ROOT / "default.nix").read_text(encoding="utf-8")
        self.assertIn('"vm.legacy_va_layout" = 0;', text)
        self.assertIn('"vm.mmap_rnd_bits" = 31;', text)
        self.assertIn("mmapRandomizationBits = 31;", text)
        self.assertIn("legacyVaLayout = false;", text)
        self.assertIn("grep -qx '0' /proc/sys/vm/legacy_va_layout", text)
        self.assertIn("grep -qx '31' /proc/sys/vm/mmap_rnd_bits", text)

    def test_probe_uses_fresh_run_local_writable_state(self) -> None:
        text = (ROOT / "run-proton-probe.sh").read_text(encoding="utf-8")
        self.assertIn('"$run_root/steam-runtime-var"', text)
        self.assertIn(
            'PRESSURE_VESSEL_VARIABLE_DIR="$run_root/steam-runtime-var"', text
        )
        self.assertIn(
            'STEAM_LINUX_RUNTIME_LOG_DIR="$run_root/results/steam-runtime"', text
        )

    def test_paired_stress_runs_have_independent_ephemeral_runtime_state(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn('runtime_variable_dir="$run_root/steam-runtime-var"', text)
        self.assertIn('runtime_copy_dir="$run_root/steam-runtime-copy"', text)
        self.assertIn('PRESSURE_VESSEL_VARIABLE_DIR="$runtime_variable_dir"', text)
        self.assertIn(
            'STEAM_LINUX_RUNTIME_LOG_DIR="$run_root/results/steam-runtime"', text
        )
        self.assertNotIn('runtime_variable_dir="$state_root/', text)
        self.assertNotIn('runtime_copy_dir="$state_root/', text)
        self.assertIn('rm -rf -- "$run_root"', text)
        self.assertNotIn('rm -rf -- "$runtime_variable_dir"', text)
        self.assertNotIn('rm -rf -- "$runtime_copy_dir"', text)

    def test_writable_runtime_copy_is_verified_before_execution(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn('--compare-copy "$runtime_copy_dir"', text)
        self.assertIn('[[ "$copied_runtime_sha256" == "$steam_runtime_sha256" ]]', text)
        self.assertLess(
            text.index('--compare-copy "$runtime_copy_dir"'),
            text.index('runtime_entry="$runtime_copy_dir/$runtime_entry_relative"'),
        )
        self.assertIn(
            'run_root_survivors "$run_root"',
            text,
        )

    def test_stress_vm_enforces_capacity_for_two_cold_runs(self) -> None:
        text = (ROOT / "default.nix").read_text(encoding="utf-8")
        self.assertIn("!runStressDriver || memoryMiB >= 8192", text)
        self.assertIn("globalTimeout = if runStressDriver then 1200 else 300;", text)
        self.assertIn("timeout=1080,", text)
        self.assertIn("!(runRuntimeProbe && runStressDriver)", text)

    def test_renderer_failure_reports_only_the_renderer_identity(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn("renderer string|Device):'", text)
        self.assertIn("'^(Error:|libGL error:|MESA-LOADER:)'", text)
        self.assertIn("observed GL renderer:", text)
        self.assertIn("GL setup diagnostic:", text)
        self.assertNotIn('cat "$run_root/results/glxinfo.txt"', text)

    def test_software_renderer_uses_the_gallium_selector(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertEqual(text.count("GALLIUM_DRIVER=llvmpipe"), 2)
        self.assertNotIn("MESA_LOADER_DRIVER_OVERRIDE=llvmpipe", text)
        self.assertIn("LC_ALL=C LIBGL_ALWAYS_SOFTWARE=1", text)
        self.assertIn("^OpenGL renderer string:[[:space:]]*llvmpipe", text)
        self.assertIn("Accelerated:[[:space:]]*no", text)

    def test_stress_guest_installs_native_mesa_driver_closure(self) -> None:
        text = (ROOT / "default.nix").read_text(encoding="utf-8")
        self.assertIn("hardware.graphics.enable = stressBundleEnabled;", text)
        self.assertNotIn("hardware.graphics.enable32Bit", text)

    def test_transport_watcher_receives_marker_only_after_process_exit(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn('--producer-done "$producer_done"', text)
        self.assertIn('>"$producer_done.tmp"', text)
        self.assertIn('mv -- "$producer_done.tmp" "$producer_done"', text)
        self.assertLess(
            text.index("process_exit=$?"),
            text.index('>"$producer_done.tmp"'),
        )
        self.assertLess(
            text.index('mv -- "$producer_done.tmp" "$producer_done"'),
            text.index('for _ in $(seq 1 1000)'),
        )
        self.assertNotIn('tail -n 20 "$run_root/results/driver-console.log"', text)
        self.assertNotIn('head -c 4096', text)
        self.assertNotIn('transport-watch.log" |', text)
        self.assertIn("sanitized failure classification", text)
        self.assertNotIn('cat "$run_root/results/driver-console.log"', text)

    def test_timeout_keeps_the_coreutils_multicall_applet_name(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn("timeout_bin=$(command -v timeout)", text)
        self.assertNotIn('timeout_bin=$(readlink -f', text)

    def test_nonzero_process_exit_is_checked_independently_of_watcher_result(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn(
            'if [[ $process_exit -ne 0 || $watcher_stuck == yes || $watcher_exit -ne 0 ||',
            text,
        )
        self.assertIn('kill "$watcher_pid" 2>/dev/null', text)
        self.assertLess(
            text.index('if [[ $process_exit -ne 0 || $watcher_stuck == yes'),
            text.index('parser_summary="$run_root/results/parser-summary.json"'),
        )

    def test_inner_runtime_sees_only_declared_guest_inputs_and_run_state(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn("PRESSURE_VESSEL_FILESYSTEMS_RO=/nix/store", text)
        self.assertIn('PRESSURE_VESSEL_FILESYSTEMS_RW="$run_root"', text)
        self.assertIn(
            'STEAM_COMPAT_TOOL_PATHS="$RTSP_LAB_STRESS_PROTON_TOOL:$runtime_copy_dir"',
            text,
        )
        self.assertIn('STEAM_COMPAT_INSTALL_PATH="$stress_install_dir"', text)
        self.assertIn("STEAM_LINUX_RUNTIME_LOG=1", text)
        self.assertIn("STEAM_LINUX_RUNTIME_VERBOSE=1", text)

    def test_synthetic_app_uses_non_steam_launch_contract(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn('UMU_ID="$app_id"', text)
        self.assertIn("UMU_USE_STEAM=0", text)
        self.assertIn("PROTONFIXES_DISABLE=1", text)
        self.assertIn("PROTON_USE_XALIA=0", text)
        self.assertIn(
            'driver_exe_windows=$(windows_path "$RTSP_LAB_STRESS_DRIVER_EXE")',
            text,
        )
        self.assertIn('"$driver_exe_windows" \\', text)
        self.assertIn(
            '"$python_bin" "$RTSP_LAB_STRESS_PROTON_TOOL/proton" waitforexitandrun',
            text,
        )
        self.assertNotIn(
            '"$python_bin" "$RTSP_LAB_STRESS_PROTON_TOOL/proton" run', text
        )

    def test_offline_upscaler_probe_fails_locally_without_blocking_fixture(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn("http_proxy=http://127.0.0.1:9", text)
        self.assertIn("https_proxy=http://127.0.0.1:9", text)
        self.assertIn("no_proxy=127.0.0.1,localhost", text)

    def test_fixture_log_is_gracefully_sealed_before_transport_scoring(self) -> None:
        text = (ROOT / "run-stress-driver.sh").read_text(encoding="utf-8")
        self.assertIn('--completion-marker "$service_completion"', text)
        self.assertIn(
            'graceful_stop_fixture_service "$service_pid" "$service_completion"',
            text,
        )
        self.assertIn('--service-completion "$service_completion"', text)
        self.assertNotIn("Give request handlers one", text)
        self.assertLess(
            text.index('graceful_stop_fixture_service "$service_pid" "$service_completion"'),
            text.index('python3 "$RTSP_LAB_TRANSPORT_ORACLE" score'),
        )


if __name__ == "__main__":
    unittest.main()
