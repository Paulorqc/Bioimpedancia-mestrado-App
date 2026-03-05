import asyncio
import os
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import matplotlib.pyplot as plt
from bleak import BleakClient
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

TARGET_DEVICE_ADDRESS = "DC:06:75:F6:57:5E"
CHAR_TX_UUID = "6ebf5002-8765-4f67-8f4f-95f56ac3a1a0"
CHAR_RX_UUID = "6ebf5003-8765-4f67-8f4f-95f56ac3a1a0"
BLE_WRITE_CHUNK_SIZE = 180
IZ_COMMAND_PATTERN = re.compile(r"^IZ\d+(?:\.\d+)?F$", re.IGNORECASE)
DEFAULT_IMAGE_DIR = r"C:\Users\Mario\Downloads"


class BLEApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("BLE PC Client")
        self.root.geometry("980x760")

        self.status_var = tk.StringVar(value="Disconnected")
        self.send_var = tk.StringVar(value="")
        self.connected = False
        self.busy = False
        self.sending = False
        self.awaiting_iz_data = False
        self.last_iz_command = ""

        self.client = None
        self.rx_stream_buffer = ""
        self.ui_queue = queue.Queue()
        self.closing = False

        # Run BLE async tasks in a dedicated thread to keep Tk responsive.
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.loop_thread.start()

        self._build_ui()
        self._update_buttons()

        self.root.after(100, self._drain_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text=f"Target device: {TARGET_DEVICE_ADDRESS}").grid(
            row=0, column=0, columnspan=3, sticky=tk.W
        )

        self.connect_btn = ttk.Button(top, text="Connect", command=self.on_connect_clicked)
        self.connect_btn.grid(row=1, column=0, pady=(10, 0), padx=(0, 8), sticky=tk.W)

        self.disconnect_btn = ttk.Button(
            top, text="Disconnect", command=self.on_disconnect_clicked
        )
        self.disconnect_btn.grid(row=1, column=1, pady=(10, 0), sticky=tk.W)

        self.erase_btn = ttk.Button(top, text="Erase", command=self.on_erase_clicked)
        self.erase_btn.grid(row=1, column=2, pady=(10, 0), padx=(8, 8), sticky=tk.W)

        self.save_image_btn = ttk.Button(
            top, text="Save Image", command=self.on_save_image_clicked
        )
        self.save_image_btn.grid(row=1, column=3, pady=(10, 0), sticky=tk.W)

        ttk.Label(top, text="Status:").grid(
            row=1, column=4, padx=(30, 8), pady=(10, 0), sticky=tk.E
        )
        self.status_label = ttk.Label(top, textvariable=self.status_var)
        self.status_label.grid(row=1, column=5, pady=(10, 0), sticky=tk.W)

        ttk.Label(top, text="Message:").grid(row=2, column=0, pady=(14, 0), sticky=tk.W)
        self.send_entry = ttk.Entry(top, textvariable=self.send_var, width=70)
        self.send_entry.grid(
            row=2, column=1, columnspan=4, padx=(0, 8), pady=(14, 0), sticky=tk.EW
        )
        self.send_entry.bind("<Return>", self._on_send_enter)
        self.send_entry.bind("<KeyRelease>", self._on_send_input_change)

        self.send_btn = ttk.Button(top, text="Send", command=self.on_send_clicked)
        self.send_btn.grid(row=2, column=5, pady=(14, 0), sticky=tk.W)

        top.columnconfigure(1, weight=1)

        ttk.Label(self.root, text="Received messages:", padding=(10, 0, 10, 5)).pack(
            anchor=tk.W
        )

        self.message_box = ScrolledText(self.root, wrap=tk.WORD, state=tk.DISABLED, height=12)
        self.message_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        plot_frame = ttk.LabelFrame(self.root, text="IZ Plot", padding=8)
        plot_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.fig, (self.ax_real, self.ax_imag) = plt.subplots(2, 1, figsize=(9.2, 4.8))
        self._reset_plot()
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _run_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _enqueue(self, event_type: str, value):
        self.ui_queue.put((event_type, value))

    def _drain_ui_queue(self):
        while True:
            try:
                event_type, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "status":
                self.status_var.set(value)
            elif event_type == "message":
                self._append_message(value)
            elif event_type == "rx_complete":
                self._append_received_block(value)
                self._handle_completed_block(value)
            elif event_type == "connected":
                self.connected = bool(value)
                self.busy = False
                self._update_buttons()
            elif event_type == "sending":
                self.sending = bool(value)
                self._update_buttons()
            elif event_type == "clear_send":
                self.send_var.set("")
                self._update_buttons()

        if not self.closing:
            self.root.after(100, self._drain_ui_queue)

    def _append_message(self, text: str):
        self.message_box.configure(state=tk.NORMAL)
        self.message_box.insert(tk.END, f"{text}\n")
        self.message_box.see(tk.END)
        self.message_box.configure(state=tk.DISABLED)

    def _append_received_block(self, text: str):
        self.message_box.configure(state=tk.NORMAL)
        self.message_box.insert(tk.END, text)
        if not text.endswith("\n"):
            self.message_box.insert(tk.END, "\n")
        self.message_box.insert(tk.END, "\n")
        self.message_box.see(tk.END)
        self.message_box.configure(state=tk.DISABLED)

    def _reset_plot(self):
        self.ax_real.clear()
        self.ax_real.set_title("Real Part vs Frequency")
        self.ax_real.set_ylabel("Real (Ohm)")
        self.ax_real.grid(True, linestyle=":", alpha=0.5)

        self.ax_imag.clear()
        self.ax_imag.set_title("Imaginary Part vs Frequency")
        self.ax_imag.set_xlabel("Frequency (Hz)")
        self.ax_imag.set_ylabel("Imag (Ohm)")
        self.ax_imag.grid(True, linestyle=":", alpha=0.5)

        self.fig.tight_layout(pad=2.0)

    def _plot_iz_data(self, real_values, imag_values, freq_values):
        self.ax_real.clear()
        self.ax_real.plot(freq_values, real_values, color="tab:blue", linewidth=1.0)
        self.ax_real.set_title("Real Part vs Frequency")
        self.ax_real.set_ylabel("Real (Ohm)")
        self.ax_real.grid(True, linestyle=":", alpha=0.5)

        self.ax_imag.clear()
        self.ax_imag.plot(freq_values, imag_values, color="tab:red", linewidth=1.0)
        self.ax_imag.set_title("Imaginary Part vs Frequency")
        self.ax_imag.set_xlabel("Frequency (Hz)")
        self.ax_imag.set_ylabel("Imag (Ohm)")
        self.ax_imag.grid(True, linestyle=":", alpha=0.5)

        self.fig.tight_layout(pad=2.0)
        self.canvas.draw_idle()

    def _extract_iz_data(self, block_text: str):
        real_values = []
        imag_values = []
        freq_values = []

        for raw_line in block_text.splitlines():
            line = raw_line.strip()
            if "&" not in line:
                continue

            parts = [part.strip() for part in line.split("&")]
            if len(parts) < 3:
                continue

            try:
                real = float(parts[0])
                imag = float(parts[1])
                freq = float(parts[2])
            except ValueError:
                continue

            real_values.append(real)
            imag_values.append(imag)
            freq_values.append(freq)

        if not freq_values:
            return None

        return real_values, imag_values, freq_values

    def _handle_completed_block(self, block_with_terminator: str):
        # Plot only when an IZ command was sent and response is complete ('@').
        if not self.awaiting_iz_data:
            return

        block_text = block_with_terminator.rsplit("@", 1)[0]
        parsed_data = self._extract_iz_data(block_text)
        if not parsed_data:
            return

        real_values, imag_values, freq_values = parsed_data
        self._plot_iz_data(real_values, imag_values, freq_values)
        self._append_message(
            f"[PLOT] {self.last_iz_command or 'IZ'} -> {len(freq_values)} puntos"
        )
        self.awaiting_iz_data = False

    def _update_buttons(self):
        if self.busy:
            self.connect_btn.configure(state=tk.DISABLED)
            self.disconnect_btn.configure(state=tk.DISABLED)
            self.send_btn.configure(state=tk.DISABLED)
            return

        if self.connected:
            self.connect_btn.configure(state=tk.DISABLED)
            self.disconnect_btn.configure(state=tk.NORMAL)
            can_send = (not self.sending) and bool(self.send_var.get().strip())
            self.send_btn.configure(state=tk.NORMAL if can_send else tk.DISABLED)
        else:
            self.connect_btn.configure(state=tk.NORMAL)
            self.disconnect_btn.configure(state=tk.DISABLED)
            self.send_btn.configure(state=tk.DISABLED)

    def _on_send_enter(self, _event):
        self.on_send_clicked()
        return "break"

    def _on_send_input_change(self, _event):
        self._update_buttons()

    def on_erase_clicked(self):
        self.message_box.configure(state=tk.NORMAL)
        self.message_box.delete("1.0", tk.END)
        self.message_box.configure(state=tk.DISABLED)

        self.rx_stream_buffer = ""
        self.awaiting_iz_data = False
        self.last_iz_command = ""
        self._reset_plot()
        self.canvas.draw_idle()
        self.status_var.set("Data erased")

    def on_save_image_clicked(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ble_iz_plot_{timestamp}.jpg"
        save_dir = DEFAULT_IMAGE_DIR
        save_path = os.path.join(save_dir, filename)

        try:
            os.makedirs(save_dir, exist_ok=True)
            self.fig.savefig(save_path, format="jpg", dpi=300, bbox_inches="tight")
            self._append_message(f"[SAVE] Image saved: {save_path}")
            self.status_var.set(f"Image saved: {filename}")
        except Exception as exc:
            self._append_message(f"Save image error: {exc}")
            self.status_var.set("Save image failed")

    def on_connect_clicked(self):
        if self.connected or self.busy:
            return

        self.busy = True
        self.status_var.set("Connecting...")
        self._update_buttons()
        self._append_message(f"Connecting to {TARGET_DEVICE_ADDRESS}...")

        asyncio.run_coroutine_threadsafe(self._connect_ble(), self.loop)

    async def _connect_ble(self):
        client = BleakClient(
            TARGET_DEVICE_ADDRESS,
            disconnected_callback=self._on_device_disconnected,
        )

        try:
            await client.connect(timeout=15.0)
            if not client.is_connected:
                raise RuntimeError("Could not connect to target device")

            await client.start_notify(CHAR_TX_UUID, self._notification_handler)
            self.client = client

            self._enqueue("message", f"Connected to {TARGET_DEVICE_ADDRESS}")
            self._enqueue("status", "Connected")
            self._enqueue("connected", True)

        except Exception as exc:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass

            self.client = None
            self._enqueue("message", f"Connection error: {exc}")
            self._enqueue("status", "Disconnected")
            self._enqueue("connected", False)

    def on_disconnect_clicked(self):
        if self.busy:
            return

        self.busy = True
        self.status_var.set("Disconnecting...")
        self._update_buttons()

        asyncio.run_coroutine_threadsafe(self._disconnect_ble(user_requested=True), self.loop)

    async def _disconnect_ble(self, user_requested: bool):
        client = self.client

        try:
            if client and client.is_connected:
                try:
                    await client.stop_notify(CHAR_TX_UUID)
                except Exception:
                    pass

                await client.disconnect()

            if user_requested:
                self._enqueue("message", "Disconnected by user")

        except Exception as exc:
            self._enqueue("message", f"Disconnect error: {exc}")

        finally:
            self.client = None
            self.rx_stream_buffer = ""
            self.awaiting_iz_data = False
            self.last_iz_command = ""
            self._enqueue("status", "Disconnected")
            self._enqueue("connected", False)

    def on_send_clicked(self):
        if not self.connected or self.busy or self.sending:
            return

        text = self.send_var.get().strip()
        if not text:
            return

        if IZ_COMMAND_PATTERN.fullmatch(text):
            self.awaiting_iz_data = True
            self.last_iz_command = text.upper()
            self._append_message(f"[PLOT] Esperando datos para {self.last_iz_command} ...")
        else:
            self.awaiting_iz_data = False
            self.last_iz_command = ""

        self.sending = True
        self._update_buttons()
        asyncio.run_coroutine_threadsafe(self._send_message(text), self.loop)

    async def _send_message(self, text: str):
        client = self.client
        payload = text.encode("utf-8")

        try:
            if not client or not client.is_connected:
                raise RuntimeError("Not connected")

            for i in range(0, len(payload), BLE_WRITE_CHUNK_SIZE):
                chunk = payload[i : i + BLE_WRITE_CHUNK_SIZE]
                await client.write_gatt_char(CHAR_RX_UUID, chunk, response=False)
                await asyncio.sleep(0.005)

            self._enqueue("message", f"[TX] {text}")
            self._enqueue("clear_send", True)
        except Exception as exc:
            self._enqueue("message", f"Send error: {exc}")
        finally:
            self._enqueue("sending", False)

    def _notification_handler(self, _sender, data: bytearray):
        chunk = data.decode("utf-8", errors="ignore")
        if not chunk:
            return

        self.rx_stream_buffer += chunk
        while "@" in self.rx_stream_buffer:
            completed, remaining = self.rx_stream_buffer.split("@", 1)
            self.rx_stream_buffer = remaining
            self._enqueue("rx_complete", f"{completed}@")

    def _on_device_disconnected(self, _client):
        self.client = None
        self.rx_stream_buffer = ""
        self.awaiting_iz_data = False
        self.last_iz_command = ""
        self._enqueue("message", "Device disconnected")
        self._enqueue("status", "Disconnected")
        self._enqueue("connected", False)

    def on_close(self):
        if self.closing:
            return

        self.closing = True

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._disconnect_ble(user_requested=False), self.loop
            )
            future.result(timeout=3)
        except Exception:
            pass

        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = BLEApp(root)
    root.mainloop()
