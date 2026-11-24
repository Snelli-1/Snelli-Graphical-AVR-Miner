# Snelli-Graphical-AVR-Miner
Single-file Windows GUI miner for physical Duino-Coin AVR boards, with automatic device detection and live mining telemetry.

# Snelli-1 Miner 4.5

A single-file Windows GUI miner for **physical Duino-Coin AVR boards**. The Python application manages real serial-connected Arduino hardware; the AVR board's installed firmware performs the mining work.

> This is a community project, not an official Duino-Coin release. It does not create virtual workers or emulate AVR hardware.

## At a glance

- Python 3.11 or newer; Windows 10/11.
- One runtime file: `snelli_1_miner_v4_5.py`.
- Requires `pyserial` and genuine Duino-Coin-compatible AVR firmware already installed on each board.
- Board targets: Arduino UNO, Arduino Nano, common Nano clones, and LGT8F328P-based Nano clones.
- Uses Duino-Coin's DUCO-S1 AVR pool protocol and firmware-calibrated starting difficulty.
- No external `engine.py`, helper process, simulator, virtual serial pipe, software firmware, or dummy worker mode.

## How the project changed from virtual-only to physical AVR

The supplied V4.4 files show why a clean rewrite was needed:

1. `avr_miner_engine.py` described itself as an AVR emulator/host engine. It implemented `VirtualSerialPipe`, an emulated Arduino firmware loop, synthetic hashrate, and configurable virtual worker creation.
2. `snelli_1_miner_v4_4.py` was an emulator GUI. It tried to import that external engine, then defined another virtual serial/worker engine as a fallback if the module was missing. It also exposed emulator mode and add/remove virtual-worker controls.
3. The V4.5 rewrite kept the monitoring-first GUI direction, but replaced the backend with a physical-device engine. A worker now exists only after an Arduino USB serial candidate answers the official AVR hash test. Every mining job is sent over that worker's real COM port to its verified firmware.
4. The old simulator-specific difficulty ladder (including labels such as `AVR_LOW` and `AVR_MID`) was removed. V4.5 measures the connected firmware and sends the official AVR starting class with each pool job request.

The V4.4 source included a comment describing its account dashboard as “Siunus-style.” The V4.5 account cards and mined-balance graph continue that monitoring goal, while using the Duino-Coin REST API directly; the Siunus website or code is not bundled as a runtime dependency.

| V4.4 virtual design | V4.5 physical design |
| --- | --- |
| Configured worker count (for example, 12) | One worker per verified physical COM port |
| In-memory `VirtualSerialPipe` | pySerial connection to an Arduino USB serial port |
| Python-emulated `.ino` loop | Firmware already installed on the board |
| Synthetic board rate and simulated shares | Measured AVR test rate and returned AVR results |
| Embedded simulator fallback | One self-contained Python application |
| Virtual worker add/remove controls | Auto Config / Refresh Devices discovery |

The legacy `Frissit_LGT8F328P_portok.bat` file is not invoked by V4.5. This application does not install drivers, select/upload firmware, or modify a board's bootloader.

## What happens when it runs

### 1. Startup Auto Config

Before showing the GUI, the program enumerates COM ports and logs their descriptions and USB identifiers. Bluetooth serial ports are skipped. Ports that do not look like Arduino/known USB-serial candidates are not opened. Already managed ports are skipped during a refresh.

Candidate matching uses USB description text and a short list of common Arduino, CH340/CH341, CH9102, FTDI, and CP210x identifiers. A matching USB bridge is only a candidate; it is not proof that an Arduino is attached.

The program opens each new candidate at the configured baud rate, waits for the board to boot, and sends the Duino-Coin AVR hash-test vector: previous hash `ba29a15896fd2d792d5c4b60668bf2b9feebc51d`, expected hash `d0beba883d7e8cd119ea2b0e09b78f60f29e0968`, and test difficulty `10`. It accepts a device only when the response contains the expected nonce (`50`) and a valid `DUCOID` identity. Unverified candidates are closed and are not added to the worker list.

**USB reset note:** Opening an Arduino serial port can toggle DTR and reset the board. Startup discovery and refreshing can therefore restart a candidate board. Non-candidate and Bluetooth ports are left unopened; a candidate USB bridge may receive the test request even if the device attached to it is not AVR firmware.

### 2. AVR calibration and dynamic starting difficulty

V4.5 uses the AVR test result to calculate test H/s, with the same five-decimal timing rounding used by the official AVR miner. It then chooses the pool request's starting class from the official thresholds:

| Measured test hashrate | Starting class |
| ---: | --- |
| `> 1000 H/s` | `DUE` |
| `> 550 H/s` to `1000 H/s` | `ARM` |
| `> 380 H/s` to `550 H/s` | `MEGA` |
| `<= 380 H/s` | `AVR` |

The class is the starting hint sent to the pool; it is **not** the per-job difficulty. The pool returns the actual job difficulty (for example, `10`, `67`, or `73`), and V4.5 shows that value separately in the card, table, telemetry, and live log. The application does not invent a difficulty from a fake hashrate.

### 3. Physical mining and pool feedback

For each verified board, a worker thread:

1. Opens and re-verifies the physical AVR firmware.
2. Connects to the configured pool or the pool returned by Duino-Coin's pool picker.
3. Requests work using `JOB,<username>,<starting-class>,<mining-key>`.
4. Sends the returned previous hash, target hash, and pool difficulty to the AVR firmware over serial.
5. Reads the binary nonce/time/DUCOID response from the board and submits that share to the pool.
6. Updates the real device's hashrate, accepted/rejected counts, ping, difficulty, node, status, and activity phase.

The GUI includes Start, Stop, Auto Config, Refresh Devices, Telemetry, Save Settings, and Pool Status controls. The worker cards and terminal-style table represent verified physical devices only. The table shows miner/version, COM, accepted, rejected, hashrate, pool diff, ping, pool node, board, algorithm, status, phase, and DUCOID.

Account telemetry displays balance, account miners/hashrate/acceptance, DUCO price, and an estimated 24-hour balance increase. The estimate is calculated from balance samples observed by this application and becomes available only after enough time has elapsed; it is an estimate, not a payout promise. Account telemetry refreshes about every 30 seconds.

## Threading and recovery

- Tk widgets are updated by the GUI event loop. Worker and account-monitor threads send updates through queues instead of directly changing widgets.
- Each physical miner has its own state lock and serial/pool resources.
- Serial and network operations use finite timeouts.
- A disconnected board or lost pool connection enters a reconnect loop. Retry delay grows from 2 seconds up to 30 seconds and resets after a successful pool connection.
- A watchdog requests a clean connection reset if a running worker has made no progress for 120 seconds.
- On application close, workers are asked to stop and the GUI waits for owned threads to finish before destroying the window.

These are defensive recovery measures; no miner software can promise zero crashes or uninterrupted network/hardware service.

## Requirements and installation

1. Install Python 3.11+ for Windows and make sure the `py` launcher is available.
2. Connect an Arduino-compatible board with Duino-Coin AVR firmware already flashed. Install its USB-serial driver first if Windows does not create a COM port.
3. Open a terminal in the project folder and install pySerial:

   ```powershell
   py -3.11 -m pip install pyserial
   ```

4. Run the GUI:

   ```powershell
   py -3.11 .\snelli_1_miner_v4_5.py
   ```

5. Startup Auto Config runs before the main window appears. Enter the Duino-Coin username and any required mining key in Settings, save the settings, then press Start.

The GUI uses Python's standard-library Tkinter/ttk toolkit; no separate GUI package is required with a normal Windows Python installation.

## Settings and logs

Settings and logs are stored under the current Windows user's local application-data directory:

```text
%LOCALAPPDATA%\Snelli-1 Miner\settings.json
%LOCALAPPDATA%\Snelli-1 Miner\miner.log
```

The JSON settings include the configured mining key in readable text. Protect the Windows account and do not publish or attach this file to bug reports. The rotating log is intended for troubleshooting; review it for usernames, COM-port details, or other information before sharing it.

Board selection in a device card is a label/override for display and saved configuration. It does not upload or change firmware.

## Known limits

- USB metadata is not a definitive board-identification protocol. Some clones appear as a generic Nano/unknown clone, and unusual USB bridge IDs may need to be added to the candidate list in the source.
- A candidate port must answer the supported Duino-Coin AVR test protocol. This is not a generic Arduino serial monitor.
- Only one program can own a COM port at a time. Close Arduino IDE Serial Monitor or any other program using the same port before mining.
- A board must already have working AVR miner firmware. This Python application does not flash it.
- The legacy LGT8F328P batch script is outside the V4.5 runtime and was not available in the reviewed project folder. Its behavior is not relied on or described here.
- The listed board families are supported targets, but USB bridge and firmware combinations still need real-device validation. No board matrix was hardware-tested as part of this README preparation.

## Project files

```text
snelli_1_miner_v4_5.py   # Complete GUI, device discovery, AVR/pool engine, telemetry
README.md                # This document
```

There is no `avr_miner_engine.py` dependency and no second backend process.

## Sources and acknowledgements

- [Duino-Coin official AVR miner source](https://github.com/duino-coin/duino-coin/blob/master/AVR_Miner.py) — reference for AVR serial test behavior, binary result format, pool job request, and the `AVR` / `MEGA` / `ARM` / `DUE` startup-tier thresholds.
- [Duino-Coin Arduino firmware source](https://github.com/duino-coin/duino-coin/tree/master/Arduino_Code) — firmware target expected on the physical board.
- [Duino-Coin REST API source and endpoint documentation](https://github.com/duino-coin/duco-rest-api) — user/miner telemetry and `/statistics` endpoint reference.
- [Duino-Coin pool picker](https://server.duinocoin.com/getPool) — automatic pool host/port/node selection endpoint used by the application.
- [Siunus DUCO Monitor](https://github.com/siunus/duco-monitor) ([live dashboard](https://siunus.github.io/duco-monitor/v2023/)) — dashboard reference noted in the supplied V4.4 GUI; it is not bundled or loaded by V4.5.
- [pySerial documentation](https://pyserial.readthedocs.io/en/latest/) and [port-enumeration tools](https://pyserial.readthedocs.io/en/latest/tools.html) — serial connection and COM-port discovery library.
- [Python 3.11 Tkinter documentation](https://docs.python.org/3.11/library/tkinter.html), [threading documentation](https://docs.python.org/3.11/library/threading.html), and [urllib.request documentation](https://docs.python.org/3.11/library/urllib.request.html) — standard-library GUI, worker threads, and HTTP requests.
- Migration inputs supplied for V4.4: `avr_miner_engine.py` and `snelli_1_miner_v4_4.py`. They are historical inputs, not V4.5 runtime dependencies.

Duino-Coin's repository states that most of its project is distributed under the [MIT License](https://github.com/duino-coin/duino-coin/blob/master/LICENSE), with possible file-specific exceptions. pySerial has its own license; see its [project documentation](https://pyserial.readthedocs.io/en/latest/).

**This project folder does not currently declare its own license.** Choose and add a repository-level `LICENSE` before publishing or redistributing the application; upstream licenses do not automatically select a license for Snelli-1 Miner.
