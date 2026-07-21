from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import time
import unittest
import sys
from unittest import mock
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "ipv6monitor.py"
SPEC = importlib.util.spec_from_file_location("ipv6monitor", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ipv6monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ipv6monitor
SPEC.loader.exec_module(ipv6monitor)


class ConfigTests(unittest.TestCase):
    def test_defaults_when_config_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = ipv6monitor.parse_config(Path(directory) / "missing.conf")
        self.assertEqual(config.interface, "auto")
        self.assertEqual(config.refresh_interval, 0.5)
        self.assertEqual(config.history_retention_days, 30)

    def test_parses_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ipv6monitor.conf"
            path.write_text(
                "INTERFACE=ens3\n"
                "REFRESH_INTERVAL=1\n"
                "SAVE_INTERVAL=15\n"
                "HISTORY_INTERVAL=120\n"
                "HISTORY_RETENTION_DAYS=7\n"
                f"RUNTIME_DIR={directory}/run\n"
                f"STATE_DIR={directory}/state\n",
                encoding="utf-8",
            )
            config = ipv6monitor.parse_config(path)
        self.assertEqual(config.interface, "ens3")
        self.assertEqual(config.refresh_interval, 1.0)
        self.assertEqual(config.history_interval, 120.0)

    def test_rejects_unknown_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.conf"
            path.write_text("DANGEROUS_OPTION=yes\n", encoding="utf-8")
            with self.assertRaises(ipv6monitor.MonitorError):
                ipv6monitor.parse_config(path)

    def test_rejects_invalid_interface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.conf"
            path.write_text("INTERFACE=eth0;drop table\n", encoding="utf-8")
            with self.assertRaises(ipv6monitor.MonitorError):
                ipv6monitor.parse_config(path)


class CounterTests(unittest.TestCase):
    def test_counter_reset_never_subtracts_totals(self) -> None:
        previous = ipv6monitor.blank_counters()
        current = ipv6monitor.blank_counters()
        previous["rx4_bytes"] = 500
        current["rx4_bytes"] = 20
        deltas = ipv6monitor.calculate_deltas(current, previous)
        self.assertEqual(deltas["rx4_bytes"], 20)

    def test_regular_delta(self) -> None:
        previous = ipv6monitor.blank_counters()
        current = ipv6monitor.blank_counters()
        previous["tx6_bytes"] = 100
        current["tx6_bytes"] = 350
        deltas = ipv6monitor.calculate_deltas(current, previous)
        self.assertEqual(deltas["tx6_bytes"], 250)


class NftablesTests(unittest.TestCase):
    def test_reads_all_four_json_counters(self) -> None:
        rules = []
        for index, name in enumerate(ipv6monitor.COUNTER_NAMES, start=1):
            rules.append(
                {
                    "rule": {
                        "comment": name,
                        "expr": [
                            {"counter": {"packets": index, "bytes": index * 100}}
                        ],
                    }
                }
            )
        completed = subprocess.CompletedProcess(
            args=["nft"], returncode=0, stdout=json.dumps({"nftables": rules}), stderr=""
        )
        with mock.patch.object(ipv6monitor, "run_command", return_value=completed):
            counters = ipv6monitor.NftCounterManager("eth0").read()
        self.assertEqual(counters["rx4_bytes"], 100)
        self.assertEqual(counters["tx6_packets"], 4)

    def test_setup_creates_count_only_rules(self) -> None:
        calls = []

        def fake_run(command, *, input_text=None, check=True):
            calls.append((command, input_text, check))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(ipv6monitor, "run_command", side_effect=fake_run):
            ipv6monitor.NftCounterManager("ens3").setup()
        ruleset = next(text for _, text, _ in calls if text)
        self.assertIn("meta nfproto ipv4 counter", ruleset)
        self.assertIn("meta nfproto ipv6 counter", ruleset)
        self.assertNotIn(" drop ", ruleset.lower())
        self.assertNotIn(" reject ", ruleset.lower())



class PersistenceTests(unittest.TestCase):
    def test_totals_survive_database_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "traffic.db"
            database = ipv6monitor.TrafficDatabase(path)
            totals = ipv6monitor.blank_counters()
            totals["rx4_bytes"] = 123456
            totals["tx6_packets"] = 42
            database.save_totals(totals)

            reopened = ipv6monitor.TrafficDatabase(path, initialize=False)
            loaded = reopened.load_totals()
            self.assertEqual(loaded["rx4_bytes"], 123456)
            self.assertEqual(loaded["tx6_packets"], 42)

    def test_history_sample_is_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "traffic.db"
            database = ipv6monitor.TrafficDatabase(path)
            totals = ipv6monitor.blank_counters()
            deltas = ipv6monitor.blank_counters()
            deltas["rx6_bytes"] = 2048
            totals["rx6_bytes"] = 2048
            database.save_sample(
                timestamp=int(time.time()),
                duration_seconds=60.0,
                deltas=deltas,
                totals=totals,
                retention_days=30,
            )
            summary = database.history_summary(1)
            self.assertEqual(summary["sample_count"], 1)
            self.assertEqual(summary["rx6_bytes"], 2048)

    def test_atomic_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            ipv6monitor.atomic_write_json(path, {"متن": "سالم", "value": 1})
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual(payload["متن"], "سالم")


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self) -> None:
        self.assertEqual(ipv6monitor.format_bytes(1024), "1.00 KiB")

    def test_status_payload_shape(self) -> None:
        totals = ipv6monitor.blank_counters()
        status = ipv6monitor.build_status(
            running=True,
            interface="ens3",
            started_at=time.time(),
            rates={name: 0.0 for name in ipv6monitor.COUNTER_NAMES},
            totals=totals,
            database_path=Path("/tmp/traffic.db"),
        )
        self.assertEqual(status["interface"], "ens3")
        self.assertIn("ipv4", status["totals"])
        self.assertIn("ipv6", status["rates"])


if __name__ == "__main__":
    unittest.main()
