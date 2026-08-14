#!/usr/bin/env python3
"""HGAC USGS Houston B24 DEM downloader + ArcGIS 2-ft/5-ft contour GUI."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BUILD = "2026-08-14-hgac-usgs-contour-gui-v1.4-seamless-batch"
PROJECT_ROOT = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/OPR/Projects/TX_Houston_B24"
SUBPROJECTS = [f"TX_Houston_{i}_B24" for i in range(1, 5)]
LINK_LIST_URLS = {p: f"{PROJECT_ROOT}/{p}/0_file_download_links.txt" for p in SUBPROJECTS}
BAD_LOCAL_TOKENS = (
    "intensity", "intensity_images", "dsm", "ndsm", "chm", "canopy", "hillshade",
    "slope", "aspect", "preview", "point_cloud", "classified", "reflectance",
)
GOOD_LOCAL_TOKENS = ("bare_earth", "be_rasters", "bareearth", "dem")
USER_AGENT = "HGAC-USGS-Contour-GUI/1.4"


def normalize_tile(text: str) -> str:
    value = text.strip().strip('"').strip("'")
    if not value:
        return ""
    value = Path(value).name
    value = re.sub(r"\.(las|laz|tif|tiff)$", "", value, flags=re.I)
    value = re.sub(r"^USGS_OPR_TX_Houston_B24_", "", value, flags=re.I)
    m = re.search(r"(\d{2}[A-Z]{3}\d{6})", value.upper())
    return m.group(1) if m else value.upper()


def parse_tile_list(text: str) -> list[str]:
    parts = re.split(r"[\s,;]+", text)
    out = []
    seen = set()
    for p in parts:
        tile = normalize_tile(p)
        if tile and tile not in seen:
            seen.add(tile)
            out.append(tile)
    return out


def expected_tiff_name(tile: str) -> str:
    return f"USGS_OPR_TX_Houston_B24_{tile}.tif"


def request_text(url: str, timeout: int = 25) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def url_exists(url: str, timeout: int = 15) -> bool:
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except Exception:
        return False


def parse_index_for_tile(text: str, tile: str) -> list[str]:
    target = expected_tiff_name(tile).lower()
    candidates = []
    for raw in text.splitlines():
        line = raw.strip().strip('"').strip("'")
        if not line or not line.lower().startswith(("http://", "https://")):
            continue
        path = urlparse(line).path
        filename = Path(path).name.lower()
        if filename == target and "/tiff/" in path.lower():
            candidates.insert(0, line)
        elif tile.lower() in filename and filename.endswith((".tif", ".tiff")) and "/tiff/" in path.lower():
            candidates.append(line)
    # stable de-duplication
    return list(dict.fromkeys(candidates))


def local_dem_score(path: Path, tile: str) -> tuple[int, int, str]:
    s = str(path).lower().replace("\\", "/")
    filename = path.name.lower()
    score = 0
    if filename == f"{tile.lower()}.tif":
        score += 100
    if filename == expected_tiff_name(tile).lower():
        score += 100
    for token in GOOD_LOCAL_TOKENS:
        if token in s:
            score += 25
    for token in BAD_LOCAL_TOKENS:
        if token in s:
            score -= 200
    return (-score, len(s), s)


def find_local_dem(root: str, tile: str) -> tuple[str | None, list[str]]:
    if not root:
        return None, []
    r = Path(root)
    if not r.exists():
        return None, []
    matches = []
    tile_lower = tile.lower()
    for p in r.rglob("*.tif"):
        name = p.name.lower()
        if tile_lower in name:
            sp = str(p).lower().replace("\\", "/")
            if any(t in sp for t in BAD_LOCAL_TOKENS):
                continue
            matches.append(p)
    matches.sort(key=lambda p: local_dem_score(p, tile))
    return (str(matches[0]) if matches else None, [str(p) for p in matches[:10]])


def is_valid_tiff_file(path: Path) -> tuple[bool, str]:
    """Validate a downloaded TIFF without imposing an arbitrary large-file threshold."""
    try:
        size = path.stat().st_size
        if size < 4096:
            return False, f"file is only {size} bytes"
        with path.open("rb") as f:
            header = f.read(4)
        if header not in (b"II*\x00", b"MM\x00*"):
            return False, f"invalid TIFF signature {header!r}"
        return True, f"valid TIFF signature; {size} bytes"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def default_arcgis_python() -> str:
    candidates = [
        Path(r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HGAC Contour Studio v1.4 — Seamless Multi-Tile Contours")
        self.geometry("1380x900")
        self.minsize(1080, 720)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.worker_proc: subprocess.Popen | None = None
        self.index_cache: dict[str, str] = {}
        self.status_rows: dict[str, str] = {}

        self.script_dir = Path(__file__).resolve().parent
        self.worker_script = self.script_dir / "hgac_contour_arcgis_worker.py"
        self.batch_worker_script = self.script_dir / "hgac_contour_arcgis_batch_worker.py"

        self.output_var = tk.StringVar(value=str(Path.home() / "HGAC_Contours"))
        self.cache_var = tk.StringVar(value=str(Path.home() / "HGAC_Contours" / "_dem_cache"))
        self.local_root_var = tk.StringVar(value=r"F:\300203")
        self.arcgis_python_var = tk.StringVar(value=default_arcgis_python())
        self.mode_var = tk.StringVar(value="USGS first; local fallback")
        self.dem_z_units_var = tk.StringVar(value="meters")
        self.base_2ft_var = tk.StringVar(value="0")
        self.base_5ft_var = tk.StringVar(value="0")
        self.overwrite_var = tk.BooleanVar(value=True)
        self.keep_download_var = tk.BooleanVar(value=True)
        self.keep_work_mosaics_var = tk.BooleanVar(value=False)
        self.hide_worker_console_var = tk.BooleanVar(value=True)
        self.app_status_var = tk.StringVar(value="Ready")
        self.tiles_stat_var = tk.StringVar(value="0")
        self.strategy_stat_var = tk.StringVar(value="USGS → local")
        self.output_stat_var = tk.StringVar(value="2/5 ft + merged")
        self.engine_stat_var = tk.StringVar(value="Not checked")
        self.last_run_dir: Path | None = None

        self._configure_style()
        self._build_ui()
        self.after(100, self._poll_queue)

    def _configure_style(self):
        self.COLORS = {
            "navy": "#102A43",
            "navy_dark": "#0B1F33",
            "accent": "#0E9F9A",
            "accent_dark": "#087F7A",
            "bg": "#F3F6F9",
            "card": "#FFFFFF",
            "text": "#17324D",
            "muted": "#6B7F93",
            "border": "#D9E2EC",
            "success": "#2F855A",
            "warning": "#B7791F",
            "danger": "#C53030",
            "log_bg": "#0D1B2A",
            "log_fg": "#D9E6F2",
        }
        self.configure(bg=self.COLORS["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.COLORS["bg"])
        style.configure("Card.TFrame", background=self.COLORS["card"])
        style.configure("TLabel", background=self.COLORS["bg"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=self.COLORS["card"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=self.COLORS["card"], foreground=self.COLORS["navy"], font=("Segoe UI Semibold", 12))
        style.configure("Muted.TLabel", background=self.COLORS["card"], foreground=self.COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), foreground="white", background=self.COLORS["accent"], padding=(16, 9), borderwidth=0)
        style.map("Accent.TButton", background=[("active", self.COLORS["accent_dark"]), ("disabled", "#9FB8B7")])
        style.configure("Secondary.TButton", font=("Segoe UI Semibold", 9), foreground=self.COLORS["navy"], background="#E8EEF4", padding=(12, 8), borderwidth=0)
        style.map("Secondary.TButton", background=[("active", "#DCE6EF")])
        style.configure("Danger.TButton", font=("Segoe UI Semibold", 9), foreground="white", background=self.COLORS["danger"], padding=(12, 8), borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#9B2C2C"), ("disabled", "#D9A6A6")])
        style.configure("TEntry", fieldbackground="white", foreground=self.COLORS["text"], padding=7)
        style.configure("TCombobox", fieldbackground="white", foreground=self.COLORS["text"], padding=6)
        style.configure("TCheckbutton", background=self.COLORS["card"], foreground=self.COLORS["text"], font=("Segoe UI", 9))
        style.configure("TNotebook", background=self.COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background="#E7EDF3", foreground=self.COLORS["muted"], padding=(14, 8), font=("Segoe UI Semibold", 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", self.COLORS["card"])], foreground=[("selected", self.COLORS["navy"])])
        style.configure("Treeview", background="white", fieldbackground="white", foreground=self.COLORS["text"], rowheight=30, font=("Segoe UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", background=self.COLORS["navy"], foreground="white", font=("Segoe UI Semibold", 9), relief="flat", padding=6)
        style.map("Treeview.Heading", background=[("active", self.COLORS["navy_dark"])])
        style.configure("Modern.Horizontal.TProgressbar", troughcolor="#DFE7EE", background=self.COLORS["accent"], bordercolor="#DFE7EE", lightcolor=self.COLORS["accent"], darkcolor=self.COLORS["accent"])
        style.configure("Vertical.TScrollbar", background="#D7E1EA", troughcolor="#F2F5F8", bordercolor="#F2F5F8", arrowcolor=self.COLORS["navy"])
        style.configure("Horizontal.TScrollbar", background="#D7E1EA", troughcolor="#F2F5F8", bordercolor="#F2F5F8", arrowcolor=self.COLORS["navy"])

    def _make_stat_card(self, parent, title: str, variable: tk.StringVar, caption: str, accent: str):
        card = tk.Frame(parent, bg=self.COLORS["card"], highlightthickness=1, highlightbackground=self.COLORS["border"], bd=0)
        strip = tk.Frame(card, bg=accent, width=5)
        strip.pack(side="left", fill="y")
        body = tk.Frame(card, bg=self.COLORS["card"], padx=13, pady=10)
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text=title.upper(), bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(body, textvariable=variable, bg=self.COLORS["card"], fg=self.COLORS["navy"], font=("Segoe UI Semibold", 16)).pack(anchor="w", pady=(1, 0))
        tk.Label(body, text=caption, bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).pack(anchor="w")
        return card

    def _card(self, parent, padx=14, pady=12):
        frame = tk.Frame(parent, bg=self.COLORS["card"], highlightthickness=1, highlightbackground=self.COLORS["border"], bd=0)
        inner = tk.Frame(frame, bg=self.COLORS["card"], padx=padx, pady=pady)
        inner.pack(fill="both", expand=True)
        return frame, inner

    def _labeled_entry(self, parent, label, variable, browse=None, row=0):
        tk.Label(parent, text=label, bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 9)).grid(row=row, column=0, sticky="w", pady=(5, 3))
        ent = ttk.Entry(parent, textvariable=variable)
        ent.grid(row=row + 1, column=0, sticky="ew", pady=(0, 7))
        if browse:
            ttk.Button(parent, text="Browse", style="Secondary.TButton", command=browse).grid(row=row + 1, column=1, padx=(7, 0), pady=(0, 7))
        return ent

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=self.COLORS["navy_dark"], height=88)
        header.pack(fill="x")
        header.pack_propagate(False)
        title_box = tk.Frame(header, bg=self.COLORS["navy_dark"])
        title_box.pack(side="left", padx=24, pady=16)
        tk.Label(title_box, text="HGAC Contour Studio", bg=self.COLORS["navy_dark"], fg="white", font=("Segoe UI Semibold", 21)).pack(anchor="w")
        tk.Label(title_box, text="USGS Houston B24 DEM discovery • seamless multi-tile 2-ft + 5-ft contour production", bg=self.COLORS["navy_dark"], fg="#BFD3E6", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        status_box = tk.Frame(header, bg=self.COLORS["navy_dark"])
        status_box.pack(side="right", padx=24)
        tk.Label(status_box, text="APPLICATION STATUS", bg=self.COLORS["navy_dark"], fg="#85A4BE", font=("Segoe UI Semibold", 8)).pack(anchor="e")
        self.header_status = tk.Label(status_box, textvariable=self.app_status_var, bg=self.COLORS["accent"], fg="white", font=("Segoe UI Semibold", 9), padx=13, pady=5)
        self.header_status.pack(anchor="e", pady=(4, 0))

        page = tk.Frame(self, bg=self.COLORS["bg"], padx=18, pady=14)
        page.pack(fill="both", expand=True)

        # Summary cards
        stats = tk.Frame(page, bg=self.COLORS["bg"])
        stats.pack(fill="x", pady=(0, 12))
        for c in range(4):
            stats.columnconfigure(c, weight=1, uniform="stats")
        cards = [
            self._make_stat_card(stats, "Tiles", self.tiles_stat_var, "normalized input tiles", self.COLORS["accent"]),
            self._make_stat_card(stats, "DEM strategy", self.strategy_stat_var, "online + local resolver", "#3B82C4"),
            self._make_stat_card(stats, "Outputs", self.output_stat_var, "per-tile + seamless batch", "#805AD5"),
            self._make_stat_card(stats, "ArcGIS", self.engine_stat_var, "environment readiness", "#D69E2E"),
        ]
        for i, card in enumerate(cards):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 3 else 5))

        body = ttk.Panedwindow(page, orient="horizontal")
        body.pack(fill="both", expand=True)

        # LEFT: configuration workspace
        left = tk.Frame(body, bg=self.COLORS["bg"], width=470)
        left.pack_propagate(False)
        body.add(left, weight=2)
        left_card, left_inner = self._card(left, padx=12, pady=10)
        left_card.pack(fill="both", expand=True, padx=(0, 6))
        tk.Label(left_inner, text="Configuration", bg=self.COLORS["card"], fg=self.COLORS["navy"], font=("Segoe UI Semibold", 13)).pack(anchor="w")
        tk.Label(left_inner, text="Set tile input, DEM resolution strategy and delivery settings.", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 8))

        config_tabs = ttk.Notebook(left_inner)
        config_tabs.pack(fill="both", expand=True)

        # Tiles tab
        tile_tab = tk.Frame(config_tabs, bg=self.COLORS["card"], padx=10, pady=10)
        config_tabs.add(tile_tab, text="Tiles")
        tk.Label(tile_tab, text="Tile names", bg=self.COLORS["card"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(tile_tab, text="Paste LAS/LAZ names, tile IDs, or load a text/CSV list.", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(1, 6))
        text_wrap = tk.Frame(tile_tab, bg=self.COLORS["border"], padx=1, pady=1)
        text_wrap.pack(fill="both", expand=True)
        self.tile_text = tk.Text(text_wrap, height=12, wrap="none", relief="flat", borderwidth=0, bg="#FBFCFE", fg=self.COLORS["text"], insertbackground=self.COLORS["accent"], font=("Cascadia Mono", 10), padx=9, pady=8)
        tile_y = ttk.Scrollbar(text_wrap, orient="vertical", command=self.tile_text.yview)
        tile_x = ttk.Scrollbar(text_wrap, orient="horizontal", command=self.tile_text.xview)
        self.tile_text.configure(yscrollcommand=tile_y.set, xscrollcommand=tile_x.set)
        self.tile_text.grid(row=0, column=0, sticky="nsew")
        tile_y.grid(row=0, column=1, sticky="ns")
        tile_x.grid(row=1, column=0, sticky="ew")
        text_wrap.rowconfigure(0, weight=1); text_wrap.columnconfigure(0, weight=1)
        self.tile_text.insert("1.0", "15RUN316243.las\n")
        tile_actions = tk.Frame(tile_tab, bg=self.COLORS["card"])
        tile_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(tile_actions, text="Load TXT / CSV", style="Secondary.TButton", command=self.load_tile_txt).pack(side="left")
        ttk.Button(tile_actions, text="Normalize & preview", style="Secondary.TButton", command=self.preview_tiles).pack(side="left", padx=6)
        self.tile_count_label = tk.Label(tile_actions, text="", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 9))
        self.tile_count_label.pack(side="right")

        # DEM tab
        dem_tab = tk.Frame(config_tabs, bg=self.COLORS["card"], padx=10, pady=10)
        config_tabs.add(dem_tab, text="DEM source")
        dem_tab.columnconfigure(0, weight=1)
        tk.Label(dem_tab, text="DEM resolution strategy", bg=self.COLORS["card"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10)).grid(row=0, column=0, sticky="w")
        mode_combo = ttk.Combobox(dem_tab, textvariable=self.mode_var, state="readonly", values=("USGS first; local fallback", "Local first; USGS fallback", "USGS only", "Local only"))
        mode_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 8))
        mode_combo.bind("<<ComboboxSelected>>", lambda e: self._update_summary())
        self._labeled_entry(dem_tab, "Local bare-earth search root", self.local_root_var, lambda: self.choose_dir(self.local_root_var), row=2)
        self._labeled_entry(dem_tab, "USGS DEM download cache", self.cache_var, lambda: self.choose_dir(self.cache_var), row=4)
        ttk.Button(dem_tab, text="Test all four USGS indexes", style="Secondary.TButton", command=self.test_indexes).grid(row=6, column=0, sticky="w", pady=(7, 0))
        tk.Label(dem_tab, text="Local fallback prioritizes bare_earth / be_rasters and rejects intensity, DSM, CHM and preview products.", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI", 8), wraplength=390, justify="left").grid(row=7, column=0, columnspan=2, sticky="w", pady=(9, 0))

        # Settings tab
        set_tab = tk.Frame(config_tabs, bg=self.COLORS["card"], padx=10, pady=10)
        config_tabs.add(set_tab, text="Contours & output")
        set_tab.columnconfigure(0, weight=1)
        self._labeled_entry(set_tab, "Output root", self.output_var, lambda: self.choose_dir(self.output_var), row=0)
        self._labeled_entry(set_tab, "ArcGIS Pro Python", self.arcgis_python_var, self.choose_arcgis_python, row=2)
        tk.Label(set_tab, text="DEM vertical units", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 9)).grid(row=4, column=0, sticky="w", pady=(5, 3))
        ttk.Combobox(set_tab, textvariable=self.dem_z_units_var, state="readonly", values=("meters", "feet", "us_survey_feet"), width=22).grid(row=5, column=0, sticky="w", pady=(0, 8))
        bases = tk.Frame(set_tab, bg=self.COLORS["card"])
        bases.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(2, 6))
        tk.Label(bases, text="2-ft base (ft)", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 9)).grid(row=0, column=0, sticky="w")
        tk.Label(bases, text="5-ft base (ft)", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 9)).grid(row=0, column=1, sticky="w", padx=(16, 0))
        ttk.Entry(bases, textvariable=self.base_2ft_var, width=14).grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Entry(bases, textvariable=self.base_5ft_var, width=14).grid(row=1, column=1, sticky="w", padx=(16, 0), pady=(3, 0))
        ttk.Checkbutton(set_tab, text="Overwrite existing tile outputs", variable=self.overwrite_var).grid(row=7, column=0, sticky="w", pady=(6, 2))
        ttk.Checkbutton(set_tab, text="Hide ArcGIS worker command windows (recommended)", variable=self.hide_worker_console_var).grid(row=8, column=0, sticky="w", pady=2)
        ttk.Checkbutton(set_tab, text="Keep working mosaic TIFFs for QA (uses more disk space)", variable=self.keep_work_mosaics_var).grid(row=9, column=0, sticky="w", pady=2)
        note = tk.Frame(set_tab, bg="#EEF8F7", padx=10, pady=8, highlightthickness=1, highlightbackground="#C7E9E6")
        note.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        tk.Label(note, text="Delivery behavior", bg="#EEF8F7", fg=self.COLORS["accent_dark"], font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(note, text="Adjacent DEMs are mosaicked before contouring. The run creates seamless RUN_Contours_2FT / RUN_Contours_5FT plus per-tile layers clipped from those same lines. Attributes are verified before completion.", bg="#EEF8F7", fg="#436A67", font=("Segoe UI", 8), wraplength=380, justify="left").pack(anchor="w", pady=(2, 0))

        # RIGHT: operations & monitoring
        right = tk.Frame(body, bg=self.COLORS["bg"])
        body.add(right, weight=5)

        action_card, action_inner = self._card(right, padx=14, pady=11)
        action_card.pack(fill="x", padx=(6, 0), pady=(0, 9))
        action_top = tk.Frame(action_inner, bg=self.COLORS["card"])
        action_top.pack(fill="x")
        title_group = tk.Frame(action_top, bg=self.COLORS["card"])
        title_group.pack(side="left")
        tk.Label(title_group, text="Batch control", bg=self.COLORS["card"], fg=self.COLORS["navy"], font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(title_group, text="ArcGIS workers run silently in the background by default.", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).pack(anchor="w")
        buttons = tk.Frame(action_top, bg=self.COLORS["card"])
        buttons.pack(side="right")
        self.check_btn = ttk.Button(buttons, text="Check ArcGIS", style="Secondary.TButton", command=self.check_arcgis)
        self.check_btn.pack(side="left", padx=(0, 6))
        self.run_btn = ttk.Button(buttons, text="RUN SEAMLESS CONTOURS", style="Accent.TButton", command=self.start_run)
        self.run_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(buttons, text="STOP", style="Danger.TButton", command=self.stop_run, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 6))
        self.open_btn = ttk.Button(buttons, text="Open output", style="Secondary.TButton", command=self.open_last_output, state="disabled")
        self.open_btn.pack(side="left")
        progress_box = tk.Frame(action_inner, bg=self.COLORS["card"])
        progress_box.pack(fill="x", pady=(11, 0))
        self.progress = ttk.Progressbar(progress_box, style="Modern.Horizontal.TProgressbar", mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = tk.Label(progress_box, text="0%", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI Semibold", 9), width=6)
        self.progress_label.pack(side="right", padx=(8, 0))

        monitor_card, monitor_inner = self._card(right, padx=0, pady=0)
        monitor_card.pack(fill="both", expand=True, padx=(6, 0))
        monitor_tabs = ttk.Notebook(monitor_inner)
        monitor_tabs.pack(fill="both", expand=True)

        status_tab = tk.Frame(monitor_tabs, bg=self.COLORS["card"], padx=10, pady=10)
        monitor_tabs.add(status_tab, text="Tile status")
        status_tab.rowconfigure(0, weight=1); status_tab.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(status_tab, columns=("tile", "dem", "status"), show="headings", selectmode="browse")
        self.tree.heading("tile", text="Tile")
        self.tree.heading("dem", text="DEM source / path")
        self.tree.heading("status", text="Status")
        self.tree.column("tile", width=145, stretch=False, anchor="w")
        self.tree.column("dem", width=500, stretch=True, anchor="w")
        self.tree.column("status", width=250, stretch=False, anchor="w")
        yscroll = ttk.Scrollbar(status_tab, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(status_tab, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("ok", background="#ECF8F2")
        self.tree.tag_configure("fail", background="#FFF0F0")
        self.tree.tag_configure("active", background="#EEF6FC")

        log_tab = tk.Frame(monitor_tabs, bg=self.COLORS["card"], padx=10, pady=10)
        monitor_tabs.add(log_tab, text="Live log")
        log_toolbar = tk.Frame(log_tab, bg=self.COLORS["card"])
        log_toolbar.pack(fill="x", pady=(0, 6))
        tk.Label(log_toolbar, text="Detailed activity and ArcGIS output", bg=self.COLORS["card"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).pack(side="left")
        ttk.Button(log_toolbar, text="Copy log", style="Secondary.TButton", command=self.copy_log).pack(side="right")
        ttk.Button(log_toolbar, text="Clear", style="Secondary.TButton", command=self.clear_log).pack(side="right", padx=(0, 5))
        log_wrap = tk.Frame(log_tab, bg="#24364A", padx=1, pady=1)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, wrap="word", relief="flat", borderwidth=0, bg=self.COLORS["log_bg"], fg=self.COLORS["log_fg"], insertbackground="white", font=("Cascadia Mono", 9), padx=10, pady=9, state="disabled")
        sy = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sy.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.log_text.tag_configure("warning", foreground="#FFD27A")
        self.log_text.tag_configure("error", foreground="#FF8E8E")
        self.log_text.tag_configure("success", foreground="#8CE1B3")
        self.log_text.tag_configure("accent", foreground="#7EDDD8")

        # Footer
        footer = tk.Frame(self, bg="#E7EDF3", height=28)
        footer.pack(fill="x")
        tk.Label(footer, text=f"{BUILD}   |   seamless batch + per-tile contours   |   raw / unsmoothed", bg="#E7EDF3", fg=self.COLORS["muted"], font=("Segoe UI", 8)).pack(side="left", padx=18, pady=5)
        tk.Label(footer, text="Worker consoles hidden by default", bg="#E7EDF3", fg=self.COLORS["accent_dark"], font=("Segoe UI Semibold", 8)).pack(side="right", padx=18, pady=5)

        self.log(f"GUI build: {BUILD}")
        self.log("USGS index sources: TX_Houston_1_B24 through TX_Houston_4_B24")
        self.log("Seamless batch mode enabled: adjacent DEMs are mosaicked before contouring; worker windows are hidden by default.")
        self.preview_tiles()

    def _update_summary(self):
        tiles = parse_tile_list(self.tile_text.get("1.0", "end")) if hasattr(self, "tile_text") else []
        self.tiles_stat_var.set(str(len(tiles)))
        mode = self.mode_var.get()
        compact = {
            "USGS first; local fallback": "USGS → local",
            "Local first; USGS fallback": "Local → USGS",
            "USGS only": "USGS only",
            "Local only": "Local only",
        }.get(mode, mode)
        self.strategy_stat_var.set(compact)

    def _set_app_status(self, text: str, color: str | None = None):
        self.app_status_var.set(text)
        if hasattr(self, "header_status"):
            self.header_status.configure(bg=color or self.COLORS["accent"])

    def _subprocess_window_kwargs(self):
        if os.name != "nt" or not self.hide_worker_console_var.get():
            return {}
        kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            kw["startupinfo"] = si
        except Exception:
            pass
        return kw

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self):
        text = self.log_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_app_status("Log copied", self.COLORS["accent"])
        self.after(1600, lambda: self._set_app_status("Ready" if not self.running else "Processing"))

    def log(self, msg: str):
        stamp = time.strftime("%H:%M:%S")
        upper = msg.upper()
        tag = None
        if "ERROR" in upper or "FAILED" in upper:
            tag = "error"
        elif "WARNING" in upper:
            tag = "warning"
        elif "COMPLETE" in upper or "PASSED" in upper or "DOWNLOADED DEM" in upper:
            tag = "success"
        elif "FETCHING" in upper or "ARCGIS:" in upper or "OUTPUT" in upper:
            tag = "accent"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {msg}\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.update_idletasks()

    def qlog(self, msg: str):
        self.q.put(("log", msg))

    def choose_dir(self, var):
        p = filedialog.askdirectory(initialdir=var.get() or None)
        if p:
            var.set(p)

    def choose_arcgis_python(self):
        p = filedialog.askopenfilename(filetypes=[("Python", "python.exe"), ("Executable", "*.exe"), ("All", "*.*")])
        if p:
            self.arcgis_python_var.set(p)

    def load_tile_txt(self):
        p = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("CSV", "*.csv"), ("All", "*.*")])
        if not p:
            return
        text = Path(p).read_text(encoding="utf-8", errors="replace")
        self.tile_text.delete("1.0", "end")
        self.tile_text.insert("1.0", text)
        self.preview_tiles()

    def preview_tiles(self):
        tiles = parse_tile_list(self.tile_text.get("1.0", "end"))
        self.tile_count_label.config(text=f"{len(tiles)} unique tile(s)")
        self.tiles_stat_var.set(str(len(tiles)))
        self._update_summary()
        self.tree.delete(*self.tree.get_children())
        self.status_rows.clear()
        for tile in tiles:
            iid = self.tree.insert("", "end", values=(tile, "", "Ready"))
            self.status_rows[tile] = iid
        self.log("Tiles: " + ", ".join(tiles) if tiles else "No valid tiles found.")

    def update_tile(self, tile: str, dem: str | None = None, status: str | None = None):
        iid = self.status_rows.get(tile)
        if iid is None:
            iid = self.tree.insert("", "end", values=(tile, "", ""))
            self.status_rows[tile] = iid
        vals = list(self.tree.item(iid, "values"))
        while len(vals) < 3:
            vals.append("")
        if dem is not None:
            vals[1] = dem
        if status is not None:
            vals[2] = status
        status_text = str(vals[2]).upper()
        if "COMPLETE" in status_text or "VERIFIED" in status_text:
            tags = ("ok",)
        elif "FAIL" in status_text or "NOT FOUND" in status_text:
            tags = ("fail",)
        elif status_text and status_text not in ("READY",):
            tags = ("active",)
        else:
            tags = ()
        self.tree.item(iid, values=vals, tags=tags)

    def _poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                typ = item[0]
                if typ == "log":
                    self.log(item[1])
                elif typ == "tile":
                    self.update_tile(item[1], item[2], item[3])
                elif typ == "progress":
                    self.progress["value"] = item[1]
                    if hasattr(self, "progress_label"):
                        self.progress_label.config(text=f"{item[1]:.0f}%")
                elif typ == "done":
                    self.running = False
                    self.run_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.check_btn.config(state="normal")
                    if self.last_run_dir and self.last_run_dir.exists():
                        self.open_btn.config(state="normal")
                    if item[1]:
                        self.progress["value"] = 100
                        if hasattr(self, "progress_label"):
                            self.progress_label.config(text="100%")
                        self._set_app_status("Completed", self.COLORS["success"])
                        messagebox.showinfo("HGAC contours", "Batch completed. See the tile status, log and run manifest.")
                    else:
                        self._set_app_status("Attention needed", self.COLORS["warning"])
                        messagebox.showwarning("HGAC contours", "Batch stopped or completed with failures. See the tile status and log.")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def fetch_indexes(self, refresh: bool = False) -> dict[str, str]:
        cache_dir = Path(self.cache_var.get()) / "_link_indexes"
        cache_dir.mkdir(parents=True, exist_ok=True)
        results = {}
        for subproject, url in LINK_LIST_URLS.items():
            if self.stop_event.is_set():
                break
            cache_file = cache_dir / f"{subproject}_0_file_download_links.txt"
            text = None
            if not refresh and subproject in self.index_cache:
                text = self.index_cache[subproject]
            if text is None:
                try:
                    self.qlog(f"Fetching USGS index: {subproject}")
                    text = request_text(url)
                    cache_file.write_text(text, encoding="utf-8")
                except Exception as exc:
                    if cache_file.exists():
                        text = cache_file.read_text(encoding="utf-8", errors="replace")
                        self.qlog(f"WARNING: online index failed for {subproject}; using cached copy: {exc}")
                    else:
                        self.qlog(f"WARNING: could not read {subproject} index: {exc}")
                        text = ""
                self.index_cache[subproject] = text
            results[subproject] = text
        return results

    def resolve_usgs_url(self, tile: str, indexes: dict[str, str]) -> tuple[str | None, str | None]:
        for subproject in SUBPROJECTS:
            matches = parse_index_for_tile(indexes.get(subproject, ""), tile)
            if matches:
                return matches[0], subproject
        # Robust fallback: construct the expected TIFF URL and HEAD-test each Houston subdivision.
        filename = expected_tiff_name(tile)
        for subproject in SUBPROJECTS:
            url = f"{PROJECT_ROOT}/{subproject}/TIFF/{filename}"
            if url_exists(url):
                return url, subproject
        return None, None

    def download_dem(self, url: str, tile: str) -> str:
        cache_dir = Path(self.cache_var.get())
        cache_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(urlparse(url).path).name or expected_tiff_name(tile)
        out = cache_dir / filename
        if out.exists():
            ok, detail = is_valid_tiff_file(out)
            if ok:
                self.qlog(f"Reusing cached DEM for {tile}: {out} ({detail})")
                return str(out)
            self.qlog(f"WARNING: deleting invalid cached TIFF for {tile}: {detail}")
            try:
                out.unlink()
            except Exception:
                pass
        part = out.with_suffix(out.suffix + ".part")
        if part.exists():
            part.unlink()
        req = Request(url, headers={"User-Agent": USER_AGENT})
        self.qlog(f"Downloading {tile}: {url}")
        try:
            with urlopen(req, timeout=60) as r, part.open("wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if self.stop_event.is_set():
                        raise InterruptedError("Download stopped by user")
                    chunk = r.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.qlog(f"  {tile}: {done/1048576:.1f}/{total/1048576:.1f} MB ({done*100/total:.0f}%)")
            ok, detail = is_valid_tiff_file(part)
            if not ok:
                raise RuntimeError(f"Downloaded file is not a valid TIFF: {detail}")
            if part.stat().st_size < 1_000_000:
                self.qlog(
                    f"  NOTE: downloaded TIFF is small ({part.stat().st_size} bytes) but has a valid TIFF signature; accepting it."
                )
            part.replace(out)
            self.qlog(f"Downloaded DEM: {out}")
            return str(out)
        except Exception:
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def resolve_dem(self, tile: str, indexes: dict[str, str]) -> tuple[str | None, str, str]:
        mode = self.mode_var.get()

        def local():
            path, alternatives = find_local_dem(self.local_root_var.get(), tile)
            if path:
                self.qlog(f"Local DEM match for {tile}: {path}")
                if len(alternatives) > 1:
                    self.qlog(f"  {len(alternatives)} local candidate(s); selected highest-ranked bare-earth match.")
                return path, "local", ""
            return None, "", ""

        def usgs():
            url, subproject = self.resolve_usgs_url(tile, indexes)
            if not url:
                return None, "", ""
            path = self.download_dem(url, tile)
            return path, f"USGS {subproject}", url

        attempts = []
        if mode == "USGS first; local fallback":
            attempts = [usgs, local]
        elif mode == "Local first; USGS fallback":
            attempts = [local, usgs]
        elif mode == "USGS only":
            attempts = [usgs]
        else:
            attempts = [local]

        errors = []
        for fn in attempts:
            try:
                p, src, url = fn()
                if p:
                    return p, src, url
            except Exception as exc:
                errors.append(str(exc))
                self.qlog(f"WARNING: {tile} DEM attempt failed: {exc}")
        return None, "", "; ".join(errors)

    def check_arcgis(self):
        py = self.arcgis_python_var.get().strip()
        if not py or not Path(py).exists():
            messagebox.showerror("ArcGIS Python", "Select a valid ArcGIS Pro python.exe first.")
            return
        cmd = [py, str(self.worker_script), "--check-only"]
        self.log("Checking ArcGIS contour environment...")
        self._set_app_status("Checking ArcGIS", self.COLORS["warning"])
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60, **self._subprocess_window_kwargs())
            if cp.stdout:
                self.log(cp.stdout.strip())
            if cp.stderr:
                self.log(cp.stderr.strip())
            if cp.returncode == 0:
                self.engine_stat_var.set("Ready")
                self._set_app_status("Ready", self.COLORS["success"])
                messagebox.showinfo("ArcGIS check", "ArcGIS contour environment is ready.")
            else:
                self.engine_stat_var.set("Check failed")
                self._set_app_status("ArcGIS check failed", self.COLORS["danger"])
                messagebox.showerror("ArcGIS check", "ArcGIS check failed. See log.")
        except Exception as exc:
            self.engine_stat_var.set("Check failed")
            self._set_app_status("ArcGIS check failed", self.COLORS["danger"])
            self.log(f"ERROR: {exc}")
            messagebox.showerror("ArcGIS check", str(exc))

    def test_indexes(self):
        if self.running:
            return
        def work():
            try:
                idx = self.fetch_indexes(refresh=True)
                for sp in SUBPROJECTS:
                    n = len(idx.get(sp, "").splitlines())
                    self.qlog(f"{sp}: {n:,} link line(s) loaded")
            except Exception as exc:
                self.qlog(f"ERROR testing indexes: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def start_run(self):
        if self.running:
            return
        tiles = parse_tile_list(self.tile_text.get("1.0", "end"))
        if not tiles:
            messagebox.showerror("Tiles", "Paste at least one valid tile name.")
            return
        py = Path(self.arcgis_python_var.get().strip())
        if not py.exists():
            messagebox.showerror("ArcGIS Python", "Select a valid ArcGIS Pro python.exe.")
            return
        try:
            float(self.base_2ft_var.get())
            float(self.base_5ft_var.get())
        except ValueError:
            messagebox.showerror("Base contour", "Both base contour values must be numeric feet, normally 0.")
            return
        out = Path(self.output_var.get())
        out.mkdir(parents=True, exist_ok=True)
        Path(self.cache_var.get()).mkdir(parents=True, exist_ok=True)

        self.preview_tiles()
        self.stop_event.clear()
        self.running = True
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.check_btn.config(state="disabled")
        self.progress["value"] = 0
        if hasattr(self, "progress_label"):
            self.progress_label.config(text="0%")
        self._set_app_status("Processing", self.COLORS["accent"])
        self.open_btn.config(state="disabled")
        threading.Thread(target=self._batch_thread, args=(tiles,), daemon=True).start()

    def _run_worker(self, tile: str, dem: str, source_url: str, gdb: Path, report: Path) -> int:
        cmd = [
            self.arcgis_python_var.get().strip(),
            "-u",
            str(self.worker_script),
            "--dem", dem,
            "--tile", tile,
            "--output-gdb", str(gdb),
            "--dem-z-units", self.dem_z_units_var.get(),
            "--base-2ft", self.base_2ft_var.get(),
            "--base-5ft", self.base_5ft_var.get(),
            "--report", str(report),
        ]
        if source_url:
            cmd.extend(["--source-url", source_url])
        if self.overwrite_var.get():
            cmd.append("--overwrite")
        self.qlog("ArcGIS command: " + subprocess.list2cmdline(cmd))
        popen_kwargs = self._subprocess_window_kwargs()
        self.worker_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **popen_kwargs,
        )
        assert self.worker_proc.stdout is not None
        for line in self.worker_proc.stdout:
            self.qlog("  ArcGIS: " + line.rstrip())
            if self.stop_event.is_set():
                try:
                    self.worker_proc.terminate()
                except Exception:
                    pass
                break
        rc = self.worker_proc.wait()
        self.worker_proc = None
        return rc

    def _run_batch_worker(self, input_json: Path, gdb: Path, work_dir: Path, report: Path) -> int:
        cmd = [
            self.arcgis_python_var.get().strip(),
            "-u",
            str(self.batch_worker_script),
            "--input-json", str(input_json),
            "--output-gdb", str(gdb),
            "--work-dir", str(work_dir),
            "--dem-z-units", self.dem_z_units_var.get(),
            "--base-2ft", self.base_2ft_var.get(),
            "--base-5ft", self.base_5ft_var.get(),
            "--report", str(report),
        ]
        if self.overwrite_var.get():
            cmd.append("--overwrite")
        if self.keep_work_mosaics_var.get():
            cmd.append("--keep-work-mosaics")
        self.qlog("ArcGIS seamless batch command: " + subprocess.list2cmdline(cmd))
        self.worker_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **self._subprocess_window_kwargs(),
        )
        assert self.worker_proc.stdout is not None
        for line in self.worker_proc.stdout:
            self.qlog("  ArcGIS batch: " + line.rstrip())
            if self.stop_event.is_set():
                try:
                    self.worker_proc.terminate()
                except Exception:
                    pass
                break
        rc = self.worker_proc.wait()
        self.worker_proc = None
        return rc

    def _batch_thread(self, tiles: list[str]):
        out = Path(self.output_var.get())
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = out / f"HGAC_Contours_batch_{timestamp}"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.last_run_dir = run_dir
        reports_dir = run_dir / "reports"
        reports_dir.mkdir(exist_ok=True)
        work_dir = run_dir / "_seamless_work"
        work_dir.mkdir(exist_ok=True)
        gdb = run_dir / "HGAC_Contours_2FT_5FT.gdb"
        manifest_path = run_dir / "run_manifest.csv"
        json_path = run_dir / "run_manifest.json"
        input_json = reports_dir / "resolved_dem_inputs.json"
        batch_report = reports_dir / "seamless_batch_arcgis_report.json"

        self.qlog(f"Output batch: {run_dir}")
        self.qlog("SEAMLESS MODE: adjacent DEMs are mosaicked BEFORE contouring to eliminate tile-edge contour gaps.")
        self.qlog("Final combined outputs: RUN_Contours_2FT and RUN_Contours_5FT; per-tile outputs are clipped from those same seamless lines.")

        indexes = {}
        if self.mode_var.get() != "Local only":
            indexes = self.fetch_indexes(refresh=False)

        rows_by_tile = {}
        resolved = []
        # Phase 1: resolve every DEM first. This allows ArcGIS to see the whole
        # selected run and identify adjacent tiles before contouring.
        for i, tile in enumerate(tiles, start=1):
            if self.stop_event.is_set():
                break
            self.q.put(("progress", (i - 1) * 35 / max(1, len(tiles))))
            self.q.put(("tile", tile, "", "Resolving DEM"))
            self.qlog(f"[Resolve {i}/{len(tiles)}] {tile}")
            dem, source, source_url = self.resolve_dem(tile, indexes)
            if not dem:
                err = source_url or "No USGS or local bare-earth DEM match found"
                self.q.put(("tile", tile, "", "DEM NOT FOUND"))
                self.qlog(f"ERROR: {tile}: {err}")
                rows_by_tile[tile] = {
                    "tile": tile, "status": "dem_not_found", "dem": "", "source": "",
                    "source_url": "", "attributes_verified": False, "2ft_count": "",
                    "5ft_count": "", "2ft_feature_class": "", "5ft_feature_class": "",
                    "combined_2ft_feature_class": "", "combined_5ft_feature_class": "", "error": err,
                }
                continue
            rec = {"tile": tile, "dem": dem, "source": source, "source_url": source_url}
            resolved.append(rec)
            self.q.put(("tile", tile, dem, "DEM ready — queued for seamless batch"))

        if self.stop_event.is_set():
            self.q.put(("done", False))
            return

        if not resolved:
            self.qlog("ERROR: No DEMs were resolved; ArcGIS batch was not started.")
            self.q.put(("done", False))
            return

        input_json.write_text(json.dumps(resolved, indent=2), encoding="utf-8")
        self.q.put(("progress", 40))
        for rec in resolved:
            self.q.put(("tile", rec["tile"], rec["dem"], "Building seamless surface + contours"))

        rc = self._run_batch_worker(input_json, gdb, work_dir, batch_report)
        report_data = {}
        if batch_report.exists():
            try:
                report_data = json.loads(batch_report.read_text(encoding="utf-8"))
            except Exception as exc:
                self.qlog(f"WARNING: could not parse seamless batch report: {exc}")

        # If ArcGIS rejects a downloaded USGS raster, retry only those invalid
        # inputs with the best local bare-earth match, then rerun the batch once.
        invalid = report_data.get("invalid_inputs", []) if isinstance(report_data, dict) else []
        if rc != 0 and invalid and self.mode_var.get() in (
            "USGS first; local fallback", "Local first; USGS fallback"
        ):
            changed = False
            invalid_tiles = {str(x.get("tile")) for x in invalid}
            for rec in resolved:
                if rec["tile"] not in invalid_tiles or not str(rec.get("source", "")).startswith("USGS"):
                    continue
                local_dem, alternatives = find_local_dem(self.local_root_var.get(), rec["tile"])
                if local_dem:
                    self.qlog(f"WARNING: ArcGIS rejected USGS DEM for {rec['tile']}; batch retry will use local bare-earth DEM: {local_dem}")
                    rec["dem"] = local_dem
                    rec["source"] = "local retry after USGS validation failure"
                    rec["source_url"] = ""
                    changed = True
                    self.q.put(("tile", rec["tile"], local_dem, "Retrying seamless batch with local DEM"))
            if changed:
                input_json.write_text(json.dumps(resolved, indent=2), encoding="utf-8")
                retry_report = reports_dir / "seamless_batch_arcgis_report_local_retry.json"
                rc = self._run_batch_worker(input_json, gdb, work_dir, retry_report)
                batch_report = retry_report
                if retry_report.exists():
                    try:
                        report_data = json.loads(retry_report.read_text(encoding="utf-8"))
                    except Exception as exc:
                        self.qlog(f"WARNING: could not parse seamless retry report: {exc}")

        self.q.put(("progress", 90))
        combined = report_data.get("combined_outputs", {}) if isinstance(report_data, dict) else {}
        combined2 = combined.get("2ft", {}).get("feature_class", "")
        combined5 = combined.get("5ft", {}).get("feature_class", "")
        if combined2:
            self.qlog(f"Combined seamless 2-ft layer: {combined2}")
        if combined5:
            self.qlog(f"Combined seamless 5-ft layer: {combined5}")

        tile_results = report_data.get("tiles", {}) if isinstance(report_data, dict) else {}
        success_count = 0
        for rec in resolved:
            tile = rec["tile"]
            tdata = tile_results.get(tile, {})
            tout = tdata.get("outputs", {}) if isinstance(tdata, dict) else {}
            out2 = tout.get("2ft", {})
            out5 = tout.get("5ft", {})
            attribute_ok = bool(tdata.get("attribute_validation_passed"))
            if rc == 0 and attribute_ok:
                success_count += 1
                status = "succeeded"
                err = ""
                self.q.put(("tile", tile, rec["dem"], "COMPLETE — seamless 2ft + 5ft VERIFIED"))
                self.qlog(
                    f"  {tile}: seamless per-tile outputs verified; "
                    f"2-ft={out2.get('count', 0):,}, 5-ft={out5.get('count', 0):,}"
                )
            else:
                status = "failed"
                err = report_data.get("error", f"ArcGIS seamless batch worker exit code {rc}; see {batch_report}")
                self.q.put(("tile", tile, rec["dem"], "FAILED — seamless batch"))
            rows_by_tile[tile] = {
                "tile": tile, "status": status, "dem": rec["dem"], "source": rec.get("source", ""),
                "source_url": rec.get("source_url", ""), "attributes_verified": attribute_ok,
                "2ft_count": out2.get("count", ""), "5ft_count": out5.get("count", ""),
                "2ft_feature_class": out2.get("feature_class", ""),
                "5ft_feature_class": out5.get("feature_class", ""),
                "combined_2ft_feature_class": combined2, "combined_5ft_feature_class": combined5,
                "error": err,
            }

        rows = [rows_by_tile[t] for t in tiles if t in rows_by_tile]
        fields = (
            "tile", "status", "dem", "source", "source_url", "attributes_verified",
            "2ft_count", "5ft_count", "2ft_feature_class", "5ft_feature_class",
            "combined_2ft_feature_class", "combined_5ft_feature_class", "error",
        )
        with manifest_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        json_path.write_text(json.dumps({
            "build": BUILD,
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "output_gdb": str(gdb),
            "seamless_mode": True,
            "dem_z_units": self.dem_z_units_var.get(),
            "base_2ft": self.base_2ft_var.get(),
            "base_5ft": self.base_5ft_var.get(),
            "contour_intervals_ft": [2, 5],
            "adjacency_groups": report_data.get("adjacency_groups", []),
            "combined_outputs": combined,
            "tile_footprints": report_data.get("tile_footprints", ""),
            "rows": rows,
        }, indent=2), encoding="utf-8")

        self.qlog(f"Manifest: {manifest_path}")
        self.qlog(f"Output geodatabase: {gdb}")
        if combined2 and combined5:
            self.qlog("CUSTOMER / RUN-WIDE OUTPUTS: RUN_Contours_2FT + RUN_Contours_5FT")
        self.qlog(f"Completed {success_count}/{len(tiles)} requested tile(s).")
        self.q.put(("progress", 100))
        ok = (success_count == len(tiles) and rc == 0 and not self.stop_event.is_set())
        self.q.put(("done", ok))

    def open_last_output(self):
        if not self.last_run_dir or not self.last_run_dir.exists():
            messagebox.showinfo("Output", "No completed batch output is available yet.")
            return
        try:
            os.startfile(str(self.last_run_dir))
        except Exception as exc:
            messagebox.showerror("Open output", str(exc))

    def stop_run(self):
        if not self.running:
            return
        self.stop_event.set()
        self._set_app_status("Stopping", self.COLORS["warning"])
        self.log("STOP requested. Current download/ArcGIS process will be terminated as soon as possible.")
        if self.worker_proc is not None:
            try:
                self.worker_proc.terminate()
            except Exception:
                pass

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Quit", "A batch is running. Stop it and exit?"):
                return
            self.stop_run()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
