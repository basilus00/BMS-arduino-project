"""
=============================================================================
  BMS DASHBOARD — Samsung 28A Battery Monitor
  Python/Dash Real-Time Dashboard for Arduino BMS (ACS712-05B + DHT11)
=============================================================================
  COMPATIBLE WITH:
    - Arduino BMS sketch (Samsung28A / ACS712-05B / DHT11)
    - Serial output format:
        V:X.XXX I:+X.XXX SOC:X.X% T:X.X H:X Status:XXXX

  STANDARDS CONTEXT (Automotive / Embedded):
    - Fault classification mirrors AUTOSAR BSW BswM patterns
    - SOC mapping matches simple open-circuit voltage (OCV) method
    - Thresholds kept in sync with Arduino firmware constants
    - 1 Hz update rate aligned with DHT11 minimum sampling spec

  REQUIREMENTS:
    pip install dash plotly pyserial pandas

  USAGE:
    python bms_dashboard.py --port COM3 --baud 9600
    python bms_dashboard.py --port /dev/ttyUSB0
    python bms_dashboard.py --demo          (simulated data, no hardware needed)

  AUTHOR: Generated for Samsung 28A BMS Project
=============================================================================
"""

import argparse
import threading
import time
import re
import csv
import os
import math
import random
from collections import deque
from datetime import datetime

import serial
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash
from dash import dcc, html, Input, Output, State
import dash_bootstrap_components as dbc

# ─────────────────────────────────────────────────────────────────────────────
# 1.  FIRMWARE CONSTANTS  (keep in sync with Arduino sketch!)
# ─────────────────────────────────────────────────────────────────────────────
UNDERVOLT_CUTOFF  = 0.40   # V  – SOC = 0 %
OVERVOLT_CUTOFF   = 1.00   # V  – SOC = 100 %
MAX_DISCHARGE_A   = 5.60   # A  – discharge over-current threshold
MAX_CHARGE_A      = 2.80   # A  – charge over-current threshold
SHORT_CIRCUIT_A   = 10.0   # A  – short-circuit detection
ACS712_SENSITIVITY= 0.185  # V/A – ACS712-05B datasheet
VCC               = 5.0    # V  – Arduino supply

# Fault priority order (highest → lowest, ISO 26262 inspired)
FAULT_PRIORITY = {
    "SHORT"    : 5,
    "OVERVOLT" : 4,
    "UNDERVOLT": 3,
    "OVER D"   : 2,
    "OVER C"   : 1,
    "NORMAL"   : 0,
}

# ─────────────────────────────────────────────────────────────────────────────
# 2.  HISTORY BUFFERS   (circular / FIFO, thread-safe via lock)
# ─────────────────────────────────────────────────────────────────────────────
MAX_HISTORY  = 300          # samples – ~5 min @ 1 Hz
data_lock    = threading.Lock()

history = {
    "ts"        : deque(maxlen=MAX_HISTORY),
    "voltage"   : deque(maxlen=MAX_HISTORY),
    "current"   : deque(maxlen=MAX_HISTORY),
    "soc"       : deque(maxlen=MAX_HISTORY),
    "temp"      : deque(maxlen=MAX_HISTORY),
    "humidity"  : deque(maxlen=MAX_HISTORY),
    "status"    : deque(maxlen=MAX_HISTORY),
}

# Latest snapshot (written by serial thread, read by Dash callbacks)
latest = {
    "voltage" : 0.0,
    "current" : 0.0,
    "soc"     : 0.0,
    "temp"    : 0.0,
    "humidity": 0.0,
    "status"  : "NORMAL",
    "uptime"  : 0,
    "fault_log": [],        # list of (timestamp, fault_name)
    "connected": False,
}

# ─────────────────────────────────────────────────────────────────────────────
# 3.  SERIAL PARSER
# ─────────────────────────────────────────────────────────────────────────────
# Regex for the Arduino serial output:
#   V:0.123 I:+1.234 SOC:56.7% T:25.3 H:60 Status:NORMAL
LINE_REGEX = re.compile(
    r"V:([-+]?\d+\.\d+)"
    r"\s+I:([-+]?\d+\.\d+)"
    r"\s+SOC:([-+]?\d+\.\d+)%"
    r"\s+T:([-+]?\d+\.\d+)"
    r"\s+H:(\d+)"
    r"\s+Status:(\w[\w ]*)"
)

CSV_LOG_FILE = "bms_log.csv"
_csv_initialized = False

def _ensure_csv():
    global _csv_initialized
    if _csv_initialized:
        return
    write_header = not os.path.exists(CSV_LOG_FILE)
    with open(CSV_LOG_FILE, "a", newline="") as f:
        if write_header:
            writer = csv.writer(f)
            writer.writerow(
                ["timestamp", "voltage_V", "current_A", "soc_pct",
                 "temp_C", "humidity_pct", "status"]
            )
    _csv_initialized = True

def _log_csv(ts, v, i, soc, t, h, status):
    _ensure_csv()
    with open(CSV_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([ts, round(v, 4), round(i, 4),
                         round(soc, 2), round(t, 2), round(h, 1), status])

def parse_line(line: str) -> dict | None:
    """Parse one serial line. Returns dict or None on mismatch."""
    m = LINE_REGEX.search(line.strip())
    if not m:
        return None
    return {
        "voltage" : float(m.group(1)),
        "current" : float(m.group(2)),
        "soc"     : float(m.group(3)),
        "temp"    : float(m.group(4)),
        "humidity": float(m.group(5)),
        "status"  : m.group(6).strip().upper(),
    }

def ingest(sample: dict):
    """Push one parsed sample into history buffers and latest snapshot."""
    ts = datetime.now().strftime("%H:%M:%S")
    with data_lock:
        for key in ("voltage", "current", "soc", "temp", "humidity", "status"):
            history[key].append(sample[key])
        history["ts"].append(ts)

        latest.update(sample)
        latest["connected"] = True
        latest["uptime"] = len(history["ts"])

        # Fault log entry
        if sample["status"] != "NORMAL":
            latest["fault_log"].append((ts, sample["status"]))
            if len(latest["fault_log"]) > 50:
                latest["fault_log"].pop(0)

    # Async CSV logging (non-blocking for the serial thread)
    _log_csv(datetime.now().isoformat(timespec="seconds"),
             sample["voltage"], sample["current"], sample["soc"],
             sample["temp"], sample["humidity"], sample["status"])

# ─────────────────────────────────────────────────────────────────────────────
# 4.  SERIAL THREAD
# ─────────────────────────────────────────────────────────────────────────────
def serial_reader_thread(port: str, baud: int = 9600):
    """
    Continuously reads the serial port and feeds parsed data into buffers.
    Reconnects automatically on disconnect (robust for bench use).
    """
    print(f"[Serial] Connecting to {port} @ {baud} baud …")
    while True:
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                print(f"[Serial] Connected to {port}")
                while True:
                    raw = ser.readline().decode("utf-8", errors="replace")
                    sample = parse_line(raw)
                    if sample:
                        ingest(sample)
        except serial.SerialException as exc:
            print(f"[Serial] Disconnected: {exc} — retrying in 3 s …")
            with data_lock:
                latest["connected"] = False
            time.sleep(3)
        except Exception as exc:
            print(f"[Serial] Unexpected error: {exc}")
            time.sleep(3)

# ─────────────────────────────────────────────────────────────────────────────
# 5.  DEMO / SIMULATION THREAD  (no hardware)
# ─────────────────────────────────────────────────────────────────────────────
def demo_thread():
    """
    Generates synthetic BMS data that exercises all fault conditions.
    Mirrors the exact same signal path as real serial data.
    """
    print("[Demo] Simulation mode active — no serial port required.")
    t = 0
    while True:
        # Simulate a slow charge–discharge cycle
        cycle = math.sin(t * 0.02)           # –1 … +1
        v    = 0.70 + cycle * 0.25            # 0.45 … 0.95 V
        i    = cycle * 3.5                    # –3.5 … +3.5 A
        soc  = max(0, min(100, ((v - 0.4) / 0.6) * 100))
        temp = 28 + 4 * math.sin(t * 0.05) + random.gauss(0, 0.3)
        hum  = 55 + 5 * math.sin(t * 0.03)

        # Inject occasional fault spikes
        status = "NORMAL"
        if t % 120 == 60:
            i = 11.0; status = "SHORT"
        elif t % 120 == 30:
            v = 0.38; status = "UNDERVOLT"
        elif t % 120 == 90:
            v = 1.02; status = "OVERVOLT"

        ingest({
            "voltage" : round(v, 3),
            "current" : round(i, 3),
            "soc"     : round(soc, 1),
            "temp"    : round(temp, 1),
            "humidity": round(hum, 1),
            "status"  : status,
        })
        t += 1
        time.sleep(1)

# ─────────────────────────────────────────────────────────────────────────────
# 6.  PLOTLY FIGURE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────
DARK_BG      = "#0d1117"
PANEL_BG     = "#161b22"
BORDER_COLOR = "#30363d"
GREEN        = "#39d353"
YELLOW       = "#e3b341"
RED          = "#f85149"
BLUE         = "#58a6ff"
CYAN         = "#79c0ff"
GRAY         = "#8b949e"
WHITE        = "#e6edf3"

GAUGE_FONT   = dict(family="'Courier New', monospace", color=WHITE)

# ── 6a. SOC Gauge ──────────────────────────────────────────────────────────
def soc_gauge(soc: float) -> go.Figure:
    color = GREEN if soc > 40 else (YELLOW if soc > 15 else RED)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=soc,
        number={"suffix": "%", "font": {"size": 36, "color": color}},
        title={"text": "STATE OF CHARGE", "font": {"size": 13, "color": GRAY}},
        gauge={
            "axis"    : {"range": [0, 100], "tickcolor": GRAY,
                          "tickfont": {"size": 10, "color": GRAY}},
            "bar"     : {"color": color, "thickness": 0.28},
            "bgcolor" : PANEL_BG,
            "borderwidth": 1,
            "bordercolor": BORDER_COLOR,
            "steps"   : [
                {"range": [0, 15],  "color": "#3d1010"},
                {"range": [15, 40], "color": "#3d2e10"},
                {"range": [40, 100],"color": "#0d1f0d"},
            ],
            "threshold": {
                "line" : {"color": RED, "width": 2},
                "thickness": 0.75,
                "value": 15,
            },
        },
    ))
    fig.update_layout(
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
        font=GAUGE_FONT,
        margin=dict(t=60, b=10, l=20, r=20),
        height=220,
    )
    return fig

# ── 6b. Voltage Gauge ──────────────────────────────────────────────────────
def voltage_gauge(v: float) -> go.Figure:
    color = (RED if v < UNDERVOLT_CUTOFF or v > OVERVOLT_CUTOFF else
             YELLOW if v < 0.50 or v > 0.95 else GREEN)
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=v,
        number={"suffix": " V", "font": {"size": 28, "color": color},
                "valueformat": ".3f"},
        delta={"reference": 0.70, "valueformat": ".3f",
               "increasing": {"color": GREEN},
               "decreasing": {"color": RED}},
        title={"text": "CELL VOLTAGE", "font": {"size": 13, "color": GRAY}},
        gauge={
            "axis" : {"range": [0, 1.2], "tickformat": ".1f",
                      "tickcolor": GRAY, "tickfont": {"size": 9, "color": GRAY}},
            "bar"  : {"color": color, "thickness": 0.28},
            "bgcolor": PANEL_BG,
            "borderwidth": 1, "bordercolor": BORDER_COLOR,
            "steps": [
                {"range": [0, UNDERVOLT_CUTOFF],   "color": "#3d1010"},
                {"range": [UNDERVOLT_CUTOFF, 0.95], "color": "#0d1f0d"},
                {"range": [0.95, OVERVOLT_CUTOFF],  "color": "#3d2e10"},
                {"range": [OVERVOLT_CUTOFF, 1.2],   "color": "#3d1010"},
            ],
            "threshold": {
                "line": {"color": YELLOW, "width": 2},
                "thickness": 0.75, "value": OVERVOLT_CUTOFF,
            },
        },
    ))
    fig.update_layout(
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
        font=GAUGE_FONT,
        margin=dict(t=60, b=10, l=20, r=20),
        height=220,
    )
    return fig

# ── 6c. Current Gauge (bidirectional: –MAX_CHARGE … +MAX_DISCHARGE) ────────
def current_gauge(i: float) -> go.Figure:
    color = (RED if abs(i) >= SHORT_CIRCUIT_A else
             RED if i > MAX_DISCHARGE_A else
             RED if i < -MAX_CHARGE_A else
             CYAN if i < -0.1 else YELLOW if i > 0 else GRAY)
    label = "CHG" if i < -0.1 else ("DSC" if i > 0.1 else "IDLE")
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=i,
        number={"suffix": " A", "font": {"size": 28, "color": color},
                "valueformat": "+.3f"},
        title={"text": f"CURRENT ({label})", "font": {"size": 13, "color": GRAY}},
        gauge={
            "axis" : {"range": [-SHORT_CIRCUIT_A, SHORT_CIRCUIT_A],
                      "tickcolor": GRAY,
                      "tickfont": {"size": 9, "color": GRAY}},
            "bar"  : {"color": color, "thickness": 0.28},
            "bgcolor": PANEL_BG,
            "borderwidth": 1, "bordercolor": BORDER_COLOR,
            "steps": [
                {"range": [-SHORT_CIRCUIT_A, -MAX_CHARGE_A],  "color": "#3d1010"},
                {"range": [-MAX_CHARGE_A, 0],                  "color": "#0d1a2a"},
                {"range": [0, MAX_DISCHARGE_A],                "color": "#0d1f0d"},
                {"range": [MAX_DISCHARGE_A, SHORT_CIRCUIT_A],  "color": "#3d1010"},
            ],
        },
    ))
    fig.update_layout(
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
        font=GAUGE_FONT,
        margin=dict(t=60, b=10, l=20, r=20),
        height=220,
    )
    return fig

# ── 6d. Temperature Gauge ───────────────────────────────────────────────────
def temp_gauge(t_val: float) -> go.Figure:
    color = RED if t_val > 55 else YELLOW if t_val > 45 else GREEN
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=t_val,
        number={"suffix": " °C", "font": {"size": 28, "color": color},
                "valueformat": ".1f"},
        title={"text": "CELL TEMP (DHT11)", "font": {"size": 13, "color": GRAY}},
        gauge={
            "axis" : {"range": [-10, 80], "tickcolor": GRAY,
                      "tickfont": {"size": 9, "color": GRAY}},
            "bar"  : {"color": color, "thickness": 0.28},
            "bgcolor": PANEL_BG,
            "borderwidth": 1, "bordercolor": BORDER_COLOR,
            "steps": [
                {"range": [-10, 0],  "color": "#10203d"},
                {"range": [0, 45],   "color": "#0d1f0d"},
                {"range": [45, 55],  "color": "#3d2e10"},
                {"range": [55, 80],  "color": "#3d1010"},
            ],
            "threshold": {
                "line": {"color": RED, "width": 2},
                "thickness": 0.75, "value": 55,
            },
        },
    ))
    fig.update_layout(
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
        font=GAUGE_FONT,
        margin=dict(t=60, b=10, l=20, r=20),
        height=220,
    )
    return fig

# ── 6e. Time-Series Chart (V, I, SOC) ──────────────────────────────────────
def time_series_chart() -> go.Figure:
    with data_lock:
        ts   = list(history["ts"])
        v    = list(history["voltage"])
        i    = list(history["current"])
        soc  = list(history["soc"])
        temp = list(history["temp"])
        stat = list(history["status"])

    if not ts:
        fig = go.Figure()
        fig.update_layout(paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG)
        return fig

    # Mark fault points
    fault_ts  = [ts[k]  for k, s in enumerate(stat) if s != "NORMAL"]
    fault_v   = [v[k]   for k, s in enumerate(stat) if s != "NORMAL"]
    fault_lbl = [stat[k] for k, s in enumerate(stat) if s != "NORMAL"]

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.38, 0.30, 0.32],
        vertical_spacing=0.04,
    )

    # Row 1 – Voltage + fault markers
    fig.add_trace(go.Scatter(
        x=ts, y=v, name="Voltage",
        line=dict(color=BLUE, width=1.8),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.07)",
    ), row=1, col=1)
    if fault_ts:
        fig.add_trace(go.Scatter(
            x=fault_ts, y=fault_v, mode="markers",
            marker=dict(symbol="x", size=9, color=RED),
            name="FAULT", text=fault_lbl,
            hovertemplate="%{text}<extra></extra>",
        ), row=1, col=1)
    fig.add_hline(y=UNDERVOLT_CUTOFF, line_dash="dot",
                  line_color=RED, opacity=0.4, row=1, col=1)
    fig.add_hline(y=OVERVOLT_CUTOFF,  line_dash="dot",
                  line_color=RED, opacity=0.4, row=1, col=1)

    # Row 2 – Current
    fig.add_trace(go.Scatter(
        x=ts, y=i, name="Current",
        line=dict(color=CYAN, width=1.8),
        fill="tozeroy", fillcolor="rgba(121,192,255,0.07)",
    ), row=2, col=1)
    fig.add_hline(y=MAX_DISCHARGE_A,  line_dash="dot",
                  line_color=YELLOW, opacity=0.4, row=2, col=1)
    fig.add_hline(y=-MAX_CHARGE_A,    line_dash="dot",
                  line_color=CYAN, opacity=0.4, row=2, col=1)

    # Row 3 – SOC + Temperature overlay
    fig.add_trace(go.Scatter(
        x=ts, y=soc, name="SOC %",
        line=dict(color=GREEN, width=1.8),
        fill="tozeroy", fillcolor="rgba(57,211,83,0.07)",
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=ts, y=temp, name="Temp °C",
        line=dict(color=YELLOW, width=1.4, dash="dot"),
        yaxis="y4",
    ), row=3, col=1)

    fig.update_layout(
        paper_bgcolor=DARK_BG,
        plot_bgcolor=DARK_BG,
        font=dict(family="'Courier New', monospace", size=10, color=GRAY),
        legend=dict(bgcolor=PANEL_BG, bordercolor=BORDER_COLOR,
                    borderwidth=1, font=dict(size=9)),
        margin=dict(t=20, b=30, l=55, r=45),
        height=380,
        hovermode="x unified",
        hoverlabel=dict(bgcolor=PANEL_BG, bordercolor=BORDER_COLOR,
                        font_color=WHITE),
    )

    # Y-axis labels
    fig.update_yaxes(
        gridcolor=BORDER_COLOR, zerolinecolor=BORDER_COLOR,
        tickfont=dict(size=9, color=GRAY),
    )
    fig.update_yaxes(title_text="V (Volt)",    row=1, col=1,
                     range=[0, 1.3])
    fig.update_yaxes(title_text="I (Amp)",     row=2, col=1,
                     range=[-SHORT_CIRCUIT_A - 1, SHORT_CIRCUIT_A + 1])
    fig.update_yaxes(title_text="SOC (%)",     row=3, col=1,
                     range=[-5, 105])
    fig.update_xaxes(
        gridcolor=BORDER_COLOR,
        tickfont=dict(size=8, color=GRAY),
        tickangle=-35,
        showticklabels=True, row=3, col=1,
    )
    fig.update_xaxes(showticklabels=False, row=1, col=1)
    fig.update_xaxes(showticklabels=False, row=2, col=1)

    return fig

# ─────────────────────────────────────────────────────────────────────────────
# 7.  DASH LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.DARKLY],
    title="Samsung 28A BMS Monitor",
    update_title=None,
)

_STATUS_COLORS = {
    "NORMAL"   : GREEN,
    "UNDERVOLT": YELLOW,
    "OVERVOLT" : YELLOW,
    "OVER D"   : YELLOW,
    "OVER C"   : CYAN,
    "SHORT"    : RED,
}

# ── Header ──────────────────────────────────────────────────────────────────
header = html.Div(
    style={
        "background": PANEL_BG,
        "borderBottom": f"1px solid {BORDER_COLOR}",
        "padding": "14px 24px",
        "display": "flex",
        "alignItems": "center",
        "justifyContent": "space-between",
    },
    children=[
        html.Div([
            html.Span("⚡ ", style={"fontSize": "22px"}),
            html.Span(
                "SAMSUNG 28A  BMS DASHBOARD",
                style={
                    "fontFamily": "'Courier New', monospace",
                    "fontSize": "18px",
                    "fontWeight": "bold",
                    "letterSpacing": "3px",
                    "color": WHITE,
                },
            ),
            html.Span(
                "  ACS712-05B · DHT11 · Arduino",
                style={
                    "fontFamily": "'Courier New', monospace",
                    "fontSize": "11px",
                    "color": GRAY,
                    "marginLeft": "12px",
                },
            ),
        ]),
        html.Div(id="conn-badge"),
    ],
)

# ── Status Bar ───────────────────────────────────────────────────────────────
status_bar = dbc.Row(
    [
        dbc.Col(html.Div(id="status-indicator"), width=4),
        dbc.Col(html.Div(id="uptime-display"),   width=4),
        dbc.Col(html.Div(id="csv-status"),        width=4),
    ],
    style={
        "background": PANEL_BG,
        "borderBottom": f"1px solid {BORDER_COLOR}",
        "padding": "8px 24px",
        "margin": "0",
    },
    className="g-0",
)

# ── Gauge Row ────────────────────────────────────────────────────────────────
gauge_row = dbc.Row(
    [
        dbc.Col(dcc.Graph(id="gauge-soc",     config={"displayModeBar": False}), md=3),
        dbc.Col(dcc.Graph(id="gauge-voltage", config={"displayModeBar": False}), md=3),
        dbc.Col(dcc.Graph(id="gauge-current", config={"displayModeBar": False}), md=3),
        dbc.Col(dcc.Graph(id="gauge-temp",    config={"displayModeBar": False}), md=3),
    ],
    style={"padding": "6px 16px", "marginTop": "6px"},
    className="g-1",
)

# ── Humidity + Raw Values KPI Bar ─────────────────────────────────────────────
kpi_bar = dbc.Row(
    id="kpi-bar",
    style={
        "padding": "4px 24px",
        "fontFamily": "'Courier New', monospace",
        "fontSize": "13px",
        "color": GRAY,
    },
    className="g-0",
)

# ── Time-Series Chart ─────────────────────────────────────────────────────────
chart_row = dbc.Row(
    [
        dbc.Col(
            html.Div(
                dcc.Graph(id="chart-timeseries", config={"displayModeBar": True}),
                style={
                    "background": PANEL_BG,
                    "borderRadius": "4px",
                    "border": f"1px solid {BORDER_COLOR}",
                    "padding": "8px",
                },
            ),
            md=9,
        ),
        dbc.Col(
            html.Div(
                [
                    html.Div(
                        "FAULT LOG",
                        style={
                            "fontFamily": "'Courier New', monospace",
                            "fontSize": "11px",
                            "letterSpacing": "2px",
                            "color": RED,
                            "marginBottom": "8px",
                            "borderBottom": f"1px solid {BORDER_COLOR}",
                            "paddingBottom": "4px",
                        },
                    ),
                    html.Div(id="fault-log", style={"maxHeight": "330px", "overflowY": "auto"}),
                ],
                style={
                    "background": PANEL_BG,
                    "borderRadius": "4px",
                    "border": f"1px solid {BORDER_COLOR}",
                    "padding": "12px",
                    "height": "100%",
                },
            ),
            md=3,
        ),
    ],
    style={"padding": "6px 16px"},
    className="g-1",
)

# ── Threshold Reference Table ─────────────────────────────────────────────────
thresh_table = html.Div(
    [
        html.Div(
            "PROTECTION THRESHOLDS (firmware constants)",
            style={
                "fontFamily": "'Courier New', monospace",
                "fontSize": "11px",
                "letterSpacing": "2px",
                "color": GRAY,
                "marginBottom": "6px",
            },
        ),
        dbc.Table(
            [
                html.Thead(html.Tr([
                    html.Th("Parameter"), html.Th("Threshold"), html.Th("Fault Code"),
                ])),
                html.Tbody([
                    html.Tr([html.Td("Under Voltage"),    html.Td(f"< {UNDERVOLT_CUTOFF:.2f} V"), html.Td("UNDERVOLT", style={"color": YELLOW})]),
                    html.Tr([html.Td("Over Voltage"),     html.Td(f"> {OVERVOLT_CUTOFF:.2f} V"),  html.Td("OVERVOLT",  style={"color": YELLOW})]),
                    html.Tr([html.Td("Over Discharge"),   html.Td(f"> {MAX_DISCHARGE_A:.2f} A"),  html.Td("OVER D",    style={"color": YELLOW})]),
                    html.Tr([html.Td("Over Charge"),      html.Td(f"> {MAX_CHARGE_A:.2f} A"),     html.Td("OVER C",    style={"color": CYAN})]),
                    html.Tr([html.Td("Short Circuit"),    html.Td(f"> {SHORT_CIRCUIT_A:.1f} A"),  html.Td("SHORT",     style={"color": RED})]),
                    html.Tr([html.Td("ACS712 Sensitivity"), html.Td(f"{ACS712_SENSITIVITY*1000:.0f} mV/A"), html.Td("—")]),
                ]),
            ],
            bordered=True, hover=True, responsive=True, size="sm",
            style={
                "fontFamily": "'Courier New', monospace",
                "fontSize": "11px",
            },
        ),
    ],
    style={
        "padding": "6px 24px 16px",
        "maxWidth": "600px",
    },
)

# ── App Layout ────────────────────────────────────────────────────────────────
app.layout = html.Div(
    style={"backgroundColor": DARK_BG, "minHeight": "100vh", "color": WHITE},
    children=[
        header,
        status_bar,
        gauge_row,
        kpi_bar,
        chart_row,
        thresh_table,
        dcc.Interval(id="interval", interval=1000, n_intervals=0),  # 1 Hz
    ],
)

# ─────────────────────────────────────────────────────────────────────────────
# 8.  DASH CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────
@app.callback(
    Output("conn-badge",       "children"),
    Output("status-indicator", "children"),
    Output("uptime-display",   "children"),
    Output("csv-status",       "children"),
    Output("gauge-soc",        "figure"),
    Output("gauge-voltage",    "figure"),
    Output("gauge-current",    "figure"),
    Output("gauge-temp",       "figure"),
    Output("kpi-bar",          "children"),
    Output("chart-timeseries", "figure"),
    Output("fault-log",        "children"),
    Input("interval", "n_intervals"),
)
def refresh(_):
    with data_lock:
        snap = dict(latest)

    v    = snap["voltage"]
    i    = snap["current"]
    soc  = snap["soc"]
    t    = snap["temp"]
    h    = snap["humidity"]
    stat = snap["status"]
    conn = snap["connected"]
    up   = snap["uptime"]
    flog = list(snap["fault_log"])

    # Connection badge
    conn_badge = html.Span(
        "● CONNECTED" if conn else "● DISCONNECTED",
        style={
            "fontFamily": "'Courier New', monospace",
            "fontSize": "12px",
            "color": GREEN if conn else RED,
            "letterSpacing": "2px",
        },
    )

    # Status indicator
    s_color = _STATUS_COLORS.get(stat, WHITE)
    status_ind = html.Span(
        f"■ STATUS: {stat}",
        style={
            "fontFamily": "'Courier New', monospace",
            "fontSize": "13px",
            "color": s_color,
            "fontWeight": "bold",
            "letterSpacing": "2px",
        },
    )

    # Uptime
    uptime_disp = html.Span(
        f"SAMPLES: {up}  |  ~{up}s",
        style={
            "fontFamily": "'Courier New', monospace",
            "fontSize": "11px",
            "color": GRAY,
        },
    )

    # CSV status
    csv_stat = html.Span(
        f"LOG → {CSV_LOG_FILE}",
        style={
            "fontFamily": "'Courier New', monospace",
            "fontSize": "11px",
            "color": GRAY,
        },
    )

    # KPI bar
    kpi_items = [
        ("V", f"{v:.3f}", "V", BLUE),
        ("I", f"{i:+.3f}", "A", CYAN),
        ("SOC", f"{soc:.1f}", "%", GREEN),
        ("T", f"{t:.1f}", "°C", YELLOW),
        ("H", f"{h:.0f}", "%", GRAY),
    ]
    kpi_children = [
        dbc.Col(
            html.Span([
                html.Span(f"{lbl}: ", style={"color": GRAY}),
                html.Span(val, style={"color": color, "fontWeight": "bold"}),
                html.Span(unit, style={"color": GRAY}),
            ]),
            style={"textAlign": "center", "padding": "4px 0"},
        )
        for lbl, val, unit, color in kpi_items
    ]

    # Fault log
    fault_entries = []
    for ts_f, fault in reversed(flog[-30:]):
        fc = _STATUS_COLORS.get(fault, WHITE)
        fault_entries.append(html.Div(
            [
                html.Span(ts_f + "  ", style={"color": GRAY, "fontSize": "10px"}),
                html.Span(fault, style={"color": fc, "fontWeight": "bold", "fontSize": "11px"}),
            ],
            style={
                "borderBottom": f"1px solid {BORDER_COLOR}",
                "padding": "3px 0",
                "fontFamily": "'Courier New', monospace",
            },
        ))
    if not fault_entries:
        fault_entries = [
            html.Div(
                "No faults detected.",
                style={
                    "fontFamily": "'Courier New', monospace",
                    "fontSize": "11px",
                    "color": GREEN,
                },
            )
        ]

    return (
        conn_badge,
        status_ind,
        uptime_disp,
        csv_stat,
        soc_gauge(soc),
        voltage_gauge(v),
        current_gauge(i),
        temp_gauge(t),
        kpi_children,
        time_series_chart(),
        fault_entries,
    )

# ─────────────────────────────────────────────────────────────────────────────
# 9.  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="BMS Dashboard — Samsung 28A / ACS712-05B / DHT11"
    )
    parser.add_argument("--port",  type=str, default=None,
                        help="Serial port (e.g. COM3, /dev/ttyUSB0)")
    parser.add_argument("--baud",  type=int, default=9600,
                        help="Serial baud rate (default: 9600)")
    parser.add_argument("--demo",  action="store_true",
                        help="Run with simulated data (no hardware needed)")
    parser.add_argument("--host",  type=str, default="127.0.0.1",
                        help="Dashboard host (default: 127.0.0.1)")
    parser.add_argument("--port-web", type=int, default=8050,
                        help="Dashboard web port (default: 8050)")
    args = parser.parse_args()

    if args.demo or args.port is None:
        t = threading.Thread(target=demo_thread, daemon=True)
    else:
        t = threading.Thread(
            target=serial_reader_thread,
            args=(args.port, args.baud),
            daemon=True,
        )
    t.start()

    print(f"\n[Dashboard] Open in browser:  http://{args.host}:{args.port_web}/\n")
    app.run(host=args.host, port=args.port_web, debug=False)

if __name__ == "__main__":
    main()