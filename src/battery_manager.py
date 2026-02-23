"""BlackRoad Battery Manager - IoT device battery monitoring and lifecycle tracking."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

GREEN = "\033[0;32m"
RED   = "\033[0;31m"
YELLOW= "\033[1;33m"
CYAN  = "\033[0;36m"
BLUE  = "\033[0;34m"
BOLD  = "\033[1m"
NC    = "\033[0m"

DB_PATH = Path.home() / ".blackroad" / "battery-manager.db"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class IoTDevice:
    id: Optional[int]
    name: str
    device_type: str        # sensor | gateway | actuator | camera | tracker
    location: str
    battery_type: str       # LiPo | LiIon | AA | AAA | NiMH | LiFePO4
    battery_capacity_mah: int
    current_pct: float
    last_seen: str
    status: str             # online | offline | warning | critical
    firmware_version: str
    notes: str
    created_at: Optional[str] = None

    def health_label(self) -> str:
        if self.current_pct >= 60: return "healthy"
        if self.current_pct >= 30: return "warning"
        if self.current_pct >= 15: return "low"
        return "critical"

    def days_remaining(self, daily_drain_pct: float = 2.0) -> Optional[float]:
        if daily_drain_pct <= 0:
            return None
        return round(self.current_pct / daily_drain_pct, 1)


@dataclass
class BatteryReading:
    id: Optional[int]
    device_id: int
    reading_time: str
    battery_pct: float
    voltage_mv: Optional[float]
    temperature_c: Optional[float]
    signal_rssi: Optional[int]
    drain_rate: Optional[float]   # % per hour


@dataclass
class BatteryAlert:
    id: Optional[int]
    device_id: int
    alert_type: str    # low_battery | critical | offline | temp_high
    threshold: float
    current_value: float
    message: str
    resolved: bool
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Core business logic
# ---------------------------------------------------------------------------

class BatteryManager:
    """Monitor IoT device batteries, track lifecycle, and generate alerts."""

    LOW_PCT      = 30.0
    CRITICAL_PCT = 15.0

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS iot_devices (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                 TEXT NOT NULL UNIQUE,
                    device_type          TEXT NOT NULL,
                    location             TEXT NOT NULL DEFAULT 'unknown',
                    battery_type         TEXT NOT NULL DEFAULT 'LiPo',
                    battery_capacity_mah INTEGER NOT NULL DEFAULT 2000,
                    current_pct          REAL NOT NULL DEFAULT 100.0,
                    last_seen            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    status               TEXT NOT NULL DEFAULT 'online',
                    firmware_version     TEXT DEFAULT '1.0.0',
                    notes                TEXT DEFAULT '',
                    created_at           TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS battery_readings (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id     INTEGER REFERENCES iot_devices(id) ON DELETE CASCADE,
                    reading_time  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    battery_pct   REAL NOT NULL,
                    voltage_mv    REAL,
                    temperature_c REAL,
                    signal_rssi   INTEGER,
                    drain_rate    REAL
                );
                CREATE TABLE IF NOT EXISTS battery_alerts (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id     INTEGER REFERENCES iot_devices(id),
                    alert_type    TEXT NOT NULL,
                    threshold     REAL NOT NULL,
                    current_value REAL NOT NULL,
                    message       TEXT NOT NULL,
                    resolved      INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def add_device(self, name: str, device_type: str, location: str,
                   battery_type: str = "LiPo", capacity_mah: int = 2000,
                   firmware: str = "1.0.0", notes: str = "") -> IoTDevice:
        """Register a new IoT device for battery monitoring."""
        ts = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO iot_devices
                   (name, device_type, location, battery_type, battery_capacity_mah,
                    current_pct, last_seen, firmware_version, notes)
                   VALUES (?, ?, ?, ?, ?, 100.0, ?, ?, ?)""",
                (name, device_type, location, battery_type, capacity_mah, ts, firmware, notes),
            )
            conn.commit()
        return self._get_device(cur.lastrowid)

    def _get_device(self, device_id: int) -> Optional[IoTDevice]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM iot_devices WHERE id = ?", (device_id,)
            ).fetchone()
        return IoTDevice(**dict(row)) if row else None

    def record_reading(self, device_id: int, battery_pct: float,
                       voltage_mv: Optional[float] = None,
                       temperature_c: Optional[float] = None,
                       signal_rssi: Optional[int] = None) -> BatteryReading:
        """Log battery telemetry, compute drain rate, update device state."""
        ts = datetime.now().isoformat()
        drain_rate: Optional[float] = None
        with sqlite3.connect(self.db_path) as conn:
            prev = conn.execute(
                "SELECT battery_pct, reading_time FROM battery_readings "
                "WHERE device_id = ? ORDER BY reading_time DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            if prev:
                try:
                    elapsed_h = (datetime.fromisoformat(ts) -
                                 datetime.fromisoformat(prev[1])).total_seconds() / 3600
                    if elapsed_h > 0:
                        drain_rate = round((prev[0] - battery_pct) / elapsed_h, 4)
                except ValueError:
                    pass
            new_status = ("critical" if battery_pct <= self.CRITICAL_PCT
                          else "warning" if battery_pct <= self.LOW_PCT else "online")
            cur = conn.execute(
                """INSERT INTO battery_readings
                   (device_id, reading_time, battery_pct, voltage_mv, temperature_c,
                    signal_rssi, drain_rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (device_id, ts, battery_pct, voltage_mv, temperature_c,
                 signal_rssi, drain_rate),
            )
            conn.execute(
                "UPDATE iot_devices SET current_pct=?, last_seen=?, status=? WHERE id=?",
                (battery_pct, ts, new_status, device_id),
            )
            conn.commit()
        self._check_alerts(device_id, battery_pct)
        return BatteryReading(id=cur.lastrowid, device_id=device_id,
                              reading_time=ts, battery_pct=battery_pct,
                              voltage_mv=voltage_mv, temperature_c=temperature_c,
                              signal_rssi=signal_rssi, drain_rate=drain_rate)

    def _check_alerts(self, device_id: int, pct: float) -> None:
        if pct <= self.CRITICAL_PCT:
            self._fire_alert(device_id, "critical",   self.CRITICAL_PCT, pct,
                             f"CRITICAL: battery at {pct:.1f}%!")
        elif pct <= self.LOW_PCT:
            self._fire_alert(device_id, "low_battery", self.LOW_PCT, pct,
                             f"Battery low: {pct:.1f}%")

    def _fire_alert(self, device_id: int, atype: str, threshold: float,
                    current: float, message: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO battery_alerts "
                "(device_id, alert_type, threshold, current_value, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (device_id, atype, threshold, current, message),
            )
            conn.commit()

    def list_devices(self, status: Optional[str] = None) -> list[IoTDevice]:
        """Retrieve all monitored IoT devices, sorted by battery level (lowest first)."""
        q, params = "SELECT * FROM iot_devices", []
        if status:
            q += " WHERE status = ?"; params.append(status)
        q += " ORDER BY current_pct ASC"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(q, params).fetchall()
        return [IoTDevice(**dict(r)) for r in rows]

    def fleet_status(self) -> dict:
        """Aggregate battery health across the IoT fleet."""
        devices = self.list_devices()
        total   = len(devices)
        with sqlite3.connect(self.db_path) as conn:
            alerts = conn.execute(
                "SELECT COUNT(*) FROM battery_alerts WHERE resolved=0"
            ).fetchone()[0]
        avg = round(sum(d.current_pct for d in devices) / total, 1) if total else 0.0
        return {
            "total_devices": total,
            "avg_battery_pct": avg,
            "healthy":  sum(1 for d in devices if d.current_pct >= 60),
            "warning":  sum(1 for d in devices if 30 <= d.current_pct < 60),
            "low":      sum(1 for d in devices if 15 <= d.current_pct < 30),
            "critical": sum(1 for d in devices if d.current_pct < 15),
            "active_alerts": alerts,
        }

    def export_json(self, output_path: str = "battery_export.json") -> str:
        """Export fleet battery status to JSON."""
        devices = self.list_devices()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            alerts = [dict(r) for r in conn.execute(
                "SELECT * FROM battery_alerts WHERE resolved=0 ORDER BY created_at DESC"
            ).fetchall()]
        payload = {
            "exported_at":  datetime.now().isoformat(),
            "fleet_status": self.fleet_status(),
            "devices":      [asdict(d) for d in devices],
            "active_alerts": alerts,
        }
        with open(output_path, "w") as fh:
            json.dump(payload, fh, indent=2)
        return output_path


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _batt_bar(pct: float, width: int = 12) -> str:
    filled = int(min(pct, 100) / 100 * width)
    color  = GREEN if pct >= 60 else (YELLOW if pct >= 30 else RED)
    icon   = "🔋" if pct >= 30 else "🪫"
    return f"{icon} {color}{'█' * filled}{'░' * (width - filled)}{NC} {pct:5.1f}%"


def _print_device(d: IoTDevice) -> None:
    sc = {"online": GREEN, "offline": RED, "warning": YELLOW, "critical": RED}.get(d.status, NC)
    print(f"  {BOLD}[{d.id:>3}]{NC} {CYAN}{d.name}{NC}  {BLUE}({d.device_type}){NC}  📍{d.location}")
    print(f"        Battery  : {_batt_bar(d.current_pct)}")
    print(f"        Status   : {sc}{d.status}{NC}   "
          f"Type: {d.battery_type}   Cap: {d.battery_capacity_mah} mAh")
    print(f"        Last seen: {d.last_seen[:19]}   FW: {d.firmware_version}")
    if d.notes:
        print(f"        Notes    : {d.notes}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="battery_manager",
        description="BlackRoad Battery Manager — IoT device battery lifecycle tracking",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    lp = sub.add_parser("list", help="List IoT devices")
    lp.add_argument("--status", choices=["online", "offline", "warning", "critical"])

    ap = sub.add_parser("add", help="Register an IoT device")
    ap.add_argument("name")
    ap.add_argument("device_type", choices=["sensor", "gateway", "actuator", "camera", "tracker"])
    ap.add_argument("location")
    ap.add_argument("--battery-type", default="LiPo")
    ap.add_argument("--capacity",     type=int, default=2000, dest="capacity_mah")
    ap.add_argument("--firmware",     default="1.0.0")
    ap.add_argument("--notes",        default="")

    rp = sub.add_parser("reading", help="Record a battery telemetry reading")
    rp.add_argument("device_id",   type=int)
    rp.add_argument("battery_pct", type=float)
    rp.add_argument("--voltage",   type=float, default=None, dest="voltage_mv")
    rp.add_argument("--temp",      type=float, default=None, dest="temperature_c")
    rp.add_argument("--rssi",      type=int,   default=None, dest="signal_rssi")

    sub.add_parser("status", help="Show fleet battery summary")

    ep = sub.add_parser("export", help="Export fleet data to JSON")
    ep.add_argument("--output", default="battery_export.json")

    return p


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    mgr    = BatteryManager()
    print(f"\n{BOLD}{BLUE}╔══ BlackRoad Battery Manager ══╗{NC}\n")

    if args.cmd == "list":
        devices = mgr.list_devices(status=getattr(args, "status", None))
        if not devices:
            print(f"  {YELLOW}No devices registered.{NC}\n"); return
        print(f"  {BOLD}IoT Fleet ({len(devices)} devices){NC}\n")
        for d in devices:
            _print_device(d)

    elif args.cmd == "add":
        dev = mgr.add_device(args.name, args.device_type, args.location,
                             args.battery_type, args.capacity_mah,
                             args.firmware, args.notes)
        print(f"  {GREEN}✓ Device registered: [{dev.id}] {dev.name}{NC}\n")

    elif args.cmd == "reading":
        r   = mgr.record_reading(args.device_id, args.battery_pct,
                                  args.voltage_mv, args.temperature_c, args.signal_rssi)
        dev = mgr._get_device(args.device_id)
        hlc = GREEN if dev and dev.health_label() == "healthy" else               (YELLOW if dev and dev.health_label() == "warning" else RED)
        print(f"  {GREEN}✓ Reading logged: {args.battery_pct:.1f}%{NC}")
        print(f"  Battery: {_batt_bar(args.battery_pct)}   "
              f"Health: {hlc}{dev.health_label() if dev else '?'}{NC}")
        if r.drain_rate is not None:
            print(f"  Drain rate: {r.drain_rate:.3f} %/hr")
        print()

    elif args.cmd == "status":
        s = mgr.fleet_status()
        print(f"  {BOLD}IoT Fleet Battery Status{NC}")
        print(f"  {'Total Devices':<24} {CYAN}{s['total_devices']}{NC}")
        print(f"  {'Average Battery':<24} {_batt_bar(s['avg_battery_pct'])}")
        print(f"  {'Healthy  (≥60%)':<24} {GREEN}{s['healthy']}{NC}")
        print(f"  {'Warning (30–60%)':<24} {YELLOW}{s['warning']}{NC}")
        print(f"  {'Low    (15–30%)':<24} {YELLOW}{s['low']}{NC}")
        print(f"  {'Critical (<15%)':<24} {RED}{s['critical']}{NC}")
        print(f"  {'Active Alerts':<24} "
              f"{RED if s['active_alerts'] else GREEN}{s['active_alerts']}{NC}")
        print()

    elif args.cmd == "export":
        path = mgr.export_json(args.output)
        print(f"  {GREEN}✓ Exported to: {path}{NC}\n")

    else:
        parser.print_help(); print()


if __name__ == "__main__":
    main()
