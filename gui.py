"""Tk front end: playlist, transport, arrangement controls and a live keyboard.

Every player and hotkey callback arrives on a worker thread, so anything that
touches a widget is bounced back through ``root.after``.
"""

from __future__ import annotations

import ctypes
import os
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from piano import keymap
from piano.arrange import ArrangeConfig, arrange
from piano.config import Settings
from piano.hotkeys import HotkeyManager
from piano.keyboard import Keyboard
from piano.midi import MidiError, MidiSong, read_midi
from piano.player import FINISHED, PLAYING, Player
from piano.sheet import parse_sheet, read_sheet_file, sheet_report, to_sheet

MIDI_TYPES = [("MIDI files", "*.mid *.midi"), ("All files", "*.*")]
SHEET_TYPES = [("Sheet text", "*.txt"), ("All files", "*.*")]

BG = "#1e1f26"
PANEL = "#282a36"
FG = "#e6e6ec"
MUTED = "#9a9ab0"
ACCENT = "#7c9cff"
LIT = "#ff9d5c"


class SheetWindow(tk.Toplevel):
    """Paste letters in, or write the loaded song out as letters.

    Sheets are how most Roblox piano music is actually shared, and they arrive
    by clipboard from a forum post rather than as a tidy file on disk, so the
    editor treats pasted text as a first-class source.
    """

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("Virtual Piano letters")
        self.configure(bg=BG)
        self.geometry(f"{app.px(720)}x{app.px(520)}")
        self.transient(app.root)
        self._job: str | None = None

        outer = ttk.Frame(self, padding=app.px(10))
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top, text="Name").pack(side="left")
        self.name_var = tk.StringVar(value="pasted sheet")
        ttk.Entry(top, textvariable=self.name_var, width=28).pack(side="left", padx=(6, 14))
        ttk.Label(top, text="Tempo").pack(side="left")
        self.bpm_var = tk.IntVar(value=int(app.vars["sheet_bpm"].get()))
        ttk.Spinbox(top, from_=20, to=400, textvariable=self.bpm_var, width=6,
                    command=self._schedule).pack(side="left", padx=6)
        ttk.Label(top, text="bpm", style="Muted.TLabel").pack(side="left")

        body = ttk.Frame(outer)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.text = tk.Text(body, bg=PANEL, fg=FG, insertbackground=FG, wrap="word",
                            borderwidth=0, highlightthickness=0, font=("Consolas", 11),
                            padx=app.px(8), pady=app.px(8))
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.config(yscrollcommand=scroll.set)
        self.text.bind("<KeyRelease>", lambda e: self._schedule())

        self.report_var = tk.StringVar(value="Paste a sheet, or pull one from the loaded song.")
        ttk.Label(outer, textvariable=self.report_var, style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(6, 6))

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew")
        ttk.Button(buttons, text="Paste", command=self.paste).pack(side="left")
        ttk.Button(buttons, text="Copy", command=self.copy).pack(side="left", padx=4)
        ttk.Button(buttons, text="From loaded song",
                   command=self.from_song).pack(side="left")
        ttk.Button(buttons, text="Save to songs",
                   command=self.save).pack(side="left", padx=4)
        ttk.Button(buttons, text="Play", style="Accent.TButton",
                   command=lambda: self.load(play=True)).pack(side="right")
        ttk.Button(buttons, text="Load", command=lambda: self.load(play=False)).pack(
            side="right", padx=4)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.text.focus_set()

    # -- content ----------------------------------------------------------
    @property
    def content(self) -> str:
        return self.text.get("1.0", "end").strip()

    def set_content(self, text: str) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self._update_report()

    def paste(self) -> None:
        """Replace the editor contents with the clipboard, on request only.

        Deliberately not automatic on open: every letter of the alphabet is a
        piano key, so any old clipboard text parses as notes and would end up
        on screen unasked.
        """
        try:
            clip = self.app.root.clipboard_get()
        except tk.TclError:
            self.report_var.set("The clipboard is empty or does not hold text.")
            return
        if not clip.strip():
            self.report_var.set("The clipboard is empty.")
            return
        self.set_content(clip.strip())

    def copy(self) -> None:
        text = self.content
        if not text:
            return
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(text)
        self.report_var.set("Copied to the clipboard.")

    def from_song(self) -> None:
        """Convert whatever is loaded into letters, honouring the current settings."""
        if not self.app.song:
            self.report_var.set("Load a song in the main window first.")
            return
        cfg = self.app._collect()
        cfg.humanize = 0.0          # a rolled chord would only blur the grid
        bpm = self.bpm_var.get() or int(self.app.song.base_bpm)
        self.bpm_var.set(bpm)
        self.name_var.set(self.app.song.title or "song")
        self.set_content(to_sheet(arrange(self.app.song, cfg), bpm, self.app.sheet_units))

    # -- feedback ---------------------------------------------------------
    def _schedule(self) -> None:
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(250, self._update_report)

    def _update_report(self) -> None:
        self._job = None
        text = self.content
        if not text:
            self.report_var.set("Nothing here yet.")
            return
        notes, unknown = sheet_report(text)
        song = parse_sheet(text, bpm=max(self.bpm_var.get(), 1),
                           units_per_beat=self.app.sheet_units)
        m, s = divmod(int(song.duration), 60)
        message = f"{notes} notes, about {m}:{s:02d} at {self.bpm_var.get()} bpm"
        if unknown:
            shown = " ".join(unknown[:12])
            message += f"   -   ignoring {len(unknown)} unplayable characters: {shown}"
        self.report_var.set(message)

    # -- actions ----------------------------------------------------------
    def load(self, play: bool) -> bool:
        text = self.content
        if not text:
            self.report_var.set("Nothing to load.")
            return False
        bpm = max(self.bpm_var.get(), 1)
        song = parse_sheet(text, bpm=bpm, title=self.name_var.get().strip() or "pasted sheet",
                           units_per_beat=self.app.sheet_units)
        if not song.notes:
            self.report_var.set("No playable notes in that text.")
            return False
        self.app.vars["sheet_bpm"].set(bpm)
        self.app.adopt_song(song)
        if play:
            self.app.play()
        return True

    def save(self) -> None:
        text = self.content
        if not text:
            return
        name = "".join(c for c in self.name_var.get().strip() if c not in '\\/:*?"<>|')
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "songs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{name or 'sheet'}.txt")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"// {name}  -  {self.bpm_var.get()} bpm\n{text}\n")
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc))
            return
        self.app._add([path])
        self.report_var.set(f"Saved to songs\\{os.path.basename(path)}")

    def close(self) -> None:
        if self._job:
            self.after_cancel(self._job)
        self.app.sheet_window = None
        self.destroy()


def _display_scale(root: tk.Tk) -> float:
    """Make the window DPI-aware and return the scale factor.

    Without this Tk draws at 96 DPI and Windows stretches the bitmap, which
    looks soft on the high-DPI screens most people game on.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)       # per-monitor
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            return 1.0
    try:
        dpi = root.winfo_fpixels("1i")
    except tk.TclError:
        return 1.0
    scale = max(1.0, dpi / 96.0)
    root.tk.call("tk", "scaling", dpi / 72.0)
    return scale


class App:
    def px(self, value: float) -> int:
        """Logical pixels to physical, for anything Tk will not scale itself."""
        return int(round(value * self.scale))

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.keyboard = Keyboard()
        self.player = Player(self.keyboard)
        self.hotkeys = HotkeyManager()
        self.messages: queue.Queue[str] = queue.Queue()

        self.playlist: list[str] = []
        self.sheet_window: SheetWindow | None = None
        self.song: MidiSong | None = None
        self.arrangement = None
        self.dirty = False
        self._dragging = False
        self._rebuild_job: str | None = None
        self._lit: dict[int, int] = {}

        self.scale = _display_scale(root)
        root.title("Pianola")
        root.configure(bg=BG)
        root.geometry(f"{self.px(1000)}x{self.px(690)}")
        root.minsize(self.px(880), self.px(620))

        self._build_style()
        self._build_layout()
        self._apply_settings_to_widgets()
        self._wire_player()
        self._setup_hotkeys()

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._autoload()
        self._tick()

    def _autoload(self) -> None:
        """Pick up anything sitting in the songs folder next to the app."""
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "songs")
        if not os.path.isdir(folder):
            return
        found = [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                 if name.lower().endswith((".mid", ".midi", ".txt"))]
        if found:
            self._add(found)

    # ------------------------------------------------------------------ UI
    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                        bordercolor="#3a3d4d", lightcolor=PANEL, darkcolor=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG,
                        font=("Segoe UI Semibold", 11))
        style.configure("Big.TLabel", background=BG, foreground=ACCENT,
                        font=("Segoe UI Semibold", 13))
        style.configure("TButton", background="#343747", foreground=FG,
                        padding=(self.px(10), self.px(5)), borderwidth=0)
        style.map("TButton", background=[("active", "#41455a"), ("pressed", "#2c2f3d")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#12131a",
                        font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#93aeff")])
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TLabelframe", background=BG, foreground=MUTED)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
        style.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)
        style.configure("TSpinbox", fieldbackground=PANEL, foreground=FG, arrowcolor=FG)
        style.configure("TEntry", fieldbackground=PANEL, foreground=FG)

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=self.px(12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=3)
        outer.columnconfigure(1, weight=2)
        outer.rowconfigure(1, weight=1)

        # --- header -------------------------------------------------------
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.title_var = tk.StringVar(value="No song loaded")
        self.info_var = tk.StringVar(value="Load a MIDI file or a Virtual Piano sheet to begin.")
        ttk.Label(header, textvariable=self.title_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(header, textvariable=self.info_var, style="Muted.TLabel").pack(anchor="w")

        # --- playlist -----------------------------------------------------
        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        buttons = ttk.Frame(left)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(buttons, text="Add MIDI", command=self.add_midi).pack(side="left")
        ttk.Button(buttons, text="Add Sheet", command=self.add_sheet).pack(side="left", padx=4)
        ttk.Button(buttons, text="Add Folder", command=self.add_folder).pack(side="left")
        ttk.Button(buttons, text="Letters...", command=self.open_letters).pack(side="left", padx=4)
        ttk.Button(buttons, text="Remove", command=self.remove_selected).pack(side="right")

        self.listbox = tk.Listbox(left, bg=PANEL, fg=FG, selectbackground=ACCENT,
                                  selectforeground="#12131a", highlightthickness=0,
                                  borderwidth=0, activestyle="none", font=("Segoe UI", 10))
        self.listbox.grid(row=1, column=0, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.load_selected())
        self.listbox.bind("<Double-Button-1>", lambda e: self.play())

        # --- settings -----------------------------------------------------
        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky="nsew")
        self._build_settings(right)

        # --- transport ----------------------------------------------------
        transport = ttk.Frame(outer)
        transport.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 6))
        transport.columnconfigure(4, weight=1)

        self.play_button = ttk.Button(transport, text="Play  (F1)", style="Accent.TButton",
                                      command=self.toggle)
        self.play_button.grid(row=0, column=0)
        ttk.Button(transport, text="Stop  (F2)", command=self.stop).grid(row=0, column=1, padx=4)
        ttk.Button(transport, text="Restart  (F3)", command=self.restart).grid(row=0, column=2)
        ttk.Button(transport, text="Release keys  (F4)",
                   command=self.panic).grid(row=0, column=3, padx=4)

        self.seek = ttk.Scale(transport, from_=0, to=1, orient="horizontal", command=self._on_seek)
        self.seek.grid(row=0, column=4, sticky="ew", padx=10)
        self.seek.bind("<ButtonPress-1>", lambda e: setattr(self, "_dragging", True))
        self.seek.bind("<ButtonRelease-1>", self._end_seek)

        self.time_var = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(transport, textvariable=self.time_var,
                  font=("Consolas", 10)).grid(row=0, column=5)

        # --- keyboard view ------------------------------------------------
        self.canvas = tk.Canvas(outer, height=self.px(86), bg=PANEL, highlightthickness=0)
        self.canvas.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.canvas.bind("<Configure>", lambda e: self._draw_keyboard())

        # --- status -------------------------------------------------------
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_settings(self, parent: ttk.Frame) -> None:
        self.vars: dict[str, tk.Variable] = {}

        def spin(parent, key, lo, hi, live=False):
            var = tk.IntVar()
            self.vars[key] = var
            widget = ttk.Spinbox(parent, from_=lo, to=hi, increment=1, textvariable=var,
                                 width=7, command=lambda: self._changed(live))
            widget.bind("<FocusOut>", lambda e: self._changed(live))
            widget.bind("<Return>", lambda e: self._changed(live))
            return widget

        def row(parent, label, key, lo, hi, live=False, hint=""):
            line = ttk.Frame(parent)
            line.pack(fill="x", pady=2)
            ttk.Label(line, text=label, width=16).pack(side="left")
            spin(line, key, lo, hi, live).pack(side="left")
            if hint:
                ttk.Label(line, text=hint, style="Muted.TLabel").pack(side="left", padx=(5, 0))

        def check(parent, key, text, live=False):
            var = tk.BooleanVar()
            self.vars[key] = var
            return ttk.Checkbutton(parent, text=text, variable=var,
                                   command=lambda: self._changed(live))

        pitch = ttk.LabelFrame(parent, text=" Pitch ", padding=8)
        pitch.pack(fill="x")
        auto = ttk.Frame(pitch)
        auto.pack(fill="x")
        check(auto, "auto_transpose", "Auto transpose").pack(side="left")
        self.transpose_spin = spin(auto, "transpose", -24, 24)
        self.transpose_spin.pack(side="right")
        check(pitch, "octave_only", "Octaves only (keeps the song's key)").pack(anchor="w")
        check(pitch, "fold_octaves", "Fold stray notes into range").pack(anchor="w")

        texture = ttk.LabelFrame(parent, text=" Texture ", padding=8)
        texture.pack(fill="x", pady=(8, 0))
        row(texture, "Max notes at once", "max_polyphony", 1, 12)
        row(texture, "Chord window", "chord_window_ms", 0, 200, hint="ms")
        check(texture, "drop_drums", "Ignore the drum channel").pack(anchor="w")
        check(texture, "sustain_pedal", "Follow the sustain pedal").pack(anchor="w")

        feel = ttk.LabelFrame(parent, text=" Feel ", padding=8)
        feel.pack(fill="x", pady=(8, 0))
        self.speed_label = ttk.Label(feel, text="Speed  1.00x", style="Muted.TLabel")
        self.speed_label.pack(anchor="w")
        self.vars["rate"] = tk.DoubleVar(value=1.0)
        ttk.Scale(feel, from_=0.25, to=3.0, orient="horizontal", variable=self.vars["rate"],
                  command=lambda v: self._on_rate()).pack(fill="x")
        self.humanize_label = ttk.Label(feel, text="Humanise  40%", style="Muted.TLabel")
        self.humanize_label.pack(anchor="w", pady=(6, 0))
        self.vars["humanize"] = tk.DoubleVar(value=0.4)
        ttk.Scale(feel, from_=0.0, to=1.0, orient="horizontal", variable=self.vars["humanize"],
                  command=lambda v: self._on_humanize()).pack(fill="x")

        mech = ttk.LabelFrame(parent, text=" Mechanics ", padding=8)
        mech.pack(fill="x", pady=(8, 0))
        row(mech, "Min note length", "min_note_ms", 5, 400, hint="ms")
        row(mech, "Min repeat gap", "min_gap_ms", 5, 400, hint="ms")
        row(mech, "Sheet tempo", "sheet_bpm", 20, 400, hint="bpm")
        row(mech, "Sheet notes/beat", "sheet_units", 1, 8, hint="")
        row(mech, "Countdown", "countdown", 0, 15, live=True, hint="s")

        window = ttk.LabelFrame(parent, text=" Safety ", padding=8)
        window.pack(fill="x", pady=(8, 0))
        check(window, "focus_guard", "Only type into a window named", live=True).pack(anchor="w")
        self.vars["focus_filter"] = tk.StringVar(value="Roblox")
        # Editable: the presets cover Roblox and the browser tab title
        # virtualpiano.net gives its sheet pages, but any title fragment works.
        combo = ttk.Combobox(window, textvariable=self.vars["focus_filter"],
                             values=("Roblox", "Virtual Piano"))
        combo.pack(fill="x", pady=(2, 0))
        combo.bind("<<ComboboxSelected>>", lambda e: self._changed(True))
        combo.bind("<FocusOut>", lambda e: self._changed(True))
        combo.bind("<Return>", lambda e: self._changed(True))
        check(window, "loop", "Loop the song", live=True).pack(anchor="w", pady=(4, 0))

    # ------------------------------------------------- settings plumbing
    def _apply_settings_to_widgets(self) -> None:
        s = self.settings
        a = s.arrange
        self.vars["auto_transpose"].set(a.transpose is None)
        self.vars["transpose"].set(a.transpose or 0)
        for key in ("octave_only", "fold_octaves", "drop_drums", "sustain_pedal",
                    "max_polyphony", "chord_window_ms", "min_note_ms", "min_gap_ms"):
            self.vars[key].set(getattr(a, key))
        self.vars["humanize"].set(a.humanize)
        self.vars["rate"].set(s.rate)
        self.vars["loop"].set(s.loop)
        self.vars["countdown"].set(s.countdown)
        self.vars["focus_guard"].set(bool(s.focus_filter))
        self.vars["focus_filter"].set(s.focus_filter or "Roblox")
        self.vars["sheet_bpm"].set(s.sheet_bpm)
        self.vars["sheet_units"].set(s.sheet_units)
        self._on_rate()
        self.humanize_label.config(text=f"Humanise  {a.humanize * 100:.0f}%")
        self._changed(live=True)

    def _collect(self) -> ArrangeConfig:
        v = self.vars
        cfg = ArrangeConfig()
        cfg.transpose = None if v["auto_transpose"].get() else int(v["transpose"].get())
        cfg.octave_only = bool(v["octave_only"].get())
        cfg.fold_octaves = bool(v["fold_octaves"].get())
        cfg.drop_drums = bool(v["drop_drums"].get())
        cfg.sustain_pedal = bool(v["sustain_pedal"].get())
        cfg.max_polyphony = max(1, int(v["max_polyphony"].get()))
        cfg.chord_window_ms = float(v["chord_window_ms"].get())
        cfg.min_note_ms = float(v["min_note_ms"].get())
        cfg.min_gap_ms = float(v["min_gap_ms"].get())
        cfg.humanize = float(v["humanize"].get())
        return cfg

    @property
    def sheet_units(self) -> float:
        return max(1.0, float(self.vars["sheet_units"].get()))

    def _push_live_settings(self) -> None:
        self.player.rate = float(self.vars["rate"].get())
        self.player.loop = bool(self.vars["loop"].get())
        self.player.countdown = float(self.vars["countdown"].get())
        guard = bool(self.vars["focus_guard"].get())
        self.player.focus_filter = self.vars["focus_filter"].get().strip() if guard else None

    def _changed(self, live: bool) -> None:
        self._push_live_settings()
        self.transpose_spin.config(
            state="disabled" if self.vars["auto_transpose"].get() else "normal")
        if live:
            return
        if self.player.running:
            self.dirty = True
            self.status_var.set("Settings changed - they take effect when you restart the song.")
        elif self.song:
            # Debounced: dragging a slider would otherwise re-arrange per pixel.
            if self._rebuild_job:
                self.root.after_cancel(self._rebuild_job)
            self._rebuild_job = self.root.after(180, self.rebuild)

    def _on_rate(self) -> None:
        rate = float(self.vars["rate"].get())
        self.speed_label.config(text=f"Speed  {rate:.2f}x")
        self.player.rate = rate

    def _on_humanize(self) -> None:
        value = float(self.vars["humanize"].get())
        self.humanize_label.config(text=f"Humanise  {value * 100:.0f}%")
        self._changed(live=False)

    # ------------------------------------------------------------ library
    def _add(self, paths) -> None:
        added = 0
        for path in paths:
            if path and path not in self.playlist:
                self.playlist.append(path)
                self.listbox.insert("end", os.path.basename(path))
                added += 1
        if added and self.listbox.size() == added:
            self.listbox.selection_set(0)
            self.load_selected()

    def add_midi(self) -> None:
        paths = filedialog.askopenfilenames(title="Add MIDI files", filetypes=MIDI_TYPES,
                                            initialdir=self.settings.last_folder or None)
        if paths:
            self.settings.last_folder = os.path.dirname(paths[0])
        self._add(paths)

    def add_sheet(self) -> None:
        paths = filedialog.askopenfilenames(title="Add Virtual Piano sheets",
                                            filetypes=SHEET_TYPES,
                                            initialdir=self.settings.last_folder or None)
        if paths:
            self.settings.last_folder = os.path.dirname(paths[0])
        self._add(paths)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Add every song in a folder",
                                         initialdir=self.settings.last_folder or None)
        if not folder:
            return
        self.settings.last_folder = folder
        found = [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                 if name.lower().endswith((".mid", ".midi", ".txt"))]
        if not found:
            messagebox.showinfo("Nothing found", "No .mid, .midi or .txt files in that folder.")
        self._add(found)

    def remove_selected(self) -> None:
        for index in reversed(self.listbox.curselection()):
            self.listbox.delete(index)
            del self.playlist[index]

    def load_selected(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        path = self.playlist[selection[0]]
        try:
            if path.lower().endswith((".mid", ".midi")):
                self.song = read_midi(path)
            else:
                self.song = read_sheet_file(path, float(self.vars["sheet_bpm"].get()),
                                            units_per_beat=self.sheet_units)
        except (MidiError, OSError, ValueError) as exc:
            messagebox.showerror("Could not read that file", str(exc))
            self.song = None
            return
        if not self.song.title:
            self.song.title = os.path.splitext(os.path.basename(path))[0]
        self.title_var.set(self.song.title or os.path.basename(path))
        self.rebuild()

    def adopt_song(self, song) -> None:
        """Take a song that came from somewhere other than the playlist."""
        self.song = song
        self.listbox.selection_clear(0, "end")
        self.title_var.set(song.title or "pasted sheet")
        self.rebuild()

    def open_letters(self) -> None:
        if self.sheet_window is not None and self.sheet_window.winfo_exists():
            self.sheet_window.lift()
            self.sheet_window.focus_force()
            return
        self.sheet_window = SheetWindow(self)

    def rebuild(self) -> None:
        self._rebuild_job = None
        if not self.song:
            return
        self.arrangement = arrange(self.song, self._collect())
        self.player.load(self.arrangement)
        self.dirty = False
        self.info_var.set(self.arrangement.summary())
        self.seek.config(to=max(self.arrangement.duration, 0.01))
        self.seek.set(0)
        if self.arrangement.coverage < 0.75:
            self.status_var.set("Heads up: a lot of this song sits outside the 61-key range.")
        else:
            self.status_var.set("Ready. Focus the game window, then press F1.")

    # ---------------------------------------------------------- transport
    def play(self) -> None:
        if not self.arrangement:
            self.status_var.set("Load a song first.")
            return
        if self.dirty:
            self.rebuild()
        self.player.play(from_start=True)

    def toggle(self) -> None:
        if not self.arrangement:
            self.status_var.set("Load a song first.")
            return
        if not self.player.running and self.dirty:
            self.rebuild()
        self.player.toggle()

    def stop(self) -> None:
        self.player.stop()

    def restart(self) -> None:
        self.player.stop()
        self.play()

    def panic(self) -> None:
        self.player.panic()

    def next_song(self) -> None:
        if not self.playlist:
            return
        selection = self.listbox.curselection()
        index = (selection[0] + 1) % len(self.playlist) if selection else 0
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self.load_selected()

    def _on_seek(self, value: str) -> None:
        if self._dragging:
            self.time_var.set(f"{self._clock(float(value))} / {self._clock(self.player.duration)}")

    def _end_seek(self, _event) -> None:
        if self._dragging:
            self._dragging = False
            self.player.seek(float(self.seek.get()))

    # ------------------------------------------------------------ hotkeys
    def _setup_hotkeys(self) -> None:
        keys = self.settings.hotkeys
        actions = {
            "play_pause": self.toggle,
            "stop": self.stop,
            "restart": self.restart,
            "panic": self.panic,
            "speed_down": lambda: self._nudge_rate(-0.05),
            "speed_up": lambda: self._nudge_rate(0.05),
        }
        for name, action in actions.items():
            combo = keys.get(name)
            if combo:
                self.hotkeys.bind(combo, lambda a=action: self.root.after(0, a))
        self.hotkeys.start()
        if self.hotkeys.failed:
            self.messages.put("Hotkeys already taken by another app: "
                              + ", ".join(self.hotkeys.failed))

    def _nudge_rate(self, delta: float) -> None:
        rate = min(3.0, max(0.25, float(self.vars["rate"].get()) + delta))
        self.vars["rate"].set(rate)
        self._on_rate()

    def _wire_player(self) -> None:
        self.player.on_message = self.messages.put
        self.player.on_state = lambda state: self.messages.put(f"\x00{state}")

    # ------------------------------------------------------- keyboard view
    def _draw_keyboard(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        self._lit.clear()
        width = max(canvas.winfo_width(), 100)
        whites = [n for n in range(keymap.NOTE_MIN, keymap.NOTE_MAX + 1)
                  if not keymap.is_sharp(n)]
        step = width / len(whites)
        top = self.px(6)
        height = self.px(74)
        font = ("Consolas", 7)
        white_index = {n: i for i, n in enumerate(whites)}

        for note in whites:
            x = white_index[note] * step
            rect = canvas.create_rectangle(x, top, x + step - 1, height,
                                           fill="#f2f2f6", outline="#c9c9d4")
            self._lit[note] = rect
            canvas.create_text(x + step / 2, height - self.px(9),
                               text=keymap.NOTE_TO_KEY[note], fill="#71718a", font=font)
        for note in range(keymap.NOTE_MIN, keymap.NOTE_MAX + 1):
            if not keymap.is_sharp(note):
                continue
            left = white_index.get(note - 1)
            if left is None:
                continue
            x = (left + 1) * step
            rect = canvas.create_rectangle(x - step * 0.32, top, x + step * 0.32, height * 0.62,
                                           fill="#23242e", outline="#12131a")
            self._lit[note] = rect
            canvas.create_text(x, height * 0.5, text=keymap.NOTE_TO_KEY[note],
                               fill="#8a8aa4", font=font)

    def _refresh_keyboard(self) -> None:
        held = self.keyboard.held
        for note, rect in self._lit.items():
            char = keymap.NOTE_TO_KEY[note]
            on = char in held
            if keymap.is_sharp(note):
                self.canvas.itemconfig(rect, fill=LIT if on else "#23242e")
            else:
                self.canvas.itemconfig(rect, fill=LIT if on else "#f2f2f6")

    # ----------------------------------------------------------- main loop
    @staticmethod
    def _clock(seconds: float) -> str:
        m, s = divmod(int(max(seconds, 0)), 60)
        return f"{m}:{s:02d}"

    def _tick(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if message.startswith("\x00"):
                state = message[1:]
                self.play_button.config(text="Pause  (F1)" if state == PLAYING else "Play  (F1)")
                if state == FINISHED and self.player.loop is False:
                    self.status_var.set("Finished.")
            else:
                self.status_var.set(message)

        position = self.player.position
        if not self._dragging:
            self.seek.set(position)
            self.time_var.set(f"{self._clock(position)} / {self._clock(self.player.duration)}")
        self._refresh_keyboard()
        self.root.after(40, self._tick)

    def quit(self) -> None:
        self.player.stop()
        self.hotkeys.stop()
        s = self.settings
        s.arrange = self._collect()
        s.rate = float(self.vars["rate"].get())
        s.loop = bool(self.vars["loop"].get())
        s.countdown = float(self.vars["countdown"].get())
        s.focus_filter = (self.vars["focus_filter"].get().strip()
                          if self.vars["focus_guard"].get() else "")
        s.sheet_bpm = float(self.vars["sheet_bpm"].get())
        s.sheet_units = self.sheet_units
        s.save()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
