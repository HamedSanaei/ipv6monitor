# AGENTS.md

## Project mission

Build and maintain `ipv6monitor`, a lightweight Ubuntu utility that separates
IPv4 and IPv6 traffic, displays live RX/TX rates, and preserves cumulative and
historical usage across terminal exits, service restarts, and machine reboots.

## User-facing guarantees

- `ipv6monitor` with no arguments opens the live monitor.
- Default collector and live display refresh interval is 1 second.
- Installation works with one `curl | sudo bash` command.
- Required Ubuntu packages are installed automatically.
- The systemd service is enabled and started during installation.
- Persistent totals survive reboot and service restart.
- The monitor must not change firewall policy or drop/reject traffic.
- Uninstallation must remove only resources owned by this project.

## Architecture

- `src/ipv6monitor.py`: CLI, daemon, nftables counter management, SQLite
  persistence, runtime snapshots, history output, and reset handling.
- `install.sh`: idempotent installer for local clones and remote one-line use.
- `uninstall.sh`: safe removal of the service, program, nftables table, runtime
  files, and optionally persistent data.
- `systemd/ipv6monitor.service`: boot-time daemon definition.
- `config/ipv6monitor.conf`: administrator-editable defaults.
- `tests/`: standard-library unit tests that do not require root or nftables.

## Networking rules

- Use a dedicated nftables table named `inet ipv6monitor`.
- Count only; never add accept, drop, reject, NAT, redirect, mark, or mutation
  behavior beyond the base-chain policy required for a passive counter chain.
- Separate IPv4 and IPv6 with `meta nfproto ipv4` and `meta nfproto ipv6`.
- Count ingress on `prerouting` with `iifname` and egress on `postrouting` with
  `oifname`.
- On daemon startup, safely replace only the project's own nftables table.
- On graceful shutdown, persist final deltas before removing the table.
- Interface names must be validated before they are interpolated into nftables.

## Persistence rules

- Store durable state in `/var/lib/ipv6monitor/traffic.db` using Python's
  standard-library SQLite module.
- Runtime snapshots belong in `/run/ipv6monitor/status.json` and are not the
  source of truth.
- Database updates must be transactional.
- Runtime JSON writes must be atomic (`write -> fsync -> os.replace`).
- A counter reset must never subtract from persisted totals.
- Schema changes require a migration path and a schema-version update.
- Never silently delete persistent traffic data during upgrades.

## systemd rules

- The service must remain compatible with supported Ubuntu systemd versions.
- Keep automatic restart enabled for unexpected failures.
- Do not use `PrivateNetwork=true`; the daemon needs access to host networking.
- Retain the minimum practical capabilities for nftables operation.
- Installer changes to a unit file must call `systemctl daemon-reload`.
- Installation must finish by running `systemctl enable --now ipv6monitor`.

## Installer rules

- Support both `sudo bash install.sh` from a clone and remote `curl | sudo bash`.
- Fail fast with actionable error messages.
- Use `apt-get` non-interactively on Ubuntu/Debian.
- Preserve an existing `/etc/ipv6monitor/ipv6monitor.conf` during upgrades.
- Install application files atomically where practical.
- Never execute content downloaded from an untrusted or mutable URL other than
  the explicitly configured GitHub raw base.
- The default raw base is the project's `main` branch, but release work should
  prefer immutable version tags in published installation commands.

## Python and text standards

- Support Python 3.10+ available on current Ubuntu LTS releases.
- Use only the Python standard library unless a dependency is justified.
- All text files must use UTF-8.
- Python file operations must explicitly use `encoding="utf-8"`.
- JSON containing Persian text must use `ensure_ascii=False`.
- Persian text must be preserved exactly; mojibake is not acceptable.
- Use type hints for public functions and non-trivial internal data structures.
- Use `subprocess.run(..., check=True)` and surface stderr in operational errors.
- Do not use `shell=True`.

## Security and reliability

- Treat config files as untrusted input and parse only known keys.
- Never `source` the config file from a privileged shell.
- Validate numeric ranges and filesystem paths.
- Use a daemon lock to prevent two collectors from running simultaneously.
- Avoid logging secrets, environment dumps, or unrelated host configuration.
- Handle SIGTERM and SIGINT so final counters are committed.
- Do not modify the host's persistent `/etc/nftables.conf`.

## Tests and quality gates

Before completing a change, run:

```bash
python3 -m py_compile src/ipv6monitor.py
python3 -m unittest discover -s tests -v
bash -n install.sh
bash -n uninstall.sh
```

When root and nftables are available, also perform an integration smoke test:

```bash
sudo bash install.sh
systemctl is-active --quiet ipv6monitor
sudo nft list table inet ipv6monitor
ipv6monitor status
sudo systemctl restart ipv6monitor
ipv6monitor status
```

Verify that cumulative totals do not decrease after restart.

## Documentation requirements

Update `README.md` whenever commands, config keys, persistence behavior,
installation paths, or compatibility changes. Keep the one-line installer and
uninstaller examples copy-pasteable.

## Required completion report

After completing a task, provide:

- Changed files
- Implementation summary
- Tests executed
- Known limitations
- Suggested Git commit message
