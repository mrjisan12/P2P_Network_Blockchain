"""
main.py - Tkinter user interface for the P2P network.

Run with:   python main.py

Network threads must never touch Tkinter widgets directly (Tkinter is not
thread-safe). Instead, P2PNode events are put into a queue, and the GUI
thread drains that queue every 100 ms using root.after().

Layout notes:
  * The window uses grid(). Only the middle row (peer list + log) has a
    weight, so when the window shrinks, only that area shrinks: the send
    text / send file controls always stay visible.
  * On narrow windows (< 820 px) the cards re-flow from side-by-side to
    stacked, so everything still fits.
"""

import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from p2p_node import P2PNode, DOWNLOADS_DIR

# ----------------------------------------------------------------------
# Colour palette
# ----------------------------------------------------------------------
BG = "#eef2f9"            # window background
CARD = "#ffffff"          # card background
BORDER = "#dde3ee"
TEXT = "#1e293b"
MUTED = "#64748b"
ACCENT = "#4f46e5"        # indigo
ACCENT_HOVER = "#4338ca"
ACCENT_PRESS = "#3730a3"
SUCCESS = "#16a34a"
SUCCESS_HOVER = "#15803d"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"
WARN = "#f59e0b"
HEADER_BG = "#1e1b4b"     # deep indigo header
LOG_BG = "#0f172a"        # dark console
LOG_FG = "#e2e8f0"

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI", 15, "bold")
FONT_CARD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 10)

NARROW_WIDTH = 820        # below this width the layout stacks vertically


def get_local_ip():
    """Best-effort guess of this computer's LAN IP (shown so others can connect)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))     # UDP connect sends no packets
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def setup_styles(root):
    style = ttk.Style(root)
    style.theme_use("clam")      # clam lets us recolour buttons, entries, etc.

    style.configure(".", font=FONT, background=BG, foreground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
    style.configure("CardTitle.TLabel", background=CARD, foreground=ACCENT, font=FONT_CARD)

    style.configure(
        "TEntry", fieldbackground="#f8fafc", foreground=TEXT, padding=6,
        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, insertcolor=TEXT,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
        fieldbackground=[("disabled", "#eef1f6")], foreground=[("disabled", MUTED)],
    )

    def button(name, bg, hover, press):
        style.configure(
            name, background=bg, foreground="white", font=FONT_BOLD, padding=(14, 6),
            borderwidth=0, focusthickness=0, bordercolor=bg, lightcolor=bg, darkcolor=bg,
        )
        style.map(
            name,
            background=[("disabled", "#cbd5e1"), ("pressed", press), ("active", hover)],
            bordercolor=[("disabled", "#cbd5e1"), ("pressed", press), ("active", hover)],
            lightcolor=[("disabled", "#cbd5e1"), ("pressed", press), ("active", hover)],
            darkcolor=[("disabled", "#cbd5e1"), ("pressed", press), ("active", hover)],
            foreground=[("disabled", "#f1f5f9")],
        )

    button("Accent.TButton", ACCENT, ACCENT_HOVER, ACCENT_PRESS)
    button("Success.TButton", SUCCESS, SUCCESS_HOVER, "#166534")
    button("Danger.TButton", DANGER, DANGER_HOVER, "#991b1b")

    style.configure(
        "Accent.Horizontal.TProgressbar", troughcolor="#e2e8f0", background=ACCENT,
        bordercolor="#e2e8f0", lightcolor=ACCENT, darkcolor=ACCENT, thickness=8,
    )
    style.configure(
        "Vertical.TScrollbar", background="#334155", troughcolor=LOG_BG,
        bordercolor=LOG_BG, arrowcolor=LOG_FG, lightcolor="#334155", darkcolor="#334155",
    )


class P2PApp:
    def __init__(self, root):
        self.root = root
        self.root.title("UAP P2P Network")
        self.root.geometry("980x680")
        self.root.configure(bg=BG)
        setup_styles(root)

        self.events = queue.Queue()
        self.node = P2PNode(on_event=lambda kind, data: self.events.put((kind, data)))
        self.peer_ids = []          # listbox index -> peer_id
        self.layout_mode = None     # "wide" or "narrow"
        scale = root.winfo_fpixels("1i") / 96      # 1.0 at 100% Windows display scaling
        root.minsize(int(420 * scale), int(400 * scale))

        self._build_ui()
        self._set_running(False)
        self._apply_layout(980)
        self.root.bind("<Configure>", self._on_resize)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._process_events)

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _card(self, parent, title):
        """A white card with a thin border and a coloured title. Returns (card, body)."""
        card = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Frame(head, bg=ACCENT, width=4, height=16).pack(side="left", padx=(0, 8))
        title_lbl = ttk.Label(head, text=title, style="CardTitle.TLabel")
        title_lbl.pack(side="left")
        body = ttk.Frame(card, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        card.title_label = title_lbl
        return card, body

    def _build_ui(self):
        # --- Header ---------------------------------------------------
        header = self.header = tk.Frame(self.root, bg=HEADER_BG)
        tk.Label(
            header, text="⇄  UAP P2P Network", bg=HEADER_BG, fg="white", font=FONT_TITLE
        ).pack(side="left", padx=16, pady=10)
        self.status_chip = tk.Label(
            header, text="●  Offline", bg="#334155", fg="white", font=FONT_BOLD, padx=12, pady=3
        )
        self.status_chip.pack(side="right", padx=16)

        # --- Top: My Peer + Connect -----------------------------------
        self.top = ttk.Frame(self.root)

        self.my_card, my = self._card(self.top, "My Peer")
        row = ttk.Frame(my, style="Card.TFrame")
        row.pack(fill="x")
        # Buttons are packed first so they keep their full size in a small window
        self.stop_btn = ttk.Button(row, text="Stop", width=-5, style="Danger.TButton", command=self.stop_peer)
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(row, text="Start Peer", width=-5, style="Success.TButton", command=self.start_peer)
        self.start_btn.pack(side="right", padx=(0, 6))
        self.port_var = tk.StringVar(value="5000")
        self.port_entry = ttk.Entry(row, textvariable=self.port_var, width=7)
        self.port_entry.pack(side="right", padx=(6, 12))
        ttk.Label(row, text="Port", style="Card.TLabel").pack(side="right")
        ttk.Label(row, text="Name", style="Card.TLabel").pack(side="left")
        self.name_var = tk.StringVar(value="Alice")
        self.name_entry = ttk.Entry(row, textvariable=self.name_var, width=10)
        self.name_entry.pack(side="left", padx=(6, 12), fill="x", expand=True)

        self.info_var = tk.StringVar(value=f"Not running  ·  Your IP: {get_local_ip()}")
        self.info_label = ttk.Label(my, textvariable=self.info_var, style="Muted.TLabel")
        self.info_label.pack(fill="x", pady=(8, 0))

        self.conn_card, conn = self._card(self.top, "Connect to Another Peer")
        row = ttk.Frame(conn, style="Card.TFrame")
        row.pack(fill="x")
        self.connect_btn = ttk.Button(row, text="Connect", style="Accent.TButton", command=self.connect_peer)
        self.connect_btn.pack(side="right")
        self.remote_port_var = tk.StringVar(value="5001")
        rport_entry = ttk.Entry(row, textvariable=self.remote_port_var, width=7)
        rport_entry.pack(side="right", padx=(6, 12))
        rport_entry.bind("<Return>", lambda e: self.connect_peer())
        ttk.Label(row, text="Port", style="Card.TLabel").pack(side="right")
        ttk.Label(row, text="IP", style="Card.TLabel").pack(side="left")
        self.ip_var = tk.StringVar(value="127.0.0.1")
        ip_entry = ttk.Entry(row, textvariable=self.ip_var, width=12)
        ip_entry.pack(side="left", padx=(6, 12), fill="x", expand=True)
        ip_entry.bind("<Return>", lambda e: self.connect_peer())
        self.hint_label = ttk.Label(
            conn, text="Use 127.0.0.1 for a peer on this computer, or the LAN IP of another computer.",
            style="Muted.TLabel",
        )
        self.hint_label.pack(fill="x", pady=(8, 0))

        # --- Middle: peers + log (the only part that grows/shrinks) ---
        self.middle = ttk.Frame(self.root)

        self.peers_card, peers_body = self._card(self.middle, "Connected Peers (0)")
        list_wrap = tk.Frame(peers_body, bg=CARD)
        list_wrap.pack(fill="both", expand=True)
        self.peer_list = tk.Listbox(
            list_wrap, height=4, width=30, exportselection=False, activestyle="none",
            font=FONT, bg="#f8fafc", fg=TEXT, bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            selectbackground=ACCENT, selectforeground="white",
        )
        self.peer_list.pack(fill="both", expand=True)
        self.peer_list.bind("<<ListboxSelect>>", lambda e: self._update_target())
        self.empty_label = tk.Label(
            list_wrap, text="No peers connected yet", bg="#f8fafc", fg=MUTED, font=("Segoe UI", 9, "italic")
        )
        self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.disconnect_btn = ttk.Button(
            peers_body, text="Disconnect", style="Danger.TButton", command=self.disconnect_peer
        )
        self.disconnect_btn.pack(fill="x", pady=(8, 0))

        self.log_card, log_body = self._card(self.middle, "Messages / Events")
        log_wrap = tk.Frame(log_body, bg=LOG_BG)
        log_wrap.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_wrap, height=4, state="disabled", wrap="word", font=FONT_MONO,
            bg=LOG_BG, fg=LOG_FG, bd=0, padx=10, pady=8, insertbackground=LOG_FG,
            selectbackground="#334155",
        )
        scroll = ttk.Scrollbar(log_wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_config("time", foreground="#64748b")
        self.log.tag_config("system", foreground="#94a3b8")
        self.log.tag_config("error", foreground="#f87171")
        self.log.tag_config("in_name", foreground="#60a5fa", font=("Consolas", 10, "bold"))
        self.log.tag_config("out_name", foreground="#4ade80", font=("Consolas", 10, "bold"))
        self.log.tag_config("file", foreground="#fbbf24")
        self.log.tag_config("msg", foreground=LOG_FG)

        # --- Bottom: send text + send file (fixed height, always visible) ---
        self.send_card, send = self._card(self.root, "Send")

        self.target_var = tk.StringVar()
        ttk.Label(send, textvariable=self.target_var, style="Muted.TLabel").pack(fill="x", pady=(0, 6))

        text_row = ttk.Frame(send, style="Card.TFrame")
        text_row.pack(fill="x")
        self.send_btn = ttk.Button(text_row, text="Send  ➤", style="Accent.TButton", command=self.send_text)
        self.send_btn.pack(side="right", padx=(8, 0))
        self.msg_var = tk.StringVar()
        self.msg_entry = ttk.Entry(text_row, textvariable=self.msg_var)
        self.msg_entry.pack(side="left", fill="x", expand=True)
        self.msg_entry.bind("<Return>", lambda e: self.send_text())

        file_row = ttk.Frame(send, style="Card.TFrame")
        file_row.pack(fill="x", pady=(8, 0))
        # Pack the button first so it keeps its space even in a tiny window
        self.file_btn = ttk.Button(
            file_row, text="Choose File & Send  ⬆", style="Accent.TButton", command=self.send_file
        )
        self.file_btn.pack(side="right", padx=(8, 0))
        self.progress = ttk.Progressbar(
            file_row, length=140, mode="determinate", style="Accent.Horizontal.TProgressbar"
        )
        self.progress.pack(side="right", padx=(8, 0))
        ttk.Label(
            file_row, text="Text, image, audio, video, PDF, ZIP, …", style="Muted.TLabel"
        ).pack(side="left", fill="x", expand=True)

        # --- Status bar ----------------------------------------------
        self.status_var = tk.StringVar(value="Ready")
        self.status_bar = tk.Label(
            self.root, textvariable=self.status_var, bg="#e2e8f0", fg=MUTED,
            anchor="w", padx=12, pady=3, font=("Segoe UI", 9),
        )

        # Pack order = priority when the window is small. Widgets packed
        # first keep their size; the middle area (packed last) shrinks first.
        # This guarantees the Send card is always visible.
        self.header.pack(side="top", fill="x")
        self.status_bar.pack(side="bottom", fill="x")
        self.send_card.pack(side="bottom", fill="x", padx=12, pady=12)
        self.top.pack(side="top", fill="x", padx=12, pady=(12, 0))
        self.middle.pack(side="top", fill="both", expand=True, padx=12, pady=(12, 0))

        self._update_target()

    # ------------------------------------------------------------------
    # Responsive layout
    # ------------------------------------------------------------------

    def _on_resize(self, event):
        if event.widget is self.root:
            self._apply_layout(event.width)

    def _apply_layout(self, width):
        mode = "wide" if width >= NARROW_WIDTH else "narrow"
        # Wrap long info lines to the available card width
        wrap = max(200, (width // 2 if mode == "wide" else width) - 60)
        self.info_label.configure(wraplength=wrap)
        self.hint_label.configure(wraplength=wrap)
        if mode == self.layout_mode:
            return
        self.layout_mode = mode

        # Compact mode: hide the help text and shorten the peer list
        if mode == "narrow":
            self.hint_label.pack_forget()
            self.peer_list.configure(height=3)
            self.start_btn.configure(text="Start")
        else:
            self.hint_label.pack(fill="x", pady=(8, 0))
            self.peer_list.configure(height=4)
            self.start_btn.configure(text="Start Peer")

        for w in (self.my_card, self.conn_card, self.peers_card, self.log_card):
            w.grid_forget()
        for frame in (self.top, self.middle):
            for i in range(2):
                frame.columnconfigure(i, weight=0, minsize=0, uniform="")
                frame.rowconfigure(i, weight=0, minsize=0)

        if mode == "wide":
            # [ My Peer | Connect ]      [ Peers | Log ]
            self.top.columnconfigure(0, weight=1, uniform="top")
            self.top.columnconfigure(1, weight=1, uniform="top")
            self.my_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            self.conn_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

            self.middle.columnconfigure(0, weight=0, minsize=260)
            self.middle.columnconfigure(1, weight=1)
            self.middle.rowconfigure(0, weight=1)
            self.peers_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            self.log_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        else:
            # Everything stacked in one column
            self.top.columnconfigure(0, weight=1)
            self.my_card.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            self.conn_card.grid(row=1, column=0, sticky="ew", pady=(6, 0))

            self.middle.columnconfigure(0, weight=1)
            self.middle.rowconfigure(1, weight=1)       # log grows, peer list stays compact
            self.peers_card.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
            self.log_card.grid(row=1, column=0, sticky="nsew", pady=(6, 0))

    def _set_running(self, running):
        on = "normal" if running else "disabled"
        off = "disabled" if running else "normal"
        self.start_btn.config(state=off)
        self.name_entry.config(state=off)
        self.port_entry.config(state=off)
        for w in (self.stop_btn, self.connect_btn, self.send_btn, self.file_btn, self.disconnect_btn):
            w.config(state=on)
        if running:
            self.status_chip.config(text="●  Online", bg=SUCCESS)
        else:
            self.status_chip.config(text="●  Offline", bg="#334155")

    # ------------------------------------------------------------------
    # Logging / event handling
    # ------------------------------------------------------------------

    def write_log(self, *parts):
        """parts = (text, tag), (text, tag), ...  A timestamp is added automatically."""
        self.log.config(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  "), "time")
        for text, tag in parts:
            self.log.insert("end", text, tag)
        self.log.insert("end", "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _process_events(self):
        """Runs on the GUI thread; applies events produced by network threads."""
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)

    def _handle_event(self, kind, data):
        if kind == "system":
            self.write_log((f"[SYSTEM] {data}", "system"))
            self.status_var.set(data)
        elif kind == "error":
            self.show_error(data)
        elif kind == "text_in":
            peer, text = data
            self.write_log((f"{peer.peer_name} → You: ", "in_name"), (text, "msg"))
        elif kind == "text_out":
            peer, text = data
            self.write_log((f"You → {peer.peer_name}: ", "out_name"), (text, "msg"))
        elif kind == "file_in":
            peer, filename, path, size = data
            self.write_log(
                (f"{peer.peer_name} → You: ", "in_name"),
                (f"[FILE] received {filename} ({format_size(size)})", "file"),
            )
            self.status_var.set(f"Saved to {os.path.relpath(path, os.path.dirname(DOWNLOADS_DIR))}")
            self.progress["value"] = 0
        elif kind == "file_out":
            peer, filename, size = data
            self.write_log(
                (f"You → {peer.peer_name}: ", "out_name"),
                (f"[FILE] sent {filename} ({format_size(size)})", "file"),
            )
            self.status_var.set(f"Sent: {filename}")
            self.progress["value"] = 0
        elif kind == "progress":
            label, done, total = data
            self.progress["value"] = 100 if total == 0 else done * 100 / total
            self.status_var.set(f"{label}: {format_size(done)} / {format_size(total)}")
        elif kind == "peers":
            self._refresh_peers(data)

    def _refresh_peers(self, peers):
        selected = self.selected_peer_id()
        self.peer_list.delete(0, "end")
        self.peer_ids = []
        for peer in peers:
            self.peer_list.insert("end", f"  ● {peer.label()}")
            self.peer_ids.append(peer.peer_id)
        if selected in self.peer_ids:
            self.peer_list.selection_set(self.peer_ids.index(selected))
        elif len(self.peer_ids) == 1:
            self.peer_list.selection_set(0)   # convenience: auto-select the only peer

        self.peers_card.title_label.config(text=f"Connected Peers ({len(peers)})")
        if peers:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self._update_target()

    def _update_target(self):
        peer_id = self.selected_peer_id()
        peer = self.node.peers.get(peer_id) if peer_id else None
        if peer:
            self.target_var.set(f"Sending to:  {peer.peer_name} [{peer.peer_id}]")
        else:
            self.target_var.set("Select a peer from the list to send a message or file")

    def selected_peer_id(self):
        sel = self.peer_list.curselection()
        if not sel or sel[0] >= len(self.peer_ids):
            return None
        return self.peer_ids[sel[0]]

    def show_error(self, message):
        self.write_log((f"[ERROR] {message}", "error"))
        self.status_var.set(f"Error: {message}")

    # ------------------------------------------------------------------
    # Button actions
    # ------------------------------------------------------------------

    def start_peer(self):
        try:
            self.node.start(self.name_var.get(), self.port_var.get())
        except (ValueError, OSError) as e:
            self.show_error(f"Could not start peer: {e}")
            return
        self._set_running(True)
        self.root.title(f"UAP P2P Network - {self.node.name}")
        self.info_var.set(
            f"{self.node.name}  ·  ID: {self.node.peer_id}  ·  Port: {self.node.port}  ·  Your IP: {get_local_ip()}"
        )

    def stop_peer(self):
        self.node.stop()
        self._set_running(False)
        self.root.title("UAP P2P Network")
        self.info_var.set(f"Not running  ·  Your IP: {get_local_ip()}")

    def connect_peer(self):
        if not self.node.running:
            self.show_error("Start your peer first")
            return
        ip, port = self.ip_var.get(), self.remote_port_var.get()
        self.status_var.set(f"Connecting to {ip}:{port} ...")

        # connect() can block for a few seconds, so run it off the GUI thread
        def worker():
            try:
                self.node.connect(ip, port)
            except (ValueError, ConnectionError, OSError) as e:
                self.events.put(("error", f"Connection failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def disconnect_peer(self):
        peer_id = self.selected_peer_id()
        if not peer_id:
            self.show_error("Select a peer to disconnect")
            return
        self.node.disconnect(peer_id)

    def send_text(self):
        try:
            self.node.send_text(self.selected_peer_id(), self.msg_var.get())
            self.msg_var.set("")
        except (ValueError, ConnectionError) as e:
            self.show_error(str(e))

    def send_file(self):
        peer_id = self.selected_peer_id()
        if not peer_id:
            self.show_error("No peer selected")
            return
        path = filedialog.askopenfilename(title="Choose a file to send")
        if not path:
            return
        try:
            self.node.send_file_async(peer_id, path)
            self.write_log((f"[SYSTEM] Sending {os.path.basename(path)} ...", "system"))
        except (ValueError, OSError) as e:
            self.show_error(str(e))

    def on_close(self):
        if self.node.running and self.node.get_peers():
            if not messagebox.askokcancel("Quit", "Disconnect from all peers and quit?"):
                return
        self.node.stop()
        self.root.destroy()


def format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main():
    root = tk.Tk()
    P2PApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
