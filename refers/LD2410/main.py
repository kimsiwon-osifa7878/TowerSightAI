from __future__ import annotations

from datetime import datetime
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import analyzer
from analyzer import RealtimeBaselineComparator
from data_recorder import DataRecorder
from ld2410_serial import (
    BAUDRATE,
    TCP_PORT,
    LD2410SerialReader,
    LD2410TCPServer,
    SimulationReader,
    available_ports,
)


APP_DIR = Path(__file__).resolve().parent
AUTO_SAVE_DIR = APP_DIR / "data"
PREPARE_SECONDS = 10
RECORD_SECONDS = 60
POLL_INTERVAL_MS = 60
MAX_FRAMES_PER_POLL = 100

PROFILE_LABELS = {
    "empty_car": "1/3 Empty Car Baseline",
    "person_still": "2/3 Person Still",
    "person_moving": "3/3 Person Moving",
}

NEXT_PROFILE = {
    "empty_car": "person_still",
    "person_still": "person_moving",
    "person_moving": None,
}


class LD2410App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LD2410C Baseline Comparator")
        self.geometry("1220x780")
        self.minsize(1080, 680)

        self.frame_queue: queue.Queue = queue.Queue()
        self.recorder = DataRecorder()
        self.comparator = RealtimeBaselineComparator(window_seconds=8)
        self.serial_reader = LD2410SerialReader(
            on_frame=self._enqueue_frame,
            on_status=self._thread_status,
            raw_log_path=str(APP_DIR / "raw_hex_errors.log"),
        )
        self.tcp_server = LD2410TCPServer(
            on_frame=self._enqueue_frame,
            on_status=self._thread_status,
            raw_log_path=str(APP_DIR / "raw_hex_errors.log"),
        )
        self.sim_reader = SimulationReader(on_frame=self._enqueue_frame, on_status=self._thread_status)

        self.latest_frame = None
        self.loaded_df = pd.DataFrame()
        self.auto_save_path: Path | None = None
        self.workflow_modal: tk.Toplevel | None = None
        self.workflow_after_id: str | None = None
        self.workflow_active_profile: str | None = None
        self.workflow_phase = "idle"
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_vars()
        self._build_layout()
        self._refresh_ports()
        self._poll_frames()

    def _build_vars(self) -> None:
        self.port_var = tk.StringVar()
        self.source_var = tk.StringVar(value="simulation")
        self.tcp_host_var = tk.StringVar(value="0.0.0.0")
        self.tcp_port_var = tk.StringVar(value=str(TCP_PORT))
        self.status_var = tk.StringVar(value="Disconnected")
        self.record_var = tk.StringVar(value="Recording: off")
        self.workflow_var = tk.StringVar(value="Guided recording: ready")
        self.margin_var = tk.IntVar(value=5)
        self.interest_gates_var = tk.StringVar(value="3,4,5")
        self.detection_var = tk.StringVar(value="CLEAR")
        self.sample_var = tk.StringVar(value="0 samples")

    def _build_layout(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, padding=10)
        left.grid(row=0, column=0, sticky="ns")
        right = ttk.Frame(self, padding=(0, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        connection = ttk.LabelFrame(parent, text="Connection", padding=10)
        connection.grid(row=0, column=0, sticky="ew")
        connection.columnconfigure(1, weight=1)

        source_row = ttk.Frame(connection)
        source_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        for column, (text, value) in enumerate(
            [("Simulation", "simulation"), ("UART", "uart"), ("TCP Server", "tcp")]
        ):
            ttk.Radiobutton(source_row, text=text, value=value, variable=self.source_var).grid(
                row=0, column=column, sticky="w", padx=(0, 6)
            )

        ttk.Label(connection, text="UART port").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.port_combo = ttk.Combobox(connection, textvariable=self.port_var, width=18, state="readonly")
        self.port_combo.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        ttk.Label(connection, text="TCP bind address").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(connection, text="Port").grid(row=3, column=1, sticky="w", pady=(4, 0))
        ttk.Entry(connection, textvariable=self.tcp_host_var, width=15).grid(row=4, column=0, sticky="ew", padx=(0, 4))
        ttk.Entry(connection, textvariable=self.tcp_port_var, width=8).grid(row=4, column=1, sticky="ew")

        ttk.Button(connection, text="Refresh UART", command=self._refresh_ports).grid(
            row=5, column=0, sticky="ew", padx=(0, 4), pady=(6, 0)
        )
        ttk.Button(connection, text="Connect / Listen", command=self._connect).grid(
            row=5, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Button(connection, text="Disconnect", command=self._disconnect).grid(
            row=6, column=0, columnspan=2, sticky="ew", pady=(4, 0)
        )
        ttk.Label(connection, textvariable=self.status_var, wraplength=230).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        record = ttk.LabelFrame(parent, text="Guided Record", padding=10)
        record.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for idx, (label, profile) in enumerate(
            [
                ("Record Baseline", "empty_car"),
                ("Record Person Still", "person_still"),
                ("Record Person Moving", "person_moving"),
            ]
        ):
            ttk.Button(record, text=label, command=lambda p=profile: self._start_guided_recording(p)).grid(
                row=idx, column=0, sticky="ew", pady=2
            )
        ttk.Button(record, text="Stop / Cancel Recording", command=self._stop_recording).grid(
            row=3, column=0, sticky="ew", pady=(8, 2)
        )
        ttk.Button(record, text="Clear Memory", command=self._clear_memory).grid(row=4, column=0, sticky="ew", pady=2)
        ttk.Label(record, textvariable=self.record_var).grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Label(record, textvariable=self.workflow_var, wraplength=210).grid(row=6, column=0, sticky="w", pady=(4, 0))

        files = ttk.LabelFrame(parent, text="CSV / Analysis", padding=10)
        files.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(files, text="Save CSV", command=self._save_csv).grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(files, text="Load CSV", command=self._load_csv).grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Label(files, text="Margin").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(files, from_=0, to=50, textvariable=self.margin_var, width=8).grid(row=3, column=0, sticky="ew")
        ttk.Label(files, text="Interest gates").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(files, textvariable=self.interest_gates_var).grid(row=5, column=0, sticky="ew")
        ttk.Button(files, text="Analyze / Update Baseline", command=self._analyze).grid(
            row=6, column=0, sticky="ew", pady=(10, 2)
        )

        detect = ttk.LabelFrame(parent, text="Realtime Baseline Compare", padding=10)
        detect.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.detect_label = ttk.Label(detect, textvariable=self.detection_var, font=("Segoe UI", 22, "bold"))
        self.detect_label.grid(row=0, column=0, sticky="ew")
        ttk.Label(detect, textvariable=self.sample_var, wraplength=230).grid(row=1, column=0, sticky="w", pady=(4, 0))

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.summary_var = tk.StringVar(value="Waiting for data")
        ttk.Label(top, textvariable=self.summary_var).grid(row=0, column=0, sticky="w")

        content = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        content.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        live_frame = ttk.Frame(content)
        live_frame.columnconfigure(0, weight=1)
        live_frame.rowconfigure(0, weight=1)
        content.add(live_frame, weight=3)

        self.tree = ttk.Treeview(
            live_frame,
            columns=("gate", "moving", "motionless", "moving_threshold", "motionless_threshold"),
            show="headings",
            height=9,
        )
        for col, text, width in [
            ("gate", "Gate", 70),
            ("moving", "Moving Energy", 130),
            ("motionless", "Motionless Energy", 150),
            ("moving_threshold", "Moving Th.", 110),
            ("motionless_threshold", "Motionless Th.", 130),
        ]:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        for gate in range(9):
            self.tree.insert("", "end", iid=str(gate), values=(gate, 0, 0, "-", "-"))

        chart_frame = ttk.Frame(content)
        chart_frame.columnconfigure(0, weight=1)
        chart_frame.rowconfigure(0, weight=1)
        content.add(chart_frame, weight=4)

        self.figure = Figure(figsize=(8, 3.6), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_ylim(0, 100)
        self.ax.set_xlabel("Gate")
        self.ax.set_ylabel("Energy")
        self.ax.set_title("Moving / Motionless Energy")
        gates = list(range(9))
        self.moving_bars = self.ax.bar(
            [g - 0.18 for g in gates],
            [0] * 9,
            width=0.36,
            label="Moving",
            color="#2f7ed8",
        )
        self.motionless_bars = self.ax.bar(
            [g + 0.18 for g in gates],
            [0] * 9,
            width=0.36,
            label="Motionless",
            color="#34a853",
        )
        self.ax.set_xticks(gates)
        self.ax.legend(loc="upper right")
        self.figure.tight_layout()
        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_frame)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        analysis_frame = ttk.LabelFrame(parent, text="Analysis", padding=8)
        analysis_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        analysis_frame.columnconfigure(0, weight=1)
        self.analysis_text = tk.Text(analysis_frame, height=9, wrap="none")
        self.analysis_text.grid(row=0, column=0, sticky="ew")

    def _refresh_ports(self) -> None:
        ports = available_ports()
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _connect(self) -> None:
        try:
            source = self.source_var.get()
            if source == "simulation":
                if self.serial_reader.connected:
                    self.serial_reader.disconnect()
                if self.tcp_server.connected:
                    self.tcp_server.stop()
                self.sim_reader.start()
            elif source == "uart":
                port = self.port_var.get()
                if not port:
                    messagebox.showwarning("COM port", "Select a COM port.")
                    return
                if self.sim_reader.connected:
                    self.sim_reader.stop()
                if self.tcp_server.connected:
                    self.tcp_server.stop()
                self.serial_reader.connect(port, BAUDRATE)
            elif source == "tcp":
                host = self.tcp_host_var.get().strip() or "0.0.0.0"
                try:
                    port = int(self.tcp_port_var.get())
                except ValueError:
                    messagebox.showwarning("TCP port", "Enter a numeric TCP port.")
                    return
                if not 1 <= port <= 65535:
                    messagebox.showwarning("TCP port", "TCP port must be between 1 and 65535.")
                    return
                if self.sim_reader.connected:
                    self.sim_reader.stop()
                if self.serial_reader.connected:
                    self.serial_reader.disconnect()
                self.tcp_server.start(host, port)
            else:
                raise ValueError(f"Unknown data source: {source}")
        except Exception as exc:
            messagebox.showerror("Connect failed", str(exc))

    def _disconnect(self) -> None:
        self._stop_recording()
        self.serial_reader.disconnect()
        self.tcp_server.stop()
        self.sim_reader.stop()
        self._thread_status("Disconnected")

    def _enqueue_frame(self, frame) -> None:
        self.frame_queue.put(frame)

    def _thread_status(self, text: str) -> None:
        self.frame_queue.put(("status", text))

    def _poll_frames(self) -> None:
        latest_frame = None
        processed_items = 0
        try:
            while processed_items < MAX_FRAMES_PER_POLL:
                item = self.frame_queue.get_nowait()
                processed_items += 1
                if isinstance(item, tuple) and item[0] == "status":
                    self.status_var.set(item[1])
                else:
                    self._process_frame(item)
                    latest_frame = item
        except queue.Empty:
            pass
        if latest_frame is not None:
            self._render_frame(latest_frame)

        # Yield to Tk even if the producer is temporarily faster than the UI.
        delay = 1 if not self.frame_queue.empty() else POLL_INTERVAL_MS
        self.after(delay, self._poll_frames)

    def _process_frame(self, frame) -> None:
        self.latest_frame = frame
        self.recorder.add_frame(frame)
        state = self.comparator.update(frame)
        self._update_detection(state)

    def _render_frame(self, frame) -> None:
        self._update_live_table(frame)
        self._update_chart(frame)
        self.summary_var.set(
            f"{frame.timestamp.strftime('%H:%M:%S.%f')[:-3]} | {frame.target_status_text} | "
            f"moving {frame.moving_energy}@{frame.moving_distance_cm}cm | "
            f"motionless {frame.motionless_energy}@{frame.motionless_distance_cm}cm | "
            f"detect {frame.detection_distance_cm}cm | rows {len(self.recorder.rows)}"
        )

    def _update_live_table(self, frame) -> None:
        for gate in range(9):
            moving_th = self.comparator.thresholds.get(("moving", gate), "-")
            motionless_th = self.comparator.thresholds.get(("motionless", gate), "-")
            if isinstance(moving_th, float):
                moving_th = f"{moving_th:.1f}"
            if isinstance(motionless_th, float):
                motionless_th = f"{motionless_th:.1f}"
            self.tree.item(
                str(gate),
                values=(
                    gate,
                    frame.moving_gate_energy[gate],
                    frame.motionless_gate_energy[gate],
                    moving_th,
                    motionless_th,
                ),
            )

    def _update_chart(self, frame) -> None:
        for bar, value in zip(self.moving_bars, frame.moving_gate_energy):
            bar.set_height(value)
        for bar, value in zip(self.motionless_bars, frame.motionless_gate_energy):
            bar.set_height(value)
        self.canvas.draw_idle()

    def _update_detection(self, state) -> None:
        self.detection_var.set(state.state)
        gates = ",".join(str(g) for g in state.active_gates) if state.active_gates else "-"
        self.sample_var.set(
            f"8s hits {state.hit_count}/{state.sample_count}, gates {gates}, last score {state.last_score:.1f}"
        )
        color = {"DETECTED": "#b00020", "SUSPECT": "#9a6700", "CLEAR": "#137333"}.get(state.state, "#333333")
        self.detect_label.configure(foreground=color)

    def _start_recording(self, profile: str) -> None:
        self.recorder.start(profile)
        self.sim_reader.set_mode(profile)
        self.record_var.set(f"Recording: {profile}")

    def _stop_recording(self) -> None:
        if self.workflow_after_id:
            self.after_cancel(self.workflow_after_id)
            self.workflow_after_id = None
        self.workflow_phase = "idle"
        self.workflow_active_profile = None
        self.recorder.stop()
        self.record_var.set("Recording: off")
        self.workflow_var.set("Guided recording: stopped")
        if self.workflow_modal and self.workflow_modal.winfo_exists():
            self.workflow_modal.destroy()
        self.workflow_modal = None

    def _clear_memory(self) -> None:
        if self.workflow_phase != "idle":
            messagebox.showwarning("Recording", "Stop the guided recording before clearing memory.")
            return
        if messagebox.askyesno("Clear Memory", "Clear all recorded rows in memory?"):
            self.recorder.clear()
            self.loaded_df = pd.DataFrame()
            self.auto_save_path = None
            self.record_var.set("Recording: off")
            self.workflow_var.set("Guided recording: ready")

    def _save_csv(self) -> None:
        if not self.recorder.rows:
            messagebox.showinfo("Save CSV", "No data to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="ld2410_baseline_data.csv",
        )
        if path:
            self.recorder.save_csv(path)
            self.status_var.set(f"Saved: {path}")

    def _load_csv(self) -> None:
        path = filedialog.askopenfilename(title="Load CSV", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.loaded_df = self.recorder.load_csv(path)
            self.status_var.set(f"Loaded: {path}")
            self._analyze()
        except Exception as exc:
            messagebox.showerror("Load CSV failed", str(exc))

    def _analyze(self) -> None:
        df = self.recorder.to_dataframe()
        if df.empty and not self.loaded_df.empty:
            df = self.loaded_df
        if df.empty:
            messagebox.showinfo("Analyze", "No CSV or recorded data to analyze.")
            return

        try:
            gates = self._parse_interest_gates()
            margin = int(self.margin_var.get())
            profiles = analyzer.create_all_profiles(df)
            compare = analyzer.compare_empty_vs_person_still(df)
            thresholds = analyzer.recommend_thresholds(df, margin=margin, interest_gates=gates)
            baseline = analyzer.create_profile(df, "empty_car")
            self.comparator.configure(baseline, margin=margin, interest_gates=gates)
            self._render_analysis(profiles, compare, thresholds)
        except Exception as exc:
            messagebox.showerror("Analyze failed", str(exc))

    def _parse_interest_gates(self) -> list[int]:
        raw = self.interest_gates_var.get().replace(" ", "")
        gates = []
        for part in raw.split(","):
            if not part:
                continue
            gate = int(part)
            if gate < 0 or gate > 8:
                raise ValueError("Interest gates must be between 0 and 8")
            gates.append(gate)
        return gates or [3, 4, 5]

    def _render_analysis(self, profiles: pd.DataFrame, compare: pd.DataFrame, thresholds: pd.DataFrame) -> None:
        self.analysis_text.delete("1.0", tk.END)
        if profiles.empty:
            self.analysis_text.insert(tk.END, "No profile data.\n")
            return

        self.analysis_text.insert(tk.END, "Baseline profile and threshold recommendation updated.\n\n")
        self.analysis_text.insert(tk.END, "Recommended thresholds:\n")
        self.analysis_text.insert(
            tk.END,
            thresholds.pivot(index="gate", columns="energy_type", values="recommended_threshold").to_string(),
        )
        self.analysis_text.insert(tk.END, "\n\nEmpty car vs person still separation:\n")
        if compare.empty:
            self.analysis_text.insert(tk.END, "Need both empty_car and person_still samples.\n")
        else:
            cols = ["energy_type", "gate", "avg_diff", "separation_score"]
            self.analysis_text.insert(tk.END, compare[cols].round(2).to_string(index=False))

    def _start_guided_recording(self, profile: str) -> None:
        if self.workflow_phase != "idle":
            messagebox.showwarning("Recording", "A guided recording is already running.")
            return
        if not self._is_data_source_running():
            messagebox.showwarning("Connect first", "Connect to the sensor, or start Simulation Mode, before recording.")
            return
        self.workflow_active_profile = profile
        self.workflow_phase = "prepare"
        self.sim_reader.set_mode(profile)
        self._ensure_auto_save_path()
        self._show_workflow_modal()
        self._run_prepare_countdown(PREPARE_SECONDS)

    def _is_data_source_running(self) -> bool:
        return self.sim_reader.connected or self.serial_reader.connected or self.tcp_server.connected

    def _ensure_auto_save_path(self) -> Path:
        if self.auto_save_path is None:
            AUTO_SAVE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.auto_save_path = AUTO_SAVE_DIR / f"ld2410_auto_record_{stamp}.csv"
        return self.auto_save_path

    def _show_workflow_modal(self) -> None:
        if self.workflow_modal and self.workflow_modal.winfo_exists():
            self.workflow_modal.destroy()

        modal = tk.Toplevel(self)
        modal.title("Guided Recording")
        modal.geometry("720x420")
        modal.transient(self)
        modal.grab_set()
        modal.protocol("WM_DELETE_WINDOW", self._cancel_guided_recording)
        modal.columnconfigure(0, weight=1)
        modal.rowconfigure(2, weight=1)

        self.modal_title_var = tk.StringVar()
        self.modal_count_var = tk.StringVar()
        self.modal_detail_var = tk.StringVar()

        ttk.Label(modal, textvariable=self.modal_title_var, font=("Segoe UI", 26, "bold"), anchor="center").grid(
            row=0, column=0, sticky="ew", padx=24, pady=(28, 8)
        )
        ttk.Label(modal, textvariable=self.modal_count_var, font=("Segoe UI", 76, "bold"), anchor="center").grid(
            row=1, column=0, sticky="ew", padx=24
        )
        ttk.Label(modal, textvariable=self.modal_detail_var, font=("Segoe UI", 17), anchor="center", wraplength=640).grid(
            row=2, column=0, sticky="nsew", padx=24, pady=12
        )
        self.modal_button_frame = ttk.Frame(modal)
        self.modal_button_frame.grid(row=3, column=0, pady=(0, 24))
        ttk.Button(self.modal_button_frame, text="Cancel / Stop", command=self._cancel_guided_recording).grid(
            row=0, column=0, ipadx=24, ipady=6
        )
        self.workflow_modal = modal

    def _run_prepare_countdown(self, remaining: int) -> None:
        profile = self.workflow_active_profile
        if not profile or self.workflow_phase != "prepare":
            return
        label = PROFILE_LABELS[profile]
        self.workflow_var.set(f"{label}: prepare {remaining}s")
        self.record_var.set("Recording: waiting")
        self.modal_title_var.set(label)
        self.modal_count_var.set(str(remaining))
        self.modal_detail_var.set(
            "Prepare now. Recording starts automatically after the countdown.\n"
            "For baseline, keep the car empty. For person steps, get into position now."
        )
        if remaining <= 0:
            self._begin_guided_recording()
            return
        self.workflow_after_id = self.after(1000, lambda: self._run_prepare_countdown(remaining - 1))

    def _begin_guided_recording(self) -> None:
        profile = self.workflow_active_profile
        if not profile:
            return
        self.workflow_phase = "record"
        self._start_recording(profile)
        self._run_record_countdown(RECORD_SECONDS)

    def _run_record_countdown(self, remaining: int) -> None:
        profile = self.workflow_active_profile
        if not profile or self.workflow_phase != "record":
            return
        label = PROFILE_LABELS[profile]
        self.workflow_var.set(f"{label}: recording {remaining}s")
        self.modal_title_var.set(f"Recording: {label}")
        self.modal_count_var.set(str(remaining))
        self.modal_detail_var.set(
            "Recording is in progress. Stay in the requested state until the timer ends.\n"
            f"Rows collected so far: {len(self.recorder.rows)}"
        )
        if remaining <= 0:
            self._finish_guided_recording()
            return
        self.workflow_after_id = self.after(1000, lambda: self._run_record_countdown(remaining - 1))

    def _finish_guided_recording(self) -> None:
        profile = self.workflow_active_profile
        self.workflow_after_id = None
        self.recorder.stop()
        self.record_var.set("Recording: off")

        save_path = self._ensure_auto_save_path()
        if self.recorder.rows:
            self.recorder.save_csv(save_path)
            self.status_var.set(f"Auto saved: {save_path}")

        self.workflow_phase = "done"
        next_profile = NEXT_PROFILE.get(profile)
        self._show_step_finished(profile, next_profile, save_path)

    def _show_step_finished(self, profile: str | None, next_profile: str | None, save_path: Path) -> None:
        label = PROFILE_LABELS.get(profile or "", "Step")
        self.workflow_var.set(f"{label}: saved")
        self.modal_title_var.set("Step complete")
        self.modal_count_var.set("Saved")

        if next_profile:
            next_label = PROFILE_LABELS[next_profile]
            self.modal_detail_var.set(
                f"{label} is complete and CSV was auto-saved.\n\n"
                f"Next step: {next_label}\n"
                f"File: {save_path}"
            )
        else:
            self.modal_detail_var.set(
                "All recording steps are complete and CSV was auto-saved.\n\n"
                "The app will run Analyze / Update Baseline next.\n"
                f"File: {save_path}"
            )

        for child in self.modal_button_frame.winfo_children():
            child.destroy()

        if next_profile:
            ttk.Button(
                self.modal_button_frame,
                text=f"Start Next: {PROFILE_LABELS[next_profile]}",
                command=lambda p=next_profile: self._continue_guided_recording(p),
            ).grid(row=0, column=0, padx=8, ipadx=18, ipady=6)
            ttk.Button(self.modal_button_frame, text="Close", command=self._close_finished_modal).grid(
                row=0, column=1, padx=8, ipadx=18, ipady=6
            )
        else:
            ttk.Button(self.modal_button_frame, text="Analyze Now", command=self._finish_all_and_analyze).grid(
                row=0, column=0, padx=8, ipadx=18, ipady=6
            )
            ttk.Button(self.modal_button_frame, text="Close", command=self._close_finished_modal).grid(
                row=0, column=1, padx=8, ipadx=18, ipady=6
            )

    def _continue_guided_recording(self, profile: str) -> None:
        if self.workflow_modal and self.workflow_modal.winfo_exists():
            self.workflow_modal.destroy()
        self.workflow_modal = None
        self.workflow_phase = "idle"
        self.workflow_active_profile = None
        self._start_guided_recording(profile)

    def _finish_all_and_analyze(self) -> None:
        self.workflow_phase = "idle"
        self.workflow_active_profile = None
        if self.workflow_modal and self.workflow_modal.winfo_exists():
            self.workflow_modal.destroy()
        self.workflow_modal = None
        self.workflow_var.set("Guided recording: complete")
        self._analyze()

    def _close_finished_modal(self) -> None:
        self.workflow_phase = "idle"
        self.workflow_active_profile = None
        if self.workflow_modal and self.workflow_modal.winfo_exists():
            self.workflow_modal.destroy()
        self.workflow_modal = None

    def _cancel_guided_recording(self) -> None:
        self._stop_recording()

    def _on_close(self) -> None:
        self._stop_recording()
        self.serial_reader.disconnect()
        self.tcp_server.stop()
        self.sim_reader.stop()
        self.destroy()


if __name__ == "__main__":
    app = LD2410App()
    app.mainloop()
