# BMS-arduino-project

Arduino-based “mini BMS” monitor that measures **cell voltage**, **current**, and **temperature/humidity**, estimates **State of Charge (SOC)**, detects basic **fault conditions**, and (optionally) streams data over Serial to a **Python Dash** real-time dashboard.

> Note: This project is a *monitor/protection logic demo* (threshold alarms + display + telemetry). It is **not** a full-featured lithium BMS with balancing, certified protection circuitry, or safety-rated hardware.

---

## Project photos

### Hardware / build
![Project photo](/projPHOTO.jpg)

![Project photo 2](/Projphoto2.jpg)

### Dashboard / UI
![Dashboard screenshot](/image.png)

---

## Features

### Arduino firmware (`BMScontrol.ino`)
- Measures:
  - **Voltage** via analog input
  - **Current** using **ACS712-05B**
  - **Temperature/Humidity** via **DHT11**
- Displays data on:
  - **16x2 I2C LCD** (LiquidCrystal_I2C)
  - **128x64 OLED SSD1306** (Adafruit_SSD1306)
- SOC estimation (simple linear mapping):
  - `0.40 V -> 0%`
  - `1.00 V -> 100%`
- Fault detection (status string reported to LCD/OLED + Serial):
  - `UNDERVOLT`
  - `OVERVOLT`
  - `OVER D` (over-discharge current)
  - `OVER C` (over-charge current)
  - `SHORT`

### Python dashboard (`bms_dashboard.py`)
- Live dashboard built with **Dash + Plotly**
- Reads Arduino Serial output and shows:
  - Gauges (SOC, Voltage, Current, Temp)
  - Time-series plots
  - Fault log
- Can run in **demo mode** (no Arduino required)
- Logs data to `bms_log.csv`

---

## Repo contents

- `BMScontrol.ino` — Arduino monitoring firmware
- `bms_dashboard.py` — PC dashboard + CSV logger
- `projPHOTO.jpg`, `Projphoto2.jpg`, `image.png` — project images

---

## Hardware (typical)

- Arduino board (UNO/Nano/Mega, etc.)
- ACS712 current sensor (configured as **ACS712-05B** in code)
- DHT11 sensor
- I2C 16x2 LCD (commonly address `0x27`)
- SSD1306 128x64 OLED (commonly `0x3C` or `0x3D`)
- (Optional/Recommended) voltage divider if measuring > 5V on analog pin

---

## Wiring / Pins (as in code)

Arduino pins used in `BMScontrol.ino`:
- `A0` → `VOLTAGE_PIN`
- `A1` → `CURRENT_PIN` (ACS712 output)
- `D2` → `DHT_PIN`

Displays:
- LCD: I2C (`0x27` in code; some modules use `0x3F`)
- OLED: I2C (`0x3C` or `0x3D`)

---

## Arduino setup

1. Open `BMScontrol.ino` in Arduino IDE
2. Install libraries (via Library Manager):
   - `LiquidCrystal_I2C`
   - `Adafruit GFX Library`
   - `Adafruit SSD1306`
   - `DHT sensor library` (Adafruit)
3. Select your board + port
4. Upload

### ACS712 calibration
On boot the sketch auto-calibrates current sensor **OFFSET**:
- Keep the circuit at **zero current** during startup for best results.

---

## Serial output format

The Python dashboard expects lines like:

```
V:0.700 I:+0.123 SOC:50.0% T:25.0 H:60 Status:NORMAL
```

This format is produced by the Arduino sketch.

---

## Python dashboard setup

### Requirements
Install dependencies:

```bash
pip install dash plotly pyserial pandas dash-bootstrap-components
```

### Run with real Arduino
1. Upload the Arduino sketch
2. Open a terminal:

```bash
python bms_dashboard.py --port COM3 --baud 9600
```

Linux example:

```bash
python bms_dashboard.py --port /dev/ttyUSB0 --baud 9600
```

Then open:

```
http://127.0.0.1:8050/
```

### Run demo mode (no hardware)
```bash
python bms_dashboard.py --demo
```

---

## Configuration / thresholds

Firmware thresholds (also mirrored in `bms_dashboard.py`):

- `UNDERVOLT_CUTOFF = 0.40`
- `OVERVOLT_CUTOFF  = 1.00`
- `MAX_DISCHARGE_A  = 5.60`
- `MAX_CHARGE_A     = 2.80`
- `SHORT_CIRCUIT_A  = 10.0`

If you change them in Arduino code, update them in `bms_dashboard.py` too.

---

## Notes / safety

- **Do not** rely on this project as the only protection for real lithium packs.
- Use proper fusing, protection circuits, correct wiring, and safe charging practices.
- If measuring battery packs above 5V, **use a voltage divider** before the Arduino analog input.

---

## License
This is a begginer friendly work .Feel free to use it as you wish and best of luck.
