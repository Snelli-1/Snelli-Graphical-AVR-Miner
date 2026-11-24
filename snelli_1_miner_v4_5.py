#!/usr/bin/env python3
"""Snelli-1 Miner: single-file GUI for physical Duino-Coin AVR miners.

Python 3.11+, Windows 10/11. Requires pyserial and a genuine Duino-Coin
Arduino AVR firmware on every device that is allowed to mine.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]
    SERIAL_IMPORT_ERROR = exc
else:
    SERIAL_IMPORT_ERROR = None


APP_NAME = "Snelli-1 Miner"
APP_VERSION = "4.5"
ALGORITHM = "DUCO-S1"
BAUDRATE = 115200
POOL_PICKER_URL = "https://server.duinocoin.com/getPool"
USER_AGENT = f"Snelli-1 AVR Miner/{APP_VERSION}"
HASH_TEST_PREV = "ba29a15896fd2d792d5c4b60668bf2b9feebc51d"
HASH_TEST_EXPECTED = "d0beba883d7e8cd119ea2b0e09b78f60f29e0968"
HASH_TEST_DIFFICULTY = 10
HASH_TEST_NONCE = 50
SUPPORTED_BOARDS = (
    "Arduino UNO",
    "Arduino Nano",
    "Arduino Nano Clone",
    "LGT8F328P Nano Clone",
    "Nano Clone / LGT8F328P (unidentified)",
)
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Snelli-1 Miner"
CONFIG_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "miner.log"

PALETTE = {
    "bg_main": "#02070d",
    "bg_panel": "#06121b",
    "bg_card": "#091823",
    "bg_input": "#041019",
    "bg_terminal": "#010807",
    "border": "#164b66",
    "cyan": "#00f6ff",
    "green": "#39ff14",
    "green_btn": "#007a64",
    "amber": "#ffdc2e",
    "red": "#ff2a6d",
    "red_btn": "#9b1248",
    "magenta": "#ff00e5",
    "text_primary": "#f5edff",
    "text_secondary": "#c4a7e7",
    "text_muted": "#725a8d",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "username": "",
    "mining_key": "None",
    "rig_name": "None",
    "pool_host": "auto",
    "pool_port": 2813,
    "baudrate": BAUDRATE,
    "board_overrides": {},
}


def configure_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s")
        )
        root_logger.addHandler(handler)


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as stream:
            loaded = json.load(stream)
        if isinstance(loaded, dict):
            cfg.update(loaded)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        logging.exception("Settings file could not be read")
        cfg["load_error"] = str(exc)

    cfg["username"] = str(cfg.get("username", "")).strip()
    cfg["mining_key"] = str(cfg.get("mining_key", "None")).strip() or "None"
    cfg["rig_name"] = str(cfg.get("rig_name", "None")).strip() or "None"
    cfg["pool_host"] = str(cfg.get("pool_host", "auto")).strip() or "auto"
    try:
        cfg["pool_port"] = max(1, min(65535, int(cfg.get("pool_port", 2813))))
    except (TypeError, ValueError):
        cfg["pool_port"] = 2813
    try:
        cfg["baudrate"] = max(1200, min(2_000_000, int(cfg.get("baudrate", BAUDRATE))))
    except (TypeError, ValueError):
        cfg["baudrate"] = BAUDRATE
    if not isinstance(cfg.get("board_overrides"), dict):
        cfg["board_overrides"] = {}
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Write settings with replace semantics so a crash cannot truncate them."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    clean = {key: value for key, value in cfg.items() if key != "load_error"}
    fd, temp_name = tempfile.mkstemp(
        prefix="settings-", suffix=".tmp", dir=str(DATA_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(clean, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            logging.exception("Could not clean temporary settings file")
        raise


def _port_text(info: Any) -> str:
    return " ".join(
        str(value or "")
        for value in (
            getattr(info, "device", ""),
            getattr(info, "description", ""),
            getattr(info, "manufacturer", ""),
            getattr(info, "product", ""),
            getattr(info, "interface", ""),
            getattr(info, "hwid", ""),
        )
    ).lower()


def is_bluetooth_port(info: Any) -> bool:
    text = _port_text(info)
    return any(
        marker in text
        for marker in (
            "bluetooth",
            "bthenum",
            "bthmodem",
            "bthcomm",
            "rfcomm",
            "standard serial over bluetooth",
        )
    )


def is_arduino_serial_candidate(info: Any) -> bool:
    """Conservatively limit probes to Arduino USB identifiers/serial bridges."""
    text = _port_text(info)
    vid = getattr(info, "vid", None)
    pid = getattr(info, "pid", None)

    if any(word in text for word in ("arduino", "lgt8f328p", "lgt8fx")):
        return True
    if vid in (0x2341, 0x2A03):  # Arduino LLC / Arduino SA
        return True
    known_bridges = {
        (0x1A86, 0x7523),  # CH340G/CH341, widely used by Nano clones
        (0x1A86, 0x5523),  # CH341
        (0x1A86, 0x55D4),  # CH9102
        (0x0403, 0x6001),  # FTDI, used by some Nano clones
        (0x10C4, 0xEA60),  # CP210x, used by some clone boards
    }
    return (vid, pid) in known_bridges


def guess_board_type(info: Any) -> str:
    text = _port_text(info)
    if "lgt8f328p" in text or "lgt8fx" in text:
        return "LGT8F328P Nano Clone"
    if "arduino uno" in text or "uno r3" in text:
        return "Arduino UNO"
    if "arduino nano" in text:
        return "Arduino Nano"
    if getattr(info, "vid", None) == 0x1A86:
        return "Arduino Nano Clone"
    if getattr(info, "vid", None) in (0x2341, 0x2A03):
        return "Arduino UNO"
    return "Nano Clone / LGT8F328P (unidentified)"


def _read_serial_line(
    connection: Any,
    deadline: float,
    cancel: threading.Event | None = None,
    limit: int = 256,
) -> bytes:
    result = bytearray()
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Device scan cancelled")
        char = connection.read(1)
        if not char:
            continue
        if char == b"\n":
            return bytes(result).rstrip(b"\r")
        result.extend(char)
        if len(result) > limit:
            raise ValueError("AVR response exceeded the line limit")
    raise TimeoutError("AVR response timed out")


def parse_firmware_result(line: bytes) -> tuple[int, int, str]:
    try:
        fields = line.decode("ascii", errors="strict").strip().split(",")
    except UnicodeDecodeError as exc:
        raise ValueError("AVR response was not ASCII") from exc
    if len(fields) < 3:
        raise ValueError("AVR response did not contain nonce, time and DUCOID")

    nonce_text, elapsed_text, duco_id = (part.strip() for part in fields[:3])
    if not re.fullmatch(r"[01]+", nonce_text):
        raise ValueError("AVR nonce was not binary")
    if not re.fullmatch(r"[01]+", elapsed_text):
        raise ValueError("AVR elapsed time was not binary")
    if not re.fullmatch(r"DUCOID[0-9A-Fa-f]{16}", duco_id):
        raise ValueError("AVR response did not contain a valid DUCOID")
    return int(nonce_text, 2), int(elapsed_text, 2), duco_id


def avr_test_hashrate(nonce: int, elapsed_us: int) -> float:
    """Match the official AVR miner's five-decimal timing calibration."""
    elapsed_seconds = round(elapsed_us / 1_000_000.0, 5)
    if nonce <= 0 or elapsed_seconds <= 0:
        raise ValueError("AVR hash test returned an invalid timing result")
    return round(nonce / elapsed_seconds, 2)


def difficulty_group_for_rate(rate: float) -> str:
    """Return the official AVR miner's pool difficulty class for test H/s."""
    if rate > 1000:
        return "DUE"
    if rate > 550:
        return "ARM"
    if rate > 380:
        return "MEGA"
    return "AVR"


def _probe_open_port(
    info: Any,
    baudrate: int,
    cancel: threading.Event | None = None,
) -> tuple[str, str, float, str]:
    """Verify official AVR firmware, returning identity, board and pool tier."""
    if serial is None:
        raise RuntimeError(f"pyserial is required: {SERIAL_IMPORT_ERROR}")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Device scan cancelled")

    connection = None
    try:
        connection = serial.Serial(
            port=info.device,
            baudrate=baudrate,
            timeout=0.25,
            write_timeout=1.0,
            xonxoff=False,
            rtscts=False,
        )
        # Opening many Uno/Nano USB serial adapters resets their MCU through DTR.
        # The official firmware needs time to boot before it can receive a job.
        boot_deadline = time.monotonic() + 1.8
        while time.monotonic() < boot_deadline:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Device scan cancelled")
            time.sleep(min(0.1, boot_deadline - time.monotonic()))
        connection.reset_input_buffer()
        request = (
            f"{HASH_TEST_PREV},{HASH_TEST_EXPECTED},"
            f"{HASH_TEST_DIFFICULTY},"
        ).encode("ascii")
        connection.write(request)
        connection.flush()
        line = _read_serial_line(
            connection, time.monotonic() + 8.0, cancel=cancel
        )
        nonce, elapsed_us, duco_id = parse_firmware_result(line)
        if nonce != HASH_TEST_NONCE:
            raise ValueError(f"Hash test returned nonce {nonce}, expected 50")
        if not 0 < elapsed_us <= 60_000_000:
            raise ValueError("AVR hash test returned an invalid elapsed time")
        test_hashrate = avr_test_hashrate(nonce, elapsed_us)
        if not 0 < test_hashrate <= 1_000_000:
            raise ValueError("AVR hash test returned an implausible hashrate")
        return (
            duco_id,
            guess_board_type(info),
            test_hashrate,
            difficulty_group_for_rate(test_hashrate),
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logging.exception("Could not close probe port %s", info.device)


def scan_avr_devices(
    baudrate: int,
    existing_ports: set[str] | None = None,
    cancel: threading.Event | None = None,
    log_callback: Any = None,
) -> list["MinerDevice"]:
    """List every COM port, then test only likely Arduino USB serial ports."""
    if list_ports is None:
        raise RuntimeError(f"pyserial is required: {SERIAL_IMPORT_ERROR}")
    existing = {port.casefold() for port in (existing_ports or set())}
    found: list[MinerDevice] = []
    ports = list(list_ports.comports())
    logging.info("Found %d serial port(s)", len(ports))
    if not ports and log_callback:
        log_callback("No COM ports were found.")
    for info in ports:
        device = str(getattr(info, "device", "") or "")
        description = str(getattr(info, "description", "") or "")
        hwid = str(getattr(info, "hwid", "") or "")
        logging.info(
            "Port inventory: %s | %s | %s | VID=%s PID=%s",
            device,
            description,
            getattr(info, "manufacturer", "") or "",
            getattr(info, "vid", None),
            getattr(info, "pid", None),
        )
        if log_callback:
            log_callback(f"Port found: {device} — {description or hwid}")
        if cancel is not None and cancel.is_set():
            break
        if is_bluetooth_port(info):
            logging.info("Skipping Bluetooth serial port %s", device)
            if log_callback:
                log_callback(f"Bluetooth COM skipped: {device}")
            continue
        if device.casefold() in existing:
            logging.info("Skipping already managed port %s", device)
            if log_callback:
                log_callback(f"Already managed; not reopened: {device}")
            continue
        if not is_arduino_serial_candidate(info):
            logging.info("Leaving non-Arduino serial port untouched: %s", device)
            if log_callback:
                log_callback(f"Not an Arduino USB candidate; left untouched: {device}")
            continue

        if log_callback:
            log_callback(f"Verifying Duino-Coin AVR firmware on {device}…")
        try:
            duco_id, board, test_hashrate, difficulty_group = _probe_open_port(
                info, baudrate, cancel
            )
            item = MinerDevice(
                port=device,
                description=description,
                hwid=hwid,
                board_type=board,
                duco_id=duco_id,
                test_hashrate=test_hashrate,
                difficulty_group=difficulty_group,
            )
            found.append(item)
            logging.info(
                "Verified Duino-Coin AVR: %s %s %s, test %.2f H/s, tier %s",
                device, board, duco_id, test_hashrate, difficulty_group,
            )
            if log_callback:
                log_callback(
                    f"Verified AVR miner: {device} ({board}) | "
                    f"test {test_hashrate:.2f} H/s | {difficulty_group} tier"
                )
        except InterruptedError:
            break
        except Exception as exc:
            logging.info("Port %s is not a verified AVR miner: %s", device, exc)
            if log_callback:
                log_callback(f"{device}: no valid Duino-Coin AVR response")
    return found


@dataclass
class MinerDevice:
    port: str
    description: str
    hwid: str
    board_type: str
    duco_id: str
    status: str = "DETECTED"
    hashrate: float = 0.0
    test_hashrate: float = 0.0
    difficulty_group: str = "AVR"
    node: str = "—"
    phase: str = "Verified; waiting to start"
    accepted: int = 0
    rejected: int = 0
    ping_ms: float | None = None
    diff: int | None = None
    last_progress: float = field(default_factory=time.monotonic)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    resource_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    serial_connection: Any = field(default=None, repr=False)
    socket_connection: socket.socket | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "port": self.port,
                "description": self.description,
                "board_type": self.board_type,
                "duco_id": self.duco_id,
                "status": self.status,
                "hashrate": self.hashrate,
                "test_hashrate": self.test_hashrate,
                "difficulty_group": self.difficulty_group,
                "node": self.node,
                "phase": self.phase,
                "accepted": self.accepted,
                "rejected": self.rejected,
                "ping_ms": self.ping_ms,
                "diff": self.diff,
                "last_progress": self.last_progress,
            }


class MinerEngine:
    """Owns one mining thread and one physical serial port per verified AVR."""

    def __init__(self, event_callback: Any):
        self.event_callback = event_callback
        self.devices: dict[str, MinerDevice] = {}
        self.devices_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._pool_condition = threading.Condition()
        self._pool_info: tuple[str, int, str] | None = None
        self._pool_time = 0.0
        self.running = False
        self.start_time: float | None = None
        self._config: dict[str, Any] = {}
        self._bootstrap_thread: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()

    def _emit(self, message: str, level: str = "info") -> None:
        self.event_callback("log", (message, level))

    def add_device(self, device: MinerDevice) -> bool:
        key = device.port.casefold()
        with self.devices_lock:
            if key in self.devices:
                return False
            self.devices[key] = device
            return True

    def device_list(self) -> list[MinerDevice]:
        with self.devices_lock:
            return list(self.devices.values())

    def set_board_type(self, device: MinerDevice, board_type: str) -> None:
        if board_type not in SUPPORTED_BOARDS:
            return
        with device.lock:
            device.board_type = board_type

    def _fetch_pool(self, cfg: dict[str, Any]) -> tuple[str, int, str]:
        host = str(cfg.get("pool_host", "auto")).strip()
        if host and host.lower() != "auto":
            return host, int(cfg.get("pool_port", 2813)), host
        request = Request(POOL_PICKER_URL, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=8) as response:
            data = json.loads(response.read(64_000).decode("utf-8"))
        if not isinstance(data, dict) or not data.get("success"):
            raise RuntimeError(f"Pool discovery failed: {data}")
        return str(data["ip"]), int(data["port"]), str(data.get("name", "DUCO pool"))

    def get_pool_info(self, cfg: dict[str, Any], force: bool = False) -> tuple[str, int, str]:
        while True:
            with self._pool_condition:
                fresh = self._pool_info and time.monotonic() - self._pool_time < 300
                if not force and fresh:
                    return self._pool_info
                if getattr(self, "_pool_fetching", False):
                    self._pool_condition.wait(timeout=0.5)
                    continue
                self._pool_fetching = True
                break
        try:
            info = self._fetch_pool(cfg)
            with self._pool_condition:
                self._pool_info = info
                self._pool_time = time.monotonic()
            self._emit(f"Pool selected: {info[2]} ({info[0]}:{info[1]})")
            return info
        finally:
            with self._pool_condition:
                self._pool_fetching = False
                self._pool_condition.notify_all()

    def check_pool(self, cfg: dict[str, Any]) -> tuple[str, int, str]:
        info = self.get_pool_info(cfg, force=True)
        probe = socket.create_connection((info[0], info[1]), timeout=4)
        try:
            probe.close()
        finally:
            self._emit(f"Pool reachable: {info[2]} ({info[0]}:{info[1]})", "good")
        return info

    def start_all(self, cfg: dict[str, Any]) -> None:
        with self._state_lock:
            if self.running:
                return
            self.running = True
            self.start_time = time.monotonic()
            self._config = dict(cfg)
            for device in self.device_list():
                with device.lock:
                    device.status = "CONNECTING"
        self._emit("Starting verified physical AVR miners…")
        thread = threading.Thread(
            target=self._bootstrap, name="PoolBootstrap", daemon=False
        )
        self._bootstrap_thread = thread
        thread.start()

    def _bootstrap(self) -> None:
        try:
            info = self.get_pool_info(self._config, force=True)
            self._emit(f"Pool connection ready: {info[2]}")
        except InterruptedError:
            return
        except Exception as exc:
            logging.exception("Pool bootstrap failed")
            self._emit(f"Pool connection failed: {exc}", "bad")
            with self._state_lock:
                self.running = False
                self.start_time = None
            for device in self.device_list():
                with device.lock:
                    device.status = "POOL ERROR"
            return
        with self._state_lock:
            if not self.running:
                return
        for device in self.device_list():
            previous = device.thread
            while (
                previous is not None
                and previous.is_alive()
                and self.running
            ):
                previous.join(timeout=0.1)
            if not self.running:
                return
            self.start_device(device)

    def start_device(self, device: MinerDevice) -> None:
        with self._state_lock:
            if not self.running:
                return
            current = device.thread
            if current is not None and current.is_alive():
                return
            device.stop_event.clear()
            thread = threading.Thread(
                target=self._device_loop,
                args=(device, dict(self._config)),
                name=f"Miner-{device.port}",
                daemon=False,
            )
            device.thread = thread
            self._threads.add(thread)
            thread.start()

    def stop_all(self) -> None:
        with self._state_lock:
            self.running = False
            self.start_time = None
            for device in self.device_list():
                device.stop_event.set()
                self._close_resources(device)
        self._emit("Stopping miners and closing physical ports…", "warn")

    def request_close(self) -> None:
        self.stop_all()

    def live_threads(self) -> list[threading.Thread]:
        with self._state_lock:
            current = [thread for thread in self._threads if thread.is_alive()]
            if self._bootstrap_thread and self._bootstrap_thread.is_alive():
                current.append(self._bootstrap_thread)
            return current

    def watchdog(self) -> None:
        now = time.monotonic()
        for device in self.device_list():
            with device.lock:
                stale = (
                    device.status == "RUNNING"
                    and now - device.last_progress > 120
                )
            if stale:
                self._emit(f"{device.port}: watchdog requested a clean reconnect", "warn")
                self._close_resources(device)

    @staticmethod
    def _close_resources(device: MinerDevice) -> None:
        with device.resource_lock:
            sock = device.socket_connection
            ser = device.serial_connection
            device.socket_connection = None
            device.serial_connection = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                logging.exception("Could not close pool socket for %s", device.port)
        if ser is not None:
            try:
                ser.close()
            except Exception:
                logging.exception("Could not close serial port %s", device.port)

    @staticmethod
    def _set_resource(device: MinerDevice, kind: str, resource: Any) -> None:
        with device.resource_lock:
            if kind == "serial":
                device.serial_connection = resource
            else:
                device.socket_connection = resource

    def _touch(self, device: MinerDevice) -> None:
        with device.lock:
            device.last_progress = time.monotonic()

    def _open_verified_serial(
        self, device: MinerDevice, cfg: dict[str, Any]
    ) -> Any:
        with device.lock:
            device.phase = "Opening and verifying physical AVR"
        if serial is None:
            raise RuntimeError("pyserial is unavailable")
        connection = serial.Serial(
            port=device.port,
            baudrate=int(cfg.get("baudrate", BAUDRATE)),
            timeout=0.25,
            write_timeout=1.0,
            xonxoff=False,
            rtscts=False,
        )
        self._set_resource(device, "serial", connection)
        try:
            boot_deadline = time.monotonic() + 1.8
            while time.monotonic() < boot_deadline:
                if device.stop_event.wait(min(0.1, boot_deadline - time.monotonic())):
                    raise InterruptedError("Miner stopped")
            connection.reset_input_buffer()
            connection.write(
                f"{HASH_TEST_PREV},{HASH_TEST_EXPECTED},{HASH_TEST_DIFFICULTY},".encode(
                    "ascii"
                )
            )
            connection.flush()
            line = _read_serial_line(
                connection, time.monotonic() + 8.0, cancel=device.stop_event
            )
            nonce, elapsed_us, duco_id = parse_firmware_result(line)
            if nonce != HASH_TEST_NONCE or not 0 < elapsed_us <= 60_000_000:
                raise ValueError("Device failed the Duino-Coin AVR firmware hash test")
            test_hashrate = avr_test_hashrate(nonce, elapsed_us)
            difficulty_group = difficulty_group_for_rate(test_hashrate)
            with device.lock:
                identity_changed = duco_id != device.duco_id
                if identity_changed:
                    device.duco_id = duco_id
                device.test_hashrate = test_hashrate
                device.difficulty_group = difficulty_group
                device.phase = (
                    f"AVR verified · {test_hashrate:.2f} H/s test · "
                    f"{difficulty_group} tier"
                )
            if identity_changed:
                self._emit(
                    f"{device.port}: verified replacement AVR {duco_id}; updating device identity",
                    "warn",
                )
            self._emit(
                f"{device.port}: physical AVR verified | test {test_hashrate:.2f} H/s "
                f"| starting pool tier {difficulty_group}",
                "good",
            )
            self._touch(device)
            return connection
        except Exception:
            self._close_resources(device)
            raise

    @staticmethod
    def _recv_message(
        sock: socket.socket,
        stop_event: threading.Event,
        first_timeout: float = 10.0,
        settle: float = 0.12,
        max_bytes: int = 4096,
    ) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + first_timeout
        while not stop_event.is_set() and time.monotonic() < deadline:
            sock.settimeout(min(0.5, max(0.05, deadline - time.monotonic())))
            try:
                chunk = sock.recv(min(1024, max_bytes - len(data)))
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("Pool closed the connection")
            data.extend(chunk)
            if len(data) >= max_bytes:
                raise ValueError("Pool response exceeded the message limit")
            break
        if stop_event.is_set():
            raise InterruptedError("Miner stopped")
        if not data:
            raise TimeoutError("Timed out waiting for the pool")

        settle_deadline = time.monotonic() + settle
        while not stop_event.is_set() and time.monotonic() < settle_deadline:
            sock.settimeout(max(0.02, settle_deadline - time.monotonic()))
            try:
                chunk = sock.recv(min(1024, max_bytes - len(data)))
            except socket.timeout:
                break
            if not chunk:
                break
            data.extend(chunk)
            if len(data) >= max_bytes:
                raise ValueError("Pool response exceeded the message limit")
        if stop_event.is_set():
            raise InterruptedError("Miner stopped")
        return bytes(data).decode("utf-8", errors="replace").strip()

    def _device_loop(self, device: MinerDevice, cfg: dict[str, Any]) -> None:
        reconnect_delay = 2.0
        while not device.stop_event.is_set():
            ser = None
            sock = None
            try:
                with device.lock:
                    device.status = "CONNECTING"
                    device.hashrate = 0.0
                ser = self._open_verified_serial(device, cfg)
                with device.lock:
                    device.phase = "Connecting to pool"
                pool_ip, pool_port, pool_name = self.get_pool_info(cfg)
                with device.lock:
                    device.node = pool_name
                if device.stop_event.is_set():
                    raise InterruptedError("Miner stopped")
                sock = socket.create_connection((pool_ip, pool_port), timeout=8.0)
                sock.settimeout(1.0)
                self._set_resource(device, "socket", sock)
                with device.lock:
                    device.phase = f"Pool connected · {pool_name}"
                server_version = self._recv_message(sock, device.stop_event, 8.0)
                if not server_version:
                    raise ConnectionError("Pool sent an empty version response")
                sock.sendall(b"MOTD")
                motd = self._recv_message(sock, device.stop_event, 5.0)
                with device.lock:
                    device.phase = "Pool ready · requesting jobs"
                self._emit(
                    f"{device.port}: pool handshake OK | {pool_name} | "
                    f"protocol {server_version[:48]}"
                )
                if motd:
                    self._emit(f"{device.port}: pool message: {motd[:120]}")
                reconnect_delay = 2.0

                while not device.stop_event.is_set():
                    with device.lock:
                        device.status = "RUNNING"
                        device.phase = "Requesting job from pool"
                        difficulty_group = device.difficulty_group
                    self._touch(device)
                    key = str(cfg.get("mining_key", "None")).strip() or "None"
                    username = str(cfg.get("username", "")).strip()
                    sock.sendall(
                        f"JOB,{username},{difficulty_group},{key}".encode("utf-8")
                    )
                    job_text = self._recv_message(sock, device.stop_event, 15.0, 0.1)
                    fields = [part.strip() for part in job_text.split(",")]
                    if len(fields) < 3:
                        raise ValueError(f"Invalid pool job response: {job_text[:100]}")
                    prev_hash, expected_hash = fields[0], fields[1]
                    if not re.fullmatch(r"[0-9a-fA-F]{40}", prev_hash):
                        raise ValueError("Pool returned an invalid previous hash")
                    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_hash):
                        raise ValueError("Pool returned an invalid target hash")
                    try:
                        diff = int(fields[2])
                    except ValueError as exc:
                        raise ValueError(f"Invalid pool difficulty: {fields[2]}") from exc
                    if not 1 <= diff <= 655:
                        raise ValueError(f"AVR difficulty outside supported range: {diff}")

                    with device.lock:
                        device.diff = diff
                        device.phase = f"Searching · pool diff {diff}"
                    self._emit(
                        f"{device.port}: job received | {difficulty_group} tier | "
                        f"pool diff {diff} | AVR searching",
                    )
                    ser.reset_input_buffer()
                    ser.write(f"{prev_hash},{expected_hash},{diff},".encode("ascii"))
                    ser.flush()
                    result_line = _read_serial_line(
                        ser, time.monotonic() + 45.0, cancel=device.stop_event
                    )
                    with device.lock:
                        device.phase = "Share ready · submitting to pool"
                    nonce, elapsed_us, duco_id = parse_firmware_result(result_line)
                    if duco_id != device.duco_id:
                        raise ValueError("The serial device returned a different DUCOID")
                    if elapsed_us <= 0:
                        raise ValueError("AVR returned a zero hash time")
                    hashrate = round(nonce / (elapsed_us / 1_000_000.0), 2)
                    rig_name = str(cfg.get("rig_name", "None")).strip() or "None"
                    submission = (
                        f"{nonce},{hashrate},Snelli-1 Miner {APP_VERSION},"
                        f"{rig_name},{duco_id}"
                    )
                    with device.lock:
                        device.phase = "Submitting share to pool"
                    started = time.perf_counter()
                    sock.sendall(submission.encode("utf-8"))
                    feedback = self._recv_message(sock, device.stop_event, 15.0, 0.1)
                    ping_ms = (time.perf_counter() - started) * 1000.0
                    feedback_parts = [part.strip() for part in feedback.split(",")]
                    verdict = feedback_parts[0].upper() if feedback_parts else ""
                    with device.lock:
                        device.hashrate = hashrate
                        device.diff = diff
                        device.ping_ms = ping_ms
                        device.last_progress = time.monotonic()
                        if verdict in ("GOOD", "BLOCK"):
                            device.accepted += 1
                        else:
                            device.rejected += 1
                        accepted_count = device.accepted
                        rejected_count = device.rejected
                        device.phase = (
                            f"{verdict} · requesting next job"
                            if verdict in ("GOOD", "BLOCK")
                            else "Share rejected · requesting next job"
                        )
                    if verdict in ("GOOD", "BLOCK"):
                        self._emit(
                            f"{device.port}: #{accepted_count} {verdict} | "
                            f"{hashrate:.2f} H/s | {difficulty_group} tier | "
                            f"pool diff {diff} | ping {ping_ms:.0f} ms",
                            "good",
                        )
                    else:
                        cause = ",".join(feedback_parts[1:])[:80]
                        self._emit(
                            f"{device.port}: rejected share #{rejected_count} "
                            f"({feedback or 'empty response'})"
                            + (f" — {cause}" if cause else ""),
                            "bad",
                        )
            except InterruptedError:
                break
            except Exception as exc:
                with device.lock:
                    disconnected = bool(
                        serial is not None
                        and isinstance(exc, serial.SerialException)
                    )
                    device.status = "DISCONNECTED" if disconnected else "RECONNECTING"
                    device.phase = (
                        "USB device disconnected"
                        if disconnected
                        else f"Reconnecting in {reconnect_delay:.0f}s"
                    )
                    device.hashrate = 0.0
                    device.last_progress = time.monotonic()
                if not device.stop_event.is_set():
                    logging.exception("Miner loop failed for %s", device.port)
                    self._emit(
                        f"{device.port}: {exc}; retrying in {reconnect_delay:.0f}s",
                        "warn",
                    )
                    if reconnect_delay >= 8.0:
                        with self._pool_condition:
                            self._pool_time = 0.0
            finally:
                self._close_resources(device)
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        logging.exception("Pool socket close failed for %s", device.port)
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        logging.exception("Serial close failed for %s", device.port)

            if not device.stop_event.is_set():
                device.stop_event.wait(reconnect_delay)
                reconnect_delay = min(30.0, reconnect_delay * 1.6)

        with device.lock:
            device.status = "STOPPED"
            device.phase = "Stopped"
            device.hashrate = 0.0
        self._emit(f"{device.port}: stopped")
        with self._state_lock:
            self._threads.discard(threading.current_thread())


class WorkerCard(tk.Frame):
    """A telemetry card backed by one firmware-verified physical board."""

    def __init__(self, parent: tk.Misc, device: MinerDevice, board_callback: Any):
        super().__init__(
            parent,
            bg=PALETTE["bg_card"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        self.device = device
        self.board_callback = board_callback
        self.configure(width=310, height=172)
        self.grid_propagate(False)

        header = tk.Frame(self, bg=PALETTE["bg_card"])
        header.pack(fill="x")
        tk.Label(
            header,
            text=device.port,
            font=("Consolas", 12, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_card"],
        ).pack(side="left")
        self.status_label = tk.Label(
            header,
            text="DETECTED",
            font=("Consolas", 8, "bold"),
            fg=PALETTE["amber"],
            bg=PALETTE["bg_card"],
        )
        self.status_label.pack(side="right")

        board_row = tk.Frame(self, bg=PALETTE["bg_card"])
        board_row.pack(fill="x", pady=(6, 4))
        tk.Label(
            board_row,
            text="BOARD",
            font=("Segoe UI", 8, "bold"),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_card"],
        ).pack(side="left", padx=(0, 8))
        self.board_var = tk.StringVar(value=device.board_type)
        self.board_box = ttk.Combobox(
            board_row,
            textvariable=self.board_var,
            values=SUPPORTED_BOARDS,
            state="readonly",
            width=33,
        )
        self.board_box.pack(side="left", fill="x", expand=True)
        self.board_box.bind("<<ComboboxSelected>>", self._on_board_selected)

        self.hashrate_label = tk.Label(
            self,
            text="0 H/s",
            font=("Consolas", 17, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_card"],
            anchor="w",
        )
        self.hashrate_label.pack(fill="x", pady=(2, 0))
        self.test_label = tk.Label(
            self,
            text=f"AVR test {device.test_hashrate:.2f} H/s · {device.difficulty_group} tier",
            font=("Consolas", 8),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_card"],
            anchor="w",
        )
        self.test_label.pack(fill="x", pady=(0, 2))
        self.phase_label = tk.Label(
            self,
            text=device.phase,
            font=("Segoe UI", 8, "bold"),
            fg=PALETTE["amber"],
            bg=PALETTE["bg_card"],
            anchor="w",
        )
        self.phase_label.pack(fill="x", pady=(0, 2))

        stats = tk.Frame(self, bg=PALETTE["bg_card"])
        stats.pack(fill="x")
        self.accepted_label = tk.Label(
            stats,
            text="Accepted  0",
            font=("Segoe UI", 9, "bold"),
            fg=PALETTE["green"],
            bg=PALETTE["bg_card"],
        )
        self.accepted_label.pack(side="left")
        self.rejected_label = tk.Label(
            stats,
            text="Rejected  0",
            font=("Segoe UI", 9, "bold"),
            fg=PALETTE["red"],
            bg=PALETTE["bg_card"],
        )
        self.rejected_label.pack(side="right")

        foot = tk.Frame(self, bg=PALETTE["bg_card"])
        foot.pack(fill="x", pady=(5, 0))
        self.ping_label = tk.Label(
            foot,
            text="Ping  —",
            font=("Segoe UI", 8),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_card"],
        )
        self.ping_label.pack(side="left")
        self.diff_label = tk.Label(
            foot,
            text="Pool diff  —",
            font=("Segoe UI", 8),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_card"],
        )
        self.diff_label.pack(side="left", padx=(12, 0))
        self.id_label = tk.Label(
            foot,
            text=device.duco_id,
            font=("Consolas", 7),
            fg=PALETTE["text_muted"],
            bg=PALETTE["bg_card"],
        )
        self.id_label.pack(side="right")

    def _on_board_selected(self, _event: Any = None) -> None:
        value = self.board_var.get()
        self.board_callback(self.device, value)

    def refresh(self) -> None:
        state = self.device.snapshot()
        status = state["status"]
        self.status_label.configure(text=status)
        if status == "RUNNING":
            color = PALETTE["green"]
        elif status in ("RECONNECTING", "CONNECTING", "POOL ERROR"):
            color = PALETTE["amber"]
        elif status in ("STOPPED", "DISCONNECTED"):
            color = PALETTE["text_muted"]
        else:
            color = PALETTE["cyan"]
        self.status_label.configure(fg=color)

        rate = float(state["hashrate"])
        if rate >= 1_000_000:
            rate_text = f"{rate / 1_000_000:.2f} MH/s"
        elif rate >= 1_000:
            rate_text = f"{rate / 1_000:.2f} kH/s"
        else:
            rate_text = f"{rate:.1f} H/s"
        self.hashrate_label.configure(text=rate_text)
        self.accepted_label.configure(text=f"Accepted  {state['accepted']}")
        self.rejected_label.configure(text=f"Rejected  {state['rejected']}")
        ping = state["ping_ms"]
        self.ping_label.configure(
            text="Ping  —" if ping is None else f"Ping  {ping:.0f} ms"
        )
        self.test_label.configure(
            text=(
                f"AVR test {state['test_hashrate']:.2f} H/s · "
                f"{state['difficulty_group']} starting tier"
            )
        )
        phase = state["phase"]
        self.phase_label.configure(text=phase)
        if any(word in phase for word in ("Searching", "Submitting", "GOOD", "BLOCK")):
            phase_color = PALETTE["green"]
        elif any(word in phase for word in ("Reconnect", "disconnected", "rejected")):
            phase_color = PALETTE["amber"]
        elif phase == "Stopped":
            phase_color = PALETTE["text_muted"]
        else:
            phase_color = PALETTE["text_secondary"]
        self.phase_label.configure(fg=phase_color)
        diff = state["diff"]
        self.diff_label.configure(
            text="Pool diff  —" if diff is None else f"Pool diff  {diff}"
        )
        self.id_label.configure(text=state["duco_id"])
        if self.board_var.get() != state["board_type"]:
            self.board_var.set(state["board_type"])


class TelemetryWindow(tk.Toplevel):
    def __init__(self, parent: "MinerGUI"):
        super().__init__(parent)
        self.app = parent
        self.title("Snelli-1 Miner | Physical AVR telemetry")
        self.geometry("850x650")
        self.configure(bg=PALETTE["bg_main"])
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        tk.Label(
            self,
            text="PHYSICAL AVR TELEMETRY",
            font=("Consolas", 14, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_main"],
        ).pack(anchor="w", padx=14, pady=(12, 4))
        self.summary_label = tk.Label(
            self,
            text="",
            font=("Segoe UI", 10),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_main"],
        )
        self.summary_label.pack(anchor="w", padx=14, pady=(0, 8))
        account_frame = tk.Frame(
            self,
            bg=PALETTE["bg_panel"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=8,
            pady=7,
        )
        account_frame.pack(fill="x", padx=14, pady=(0, 8))
        account_items = (
            ("balance", "DUCO BALANCE"),
            ("daily_profit", "EST. 24H MINING"),
            ("visible", "NETWORK MINERS"),
            ("hashrate", "ACCOUNT HASHRATE"),
            ("acceptance", "ACCOUNT ACCEPTED"),
            ("price", "DUCO PRICE"),
            ("wallet_usd", "WALLET VALUE"),
            ("api_status", "ACCOUNT API"),
        )
        self.account_fields: dict[str, tk.Label] = {}
        for index, (key, title) in enumerate(account_items):
            column = index % 4
            row = index // 4
            cell = tk.Frame(account_frame, bg=PALETTE["bg_panel"], padx=7, pady=3)
            cell.grid(row=row, column=column, sticky="nsew")
            account_frame.grid_columnconfigure(column, weight=1)
            tk.Label(
                cell,
                text=title,
                font=("Consolas", 7, "bold"),
                fg=PALETTE["text_secondary"],
                bg=PALETTE["bg_panel"],
            ).pack(anchor="w")
            value = tk.Label(
                cell,
                text="—",
                font=("Segoe UI", 10, "bold"),
                fg=PALETTE["cyan"],
                bg=PALETTE["bg_panel"],
            )
            value.pack(anchor="w")
            self.account_fields[key] = value
        self.wallet_graph = tk.Canvas(
            self,
            height=85,
            bg=PALETTE["bg_terminal"],
            highlightbackground=PALETTE["purple"],
            highlightthickness=1,
        )
        self.wallet_graph.pack(fill="x", padx=14, pady=(0, 8))
        self.graph = tk.Canvas(
            self,
            height=125,
            bg=PALETTE["bg_terminal"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
        )
        self.graph.pack(fill="x", padx=14, pady=(0, 10))
        columns = ("port", "board", "rate", "accepted", "rejected", "ping", "diff", "status")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=6)
        labels = {
            "port": "COM",
            "board": "Board",
            "rate": "Hashrate",
            "accepted": "Accepted",
            "rejected": "Rejected",
            "ping": "Ping",
            "diff": "Pool diff / tier",
            "status": "Status / activity",
        }
        widths = {
            "port": 65,
            "board": 180,
            "rate": 90,
            "accepted": 70,
            "rejected": 70,
            "ping": 70,
            "diff": 100,
            "status": 150,
        }
        for column in columns:
            self.tree.heading(column, text=labels[column])
            self.tree.column(column, width=widths[column], anchor="center")
        self.tree.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    def refresh(
        self,
        history: list[float],
        account: dict[str, Any],
        wallet_history: list[tuple[float, float]],
    ) -> None:
        devices = self.app.engine.device_list()
        snapshots = [device.snapshot() for device in devices]
        total_rate = sum(item["hashrate"] for item in snapshots)
        accepted = sum(item["accepted"] for item in snapshots)
        rejected = sum(item["rejected"] for item in snapshots)
        self.summary_label.configure(
            text=(
                f"Devices: {len(snapshots)}    Hashrate: {format_rate(total_rate)}"
                f"    Accepted: {accepted}    Rejected: {rejected}"
            )
        )
        balance = account.get("balance")
        profit = account.get("daily_profit")
        price = account.get("price")
        account_rate = account.get("hashrate")
        acceptance = account.get("acceptance")
        wallet_value = (
            float(balance) * float(price)
            if balance is not None and price is not None
            else None
        )
        fields = {
            "balance": "—" if balance is None else f"Ð {float(balance):.4f}",
            "daily_profit": (
                "CALIBRATING" if profit is None else f"Ð {float(profit):.4f}"
            ),
            "visible": "—" if account.get("visible") is None else str(account["visible"]),
            "hashrate": (
                "—" if account_rate is None else format_rate(float(account_rate))
            ),
            "acceptance": (
                "—" if acceptance is None else f"{float(acceptance):.2f}%"
            ),
            "price": "—" if price is None else f"$ {float(price):.8f}",
            "wallet_usd": (
                "—" if wallet_value is None else f"$ {wallet_value:.4f}"
            ),
            "api_status": str(account.get("api_status", "IDLE")),
        }
        for key, value in fields.items():
            color = (
                PALETTE["green"]
                if key == "api_status" and value == "ONLINE"
                else PALETTE["red"]
                if key == "api_status" and value == "OFFLINE"
                else PALETTE["cyan"]
            )
            self.account_fields[key].configure(text=value, fg=color)

        self.wallet_graph.delete("all")
        wallet_width = max(100, self.wallet_graph.winfo_width())
        wallet_height = max(60, self.wallet_graph.winfo_height())
        self.wallet_graph.create_text(
            8,
            6,
            text="WALLET BALANCE HISTORY",
            anchor="nw",
            fill=PALETTE["text_secondary"],
            font=("Consolas", 7, "bold"),
        )
        if len(wallet_history) >= 2:
            balances = [item[1] for item in wallet_history]
            low, high = min(balances), max(balances)
            spread = max(high - low, 1e-9)
            points: list[float] = []
            for index, value in enumerate(balances):
                x = 8 + index * (wallet_width - 16) / max(1, len(balances) - 1)
                y = wallet_height - 8 - (wallet_height - 25) * (
                    (value - low) / spread
                )
                points.extend((x, y))
            if len(points) >= 4:
                self.wallet_graph.create_line(
                    *points, fill=PALETTE["green"], width=2, smooth=True
                )
        else:
            self.wallet_graph.create_text(
                wallet_width // 2,
                wallet_height // 2,
                text="Account balance samples appear after API refresh",
                fill=PALETTE["text_muted"],
                font=("Segoe UI", 8),
            )
        existing = set(self.tree.get_children())
        wanted = {item["port"] for item in snapshots}
        for item_id in existing - wanted:
            self.tree.delete(item_id)
        for item in snapshots:
            ping = "—" if item["ping_ms"] is None else f"{item['ping_ms']:.0f} ms"
            diff = (
                f"— / {item['difficulty_group']}"
                if item["diff"] is None
                else f"{item['diff']} / {item['difficulty_group']}"
            )
            activity = f"{item['status']} · {item['phase']}"
            values = (
                item["port"],
                item["board_type"],
                format_rate(item["hashrate"]),
                item["accepted"],
                item["rejected"],
                ping,
                diff,
                activity,
            )
            if self.tree.exists(item["port"]):
                self.tree.item(item["port"], values=values)
            else:
                self.tree.insert("", "end", iid=item["port"], values=values)
        self.graph.delete("all")
        width = max(100, self.graph.winfo_width())
        height = max(100, self.graph.winfo_height())
        self.graph.create_text(
            10,
            8,
            text="TOTAL HASHRATE",
            anchor="nw",
            fill=PALETTE["text_secondary"],
            font=("Consolas", 8, "bold"),
        )
        if len(history) < 2:
            self.graph.create_text(
                width // 2,
                height // 2,
                text="Waiting for real device telemetry",
                fill=PALETTE["text_muted"],
                font=("Segoe UI", 9),
            )
            return
        maximum = max(max(history), 1.0)
        points: list[float] = []
        for index, value in enumerate(history):
            x = 8 + index * (width - 16) / max(1, len(history) - 1)
            y = height - 12 - (height - 34) * (value / maximum)
            points.extend((x, y))
        if len(points) >= 4:
            self.graph.create_line(
                *points, fill=PALETTE["cyan"], width=2, smooth=True
            )


def format_rate(rate: float) -> str:
    if rate >= 1_000_000:
        return f"{rate / 1_000_000:.2f} MH/s"
    if rate >= 1_000:
        return f"{rate / 1_000:.2f} kH/s"
    return f"{rate:.1f} H/s"


class MinerGUI(tk.Tk):
    def __init__(
        self,
        cfg: dict[str, Any],
        initial_devices: list[MinerDevice],
        startup_messages: list[str],
    ):
        super().__init__()
        self.cfg = cfg
        self.title(f"Snelli-1 Miner V{APP_VERSION} | Physical Duino-Coin AVR")
        self.geometry("1480x960")
        self.minsize(1120, 760)
        self.configure(bg=PALETTE["bg_main"])

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2000)
        self._monitor_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=2)
        self._monitor_lock = threading.Lock()
        self._monitor_busy = False
        self._wallet_samples: deque[tuple[float, float]] = deque(maxlen=720)
        self._last_monitor_balance: float | None = None
        self._monitor_username: str | None = None
        self._account_telemetry: dict[str, Any] = {
            "balance": None,
            "daily_profit": None,
            "visible": None,
            "hashrate": None,
            "acceptance": None,
            "price": None,
            "wallet_usd": None,
            "api_status": "IDLE",
        }
        self._closing = False
        self._discovery_busy = False
        self._pool_check_busy = False
        self._scan_cancel = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._aux_threads: set[threading.Thread] = set()
        self._history: list[float] = []
        self._last_history_at = 0.0
        self._cards: dict[str, WorkerCard] = {}
        self._telemetry_window: TelemetryWindow | None = None
        self._settings_visible = True
        self.engine = MinerEngine(self._engine_event)

        for device in initial_devices:
            override = self.cfg.get("board_overrides", {}).get(device.port)
            if override in SUPPORTED_BOARDS:
                device.board_type = override
            self.engine.add_device(device)

        self._setup_style()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.cfg.get("load_error"):
            self._append_log(
                f"Settings could not be loaded: {self.cfg['load_error']}", "warn"
            )
        if initial_devices:
            self._append_log(
                f"Startup Auto Config verified {len(initial_devices)} physical AVR miner(s).",
                "good",
            )
        else:
            self._append_log(
                "Startup Auto Config finished; no verified AVR miner was found.", "warn"
            )
        self._render_cards()
        for message in startup_messages:
            self._append_log(message, "info")
        self.after(250, self._ui_tick)
        self.after(2000, self._watchdog_tick)
        self.after(1500, self._schedule_account_monitor)

    def _engine_event(self, kind: str, payload: Any) -> None:
        if kind == "log":
            message, level = payload
            log_method = {
                "good": logging.info,
                "warn": logging.warning,
                "bad": logging.error,
            }.get(level, logging.info)
            log_method("%s", message)
        try:
            self._events.put_nowait((kind, payload))
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait((kind, payload))
            except queue.Full:
                logging.error("GUI event queue is full")

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            logging.exception("Could not select the clam Tk theme")
        style.configure(
            "TCombobox",
            fieldbackground=PALETTE["bg_input"],
            background=PALETTE["bg_card"],
            foreground=PALETTE["text_primary"],
            arrowcolor=PALETTE["cyan"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PALETTE["bg_input"])],
            foreground=[("readonly", PALETTE["text_primary"])],
        )
        style.configure(
            "Treeview",
            background=PALETTE["bg_panel"],
            fieldbackground=PALETTE["bg_panel"],
            foreground=PALETTE["text_primary"],
            font=("Consolas", 8),
            rowheight=23,
        )
        style.configure(
            "Treeview.Heading",
            background=PALETTE["bg_card"],
            foreground=PALETTE["cyan"],
            font=("Consolas", 8, "bold"),
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Any,
        color: str = "bg_card",
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 8, "bold"),
            fg="white",
            bg=PALETTE[color],
            activebackground=PALETTE["border"],
            activeforeground="white",
            relief="flat",
            padx=9,
            pady=7,
            cursor="hand2",
        )

    def _build_layout(self) -> None:
        toolbar = tk.Frame(self, bg=PALETTE["bg_panel"], padx=12, pady=9)
        self.toolbar = toolbar
        toolbar.pack(fill="x")
        brand = tk.Frame(toolbar, bg=PALETTE["bg_panel"])
        brand.pack(side="left", padx=(0, 12))
        tk.Label(
            brand,
            text="⬡",
            font=("Consolas", 25, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_panel"],
        ).pack(side="left", padx=(0, 8))
        brand_text = tk.Frame(brand, bg=PALETTE["bg_panel"])
        brand_text.pack(side="left")
        tk.Label(
            brand_text,
            text="SNELLI-1 MINER",
            font=("Consolas", 13, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_panel"],
        ).pack(anchor="w")
        tk.Label(
            brand_text,
            text="PHYSICAL DUINO-COIN AVR CONTROL // V4.5",
            font=("Consolas", 7, "bold"),
            fg=PALETTE["magenta"],
            bg=PALETTE["bg_panel"],
        ).pack(anchor="w")

        controls = tk.Frame(toolbar, bg=PALETTE["bg_panel"])
        controls.pack(side="right")
        self.btn_start = self._button(
            controls, "▶ START ALL", self._on_start, "green_btn"
        )
        self.btn_start.pack(side="left", padx=3)
        self.btn_stop = self._button(
            controls, "■ STOP ALL", self._on_stop, "red_btn"
        )
        self.btn_stop.pack(side="left", padx=3)
        self.btn_autoconfig = self._button(
            controls, "AUTO CONFIG", self._start_discovery, "bg_card"
        )
        self.btn_autoconfig.pack(side="left", padx=3)
        self.btn_refresh = self._button(
            controls, "REFRESH DEVICES", self._start_discovery, "bg_card"
        )
        self.btn_refresh.pack(side="left", padx=3)
        self.btn_telemetry = self._button(
            controls, "TELEMETRY", self._show_telemetry, "bg_card"
        )
        self.btn_telemetry.pack(side="left", padx=3)
        self.btn_pool = self._button(
            controls, "POOL STATUS", self._check_pool, "bg_card"
        )
        self.btn_pool.pack(side="left", padx=3)
        self.btn_save = self._button(
            controls, "SAVE SETTINGS", self._save_settings, "bg_card"
        )
        self.btn_save.pack(side="left", padx=3)

        self.settings_frame = tk.Frame(
            self, bg=PALETTE["bg_panel"], padx=12, pady=8
        )
        self.settings_frame.pack(fill="x", padx=10, pady=(8, 4))
        self._build_settings()

        stats = tk.Frame(self, bg=PALETTE["bg_main"], padx=8)
        stats.pack(fill="x", pady=(4, 7))
        self.stat_devices = self._stat_card(stats, 0, "VERIFIED AVR", "0")
        self.stat_rate = self._stat_card(stats, 1, "TOTAL HASHRATE", "0 H/s")
        self.stat_shares = self._stat_card(stats, 2, "ACCEPTED / REJECTED", "0 / 0")
        self.stat_uptime = self._stat_card(stats, 3, "MINING UPTIME", "00:00:00")

        account_stats = tk.Frame(self, bg=PALETTE["bg_main"], padx=8)
        account_stats.pack(fill="x", pady=(0, 5))
        self.stat_account_balance = self._stat_card(
            account_stats, 0, "DUCO BALANCE", "—"
        )
        self.stat_account_profit = self._stat_card(
            account_stats, 1, "ESTIMATED 24H", "CALIBRATING"
        )
        self.stat_account_network = self._stat_card(
            account_stats, 2, "NETWORK MINERS / HASHRATE", "—"
        )
        self.stat_account_price = self._stat_card(
            account_stats, 3, "DUCO PRICE / WALLET USD", "—"
        )

        poolbar = tk.Frame(
            self,
            bg=PALETTE["bg_panel"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=10,
            pady=7,
        )
        poolbar.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(
            poolbar,
            text="POOL STATUS",
            font=("Consolas", 9, "bold"),
            fg=PALETTE["magenta"],
            bg=PALETTE["bg_panel"],
        ).pack(side="left")
        self.pool_status_label = tk.Label(
            poolbar,
            text="Not checked",
            font=("Segoe UI", 9),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_panel"],
        )
        self.pool_status_label.pack(side="left", padx=12)
        self.scan_status_label = tk.Label(
            poolbar,
            text=f"Startup port scan complete • log: {LOG_PATH}",
            font=("Segoe UI", 8),
            fg=PALETTE["text_muted"],
            bg=PALETTE["bg_panel"],
        )
        self.scan_status_label.pack(side="right")
        self.account_api_label = tk.Label(
            poolbar,
            text="Account API: idle",
            font=("Segoe UI", 8),
            fg=PALETTE["text_muted"],
            bg=PALETTE["bg_panel"],
        )
        self.account_api_label.pack(side="right", padx=(0, 14))

        activity_bar = tk.Frame(
            self,
            bg=PALETTE["bg_panel"],
            highlightbackground=PALETTE["cyan"],
            highlightthickness=1,
            padx=10,
            pady=5,
        )
        activity_bar.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(
            activity_bar,
            text="LIVE ACTIVITY",
            font=("Consolas", 8, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_panel"],
        ).pack(side="left", padx=(0, 10))
        self.live_activity_label = tk.Label(
            activity_bar,
            text="Waiting for physical AVR activity…",
            font=("Segoe UI", 9, "bold"),
            fg=PALETTE["text_primary"],
            bg=PALETTE["bg_panel"],
            anchor="w",
            justify="left",
            wraplength=1280,
        )
        self.live_activity_label.pack(side="left", fill="x", expand=True)

        table_panel = tk.Frame(
            self,
            bg=PALETTE["bg_panel"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=6,
            pady=5,
        )
        table_panel.pack(fill="x", padx=10, pady=(0, 5))
        tk.Label(
            table_panel,
            text=">_ LIVE AVR MINER TABLE",
            font=("Consolas", 8, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_panel"],
        ).pack(anchor="w", padx=4, pady=(0, 4))
        table_shell = tk.Frame(table_panel, bg=PALETTE["bg_panel"])
        table_shell.pack(fill="x")
        worker_columns = (
            "index", "miner", "com", "accepted", "rejected", "rate",
            "diff", "ping", "node", "board", "algorithm", "status",
            "phase", "ducoid",
        )
        self.worker_table = ttk.Treeview(
            table_shell,
            columns=worker_columns,
            show="headings",
            height=5,
            selectmode="browse",
        )
        self.worker_table.tag_configure("active", foreground=PALETTE["green"])
        self.worker_table.tag_configure("reconnecting", foreground=PALETTE["amber"])
        self.worker_table.tag_configure("disconnected", foreground=PALETTE["red"])
        self.worker_table.tag_configure("idle", foreground=PALETTE["text_secondary"])
        worker_labels = {
            "index": "#",
            "miner": "Miner / Version",
            "com": "COM",
            "accepted": "Accepted",
            "rejected": "Rejected",
            "rate": "Hashrate",
            "diff": "Diff",
            "ping": "Ping",
            "node": "Node",
            "board": "Board",
            "algorithm": "Algorithm",
            "status": "Status",
            "phase": "Phase",
            "ducoid": "DUCOID",
        }
        worker_widths = {
            "index": 34, "miner": 142, "com": 62, "accepted": 72,
            "rejected": 72, "rate": 96, "diff": 52, "ping": 68,
            "node": 125, "board": 150, "algorithm": 82, "status": 92,
            "phase": 155, "ducoid": 145,
        }
        for column in worker_columns:
            self.worker_table.heading(column, text=worker_labels[column])
            self.worker_table.column(
                column, width=worker_widths[column], anchor="center", stretch=False
            )
        worker_vscroll = ttk.Scrollbar(
            table_shell, orient="vertical", command=self.worker_table.yview
        )
        worker_hscroll = ttk.Scrollbar(
            table_shell, orient="horizontal", command=self.worker_table.xview
        )
        self.worker_table.configure(
            yscrollcommand=worker_vscroll.set, xscrollcommand=worker_hscroll.set
        )
        self.worker_table.grid(row=0, column=0, sticky="nsew")
        worker_vscroll.grid(row=0, column=1, sticky="ns")
        worker_hscroll.grid(row=1, column=0, sticky="ew")
        table_shell.grid_columnconfigure(0, weight=1)

        section = tk.Frame(self, bg=PALETTE["bg_main"], padx=12)
        section.pack(fill="x", pady=(3, 4))
        tk.Label(
            section,
            text="VERIFIED PHYSICAL WORKERS",
            font=("Consolas", 10, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_main"],
        ).pack(side="left")
        self.device_count_label = tk.Label(
            section,
            text="0 devices",
            font=("Segoe UI", 8),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_main"],
        )
        self.device_count_label.pack(side="right")

        device_area = tk.Frame(self, bg=PALETTE["bg_main"], padx=10)
        device_area.pack(fill="both", expand=True)
        self.grid_canvas = tk.Canvas(
            device_area,
            bg=PALETTE["bg_main"],
            highlightthickness=0,
        )
        self.grid_scroll = ttk.Scrollbar(
            device_area, orient="vertical", command=self.grid_canvas.yview
        )
        self.grid_canvas.configure(yscrollcommand=self.grid_scroll.set)
        self.grid_scroll.pack(side="right", fill="y")
        self.grid_canvas.pack(side="left", fill="both", expand=True)
        self.grid_inner = tk.Frame(self.grid_canvas, bg=PALETTE["bg_main"])
        self.grid_window = self.grid_canvas.create_window(
            (0, 0), window=self.grid_inner, anchor="nw"
        )
        self.grid_inner.bind("<Configure>", self._on_grid_configure)
        self.grid_canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_mousewheel()

        terminal_header = tk.Frame(
            self, bg=PALETTE["bg_panel"], padx=10, pady=5
        )
        terminal_header.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(
            terminal_header,
            text=">_ LIVE MINER LOG",
            font=("Consolas", 8, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_panel"],
        ).pack(side="left")
        self.btn_settings = self._button(
            terminal_header, "SETTINGS", self._toggle_settings, "bg_card"
        )
        self.btn_settings.pack(side="right")
        log_frame = tk.Frame(self, bg=PALETTE["bg_terminal"])
        log_frame.pack(fill="x", padx=10, pady=(0, 8))
        self.log_text = tk.Text(
            log_frame,
            height=7,
            bg=PALETTE["bg_terminal"],
            fg=PALETTE["text_secondary"],
            insertbackground=PALETTE["cyan"],
            relief="flat",
            font=("Consolas", 8),
            wrap="word",
            state="disabled",
        )
        log_scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("good", foreground=PALETTE["green"])
        self.log_text.tag_configure("warn", foreground=PALETTE["amber"])
        self.log_text.tag_configure("bad", foreground=PALETTE["red"])
        self.log_text.tag_configure("info", foreground=PALETTE["text_secondary"])

        footer = tk.Frame(self, bg=PALETTE["bg_panel"], padx=10, pady=5)
        footer.pack(fill="x", side="bottom")
        self.footer_label = tk.Label(
            footer,
            text="Python 3.11+ • pyserial AVR backend",
            font=("Segoe UI", 8),
            fg=PALETTE["text_muted"],
            bg=PALETTE["bg_panel"],
        )
        self.footer_label.pack(side="left")
        self.footer_state = tk.Label(
            footer,
            text="READY",
            font=("Consolas", 8, "bold"),
            fg=PALETTE["green"],
            bg=PALETTE["bg_panel"],
        )
        self.footer_state.pack(side="right")

    def _build_settings(self) -> None:
        settings = [
            ("Username", "username", 18, False),
            ("Mining key", "mining_key", 18, True),
            ("Rig name", "rig_name", 16, False),
            ("Pool host", "pool_host", 16, False),
            ("TCP port", "pool_port", 7, False),
            ("Baud", "baudrate", 9, False),
        ]
        self.setting_vars: dict[str, tk.StringVar] = {}
        for label, key, width, secret in settings:
            cell = tk.Frame(self.settings_frame, bg=PALETTE["bg_panel"])
            cell.pack(side="left", padx=(0, 9), pady=2)
            tk.Label(
                cell,
                text=label,
                font=("Segoe UI", 8, "bold"),
                fg=PALETTE["text_secondary"],
                bg=PALETTE["bg_panel"],
            ).pack(anchor="w")
            variable = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.setting_vars[key] = variable
            entry = tk.Entry(
                cell,
                textvariable=variable,
                width=width,
                show="●" if secret else "",
                bg=PALETTE["bg_input"],
                fg=PALETTE["text_primary"],
                insertbackground=PALETTE["cyan"],
                relief="flat",
                font=("Segoe UI", 9),
            )
            entry.pack()

    def _stat_card(
        self, parent: tk.Misc, column: int, title: str, value: str
    ) -> tk.Label:
        frame = tk.Frame(
            parent,
            bg=PALETTE["bg_panel"],
            highlightbackground=PALETTE["border"],
            highlightthickness=1,
            padx=10,
            pady=7,
        )
        frame.grid(row=0, column=column, padx=4, sticky="nsew")
        parent.grid_columnconfigure(column, weight=1)
        tk.Label(
            frame,
            text=title,
            font=("Consolas", 8, "bold"),
            fg=PALETTE["text_secondary"],
            bg=PALETTE["bg_panel"],
        ).pack(anchor="w")
        label = tk.Label(
            frame,
            text=value,
            font=("Segoe UI", 15, "bold"),
            fg=PALETTE["cyan"],
            bg=PALETTE["bg_panel"],
        )
        label.pack(anchor="w", pady=(2, 0))
        return label

    def _append_log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        if not hasattr(self, "log_text") or not self.log_text.winfo_exists():
            return
        if hasattr(self, "live_activity_label") and self.live_activity_label.winfo_exists():
            activity_colors = {
                "good": PALETTE["green"],
                "warn": PALETTE["amber"],
                "bad": PALETTE["red"],
            }
            self.live_activity_label.configure(
                text=message, fg=activity_colors.get(level, PALETTE["text_primary"])
            )
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {message}\n", level)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 550:
            self.log_text.delete("1.0", f"{line_count - 500}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _read_settings(self, validate: bool = True) -> dict[str, Any]:
        cfg = dict(self.cfg)
        for key, variable in self.setting_vars.items():
            cfg[key] = variable.get().strip()
        if validate and not cfg["username"]:
            raise ValueError("Enter the Duino-Coin username before starting.")
        try:
            cfg["pool_port"] = int(cfg["pool_port"])
            if not 1 <= cfg["pool_port"] <= 65535:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("Pool TCP port must be between 1 and 65535.") from exc
        try:
            cfg["baudrate"] = int(cfg["baudrate"])
            if not 1200 <= cfg["baudrate"] <= 2_000_000:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("Baud rate is outside the supported range.") from exc
        cfg["board_overrides"] = dict(self.cfg.get("board_overrides", {}))
        return cfg

    def _save_settings(self) -> None:
        try:
            cfg = self._read_settings(validate=False)
            for device in self.engine.device_list():
                cfg["board_overrides"][device.port] = device.board_type
            save_config(cfg)
            self.cfg = cfg
            self._append_log(f"Settings saved to {CONFIG_PATH}", "good")
            self.footer_state.configure(text="SETTINGS SAVED", fg=PALETTE["green"])
        except Exception as exc:
            logging.exception("Settings save failed")
            messagebox.showerror("Save settings", str(exc), parent=self)
            self._append_log(f"Settings were not saved: {exc}", "bad")

    def _toggle_settings(self) -> None:
        self._settings_visible = not self._settings_visible
        if self._settings_visible:
            self.settings_frame.pack(
                fill="x", padx=10, pady=(8, 4), after=self.toolbar
            )
        else:
            self.settings_frame.pack_forget()

    def _on_start(self) -> None:
        if not self.engine.device_list():
            self._append_log(
                "No verified physical AVR miners. Connect a board and use Refresh Devices.",
                "warn",
            )
            return
        try:
            cfg = self._read_settings(validate=True)
        except ValueError as exc:
            messagebox.showwarning("Cannot start", str(exc), parent=self)
            return
        self.cfg.update(cfg)
        self.engine.start_all(cfg)
        self.footer_state.configure(text="STARTING", fg=PALETTE["amber"])

    def _on_stop(self) -> None:
        self.engine.stop_all()
        self.footer_state.configure(text="STOPPING", fg=PALETTE["amber"])

    def _start_discovery(self) -> None:
        if self._closing or self._discovery_busy:
            return
        self._discovery_busy = True
        self._scan_cancel = threading.Event()
        self.btn_autoconfig.configure(state="disabled")
        self.btn_refresh.configure(state="disabled")
        self.scan_status_label.configure(text="Enumerating COM ports and verifying AVR firmware…")
        existing = {device.port for device in self.engine.device_list()}
        baudrate = int(self.setting_vars["baudrate"].get() or BAUDRATE)

        def scan_task() -> None:
            try:
                devices = scan_avr_devices(
                    baudrate,
                    existing_ports=existing,
                    cancel=self._scan_cancel,
                    log_callback=lambda message: self._engine_event(
                        "log", (message, "info")
                    ),
                )
                self._engine_event("scan_complete", (devices, None))
            except Exception as exc:
                logging.exception("Device discovery failed")
                self._engine_event("scan_complete", ([], str(exc)))

        self._scan_thread = threading.Thread(
            target=scan_task, name="DeviceDiscovery", daemon=False
        )
        self._scan_thread.start()

    def _finish_discovery(self, devices: list[MinerDevice], error: str | None) -> None:
        self._discovery_busy = False
        self.btn_autoconfig.configure(state="normal")
        self.btn_refresh.configure(state="normal")
        if error:
            self.scan_status_label.configure(text="Device scan failed; see log")
            self._append_log(f"Device scan failed: {error}", "bad")
            return
        added = 0
        for device in devices:
            override = self.cfg.get("board_overrides", {}).get(device.port)
            if override in SUPPORTED_BOARDS:
                device.board_type = override
            if self.engine.add_device(device):
                added += 1
                if self.engine.running:
                    self.engine.start_device(device)
        self._render_cards()
        count = len(self.engine.device_list())
        self.device_count_label.configure(f"{count} device(s)")
        self.scan_status_label.configure(
            text=f"Scan complete • {added} new verified • {count} total"
        )
        if added:
            self._append_log(f"Auto Config added {added} verified physical AVR miner(s).", "good")
        else:
            self._append_log("Refresh complete; no new AVR miner was found.", "info")

    def _check_pool(self) -> None:
        if self._closing or self._pool_check_busy:
            return
        try:
            cfg = self._read_settings(validate=False)
        except ValueError as exc:
            self._append_log(str(exc), "warn")
            return
        self._pool_check_busy = True
        self.btn_pool.configure(state="disabled")
        self.pool_status_label.configure(text="Checking pool endpoint…", fg=PALETTE["amber"])

        def check_task() -> None:
            try:
                info = self.engine.check_pool(cfg)
                self._engine_event("pool_status", (True, info, None))
            except Exception as exc:
                logging.exception("Pool status check failed")
                self._engine_event("pool_status", (False, None, str(exc)))

        thread = threading.Thread(target=check_task, name="PoolStatusCheck", daemon=False)
        self._aux_threads.add(thread)
        thread.start()

    def _schedule_account_monitor(self) -> None:
        if self._closing:
            return
        username = self.setting_vars["username"].get().strip()
        if username:
            with self._monitor_lock:
                should_start = not self._monitor_busy
                if should_start:
                    self._monitor_busy = True
            if should_start:
                thread = threading.Thread(
                    target=self._fetch_account_monitor,
                    args=(username,),
                    name="AccountTelemetry",
                    daemon=False,
                )
                self._aux_threads.add(thread)
                thread.start()
        self.after(30_000, self._schedule_account_monitor)

    def _fetch_account_monitor(self, username: str) -> None:
        try:
            headers = {"User-Agent": USER_AGENT}
            user_url = (
                "https://server.duinocoin.com/users/"
                + quote(username, safe="")
            )
            request = Request(user_url, headers=headers)
            with urlopen(request, timeout=8) as response:
                data = json.loads(response.read(1_000_000).decode("utf-8"))
            if not isinstance(data, dict) or data.get("success") is False:
                raise RuntimeError("Duino-Coin user API did not return success")

            result = data.get("result", data)
            if not isinstance(result, dict):
                raise ValueError("Unexpected user API response")
            balance_data = result.get("balance", {})
            balance = (
                balance_data.get("balance", 0.0)
                if isinstance(balance_data, dict)
                else balance_data
            )
            balance = float(balance or 0.0)
            miners = result.get("miners", []) or []
            if not isinstance(miners, list):
                miners = []
            account_hashrate = sum(
                float(miner.get("hashrate", 0) or 0)
                for miner in miners
                if isinstance(miner, dict)
            )
            accepted = sum(
                int(miner.get("accepted", 0) or 0)
                for miner in miners
                if isinstance(miner, dict)
            )
            rejected = sum(
                int(miner.get("rejected", 0) or 0)
                for miner in miners
                if isinstance(miner, dict)
            )
            acceptance = (
                100.0 * accepted / (accepted + rejected)
                if accepted + rejected
                else 0.0
            )

            price: float | None = None
            try:
                stats_request = Request(
                    "https://server.duinocoin.com/statistics",
                    headers=headers,
                )
                with urlopen(stats_request, timeout=8) as response:
                    stats = json.loads(response.read(256_000).decode("utf-8"))
                if isinstance(stats, dict):
                    stats = stats.get("result", stats)
                    for key in ("Duco price", "DUCO price", "duco_price", "price"):
                        if key in stats:
                            value = stats[key]
                            if isinstance(value, dict):
                                value = value.get("price", value.get("usd", 0))
                            price = float(value or 0.0)
                            break
            except Exception:
                logging.exception("Could not retrieve DUCO price statistics")

            self._post_monitor_result(
                True,
                {
                    "username": username,
                    "balance": balance,
                    "visible": len(miners),
                    "hashrate": account_hashrate,
                    "accepted": accepted,
                    "rejected": rejected,
                    "acceptance": acceptance,
                    "price": price if price and price > 0 else None,
                    "observed_at": time.monotonic(),
                },
            )
        except Exception as exc:
            logging.exception("Account telemetry refresh failed")
            self._post_monitor_result(False, str(exc))
        finally:
            with self._monitor_lock:
                self._monitor_busy = False

    def _post_monitor_result(self, success: bool, payload: Any) -> None:
        try:
            self._monitor_queue.put_nowait((success, payload))
        except queue.Full:
            try:
                self._monitor_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._monitor_queue.put_nowait((success, payload))
            except queue.Full:
                logging.error("Account telemetry result queue is full")

    def _consume_account_monitor(self) -> None:
        latest: tuple[bool, Any] | None = None
        while True:
            try:
                latest = self._monitor_queue.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        success, payload = latest
        if not success:
            self._account_telemetry["api_status"] = "OFFLINE"
            self.account_api_label.configure(
                text="Account API: offline",
                fg=PALETTE["red"],
            )
            self._append_log(f"Account telemetry unavailable: {payload}", "warn")
            return

        if payload["username"] != self._monitor_username:
            self._wallet_samples.clear()
            self._last_monitor_balance = None
            self._monitor_username = payload["username"]
        balance = float(payload["balance"])
        observed_at = float(payload["observed_at"])
        previous_balance = self._last_monitor_balance
        self._last_monitor_balance = balance
        self._wallet_samples.append((observed_at, balance))
        mined_delta = (
            max(0.0, balance - previous_balance)
            if previous_balance is not None
            else None
        )
        daily_profit: float | None = None
        if len(self._wallet_samples) >= 2:
            first_time, first_balance = self._wallet_samples[0]
            elapsed = observed_at - first_time
            if elapsed >= 300 and balance >= first_balance:
                daily_profit = (balance - first_balance) * 86_400.0 / elapsed
        price = payload.get("price")
        self._account_telemetry = {
            **payload,
            "daily_profit": daily_profit,
            "mined_delta": mined_delta,
            "wallet_usd": balance * price if price is not None else None,
            "api_status": "ONLINE",
        }
        username = payload["username"]
        self.account_api_label.configure(
            text=f"Account API: {username} online",
            fg=PALETTE["green"],
        )
        self.stat_account_balance.configure(text=f"Ð {balance:.4f}")
        self.stat_account_profit.configure(
            text="CALIBRATING"
            if daily_profit is None
            else f"Ð {daily_profit:.4f} / day"
        )
        self.stat_account_network.configure(
            text=f"{payload['visible']} / {format_rate(payload['hashrate'])}"
        )
        price_text = "—" if price is None else f"$ {price:.8f}"
        wallet_value = self._account_telemetry["wallet_usd"]
        if wallet_value is not None:
            price_text += f" / $ {wallet_value:.4f}"
        self.stat_account_price.configure(text=price_text)

    def _show_telemetry(self) -> None:
        if self._telemetry_window is None or not self._telemetry_window.winfo_exists():
            self._telemetry_window = TelemetryWindow(self)
        self._telemetry_window.deiconify()
        self._telemetry_window.lift()
        self._telemetry_window.refresh(
            self._history,
            self._account_telemetry,
            list(self._wallet_samples),
        )

    def _on_board_change(self, device: MinerDevice, value: str) -> None:
        self.engine.set_board_type(device, value)
        self.cfg.setdefault("board_overrides", {})[device.port] = value

    def _render_cards(self) -> None:
        for child in self.grid_inner.winfo_children():
            child.destroy()
        self._cards.clear()
        devices = self.engine.device_list()
        canvas_width = max(320, self.grid_canvas.winfo_width())
        columns = max(1, (canvas_width - 24) // 324)
        for index, device in enumerate(devices):
            card = WorkerCard(self.grid_inner, device, self._on_board_change)
            card.grid(
                row=index // columns,
                column=index % columns,
                padx=5,
                pady=5,
                sticky="nw",
            )
            self._cards[device.port.casefold()] = card
        for column in range(columns):
            self.grid_inner.grid_columnconfigure(column, weight=0)
        self.device_count_label.configure(text=f"{len(devices)} device(s)")
        self._on_grid_configure()

    def _on_grid_configure(self, _event: Any = None) -> None:
        self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all"))

    def _on_canvas_configure(self, event: Any) -> None:
        self.grid_canvas.itemconfigure(self.grid_window, width=event.width)
        self.after_idle(self._render_cards)

    def _bind_mousewheel(self) -> None:
        self.grid_canvas.bind_all(
            "<MouseWheel>",
            lambda event: self.grid_canvas.yview_scroll(
                int(-1 * (event.delta / 120)), "units"
            ),
        )

    def _watchdog_tick(self) -> None:
        if self._closing:
            return
        try:
            self.engine.watchdog()
        except Exception:
            logging.exception("Watchdog check failed")
        self.after(2000, self._watchdog_tick)

    def _ui_tick(self) -> None:
        if self._closing:
            return
        self._aux_threads = {
            thread for thread in self._aux_threads if thread.is_alive()
        }
        self._consume_account_monitor()
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                message, level = payload
                self._append_log(message, level)
            elif kind == "scan_complete":
                devices, error = payload
                self._finish_discovery(devices, error)
            elif kind == "pool_status":
                self._pool_check_busy = False
                self.btn_pool.configure(state="normal")
                success, info, error = payload
                if success:
                    self.pool_status_label.configure(
                        text=f"Online: {info[2]} ({info[0]}:{info[1]})",
                        fg=PALETTE["green"],
                    )
                else:
                    self.pool_status_label.configure(
                        text=f"Offline: {error}",
                        fg=PALETTE["red"],
                    )
                    self._append_log(f"Pool status failed: {error}", "bad")

        devices = self.engine.device_list()
        snapshots = [device.snapshot() for device in devices]
        if hasattr(self, "worker_table") and self.worker_table.winfo_exists():
            existing_rows = set(self.worker_table.get_children())
            wanted_rows = {item["port"].casefold() for item in snapshots}
            for row_id in existing_rows - wanted_rows:
                self.worker_table.delete(row_id)
            for index, item in enumerate(snapshots, start=1):
                row_id = item["port"].casefold()
                ping = (
                    "—"
                    if item["ping_ms"] is None
                    else f"{item['ping_ms']:.0f} ms"
                )
                diff = "—" if item["diff"] is None else str(item["diff"])
                duco_id = str(item["duco_id"])
                if duco_id.startswith("DUCOID"):
                    duco_id = duco_id[6:]
                phase = str(item["phase"]).split(" · ", 1)[0][:24]
                status = str(item["status"])
                row_tag = (
                    "active"
                    if status == "RUNNING"
                    else "disconnected"
                    if status == "DISCONNECTED"
                    else "reconnecting"
                    if status in ("RECONNECTING", "CONNECTING", "POOL ERROR")
                    else "idle"
                )
                row_values = (
                    index,
                    f"{APP_NAME} {APP_VERSION}",
                    item["port"],
                    item["accepted"],
                    item["rejected"],
                    format_rate(item["hashrate"]),
                    diff,
                    ping,
                    item["node"],
                    item["board_type"],
                    ALGORITHM,
                    item["status"],
                    phase,
                    duco_id,
                )
                if self.worker_table.exists(row_id):
                    self.worker_table.item(
                        row_id, values=row_values, tags=(row_tag,)
                    )
                else:
                    self.worker_table.insert(
                        "", "end", iid=row_id, values=row_values, tags=(row_tag,)
                    )
        for device in devices:
            card = self._cards.get(device.port.casefold())
            if card and card.winfo_exists():
                card.refresh()
        total_rate = sum(item["hashrate"] for item in snapshots)
        accepted = sum(item["accepted"] for item in snapshots)
        rejected = sum(item["rejected"] for item in snapshots)
        online = sum(1 for item in snapshots if item["status"] == "RUNNING")
        self.stat_devices.configure(text=f"{online} / {len(snapshots)}")
        self.stat_rate.configure(text=format_rate(total_rate))
        self.stat_shares.configure(text=f"{accepted} / {rejected}")
        uptime = "00:00:00"
        if self.engine.start_time is not None:
            elapsed = max(0, int(time.monotonic() - self.engine.start_time))
            hours, rem = divmod(elapsed, 3600)
            minutes, seconds = divmod(rem, 60)
            uptime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        self.stat_uptime.configure(text=uptime)
        now = time.monotonic()
        if now - self._last_history_at >= 2.0:
            self._history.append(total_rate)
            self._history = self._history[-180:]
            self._last_history_at = now
        if self._telemetry_window is not None and self._telemetry_window.winfo_exists():
            self._telemetry_window.refresh(
                self._history,
                self._account_telemetry,
                list(self._wallet_samples),
            )
        if self.engine.running:
            self.footer_state.configure(text=f"MINING • {online} ONLINE", fg=PALETTE["green"])
        elif not self._closing:
            self.footer_state.configure(text="READY", fg=PALETTE["green"])
        self.after(500, self._ui_tick)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.footer_state.configure(text="SHUTTING DOWN…", fg=PALETTE["amber"])
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.btn_autoconfig.configure(state="disabled")
        self.btn_refresh.configure(state="disabled")
        self.btn_pool.configure(state="disabled")
        self._scan_cancel.set()
        self.engine.request_close()
        self.after(100, self._finish_close_when_idle)

    def _finish_close_when_idle(self) -> None:
        active = self.engine.live_threads()
        if self._scan_thread and self._scan_thread.is_alive():
            active.append(self._scan_thread)
        self._aux_threads = {thread for thread in self._aux_threads if thread.is_alive()}
        active.extend(self._aux_threads)
        if active:
            self.after(100, self._finish_close_when_idle)
            return
        try:
            self.destroy()
        except tk.TclError:
            logging.exception("Tk window close failed")


def run_startup_autoconfig(
    cfg: dict[str, Any]
) -> tuple[list[MinerDevice], list[str]]:
    """Run the mandatory port scan before constructing the visible GUI."""
    if list_ports is None:
        raise RuntimeError(
            "pyserial is not installed. Install it with: py -m pip install pyserial"
        ) from SERIAL_IMPORT_ERROR
    logging.info("Startup Auto Config began")
    startup_messages: list[str] = []
    devices = scan_avr_devices(
        int(cfg.get("baudrate", BAUDRATE)),
        log_callback=startup_messages.append,
    )
    logging.info("Startup Auto Config finished with %d verified device(s)", len(devices))
    return devices, startup_messages


def main() -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 2
    try:
        configure_logging()
    except OSError as exc:
        print(f"Could not initialize the log directory: {exc}", file=sys.stderr)
        return 2
    cfg = load_config()
    try:
        devices, startup_messages = run_startup_autoconfig(cfg)
    except Exception as exc:
        logging.exception("Startup Auto Config failed")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Snelli-1 Miner startup",
                f"Could not enumerate or inspect serial ports:\n\n{exc}",
                parent=root,
            )
            root.destroy()
        except tk.TclError:
            print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    try:
        app = MinerGUI(cfg, devices, startup_messages)
        app.mainloop()
    except Exception:
        logging.exception("Fatal GUI startup or shutdown exception")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
