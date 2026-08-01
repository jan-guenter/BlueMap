from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
TOOLS = BENCHMARK_ROOT / "tools"
CONTROLLER_TESTS = BENCHMARK_ROOT / "controller" / "tests"
if str(CONTROLLER_TESTS) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_TESTS))

from support import START_EPOCH, analyze, runpod_identity  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SAMPLER = load_module("transport_sampler_under_test", TOOLS / "sample_ssh_transport.py")


def iso(offset: float) -> str:
    return (
        datetime.fromtimestamp(START_EPOCH + offset, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proc_stat(pid: int, start_time: int, user_ticks: int, system_ticks: int) -> str:
    fields = ["S", *("0" for _ in range(24))]
    fields[11] = str(user_ticks)
    fields[12] = str(system_ticks)
    fields[19] = str(start_time)
    return f"{pid} (ssh lane {pid}) " + " ".join(fields) + "\n"


class ControllerProcSamplerTests(unittest.TestCase):
    def build_proc(self, root: Path) -> tuple[list[tuple[str, int]], dict[str, int]]:
        net = root / "net"
        net.mkdir(parents=True)
        header = (
            "sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
        )
        rows = []
        lanes: list[tuple[str, int]] = []
        identities: dict[str, int] = {}
        for index in range(1, 9):
            pid = 100 + index
            inode = 5000 + index
            start = 10_000 + index
            lanes.append((f"lane-{index}", pid))
            identities[f"lane-{index}"] = start
            process = root / str(pid)
            (process / "fd").mkdir(parents=True)
            (process / "stat").write_text(
                proc_stat(pid, start, index, index + 1), encoding="ascii"
            )
            (process / "fd" / "3").symlink_to(f"socket:[{inode}]")
            rows.append(
                f"{index - 1}: 0100007F:{40000 + index:04X} "
                f"0A7100CB:0016 01 00000000:00000000 01:00000000 "
                f"{9 - index:08X} 1000 0 {inode} 1 0000000000000000 200\n"
            )
        (net / "tcp").write_text(header + "".join(rows), encoding="ascii")
        (net / "tcp6").write_text(header, encoding="ascii")
        (net / "snmp").write_text("Tcp: RetransSegs\nTcp: 7\n", encoding="ascii")
        (net / "netstat").write_text(
            "TcpExt: TCPTimeouts TCPSynRetrans\nTcpExt: 3 2\n",
            encoding="ascii",
        )
        return lanes, identities

    def test_exact_pid_socket_join_and_endpoint_decode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lanes, identities = self.build_proc(root)
            sample = SAMPLER.capture(
                root,
                lanes,
                identities,
                "a" * 64,
                100,
                "203.0.113.10",
                22,
            )

        self.assertEqual(len(sample["lanes"]), 8)
        self.assertEqual(
            sample["lanes"][0]["socket"]["remote"],
            {"address": "203.0.113.10", "addressHex": "0A7100CB", "port": 22},
        )
        self.assertEqual(sample["lanes"][0]["socket"]["unrecoveredRtoCount"], 8)
        self.assertEqual(
            sample["tcp"],
            {"retransSegs": 7, "tcpTimeouts": 3, "tcpSynRetrans": 2},
        )

    def test_frozen_endpoint_filters_forwarded_sockets_and_rejects_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lanes, identities = self.build_proc(root)
            with self.assertRaisesRegex(SAMPLER.CaptureError, "frozen remote SSH"):
                SAMPLER.capture(
                    root,
                    lanes,
                    identities,
                    "a" * 64,
                    100,
                    "198.51.100.7",
                    22,
                )
            # Reverse-forward SSH clients own transient local target sockets in
            # addition to their persistent SSH control socket. They must not
            # invalidate or replace the endpoint-bound control socket sample.
            tcp_path = root / "net" / "tcp"
            with tcp_path.open("a", encoding="ascii") as destination:
                destination.write(
                    "8: 0100007F:9C41 0100007F:0050 01 "
                    "00000001:00000002 01:00000000 00000000 "
                    "1000 0 6001 1 0000000000000000 200\n"
                )
            (root / "101" / "fd" / "4").symlink_to("socket:[6001]")
            sample = SAMPLER.capture(
                root,
                lanes,
                identities,
                "a" * 64,
                100,
                "203.0.113.10",
                22,
            )
            self.assertEqual(sample["lanes"][0]["socket"]["inode"], 5001)

            (root / "101" / "fd" / "5").symlink_to("socket:[5002]")
            with self.assertRaisesRegex(SAMPLER.CaptureError, "exactly one"):
                SAMPLER.capture(
                    root,
                    lanes,
                    identities,
                    "a" * 64,
                    100,
                    "203.0.113.10",
                    22,
                )

    def test_unrelated_duplicate_inode_rows_do_not_invalidate_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lanes, identities = self.build_proc(root)
            unrelated_inode = 6001
            (root / "101" / "fd" / "4").symlink_to(
                f"socket:[{unrelated_inode}]"
            )
            with (root / "net" / "tcp").open("a", encoding="ascii") as destination:
                destination.write(
                    "8: 0100007F:9C41 0100007F:0050 01 "
                    "00000001:00000002 01:00000000 00000000 "
                    f"1000 0 {unrelated_inode} 1 0000000000000000 200\n"
                )
            with (root / "net" / "tcp6").open("a", encoding="ascii") as destination:
                destination.write(
                    "0: 00000000000000000000000001000000:9C41 "
                    "B80D0120000000000000000001000000:0050 01 "
                    "00000003:00000004 01:00000000 00000000 "
                    f"1000 0 {unrelated_inode} 1 0000000000000000 200\n"
                )

            sample = SAMPLER.capture(
                root,
                lanes,
                identities,
                "a" * 64,
                100,
                "203.0.113.10",
                22,
            )

        self.assertEqual(sample["lanes"][0]["socket"]["inode"], 5001)

    def test_selected_control_inode_duplicate_rows_fail_closed(self) -> None:
        duplicate_rows = {
            "identical": (
                "8: 0100007F:9C41 0A7100CB:0016 01 "
                "00000000:00000000 01:00000000 00000008 "
                "1000 0 5001 1 0000000000000000 200\n"
            ),
            "conflicting": (
                "8: 0100007F:9C41 070033C6:0016 01 "
                "00000000:00000000 01:00000000 00000008 "
                "1000 0 5001 1 0000000000000000 200\n"
            ),
        }
        for label, duplicate_row in duplicate_rows.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lanes, identities = self.build_proc(root)
                with (root / "net" / "tcp").open(
                    "a", encoding="ascii"
                ) as destination:
                    destination.write(duplicate_row)

                with self.assertRaisesRegex(
                    SAMPLER.CaptureError,
                    "control-socket inode 5001 resolves to 2 ESTABLISHED TCP rows",
                ):
                    SAMPLER.capture(
                        root,
                        lanes,
                        identities,
                        "a" * 64,
                        100,
                        "203.0.113.10",
                        22,
                    )

    def test_ipv6_proc_address_decodes_by_little_endian_words(self) -> None:
        endpoint = SAMPLER.parse_endpoint(
            "B80D0120000000000000000001000000:0016", "ipv6", "remote"
        )
        self.assertEqual(endpoint["address"], "2001:db8::1")
        self.assertEqual(endpoint["port"], 22)


class TransportTelemetryValidationTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict[str, Path | dict[str, object]]:
        controller_source = root / "sample_ssh_transport.py"
        runpod_source = root / "runpod-sample-resources.sh"
        controller_source.write_text("controller-source\n", encoding="utf-8")
        runpod_source.write_text("runpod-source\n", encoding="utf-8")
        controller_sha = digest(controller_source)
        runpod_sha = digest(runpod_source)

        transport_path = root / "transport.json"
        controller_path = root / "controller.ndjson"
        runpod_path = root / "runpod.ndjson"
        output_path = root / "validation.json"

        controller_samples = []
        runpod_samples = []
        for sample_index, offset in enumerate((0.5, 3.0, 6.0)):
            lanes = []
            for lane_index in range(1, 9):
                unrecovered = (3, 1, 2)[sample_index]
                lanes.append(
                    {
                        "id": f"lane-{lane_index}",
                        "process": {
                            "pid": 1000 + lane_index,
                            "startTimeTicks": 2000 + lane_index,
                            "userTicks": sample_index + lane_index,
                            "systemTicks": sample_index + lane_index + 1,
                        },
                        "socket": {
                            "inode": 3000 + lane_index,
                            "family": "ipv4",
                            "stateHex": "01",
                            "local": {
                                "address": "127.0.0.1",
                                "addressHex": "0100007F",
                                "port": 40_000 + lane_index,
                            },
                            "remote": {
                                "address": "203.0.113.10",
                                "addressHex": "0A7100CB",
                                "port": 22,
                            },
                            "txQueueBytes": sample_index * lane_index,
                            "rxQueueBytes": sample_index,
                            "timerActive": 1 if sample_index == 1 else 0,
                            "timerExpiresJiffies": 20 - sample_index,
                            "unrecoveredRtoCount": unrecovered,
                            "retransmitTimeoutJiffies": 200 + sample_index,
                        },
                    }
                )
            controller_samples.append(
                {
                    "formatVersion": 2,
                    "kind": "controller-ssh-transport-sample",
                    "capturedAt": iso(offset),
                    "monotonicNanoseconds": 1_000_000_000 * (sample_index + 1),
                    "sourceSha256": controller_sha,
                    "clockTicksPerSecond": 100,
                    "tcp": {
                        "retransSegs": 10 + sample_index,
                        "tcpTimeouts": 5 + sample_index,
                        "tcpSynRetrans": 2 + sample_index,
                    },
                    "lanes": lanes,
                }
            )

            def stat(stat_id: str, server_name: str) -> dict[str, object]:
                return {
                    "id": stat_id,
                    "serverName": server_name,
                    "qcur": sample_index,
                    "qmax": sample_index,
                    "scur": sample_index + 1,
                    "smax": sample_index + 1,
                    "stot": sample_index * 10 + lane_index,
                    "bin": sample_index * 1000,
                    "bout": sample_index * 2000,
                    "econ": 0,
                    "eresp": 0,
                    "wretr": 0,
                    "wredis": 0,
                    "status": "UP" if stat_id == "backend" else "no check",
                }

            runpod_samples.append(
                {
                    "formatVersion": 2,
                    "kind": "runpod-resource-transport-sample",
                    "capturedAt": iso(offset),
                    "sourceSha256": runpod_sha,
                    "cpuUsageUsec": sample_index * 1000,
                    "cpuThrottledUsec": sample_index,
                    "memoryCurrentBytes": 1024,
                    "network": {
                        "rxBytes": sample_index * 10_000,
                        "txBytes": sample_index * 5000,
                    },
                    "transport": {
                        "tcp": {
                            "retransSegs": 20 + sample_index,
                            "tcpTimeouts": 4 + sample_index,
                            "tcpSynRetrans": 1 + sample_index,
                        },
                        "haproxy": {
                            "backend": stat("backend", "BACKEND"),
                            "lanes": [
                                stat(f"lane-{index}", f"lane_{index}")
                                for index in range(1, 9)
                            ],
                        },
                    },
                }
            )

        controller_path.write_text(
            "".join(json.dumps(row) + "\n" for row in controller_samples),
            encoding="utf-8",
        )
        runpod_path.write_text(
            "".join(json.dumps(row) + "\n" for row in runpod_samples),
            encoding="utf-8",
        )
        lane_evidence = [
            {
                "id": f"lane-{index}",
                "process": {
                    "pid": 1000 + index,
                    "startTimeTicks": 2000 + index,
                },
            }
            for index in range(1, 9)
        ]
        transport: dict[str, object] = {
            "formatVersion": 2,
            "kind": "ssh-l4-traefik-transport",
            "passed": True,
            "lanes": lane_evidence,
            "commandSession": {
                "receipt": {
                    "formatVersion": 2,
                    "completedAt": iso(6),
                    "telemetry": {
                        "resourceOutput": "/artifacts/case/runpod.ndjson",
                        "readyBeforeWorkload": True,
                        "readyAt": iso(0.75),
                        "workloadReleasedAt": iso(1),
                        "samplerExitStatus": 0,
                    },
                }
            },
            "telemetry": {
                "formatVersion": 2,
                "required": True,
                "intervalSeconds": 1,
                "controller": {
                    "path": "/artifacts/case/controller.ndjson",
                    "sha256": digest(controller_path),
                    "sampleCount": 3,
                    "source": {
                        "kind": "controller-procfs-ssh-lanes-v2",
                        "samplerSha256": controller_sha,
                        "remoteAddress": "203.0.113.10",
                        "remotePort": 22,
                    },
                    "capture": {
                        "attempted": True,
                        "validBeforeWorkload": True,
                        "readyAt": iso(0.75),
                        "reaped": True,
                        "exitStatus": 0,
                        "persisted": True,
                    },
                },
                "runpod": {
                    "path": "/artifacts/case/runpod.ndjson",
                    "sha256": digest(runpod_path),
                    "sampleCount": 3,
                    "source": {
                        "kind": "runpod-haproxy-procfs-v1",
                        "imageDigest": "sha256:" + "a" * 64,
                        "samplerSha256": runpod_sha,
                        "statsSocket": "/run/haproxy/bluemap-stats.sock",
                    },
                    "capture": {
                        "attempted": True,
                        "validBeforeWorkload": True,
                        "readyAt": iso(0.75),
                        "workloadReleasedAt": iso(1),
                        "reaped": True,
                        "exitStatus": 0,
                        "persisted": True,
                        "observedSourceSha256": runpod_sha,
                    },
                },
            },
        }
        transport_path.write_text(json.dumps(transport), encoding="utf-8")
        return {
            "transport": transport_path,
            "controller": controller_path,
            "runpod": runpod_path,
            "controller_source": controller_source,
            "runpod_source": runpod_source,
            "output": output_path,
            "transport_object": transport,
            "controller_samples": controller_samples,
        }

    def run_checker(
        self, fixture: dict[str, Path | dict[str, object]]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(TOOLS / "check_ssh_transport_telemetry.py"),
                str(fixture["transport"]),
                str(fixture["controller"]),
                str(fixture["runpod"]),
                "--controller-sampler",
                str(fixture["controller_source"]),
                "--runpod-sampler",
                str(fixture["runpod_source"]),
                "--expected-remote-address",
                "203.0.113.10",
                "--expected-remote-port",
                "22",
                "--expected-runpod-image-digest",
                "sha256:" + "a" * 64,
                "--output",
                str(fixture["output"]),
            ],
            capture_output=True,
            check=False,
            text=True,
        )

    def rewrite_controller(
        self,
        fixture: dict[str, Path | dict[str, object]],
        samples: list[dict[str, object]],
    ) -> None:
        path = fixture["controller"]
        assert isinstance(path, Path)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in samples),
            encoding="utf-8",
        )
        transport = fixture["transport_object"]
        assert isinstance(transport, dict)
        transport["telemetry"]["controller"]["sha256"] = digest(path)
        transport_path = fixture["transport"]
        assert isinstance(transport_path, Path)
        transport_path.write_text(json.dumps(transport), encoding="utf-8")

    def test_checker_accepts_decreasing_proc_retransmit_gauge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            result = self.run_checker(fixture)
            self.assertEqual(result.returncode, 0, result.stderr)
            output_path = fixture["output"]
            assert isinstance(output_path, Path)
            output = json.loads(output_path.read_text(encoding="utf-8"))

        lane = output["diagnostics"]["controller"]["lanes"]["lane-1"]
        self.assertEqual(lane["maximumQueueBytes"]["unrecoveredRtoCount"], 3)
        self.assertNotIn("socketRetransmitDelta", lane)
        self.assertFalse(output["performanceGateApplied"])

    def test_endpoint_address_hex_or_frozen_identity_mutation_fails(self) -> None:
        for mutation in (
            "hex",
            "address",
            "descriptor",
            "receipt",
            "duplicate-pid",
            "duplicate-inode",
        ):
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(Path(directory))
                if mutation in {"descriptor", "receipt"}:
                    transport = fixture["transport_object"]
                    assert isinstance(transport, dict)
                    if mutation == "descriptor":
                        transport["telemetry"]["controller"]["source"][
                            "remoteAddress"
                        ] = "198.51.100.7"
                    else:
                        transport["commandSession"]["receipt"]["telemetry"][
                            "resourceOutput"
                        ] = "/artifacts/case/different.ndjson"
                    path = fixture["transport"]
                    assert isinstance(path, Path)
                    path.write_text(json.dumps(transport), encoding="utf-8")
                else:
                    samples = copy.deepcopy(fixture["controller_samples"])
                    assert isinstance(samples, list)
                    if mutation == "duplicate-pid":
                        transport = fixture["transport_object"]
                        assert isinstance(transport, dict)
                        duplicate = copy.deepcopy(transport["lanes"][0]["process"])
                        transport["lanes"][1]["process"] = duplicate
                        for sample in samples:
                            sample["lanes"][1]["process"]["pid"] = duplicate["pid"]
                            sample["lanes"][1]["process"]["startTimeTicks"] = duplicate[
                                "startTimeTicks"
                            ]
                    elif mutation == "duplicate-inode":
                        for sample in samples:
                            sample["lanes"][1]["socket"]["inode"] = sample["lanes"][0][
                                "socket"
                            ]["inode"]
                    else:
                        remote = samples[1]["lanes"][0]["socket"]["remote"]
                        if mutation == "hex":
                            remote["addressHex"] = "076433C6"
                        else:
                            remote["address"] = "198.51.100.7"
                    self.rewrite_controller(fixture, samples)
                result = self.run_checker(fixture)
                self.assertNotEqual(result.returncode, 0)

    def test_missing_lane_counter_regression_and_edge_gap_fail(self) -> None:
        for mutation in ("lane", "counter", "gap", "boolean-status"):
            with self.subTest(
                mutation=mutation
            ), tempfile.TemporaryDirectory() as directory:
                fixture = self.fixture(Path(directory))
                if mutation in {"lane", "boolean-status"}:
                    transport = fixture["transport_object"]
                    assert isinstance(transport, dict)
                    if mutation == "boolean-status":
                        transport["telemetry"]["controller"]["capture"][
                            "exitStatus"
                        ] = False
                        transport_path = fixture["transport"]
                        assert isinstance(transport_path, Path)
                        transport_path.write_text(
                            json.dumps(transport), encoding="utf-8"
                        )
                        result = self.run_checker(fixture)
                        self.assertNotEqual(result.returncode, 0)
                        continue
                    path = fixture["runpod"]
                    assert isinstance(path, Path)
                    rows = [json.loads(line) for line in path.read_text().splitlines()]
                    rows[1]["transport"]["haproxy"]["lanes"].pop()
                    path.write_text(
                        "".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8",
                    )
                    transport["telemetry"]["runpod"]["sha256"] = digest(path)
                    transport_path = fixture["transport"]
                    transport_path.write_text(json.dumps(transport), encoding="utf-8")
                else:
                    samples = copy.deepcopy(fixture["controller_samples"])
                    assert isinstance(samples, list)
                    if mutation == "counter":
                        samples[2]["tcp"]["retransSegs"] = 0
                    else:
                        samples[1]["capturedAt"] = iso(20)
                    self.rewrite_controller(fixture, samples)
                result = self.run_checker(fixture)
                self.assertNotEqual(result.returncode, 0)

    def test_analyzer_independently_recomputes_gauge_and_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.fixture(Path(directory))
            samples = fixture["controller_samples"]
            transport = fixture["transport_object"]
            controller_source = fixture["controller_source"]
            assert isinstance(samples, list)
            assert isinstance(transport, dict)
            assert isinstance(controller_source, Path)
            summary = analyze.recompute_controller_transport(
                samples,
                transport,
                digest(controller_source),
                START_EPOCH + 1,
                START_EPOCH + 6,
                "fixture",
                "203.0.113.10",
                22,
            )
            mutated = copy.deepcopy(samples)
            mutated[0]["lanes"][0]["socket"]["remote"]["port"] = 2222
            with self.assertRaisesRegex(
                analyze.AnalysisFailure, "remote endpoint differs"
            ):
                analyze.recompute_controller_transport(
                    mutated,
                    transport,
                    digest(controller_source),
                    START_EPOCH + 1,
                    START_EPOCH + 6,
                    "fixture",
                    "203.0.113.10",
                    22,
                )

        self.assertEqual(
            summary["lanes"]["lane-1"]["maximumQueueBytes"]["unrecoveredRtoCount"],
            3,
        )

    def test_full_analyzer_revalidates_raw_data_and_runner_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root = root / "fixture"
            fixture_root.mkdir()
            fixture = self.fixture(fixture_root)
            case = root / "case"
            inputs = case / "inputs"
            phase = case / "repetitions" / "01" / "measurement"
            inputs.mkdir(parents=True)
            phase.mkdir(parents=True)
            controller_source = inputs / "sample_ssh_transport.py"
            runpod_source = inputs / "runpod-sample-resources.sh"
            controller_source.write_bytes(
                Path(fixture["controller_source"]).read_bytes()
            )
            runpod_source.write_bytes(Path(fixture["runpod_source"]).read_bytes())
            (inputs / "runpod-load-generator-identity.json").write_text(
                json.dumps(runpod_identity("formal-telemetry-test")),
                encoding="utf-8",
            )
            controller_path = phase / "ssh-l4-transport.controller.ndjson"
            runpod_path = phase / "load-generator-resources.ndjson"
            controller_path.write_bytes(Path(fixture["controller"]).read_bytes())
            runpod_path.write_bytes(Path(fixture["runpod"]).read_bytes())

            transport = fixture["transport_object"]
            assert isinstance(transport, dict)
            remote_transport = (
                "/artifacts/case/repetitions/01/measurement/" "ssh-l4-transport.json"
            )
            transport["telemetry"]["controller"].update(
                {
                    "path": remote_transport.replace(
                        "ssh-l4-transport.json",
                        "ssh-l4-transport.controller.ndjson",
                    ),
                }
            )
            transport["telemetry"]["runpod"].update(
                {
                    "path": remote_transport.replace(
                        "ssh-l4-transport.json",
                        "load-generator-resources.ndjson",
                    ),
                }
            )
            transport["commandSession"]["receipt"]["telemetry"]["resourceOutput"] = (
                transport["telemetry"]["runpod"]["path"]
            )
            transport["telemetry"]["controller"]["source"]["samplerSha256"] = digest(
                controller_source
            )
            transport["telemetry"]["runpod"]["source"]["samplerSha256"] = digest(
                runpod_source
            )
            transport["telemetry"]["runpod"]["capture"]["observedSourceSha256"] = (
                digest(runpod_source)
            )

            controller_rows = [
                json.loads(line) for line in controller_path.read_text().splitlines()
            ]
            for row in controller_rows:
                row["sourceSha256"] = digest(controller_source)
            controller_path.write_text(
                "".join(json.dumps(row) + "\n" for row in controller_rows),
                encoding="utf-8",
            )
            runpod_rows = [
                json.loads(line) for line in runpod_path.read_text().splitlines()
            ]
            for row in runpod_rows:
                row["sourceSha256"] = digest(runpod_source)
            runpod_path.write_text(
                "".join(json.dumps(row) + "\n" for row in runpod_rows),
                encoding="utf-8",
            )
            transport["telemetry"]["controller"]["sha256"] = digest(controller_path)
            transport["telemetry"]["runpod"]["sha256"] = digest(runpod_path)
            transport_path = phase / "ssh-l4-transport.json"
            transport_path.write_text(json.dumps(transport), encoding="utf-8")
            runner_validation = phase / "ssh-l4-transport-telemetry.json"
            checker = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "check_ssh_transport_telemetry.py"),
                    str(transport_path),
                    str(controller_path),
                    str(runpod_path),
                    "--controller-sampler",
                    str(controller_source),
                    "--runpod-sampler",
                    str(runpod_source),
                    "--expected-remote-address",
                    "203.0.113.10",
                    "--expected-remote-port",
                    "22",
                    "--expected-runpod-image-digest",
                    "sha256:" + "a" * 64,
                    "--output",
                    str(runner_validation),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(checker.returncode, 0, checker.stderr)
            validated = analyze.validate_transport_telemetry_artifacts(
                transport_path,
                transport,
                "fixture",
                remote_transport,
            )
            self.assertTrue(validated["capturePassed"])

            original_resource_output = transport["commandSession"]["receipt"][
                "telemetry"
            ]["resourceOutput"]
            transport["commandSession"]["receipt"]["telemetry"][
                "resourceOutput"
            ] = "/artifacts/case/different.ndjson"
            with self.assertRaisesRegex(
                analyze.AnalysisFailure, "telemetry command receipt"
            ):
                analyze.validate_transport_telemetry_artifacts(
                    transport_path,
                    transport,
                    "fixture",
                    remote_transport,
                )
            transport["commandSession"]["receipt"]["telemetry"][
                "resourceOutput"
            ] = original_resource_output

            with controller_path.open("a", encoding="utf-8") as destination:
                destination.write(json.dumps(controller_rows[-1]) + "\n")
            with self.assertRaisesRegex(analyze.AnalysisFailure, "hash/count changed"):
                analyze.validate_transport_telemetry_artifacts(
                    transport_path,
                    transport,
                    "fixture",
                    remote_transport,
                )


if __name__ == "__main__":
    unittest.main()
