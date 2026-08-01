from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
RUNPOD_ROOT = BENCHMARK_ROOT / "runpod"


class RunPodImageFanoutTests(unittest.TestCase):
    def test_haproxy_fanout_is_fixed_eight_lane_tcp_static_rr(self) -> None:
        config = (RUNPOD_ROOT / "haproxy.cfg").read_text(encoding="utf-8")
        lines = [
            line.strip()
            for line in config.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        servers = [line for line in lines if line.startswith("server ")]

        self.assertIn("mode tcp", lines)
        self.assertIn("bind 127.0.0.1:18080", lines)
        self.assertIn("balance static-rr", lines)
        self.assertEqual(
            servers,
            [
                f"server lane_{lane} 127.0.0.1:{18080 + lane}"
                for lane in range(1, 9)
            ],
        )
        self.assertEqual(lines.count("retries 0"), 2)
        self.assertEqual(lines.count("no option redispatch"), 2)
        self.assertNotIn("option redispatch", lines)
        self.assertFalse(
            any(" check" in f" {line} " for line in servers),
            "the fixed lanes must not be adaptively removed by health checks",
        )
        self.assertNotIn("server-template", config)
        self.assertNotIn("resolvers", config)
        self.assertNotIn("mode http", config)

    def test_image_pins_haproxy_and_installs_root_owned_config(self) -> None:
        dockerfile = (RUNPOD_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("haproxy=3.2.22-r0", dockerfile)
        self.assertIn("socat=1.8.1.3-r0", dockerfile)
        self.assertIn("util-linux", dockerfile)
        self.assertIn("tini", dockerfile)
        self.assertIn("test -x /sbin/tini", dockerfile)
        self.assertIn("command -v setsid", dockerfile)
        self.assertIn(
            'ENTRYPOINT ["/sbin/tini", "--", '
            '"/usr/local/bin/bluemap-runpod-entrypoint"]',
            dockerfile,
        )
        self.assertIn(
            "COPY benchmarks/web-performance/runpod/haproxy.cfg "
            "/etc/haproxy/haproxy.cfg",
            dockerfile,
        )
        self.assertIn("chown root:root /etc/haproxy/haproxy.cfg", dockerfile)
        self.assertIn("chmod 0444 /etc/haproxy/haproxy.cfg", dockerfile)
        copy_position = dockerfile.index(
            "COPY benchmarks/web-performance/runpod/haproxy.cfg"
        )
        validation_position = dockerfile.index(
            "haproxy -c -f /etc/haproxy/haproxy.cfg"
        )
        self.assertGreater(validation_position, copy_position)

    def test_unprivileged_read_only_stats_socket_is_fixed(self) -> None:
        config = (RUNPOD_ROOT / "haproxy.cfg").read_text(encoding="utf-8")
        sampler = (RUNPOD_ROOT / "sample-resources.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "stats socket /run/haproxy/bluemap-stats.sock "
            "user loadgen group loadgen mode 0660 level user",
            config,
        )
        self.assertIn(
            "UNIX-CONNECT:$HAPROXY_STATS_SOCKET", sampler
        )
        self.assertIn("show stat", sampler)
        self.assertNotIn("level admin", config)

    def test_sshd_allows_only_lane_ports_not_haproxy_frontend(self) -> None:
        dockerfile = (RUNPOD_ROOT / "Dockerfile").read_text(encoding="utf-8")
        permit_listen = [
            line.strip().strip("' \\")
            for line in dockerfile.splitlines()
            if "'PermitListen " in line
        ]

        self.assertEqual(
            permit_listen,
            [
                "PermitListen "
                + " ".join(
                    f"127.0.0.1:{port}"
                    for port in range(18081, 18089)
                )
            ],
        )
        self.assertNotIn("127.0.0.1:18080", permit_listen[0])

    def test_public_key_rejects_crlf_before_regex_validation(self) -> None:
        entrypoint = (RUNPOD_ROOT / "entrypoint.sh").read_text(
            encoding="utf-8"
        )
        cr_position = entrypoint.index("*$'\\r'*")
        lf_position = entrypoint.index("*$'\\n'*")
        regex_position = entrypoint.index(
            '[[ "$ssh_public_key" =~ ^ssh-ed25519'
        )

        self.assertLess(cr_position, regex_position)
        self.assertLess(lf_position, regex_position)
        self.assertIn(
            'fail "BLUEMAP_RUNPOD_SSH_PUBLIC_KEY must not contain CR or LF"',
            entrypoint,
        )
        for separator in ("\r", "\n"):
            with self.subTest(separator=repr(separator)):
                result = subprocess.run(
                    ["bash", str(RUNPOD_ROOT / "entrypoint.sh")],
                    capture_output=True,
                    check=False,
                    env={
                        **os.environ,
                        "BLUEMAP_RUNPOD_SSH_PUBLIC_KEY": (
                            f"ssh-ed25519 AAAA{separator}injected"
                        ),
                    },
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not contain CR or LF", result.stderr)

    def test_entrypoint_mutually_supervises_haproxy_and_sshd(self) -> None:
        entrypoint_path = RUNPOD_ROOT / "entrypoint.sh"
        entrypoint = entrypoint_path.read_text(encoding="utf-8")

        subprocess.run(
            ["bash", "-n", str(entrypoint_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn(
            "/usr/sbin/haproxy -c -f /etc/haproxy/haproxy.cfg",
            entrypoint,
        )
        self.assertIn(
            "/usr/sbin/haproxy -W -db -f /etc/haproxy/haproxy.cfg &",
            entrypoint,
        )
        self.assertIn("/usr/sbin/sshd -D -e &", entrypoint)
        self.assertIn('wait -n "$haproxy_pid" "$sshd_pid"', entrypoint)
        self.assertIn('for pid in "$haproxy_pid" "$sshd_pid"', entrypoint)
        self.assertIn(
            'fail "$stopped_service exited unexpectedly with status '
            '$service_status"',
            entrypoint,
        )
        self.assertNotIn("exec /usr/sbin/sshd", entrypoint)


if __name__ == "__main__":
    unittest.main()
