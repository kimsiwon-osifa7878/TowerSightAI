# LD2410C Baseline Comparator

LD2410C Engineering Mode UART data viewer and baseline comparison tool.

The app reads gate 0-8 moving and motionless energy, records `empty_car`, `person_still`, and `person_moving` samples, saves CSV files, and recommends gate thresholds from the empty-car baseline.

## Install

```powershell
cd "D:\MyWork\1. MyWorkSpace\16. Python\2026\06.radar_detector\LD2410"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python.exe main.py
```

When using VS Code, select `.venv\Scripts\python.exe` with `Python: Select Interpreter`. The workspace setting also points new VS Code sessions to this interpreter.

Select one of the three input modes in the Connection panel:

- `Simulation`: generates test data locally.
- `UART`: reads the sensor directly at `256000`, 8N1.
- `TCP Server`: listens for a TCP client that sends the same raw binary LD2410 report frames used on UART.

For TCP Server mode, the default bind address is `0.0.0.0` and the default port is `2410`. Select `TCP Server`, set the bind address/port, and click `Connect / Listen`. The status changes when a client connects. If that client disconnects, the server remains active and waits for the next client.

The TCP stream must contain unmodified binary frames (starting with `F4 F3 F2 F1`), not an ASCII hex string. If a serial-to-TCP bridge is used, configure it as the TCP client and point it to this computer's LAN IP and port `2410`.

## Guided Recording Flow

Each record button now runs an assisted workflow:

1. Press `Record Baseline`, `Record Person Still`, or `Record Person Moving`.
2. A large modal opens with a 10-second preparation countdown.
3. Recording starts automatically.
4. The modal shows `Recording` and a 60-second countdown.
5. When the countdown ends, recording stops automatically.
6. The CSV is auto-saved to:

```text
LD2410/data/ld2410_auto_record_YYYYMMDD_HHMMSS.csv
```

After a step completes, the modal shows the next recommended step:

```text
Record Baseline -> Record Person Still -> Record Person Moving -> Analyze Now
```

The same auto-save CSV is reused for all steps in the session, so the final file contains all recorded profiles.

## Manual Buttons

- `Stop / Cancel Recording`: stops the current guided countdown or recording.
- `Clear Memory`: clears rows currently held in memory and resets the auto-save file for a new session.
- `Save CSV`: manually saves the current in-memory data to a selected path.
- `Load CSV`: loads a previous CSV and analyzes it.
- `Analyze / Update Baseline`: calculates baseline, separation score, recommended thresholds, and updates realtime comparison.

## Analysis

Baseline profile:

- Per profile and gate: `avg`, `max`, `std`, sample count.

Empty car vs person still:

- `avg_diff = person_still_avg - empty_car_avg`
- `separation_score = person_still_avg - empty_car_max`

Recommended threshold:

- Default interest gates: `3,4,5`
- Interest gates: `recommended_threshold = empty_car_max + margin`
- Non-interest gates: `100`
- Default margin: `5`

Realtime state:

- `CLEAR`: no meaningful threshold crossings in the recent 8-second window.
- `SUSPECT`: some threshold crossings.
- `DETECTED`: repeated threshold crossings in the recent 8-second window.

## Protocol

The parser is based on the official LD2410 Engineering Mode report frame:

- Report header: `F4 F3 F2 F1`
- Report tail: `F8 F7 F6 F5`
- Data type `0x01`: Engineering Mode data
- Payload marker: `AA ... 55 00`
- Basic target fields plus max moving gate, max motionless gate, moving gate energy, motionless gate energy, light, and OUT pin.

Parse failures are written to `raw_hex_errors.log` with raw hex and the failure reason.
