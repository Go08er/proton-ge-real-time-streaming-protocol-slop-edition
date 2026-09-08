# SPDX-License-Identifier: BSD-3-Clause
{
  nixpkgsPath,
  system ? builtins.currentSystem,
  originMemoryMiB ? 1024,
  clientMemoryMiB ? 1536,
  clientCores ? 2,
}:

assert builtins.isString nixpkgsPath;
assert builtins.match "^/nix/store/[^/]+-.*" nixpkgsPath != null;

let
  pin = builtins.fromJSON (builtins.readFile ../media-engine-stress/fixtures/nixpkgs-pin.json);
  suppliedNixpkgsRoot = builtins.toPath nixpkgsPath;
  nixpkgsRoot = builtins.path {
    path = suppliedNixpkgsRoot;
    name = "source";
    sha256 = pin.nar_hash_sri;
  };
  pkgs = import nixpkgsRoot {
    inherit system;
    config.allowUnfree = false;
  };
  inherit (pkgs) lib;

  originAddress = "192.0.2.10";
  clientAAddress = "192.0.2.20";
  clientBAddress = "192.0.2.21";
  prefixLength = 24;

  payload = pkgs.writeText "rtsp-media-fault-payload.bin"
    (lib.concatStrings (lib.replicate 4096 "RTSP-MEDIA-FAULT-LAB\n"));

  dnsmasqConfig = pkgs.writeText "rtsp-media-fault-dnsmasq.conf" ''
    no-daemon
    no-resolv
    no-hosts
    bind-interfaces
    listen-address=${originAddress}
    user=media-lab
    address=/media.test/${originAddress}
    address=/missing.media.test/
    log-facility=-
    log-queries=extra
  '';

  originAssets = pkgs.runCommand "rtsp-media-fault-origin-assets" { } ''
    install -d "$out/bin" "$out/share/http-fixture"
    install -m 0555 ${./fault-profile.sh} "$out/bin/rtsp-media-fault-profile"
    install -m 0444 ${./result_contract.py} "$out/bin/result-contract"
    install -m 0444 ${../http-stream-fixture/http_stream_fixture.py} \
      "$out/share/http-fixture/http_stream_fixture.py"
    install -m 0444 ${payload} "$out/share/payload.bin"
    install -m 0444 ${dnsmasqConfig} "$out/share/dnsmasq.conf"
  '';

  hardenVm = { lib, ... }: {
    virtualisation = {
      vlans = [ 1 ];
      restrictNetwork = true;
      forwardPorts = [ ];
      graphics = false;
      diskImage = null;
      useNixStoreImage = true;
      mountHostNixStore = false;
      writableStore = false;
      sharedDirectories = lib.mkForce { };
      useHostCerts = false;
    };
    networking = {
      useDHCP = false;
      defaultGateway = null;
      nameservers = [ originAddress ];
      firewall.enable = true;
    };
    services.resolved.enable = false;
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
  };

  clientNode = address: { pkgs, ... }: {
    imports = [ hardenVm ];
    virtualisation = {
      cores = clientCores;
      memorySize = clientMemoryMiB;
    };
    networking.interfaces.eth1.ipv4.addresses = [ {
      inherit address prefixLength;
    } ];
    environment.systemPackages = [
      pkgs.curl
      pkgs.iproute2
      pkgs.procps
      pkgs.util-linux
    ];
  };
in

assert lib.assertMsg (builtins.pathExists nixpkgsRoot) "nixpkgsPath does not exist";
assert lib.assertMsg (pin.schema == 1) "unsupported nixpkgs pin schema";
assert lib.assertMsg (builtins.match "^[0-9a-f]{40}$" pin.revision != null)
  "nixpkgs pin revision must be a full commit";
assert lib.assertMsg (originMemoryMiB >= 512 && originMemoryMiB <= 2048)
  "originMemoryMiB must be between 512 and 2048";
assert lib.assertMsg (clientMemoryMiB >= 1024 && clientMemoryMiB <= 4096)
  "clientMemoryMiB must be between 1024 and 4096";
assert lib.assertMsg (clientCores >= 1 && clientCores <= 4)
  "clientCores must be between 1 and 4";

pkgs.testers.runNixOSTest {
  name = "rtsp-media-private-fault-lab";
  globalTimeout = 300;

  nodes = {
    origin = { pkgs, lib, ... }: {
      imports = [ hardenVm ];
      virtualisation = {
        cores = 1;
        memorySize = originMemoryMiB;
      };
      networking = {
        interfaces.eth1.ipv4.addresses = [ {
          address = originAddress;
          inherit prefixLength;
        } ];
        firewall = {
          allowedTCPPorts = [ 53 8080 ];
          allowedUDPPorts = [ 53 ];
        };
      };
      environment.systemPackages = [
        originAssets
        pkgs.coreutils
        pkgs.dnsmasq
        pkgs.findutils
        pkgs.gnugrep
        pkgs.iproute2
        pkgs.python3
        pkgs.util-linux
      ];
      environment.etc."rtsp-media-fault-lab/target-ipv4".text = "${clientAAddress}\n";
      systemd.tmpfiles.rules = [
        "d /var/lib/rtsp-media-fault-lab 0750 media-lab media-lab -"
        "d /var/lib/rtsp-media-fault-lab/results 0700 media-lab media-lab -"
      ];
      systemd.services.rtsp-media-fault-dns = {
        description = "Private fault-lab authoritative DNS fixture";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ];
        serviceConfig = {
          ExecStart = "${pkgs.dnsmasq}/bin/dnsmasq --conf-file=${originAssets}/share/dnsmasq.conf";
          User = "media-lab";
          Group = "media-lab";
          AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
          CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ];
          NoNewPrivileges = true;
          PrivateDevices = true;
          ProtectHome = true;
          ProtectSystem = "strict";
        };
      };
      systemd.services.rtsp-media-fault-http = {
        description = "Private fault-lab deterministic HTTP fixture";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" "systemd-tmpfiles-setup.service" ];
        serviceConfig = {
          User = "media-lab";
          Group = "media-lab";
          ExecStart = lib.concatStringsSep " " [
            "${pkgs.python3}/bin/python3"
            "${originAssets}/share/http-fixture/http_stream_fixture.py"
            "--file ${originAssets}/share/payload.bin"
            "--mode range"
            "--case-id private-vlan-smoke"
            "--bind-scope vm-private"
            "--host ${originAddress}"
            "--port 8080"
            "--rate-kib 0"
            "--max-requests 64"
            "--max-concurrent 4"
            "--max-log-bytes 32768"
            "--log /var/lib/rtsp-media-fault-lab/results/http.jsonl"
          ];
          Restart = "on-failure";
          NoNewPrivileges = true;
          PrivateDevices = true;
          ProtectHome = true;
          ProtectSystem = "strict";
          ReadWritePaths = [ "/var/lib/rtsp-media-fault-lab" ];
          RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" ];
        };
      };
    };
    clientA = clientNode clientAAddress;
    clientB = clientNode clientBAddress;
  };

  testScript = ''
    import hashlib
    import json
    import math
    import os
    from pathlib import Path

    evidence_dir = Path(os.environ["out"]) / "evidence"
    evidence_dir.mkdir(mode=0o755, parents=True, exist_ok=False)

    def request_seconds(machine):
        output = machine.succeed(
            "curl --fail --silent --show-error --output /dev/null --max-time 5 "
            "--write-out '%{time_total}' http://${originAddress}:8080/media"
        ).strip()
        value = float(output)
        assert math.isfinite(value) and 0 <= value <= 30
        return value

    def apply_profile(profile):
        output = origin.succeed(
            "${originAssets}/bin/rtsp-media-fault-profile "
            + profile
            + " ${clientAAddress}"
        )
        if profile == "clean":
            assert "qdisc prio 1:" not in output
        else:
            assert "qdisc prio 1:" in output
            assert ("qdisc netem 20:" in output or "qdisc tbf 20:" in output)

    def export_fault_result():
        source = "/var/lib/rtsp-media-fault-lab/results/fault-summary.json"
        raw = origin.succeed("cat -- " + source)
        assert len(raw.encode("utf-8")) <= 16384, "guest result is too large"
        value = json.loads(raw)
        assert type(value) is dict
        assert set(value) == {
            "case", "control", "faultOwner", "measurements", "profiles",
            "schema", "status", "suite", "target",
        }
        assert type(value["schema"]) is int and value["schema"] == 1
        assert value["suite"] == "rtsp-media-fault-lab"
        assert value["case"] == "targeted-client-impairment"
        assert value["status"] == "passed"
        assert value["faultOwner"] == "origin-egress"
        assert value["target"] == "client-a"
        assert value["control"] == "client-b"
        assert value["profiles"] == [
            "clean", "delay", "jitter", "loss", "duplicate", "reorder",
            "rate", "blackhole",
        ]
        measurements = value["measurements"]
        assert type(measurements) is dict
        assert set(measurements) == {
            "blackholeControlServed", "blackholeTargetBlocked",
            "cleanControlSeconds", "cleanTargetSeconds",
            "delayControlSeconds", "delayTargetSeconds",
            "recoveryTargetServed",
        }
        assert measurements["blackholeControlServed"] is True
        assert measurements["blackholeTargetBlocked"] is True
        assert measurements["recoveryTargetServed"] is True
        for key in (
            "cleanControlSeconds", "cleanTargetSeconds",
            "delayControlSeconds", "delayTargetSeconds",
        ):
            assert type(measurements[key]) in (int, float)
            assert math.isfinite(float(measurements[key]))
            assert 0 <= float(measurements[key]) <= 30
        assert measurements["delayTargetSeconds"] >= 0.20
        assert measurements["delayTargetSeconds"] - measurements["cleanTargetSeconds"] >= 0.15
        assert measurements["delayTargetSeconds"] - measurements["delayControlSeconds"] >= 0.15
        assert measurements["delayControlSeconds"] <= max(
            0.50, measurements["cleanControlSeconds"] + 0.25
        )

        canonical = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        filename = "fault-summary.json"
        (evidence_dir / filename).write_bytes(canonical)
        digest = hashlib.sha256(canonical).hexdigest()
        (evidence_dir / "SHA256SUMS").write_text(
            digest + "  " + filename + "\n", encoding="ascii"
        )

    start_all()
    origin.wait_for_unit("multi-user.target")
    origin.wait_for_unit("rtsp-media-fault-dns.service")
    origin.wait_for_unit("rtsp-media-fault-http.service")
    clientA.wait_for_unit("multi-user.target")
    clientB.wait_for_unit("multi-user.target")

    for machine in [origin, clientA, clientB]:
        machine.fail("findmnt -rn -t 9p,virtiofs")
        machine.fail("ip route show default | grep -q .")
        machine.fail("test -e /var/lib/media-lab/.local/share/Steam")
        machine.fail("test -e /var/lib/media-lab/.steam")

    clientA.succeed("getent ahostsv4 media.test | grep -F '${originAddress}'")
    clientB.succeed("getent ahostsv4 media.test | grep -F '${originAddress}'")
    clientA.fail("getent ahostsv4 missing.media.test")

    for machine in [clientA, clientB]:
        machine.succeed(
            "curl --fail --silent --show-error --max-time 5 "
            "http://media.test:8080/media | grep -q RTSP-MEDIA-FAULT-LAB"
        )

    clean_target_seconds = request_seconds(clientA)
    clean_control_seconds = request_seconds(clientB)

    apply_profile("delay")
    delay_target_seconds = request_seconds(clientA)
    delay_control_seconds = request_seconds(clientB)
    assert delay_target_seconds >= 0.20
    assert delay_target_seconds - clean_target_seconds >= 0.15
    assert delay_target_seconds - delay_control_seconds >= 0.15
    assert delay_control_seconds <= max(0.50, clean_control_seconds + 0.25)

    for profile in ["jitter", "loss", "duplicate", "reorder", "rate"]:
        apply_profile(profile)

    apply_profile("blackhole")
    clientA.fail("curl --fail --silent --max-time 2 http://${originAddress}:8080/media")
    clientB.succeed("curl --fail --silent --max-time 5 http://${originAddress}:8080/media >/dev/null")
    apply_profile("clean")
    clientA.succeed("curl --fail --silent --max-time 5 http://${originAddress}:8080/media >/dev/null")

    origin.succeed("test -s /var/lib/rtsp-media-fault-lab/results/http.jsonl")
    origin.succeed(
        "runuser -u media-lab -- ${pkgs.python3}/bin/python3 "
        + "${originAssets}/bin/result-contract write-fault "
        + "--output /var/lib/rtsp-media-fault-lab/results/fault-summary.json "
        + "--clean-target-seconds " + format(clean_target_seconds, ".9f") + " "
        + "--clean-control-seconds " + format(clean_control_seconds, ".9f") + " "
        + "--delay-target-seconds " + format(delay_target_seconds, ".9f") + " "
        + "--delay-control-seconds " + format(delay_control_seconds, ".9f")
    )
    origin.succeed(
        "${pkgs.python3}/bin/python3 ${originAssets}/bin/result-contract validate "
        "--kind fault --path /var/lib/rtsp-media-fault-lab/results/fault-summary.json"
    )
    export_fault_result()
  '';
}
