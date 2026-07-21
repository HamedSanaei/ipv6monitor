#!/usr/bin/env python3
"""Persistent live IPv4/IPv6 traffic monitor for Linux."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable

VERSION: Final = "1.1.0"
NFT_FAMILY: Final = "inet"
NFT_TABLE: Final = "ipv6monitor"
DEFAULT_CONFIG: Final = Path("/etc/ipv6monitor/ipv6monitor.conf")
COUNTER_NAMES: Final = ("rx4", "tx4", "rx6", "tx6")
COUNTER_FIELDS: Final = tuple(
    f"{name}_{metric}" for name in COUNTER_NAMES for metric in ("bytes", "packets")
)
ALLOWED_CONFIG_KEYS: Final = {
    "INTERFACE",
    "REFRESH_INTERVAL",
    "SAVE_INTERVAL",
    "HISTORY_INTERVAL",
    "HISTORY_RETENTION_DAYS",
    "RUNTIME_DIR",
    "STATE_DIR",
}
INTERFACE_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
LOG = logging.getLogger("ipv6monitor")


@dataclass(frozen=True)
class Config:
    interface: str = "auto"
    refresh_interval: float = 1.0
    save_interval: float = 10.0
    history_interval: float = 60.0
    history_retention_days: int = 30
    runtime_dir: Path = Path("/run/ipv6monitor")
    state_dir: Path = Path("/var/lib/ipv6monitor")

    @property
    def status_path(self) -> Path:
        return self.runtime_dir / "status.json"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "daemon.lock"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "traffic.db"


class MonitorError(RuntimeError):
    """Operational error with a user-facing message."""


def parse_config(path: Path) -> Config:
    values: dict[str, str] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        raise MonitorError(f"Invalid config line {line_number}: missing '='")
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key not in ALLOWED_CONFIG_KEYS:
                        raise MonitorError(f"Unknown config key on line {line_number}: {key}")
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                        value = value[1:-1]
                    values[key] = value
        except OSError as exc:
            raise MonitorError(f"Cannot read config {path}: {exc}") from exc

    interface = values.get("INTERFACE", "auto")
    if interface != "auto" and not INTERFACE_PATTERN.fullmatch(interface):
        raise MonitorError(f"Invalid network interface name: {interface!r}")

    refresh_interval = parse_float(
        values.get("REFRESH_INTERVAL", "1"), "REFRESH_INTERVAL", 0.1, 60.0
    )
    save_interval = parse_float(
        values.get("SAVE_INTERVAL", "10"), "SAVE_INTERVAL", 1.0, 3600.0
    )
    history_interval = parse_float(
        values.get("HISTORY_INTERVAL", "60"), "HISTORY_INTERVAL", 10.0, 86400.0
    )
    retention = parse_int(
        values.get("HISTORY_RETENTION_DAYS", "30"),
        "HISTORY_RETENTION_DAYS",
        0,
        36500,
    )
    runtime_dir = parse_absolute_path(values.get("RUNTIME_DIR", "/run/ipv6monitor"))
    state_dir = parse_absolute_path(values.get("STATE_DIR", "/var/lib/ipv6monitor"))

    return Config(
        interface=interface,
        refresh_interval=refresh_interval,
        save_interval=save_interval,
        history_interval=history_interval,
        history_retention_days=retention,
        runtime_dir=runtime_dir,
        state_dir=state_dir,
    )


def parse_float(value: str, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise MonitorError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise MonitorError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def parse_int(value: str, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise MonitorError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise MonitorError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def parse_absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise MonitorError(f"Configured path must be absolute: {value}")
    return path


def run_command(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=input_text,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except FileNotFoundError as exc:
        raise MonitorError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise MonitorError(f"Command failed: {' '.join(command)}: {detail}") from exc


def require_commands(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise MonitorError(f"Missing required command(s): {', '.join(missing)}")


def detect_interface() -> str:
    candidates: list[tuple[int, str]] = []
    for command in (
        ["ip", "-j", "route", "show", "default"],
        ["ip", "-j", "-6", "route", "show", "default"],
    ):
        result = run_command(command, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            routes = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        for route in routes:
            dev = route.get("dev")
            if isinstance(dev, str) and INTERFACE_PATTERN.fullmatch(dev):
                metric = route.get("metric", 0)
                candidates.append((int(metric) if isinstance(metric, int) else 0, dev))
        if candidates:
            break

    if not candidates:
        raise MonitorError(
            "Could not auto-detect the default network interface. "
            "Set INTERFACE in /etc/ipv6monitor/ipv6monitor.conf."
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def validate_interface_exists(interface: str) -> None:
    if not INTERFACE_PATTERN.fullmatch(interface):
        raise MonitorError(f"Invalid network interface name: {interface!r}")
    if not Path("/sys/class/net", interface).exists():
        raise MonitorError(f"Network interface does not exist: {interface}")


class NftCounterManager:
    def __init__(self, interface: str) -> None:
        self.interface = interface

    def setup(self) -> None:
        self.cleanup()
        rules = f"""
add table {NFT_FAMILY} {NFT_TABLE}
add chain {NFT_FAMILY} {NFT_TABLE} rx {{ type filter hook prerouting priority -149; policy accept; }}
add chain {NFT_FAMILY} {NFT_TABLE} tx {{ type filter hook postrouting priority 149; policy accept; }}
add rule {NFT_FAMILY} {NFT_TABLE} rx iifname \"{self.interface}\" meta nfproto ipv4 counter comment \"rx4\"
add rule {NFT_FAMILY} {NFT_TABLE} rx iifname \"{self.interface}\" meta nfproto ipv6 counter comment \"rx6\"
add rule {NFT_FAMILY} {NFT_TABLE} tx oifname \"{self.interface}\" meta nfproto ipv4 counter comment \"tx4\"
add rule {NFT_FAMILY} {NFT_TABLE} tx oifname \"{self.interface}\" meta nfproto ipv6 counter comment \"tx6\"
""".lstrip()
        run_command(["nft", "-f", "-"], input_text=rules)

    def cleanup(self) -> None:
        run_command(
            ["nft", "delete", "table", NFT_FAMILY, NFT_TABLE],
            check=False,
        )

    def read(self) -> dict[str, int]:
        result = run_command(
            ["nft", "-j", "list", "table", NFT_FAMILY, NFT_TABLE]
        )
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MonitorError("nft returned invalid JSON") from exc

        counters = {field: 0 for field in COUNTER_FIELDS}
        seen: set[str] = set()
        for item in document.get("nftables", []):
            rule = item.get("rule")
            if not isinstance(rule, dict):
                continue
            name = rule.get("comment")
            if name not in COUNTER_NAMES:
                continue
            for expression in rule.get("expr", []):
                counter = expression.get("counter") if isinstance(expression, dict) else None
                if isinstance(counter, dict):
                    counters[f"{name}_bytes"] = int(counter.get("bytes", 0))
                    counters[f"{name}_packets"] = int(counter.get("packets", 0))
                    seen.add(name)
                    break

        if seen != set(COUNTER_NAMES):
            missing = ", ".join(sorted(set(COUNTER_NAMES) - seen))
            raise MonitorError(f"Missing nftables counter rule(s): {missing}")
        return counters


class TrafficDatabase:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path, *, initialize: bool = True) -> None:
        self.path = path
        if initialize:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o755)
            self._initialize()
        elif not path.exists():
            raise MonitorError(f"Persistent database does not exist: {path}")

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS samples ("
                "ts INTEGER PRIMARY KEY, "
                "duration_seconds REAL NOT NULL, "
                "rx4_bytes INTEGER NOT NULL, "
                "tx4_bytes INTEGER NOT NULL, "
                "rx6_bytes INTEGER NOT NULL, "
                "tx6_bytes INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )
        os.chmod(self.path, 0o644)

    def load_totals(self) -> dict[str, int]:
        totals = {field: 0 for field in COUNTER_FIELDS}
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'total_%'"
            ).fetchall()
        for row in rows:
            field = str(row["key"])[len("total_") :]
            if field in totals:
                try:
                    totals[field] = max(0, int(row["value"]))
                except (TypeError, ValueError):
                    LOG.warning("Ignoring invalid persisted total for %s", field)
        return totals

    def save_totals(self, totals: dict[str, int]) -> None:
        with self._connect() as connection:
            self._save_totals_in_transaction(connection, totals)

    def save_sample(
        self,
        *,
        timestamp: int,
        duration_seconds: float,
        deltas: dict[str, int],
        totals: dict[str, int],
        retention_days: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO samples("
                "ts, duration_seconds, rx4_bytes, tx4_bytes, rx6_bytes, tx6_bytes"
                ") VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET "
                "duration_seconds=samples.duration_seconds+excluded.duration_seconds, "
                "rx4_bytes=samples.rx4_bytes+excluded.rx4_bytes, "
                "tx4_bytes=samples.tx4_bytes+excluded.tx4_bytes, "
                "rx6_bytes=samples.rx6_bytes+excluded.rx6_bytes, "
                "tx6_bytes=samples.tx6_bytes+excluded.tx6_bytes",
                (
                    timestamp,
                    duration_seconds,
                    deltas["rx4_bytes"],
                    deltas["tx4_bytes"],
                    deltas["rx6_bytes"],
                    deltas["tx6_bytes"],
                ),
            )
            self._save_totals_in_transaction(connection, totals)
            if retention_days > 0:
                cutoff = int(time.time()) - retention_days * 86400
                connection.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))

    @staticmethod
    def _save_totals_in_transaction(
        connection: sqlite3.Connection, totals: dict[str, int]
    ) -> None:
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(f"total_{field}", str(max(0, int(value)))) for field, value in totals.items()],
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('updated_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now(timezone.utc).isoformat(),),
        )

    def history_summary(self, hours: float) -> dict[str, Any]:
        since = int(time.time() - hours * 3600)
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS sample_count, "
                "COALESCE(SUM(duration_seconds), 0) AS duration_seconds, "
                "COALESCE(SUM(rx4_bytes), 0) AS rx4_bytes, "
                "COALESCE(SUM(tx4_bytes), 0) AS tx4_bytes, "
                "COALESCE(SUM(rx6_bytes), 0) AS rx6_bytes, "
                "COALESCE(SUM(tx6_bytes), 0) AS tx6_bytes, "
                "MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                "FROM samples WHERE ts >= ?",
                (since,),
            ).fetchone()
        assert row is not None
        return dict(row)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def calculate_deltas(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    deltas: dict[str, int] = {}
    for field in COUNTER_FIELDS:
        current_value = max(0, int(current.get(field, 0)))
        previous_value = max(0, int(previous.get(field, 0)))
        # If a kernel counter was reset, preserve the traffic counted since reset.
        deltas[field] = (
            current_value - previous_value
            if current_value >= previous_value
            else current_value
        )
    return deltas


def blank_counters() -> dict[str, int]:
    return {field: 0 for field in COUNTER_FIELDS}


def build_status(
    *,
    running: bool,
    interface: str,
    started_at: float,
    refresh_interval: float,
    rates: dict[str, float],
    totals: dict[str, int],
    database_path: Path,
    error: str | None = None,
) -> dict[str, Any]:
    now = time.time()
    payload: dict[str, Any] = {
        "schema_version": 2,
        "version": VERSION,
        "running": running,
        "pid": os.getpid() if running else None,
        "interface": interface,
        "refresh_interval": refresh_interval,
        "collected_at": now,
        "collected_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "uptime_seconds": max(0.0, now - started_at),
        "rates": {
            "ipv4": {"rx_Bps": rates.get("rx4", 0.0), "tx_Bps": rates.get("tx4", 0.0)},
            "ipv6": {"rx_Bps": rates.get("rx6", 0.0), "tx_Bps": rates.get("tx6", 0.0)},
        },
        "totals": {
            "ipv4": {
                "rx_bytes": totals["rx4_bytes"],
                "tx_bytes": totals["tx4_bytes"],
                "rx_packets": totals["rx4_packets"],
                "tx_packets": totals["tx4_packets"],
            },
            "ipv6": {
                "rx_bytes": totals["rx6_bytes"],
                "tx_bytes": totals["tx6_bytes"],
                "rx_packets": totals["rx6_packets"],
                "tx_packets": totals["tx6_packets"],
            },
        },
        "database": str(database_path),
    }
    if error:
        payload["error"] = error
    return payload


def acquire_daemon_lock(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise MonitorError("Another ipv6monitor daemon is already running") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def run_daemon(config: Config) -> int:
    if os.geteuid() != 0:
        raise MonitorError("The daemon must run as root")
    require_commands(("ip", "nft"))

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config.runtime_dir, 0o755)
    os.chmod(config.state_dir, 0o755)
    lock_handle = acquire_daemon_lock(config.lock_path)

    interface = detect_interface() if config.interface == "auto" else config.interface
    validate_interface_exists(interface)

    database = TrafficDatabase(config.database_path)
    totals = database.load_totals()
    manager = NftCounterManager(interface)
    stop_requested = False

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        LOG.info("Received signal %s; stopping", signum)
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    started_at = time.time()
    rates = {name: 0.0 for name in COUNTER_NAMES}
    history_pending = blank_counters()
    history_duration = 0.0
    previous = blank_counters()
    last_save = time.monotonic()
    last_history = time.monotonic()

    try:
        manager.setup()
        previous = manager.read()
        previous_time = time.monotonic()
        atomic_write_json(
            config.status_path,
            build_status(
                running=True,
                interface=interface,
                started_at=started_at,
                refresh_interval=config.refresh_interval,
                rates=rates,
                totals=totals,
                database_path=config.database_path,
            ),
        )
        LOG.info("Monitoring interface %s every %.3f seconds", interface, config.refresh_interval)

        while not stop_requested:
            time.sleep(config.refresh_interval)
            now_monotonic = time.monotonic()
            elapsed = max(0.000001, now_monotonic - previous_time)
            try:
                current = manager.read()
            except MonitorError as exc:
                LOG.warning("Counter read failed; recreating nftables table: %s", exc)
                manager.setup()
                previous = manager.read()
                previous_time = now_monotonic
                rates = {name: 0.0 for name in COUNTER_NAMES}
                atomic_write_json(
                    config.status_path,
                    build_status(
                        running=True,
                        interface=interface,
                        started_at=started_at,
                        refresh_interval=config.refresh_interval,
                        rates=rates,
                        totals=totals,
                        database_path=config.database_path,
                        error=str(exc),
                    ),
                )
                continue

            deltas = calculate_deltas(current, previous)
            for field, delta in deltas.items():
                totals[field] += delta
                history_pending[field] += delta
            history_duration += elapsed
            rates = {
                name: deltas[f"{name}_bytes"] / elapsed for name in COUNTER_NAMES
            }

            atomic_write_json(
                config.status_path,
                build_status(
                    running=True,
                    interface=interface,
                    started_at=started_at,
                    refresh_interval=config.refresh_interval,
                    rates=rates,
                    totals=totals,
                    database_path=config.database_path,
                ),
            )

            if now_monotonic - last_history >= config.history_interval:
                database.save_sample(
                    timestamp=int(time.time()),
                    duration_seconds=history_duration,
                    deltas=history_pending,
                    totals=totals,
                    retention_days=config.history_retention_days,
                )
                history_pending = blank_counters()
                history_duration = 0.0
                last_history = now_monotonic
                last_save = now_monotonic
            elif now_monotonic - last_save >= config.save_interval:
                database.save_totals(totals)
                last_save = now_monotonic

            previous = current
            previous_time = now_monotonic
    finally:
        try:
            # Capture traffic accumulated after the last regular polling cycle.
            try:
                current = manager.read()
                final_deltas = calculate_deltas(current, previous)
                for field, delta in final_deltas.items():
                    totals[field] += delta
                    history_pending[field] += delta
            except Exception as exc:  # Final persistence must continue if nft is unavailable.
                LOG.warning("Could not read final nftables counters: %s", exc)

            if any(history_pending[field] for field in COUNTER_FIELDS):
                database.save_sample(
                    timestamp=int(time.time()),
                    duration_seconds=max(history_duration, 0.000001),
                    deltas=history_pending,
                    totals=totals,
                    retention_days=config.history_retention_days,
                )
            else:
                database.save_totals(totals)

            atomic_write_json(
                config.status_path,
                build_status(
                    running=False,
                    interface=interface,
                    started_at=started_at,
                    refresh_interval=config.refresh_interval,
                    rates={name: 0.0 for name in COUNTER_NAMES},
                    totals=totals,
                    database_path=config.database_path,
                ),
            )
        finally:
            manager.cleanup()
            lock_handle.close()
    return 0


def load_status(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise MonitorError(
            "No runtime status found. Start the service with: "
            "sudo systemctl enable --now ipv6monitor"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Cannot read runtime status {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MonitorError("Runtime status has an invalid format")
    return payload


def format_quantity(value: float, units: tuple[str, ...], step: float = 1024.0) -> str:
    value = max(0.0, float(value))
    index = 0
    while value >= step and index < len(units) - 1:
        value /= step
        index += 1
    return f"{value:,.2f} {units[index]}"


def format_bytes(value: float) -> str:
    return format_quantity(value, ("B", "KiB", "MiB", "GiB", "TiB", "PiB"))


def format_rate(value: float) -> str:
    return format_quantity(value, ("B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s"))


def format_bits_rate(bytes_per_second: float) -> str:
    return format_quantity(
        bytes_per_second * 8.0,
        ("bit/s", "Kbit/s", "Mbit/s", "Gbit/s", "Tbit/s"),
        step=1000.0,
    )


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")


def render_status(
    payload: dict[str, Any],
    *,
    color: bool,
    terminal_width: int | None = None,
) -> str:
    rates = payload.get("rates", {})
    totals = payload.get("totals", {})
    ipv4_rates = rates.get("ipv4", {})
    ipv6_rates = rates.get("ipv6", {})
    ipv4_totals = totals.get("ipv4", {})
    ipv6_totals = totals.get("ipv6", {})
    collected_at = float(payload.get("collected_at", 0.0) or 0.0)
    age = max(0.0, time.time() - collected_at) if collected_at else float("inf")
    refresh_interval = float(payload.get("refresh_interval", 1.0) or 1.0)
    running = bool(payload.get("running"))
    healthy = running and age < max(5.0, refresh_interval * 4.0)

    green = "\033[32m" if color else ""
    yellow = "\033[33m" if color else ""
    red = "\033[31m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""
    status_text = f"{green}RUNNING{reset}" if healthy else f"{red}NOT RUNNING{reset}"
    if running and not healthy:
        status_text = f"{yellow}STALE{reset}"

    def values(
        rate_data: dict[str, Any], total_data: dict[str, Any]
    ) -> tuple[str, str, str, str]:
        download_rate = float(rate_data.get("rx_Bps", 0.0) or 0.0)
        upload_rate = float(rate_data.get("tx_Bps", 0.0) or 0.0)
        return (
            format_bits_rate(download_rate),
            format_bits_rate(upload_rate),
            format_bytes(float(total_data.get("rx_bytes", 0))),
            format_bytes(float(total_data.get("tx_bytes", 0))),
        )

    total_rates = {
        "rx_Bps": float(ipv4_rates.get("rx_Bps", 0.0) or 0.0)
        + float(ipv6_rates.get("rx_Bps", 0.0) or 0.0),
        "tx_Bps": float(ipv4_rates.get("tx_Bps", 0.0) or 0.0)
        + float(ipv6_rates.get("tx_Bps", 0.0) or 0.0),
    }
    total_totals = {
        "rx_bytes": int(ipv4_totals.get("rx_bytes", 0) or 0)
        + int(ipv6_totals.get("rx_bytes", 0) or 0),
        "tx_bytes": int(ipv4_totals.get("tx_bytes", 0) or 0)
        + int(ipv6_totals.get("tx_bytes", 0) or 0),
    }
    rows = [
        ("IPv4", *values(ipv4_rates, ipv4_totals)),
        ("IPv6", *values(ipv6_rates, ipv6_totals)),
        ("TOTAL", *values(total_rates, total_totals)),
    ]

    if terminal_width is None:
        terminal_width = shutil.get_terminal_size(fallback=(100, 24)).columns

    lines = [
        f"{bold}IPv6Monitor {payload.get('version', '?')}{reset}",
        f"Status: {status_text}   Interface: {payload.get('interface', '?')}",
        f"Update every: {refresh_interval:g}s   Last update: {age:.1f}s ago",
        "",
    ]

    if terminal_width >= 82:
        header = (
            f"{'Protocol':<9}"
            f"{'Download':>16}"
            f"{'Upload':>16}"
            f"{'Downloaded':>17}"
            f"{'Uploaded':>17}"
        )
        separator = "-" * len(header)
        lines.extend([header, separator])
        for index, (protocol, download, upload, downloaded, uploaded) in enumerate(rows):
            if index == 2:
                lines.append(separator)
            lines.append(
                f"{protocol:<9}"
                f"{download:>16}"
                f"{upload:>16}"
                f"{downloaded:>17}"
                f"{uploaded:>17}"
            )
    else:
        for index, (protocol, download, upload, downloaded, uploaded) in enumerate(rows):
            if index == 2:
                lines.append("-" * min(terminal_width, 60))
            lines.extend(
                [
                    f"{bold}{protocol}{reset}",
                    f"  Download speed: {download}",
                    f"  Upload speed:   {upload}",
                    f"  Total download: {downloaded}",
                    f"  Total upload:   {uploaded}",
                ]
            )

    lines.extend(
        [
            "",
            "Download = traffic received by this server; Upload = traffic sent.",
            f"Persistent database: {payload.get('database', '?')}",
            "Press Ctrl+C to exit. The collector keeps running in the background.",
        ]
    )
    if payload.get("error"):
        lines.append(f"Last collector warning: {payload['error']}")
    return "\n".join(lines)


def monitor_loop(config: Config, interval: float, *, color: bool) -> int:
    try:
        while True:
            payload = load_status(config.status_path)
            if sys.stdout.isatty():
                clear_screen()
            print(render_status(payload, color=color))
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


def print_history(config: Config, hours: float) -> int:
    if not config.database_path.exists():
        raise MonitorError(f"Persistent database does not exist: {config.database_path}")
    database = TrafficDatabase(config.database_path, initialize=False)
    summary = database.history_summary(hours)
    print(f"IPv6Monitor history - last {hours:g} hour(s)")
    print("=" * 48)
    print(f"Samples:  {summary['sample_count']}")
    print(f"Covered:  {float(summary['duration_seconds']):,.1f} seconds")
    print(f"IPv4 RX:  {format_bytes(summary['rx4_bytes'])}")
    print(f"IPv4 TX:  {format_bytes(summary['tx4_bytes'])}")
    print(f"IPv6 RX:  {format_bytes(summary['rx6_bytes'])}")
    print(f"IPv6 TX:  {format_bytes(summary['tx6_bytes'])}")
    if summary.get("first_ts"):
        first = datetime.fromtimestamp(int(summary["first_ts"]), timezone.utc).isoformat()
        last = datetime.fromtimestamp(int(summary["last_ts"]), timezone.utc).isoformat()
        print(f"Range:    {first} to {last}")
    return 0


def reset_data(config: Config) -> int:
    if os.geteuid() != 0:
        raise MonitorError("Reset requires root: sudo ipv6monitor reset")
    require_commands(("systemctl",))
    run_command(["systemctl", "stop", "ipv6monitor"])
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(f"{config.database_path}{suffix}")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    try:
        config.status_path.unlink()
    except FileNotFoundError:
        pass
    run_command(["systemctl", "start", "ipv6monitor"])
    print("Persistent traffic totals and history were reset; service restarted.")
    return 0


def service_status() -> int:
    require_commands(("systemctl",))
    return subprocess.run(
        ["systemctl", "--no-pager", "--full", "status", "ipv6monitor"],
        check=False,
    ).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ipv6monitor",
        description="Persistent live IPv4/IPv6 traffic monitor",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("monitor", "status", "history", "daemon", "reset", "service-status"),
        default="monitor",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--interval",
        type=float,
        help="Display refresh interval; defaults to configured REFRESH_INTERVAL",
    )
    parser.add_argument("--hours", type=float, default=24.0, help="History window in hours")
    parser.add_argument("--json", action="store_true", help="Print status as JSON")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = parse_config(args.config)
        display_interval = args.interval if args.interval is not None else config.refresh_interval
        if not 0.1 <= display_interval <= 60.0:
            raise MonitorError("--interval must be between 0.1 and 60 seconds")
        if args.hours <= 0:
            raise MonitorError("--hours must be greater than zero")

        if args.command == "daemon":
            return run_daemon(config)
        if args.command == "monitor":
            use_color = sys.stdout.isatty() and not args.no_color
            return monitor_loop(config, display_interval, color=use_color)
        if args.command == "status":
            payload = load_status(config.status_path)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(render_status(payload, color=False))
            return 0 if payload.get("running") else 3
        if args.command == "history":
            return print_history(config, args.hours)
        if args.command == "reset":
            return reset_data(config)
        if args.command == "service-status":
            return service_status()
        parser.error(f"Unsupported command: {args.command}")
    except MonitorError as exc:
        print(f"ipv6monitor: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
