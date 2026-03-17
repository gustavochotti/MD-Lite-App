import os
import re
import sys
import json
import threading
import logging
import configparser
import subprocess
from io import BytesIO
from datetime import datetime
from tkinter import messagebox, filedialog
import tkinter as tk
from tkinter import ttk
import requests

APP_NAME = "Max Downloader - MD Lite App"
APP_VERSION = "2.1.2"

def get_data_dir() -> str:
    if os.name == "nt":
        base = os.getenv("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, APP_NAME)
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
        return os.path.join(base, APP_NAME)
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(base, APP_NAME)

DATA_DIR = get_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(DATA_DIR, "settings.ini")
HISTORY_FILE = os.path.join(DATA_DIR, "downloads_history.json")
LOG_FILE = os.path.join(DATA_DIR, "logs.txt")

DEFAULT_SETTINGS = {
    "download": {
        "default_format": "MP4",
        "output_dir": os.path.abspath("."),
    }
}

logging.basicConfig(
    filename=LOG_FILE, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

def get_base_path() -> str:
    if hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(sys.argv[0]))

BASE_PATH = get_base_path()
IS_COMPILED = hasattr(sys, '_MEIPASS')

def get_ffmpeg_path() -> str:
    ffmpeg_name = "ffmpeg/ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if IS_COMPILED:
        relative_path = os.path.join(ffmpeg_name)
    else:
        relative_path = ffmpeg_name
    return os.path.join(BASE_PATH, relative_path)

def get_yt_dlp_path() -> str:
    yt_dlp_name = "yt-dlp.exe" if os.name == "nt" else "yt-dlp"
    if IS_COMPILED:
        relative_path = os.path.join(yt_dlp_name)
    else:
        relative_path = yt_dlp_name
    return os.path.join(BASE_PATH, relative_path)

def sanitize_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", title).strip()

def format_size(num_bytes: int) -> str:
    units = ["bytes", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024

def is_youtube_url(text: str) -> bool:
    return bool(re.search(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/", text, re.IGNORECASE))

def ensure_history_file():
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)

def read_history():
    ensure_history_file()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def write_history(entries):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

def append_history(item):
    hist = read_history()
    hist.insert(0, item)
    write_history(hist)

def load_settings():
    cfg = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        save_settings(DEFAULT_SETTINGS)
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    cfg.read(CONFIG_FILE, encoding="utf-8")
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for sec in cfg.sections():
        if sec not in merged:
            merged[sec] = {}
        for key, val in cfg.items(sec):
            merged[sec][key] = val
            if isinstance(merged[sec][key], str) and merged[sec][key].lower() in ["true", "false"]:
                merged[sec][key] = merged[sec][key].lower() == "true"
    return merged

def save_settings(settings):
    cfg = configparser.ConfigParser()
    for sec, kv in settings.items():
        cfg[sec] = {}
        for k, v in kv.items():
            cfg[sec][k] = str(v)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        cfg.write(f)

def internet_available() -> bool:
    try:
        requests.get("https://www.google.com", timeout=4)
        return True
    except Exception:
        return False

class DownloadManager:
    def __init__(self, ui_ref):
        self.ui = ui_ref
        self.progress_regex = re.compile(r"\[download\]\s+([0-9.]+)%")
        self.playlist_regex = re.compile(r"Downloading video (\d+)\s+of\s+(\d+)")

    def parse_progress(self, line: str, is_generic_dl: bool = False):
        try:
            line = line.strip()
            if not is_generic_dl:
                plm = self.playlist_regex.search(line)
                if plm:
                    cur, tot = plm.groups()
                    self.ui.set_status(f"Downloading video {cur} of {tot}...")
                    self.ui.update_progress(0.0)
                    return
            if line.startswith("[download]"):
                m = self.progress_regex.search(line)
                if m:
                    p = float(m.group(1)) / 100.0
                    self.ui.update_progress(p)
                    self.ui.set_status(line)
                    return
            if line.startswith("[ExtractAudio]"):
                self.ui.set_status("Converting to MP3...")
            elif line.startswith("[Merger]") or "[ffmpeg] Merging formats into" in line:
                self.ui.set_status("Merging video and audio...")
            elif "[ThumbnailsConvertor]" in line:
                self.ui.set_status("Processing metadata...")
            elif line.startswith("WARNING:") or line.startswith("ERROR:"):
                self.ui.set_status(line)
        except Exception as e:
            logging.warning(f"Error parsing progress: {e}")

    def download(self, url: str, out_dir: str, out_format: str, embed_thumbnail: bool, 
                 resolution: str | None = None, audio_lang: str | None = None, 
                 title: str | None = None, is_playlist: bool = False, is_generic_dl: bool = False):
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            yt_dlp_path = get_yt_dlp_path()
            ffmpeg_dir = os.path.dirname(get_ffmpeg_path())
            if not os.path.exists(yt_dlp_path):
                raise FileNotFoundError(f"yt-dlp.exe not found in {yt_dlp_path}")
            if not os.path.exists(get_ffmpeg_path()):
                 raise FileNotFoundError(f"ffmpeg.exe not found in {ffmpeg_dir}")
            if is_playlist:
                playlist_folder = os.path.join(out_dir, sanitize_filename(title or "playlist"))
                os.makedirs(playlist_folder, exist_ok=True)
                output_template = os.path.join(playlist_folder, "%(playlist_index)s - %(title)s.%(ext)s")
            elif not is_generic_dl and title:
                output_template = os.path.join(out_dir, f"{sanitize_filename(title)}.%(ext)s")
            else:
                output_template = os.path.join(out_dir, "%(title)s.%(ext)s")
            command = [
                yt_dlp_path,
                "--ffmpeg-location", ffmpeg_dir,
                "--no-mtime",
                "--progress",
                "-o", output_template
            ]
            if not is_playlist and not is_generic_dl:
                command.append("--no-playlist")
            if embed_thumbnail:
                command.append("--embed-thumbnail")
            lang_filter = f"[language={audio_lang}]" if (audio_lang and audio_lang.lower() not in ("default", "default (best quality)")) else ""
            if out_format.upper() == "MP3":
                format_string = f"bestaudio[ext=m4a]{lang_filter}/bestaudio[ext=aac]{lang_filter}/bestaudio{lang_filter}/bestaudio"
                command.extend(["-f", format_string, "-x", "--audio-format", "mp3", "--audio-quality", "0"])
            else:
                if is_generic_dl:
                    format_string = f"bestvideo[ext=mp4]+bestaudio[ext=m4a]{lang_filter}/bestvideo[ext=mp4]+bestaudio{lang_filter}/bestvideo+bestaudio{lang_filter}/best"
                else:
                    height = 1080
                    if resolution and resolution.isdigit():
                        height = int(resolution)
                    format_string = (
                        f"bestvideo[height<={height}]+bestaudio[ext=m4a]{lang_filter}/"
                        f"bestvideo[height<={height}]+bestaudio{lang_filter}/"
                        f"best[height<={height}]/best"
                    )
                command.extend(["-f", format_string, "--merge-output-format", "mp4"])
            command.append(url)
            logging.info("Executing: %s", " ".join(command))
            self.ui.set_status("Starting process...")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creation_flags
            )
            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                logging.info("[yt-dlp] %s", line.strip())
                self.parse_progress(line, is_generic_dl)
            process.wait()
            if process.returncode == 0:
                self.ui.update_progress(1.0)
                self.ui.set_status("Download completed.")
                try:
                    sane_title = sanitize_filename(title or "")
                    final_file_path = ""
                    if is_playlist:
                        final_file_path = os.path.join(out_dir, sanitize_filename(title or "playlist"))
                    else:
                        ext = "mp3" if out_format.upper() == "MP3" else "mp4"
                        guess = os.path.join(out_dir, f"{sane_title}.{ext}") if (sane_title and not is_generic_dl) else ""
                        if guess and os.path.exists(guess):
                            final_file_path = guess
                    if not final_file_path or not os.path.exists(final_file_path):
                        final_file_path = out_dir
                    size_bytes = 0
                    if os.path.isfile(final_file_path):
                         size_bytes = os.path.getsize(final_file_path)
                    append_history({
                        "title": title or url,
                        "file": final_file_path,
                        "format": out_format.upper(),
                        "size_bytes": size_bytes,
                        "size": format_size(size_bytes),
                        "when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "url": url,
                        "thumb_url": self.ui.fetched_info.get("thumbnail", "") if getattr(self.ui, "fetched_info", None) and not is_generic_dl else ""
                    })
                except Exception as e:
                    logging.warning("Failed to record history: %s", e)
            else:
                error_output = (process.stderr.read() or "").strip()
                logging.error("yt-dlp failed (%s): %s", process.returncode, error_output)
                messagebox.showerror("Download Error", f"yt-dlp failed:\n\n{error_output or 'Unknown error'}")
                self.ui.set_status("Download failed.")
        except Exception as e:
            messagebox.showerror("Unexpected Error", f"An unexpected error occurred:\n{e}")
            logging.exception("Unexpected error during download:")
        finally:
            if is_generic_dl:
                final_state = "generic"
            elif self.ui.fetched_info:
                final_state = "ready"
            else:
                final_state = "initial"
            self.ui.set_controls_state(final_state)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.title(APP_NAME)
        self.geometry("700x500") 
        self.minsize(600, 450)   
        icon_path = os.path.join(BASE_PATH, "lite_app_logo.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass
        self.fetched_info: dict | None = None
        self.is_playlist = False
        self.DEFAULT_LANG_CODE = "default (best quality)"
        self.manager = DownloadManager(self)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self._build_download_tab()
        self._build_update_tab()
        self.set_controls_state("initial")

    def _build_download_tab(self):
        download_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(download_tab, text="Download")
        download_tab.grid_columnconfigure(0, weight=1)
        download_tab.grid_rowconfigure(3, weight=1)
        header = ttk.Label(download_tab, text="Download Video or Audio", font=("-weight bold", 16))
        header.grid(row=0, column=0, sticky="w", pady=(0, 5))
        sub = ttk.Label(download_tab, text="Paste the link and click Search.")
        sub.grid(row=1, column=0, sticky="w", pady=(0, 10))
        url_row = ttk.Frame(download_tab)
        url_row.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        url_row.grid_columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var, width=60)
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8), ipady=2)
        self.fetch_btn = ttk.Button(url_row, text="Search", command=self.fetch_info)
        self.fetch_btn.grid(row=0, column=1, sticky="e")
        self.content_frame = ttk.Frame(download_tab)
        self.content_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        self.content_frame.grid_columnconfigure(0, weight=1)
        content_parent = self.content_frame
        self.details_frame = ttk.Frame(content_parent, padding=5, relief="groove", borderwidth=1)
        self.details_frame.grid_columnconfigure(0, weight=1)
        self.title_lbl = ttk.Label(self.details_frame, text="Waiting for link...", font=("-weight bold", 12), wraplength=550, justify="left")
        self.title_lbl.grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.playlist_frame = ttk.Frame(content_parent)
        self.download_playlist_var = tk.BooleanVar(value=False)
        self.playlist_checkbox = ttk.Checkbutton(self.playlist_frame, text="Download entire playlist", variable=self.download_playlist_var)
        self.playlist_checkbox.grid(row=0, column=0, sticky="w", padx=5)
        self.yt_opts_frame = ttk.Frame(content_parent, padding=5, relief="groove", borderwidth=1)
        self.yt_opts_frame.grid_columnconfigure((0, 1), weight=1, uniform="group1")
        ttk.Label(self.yt_opts_frame, text="Quality").grid(row=0, column=0, sticky="w", padx=5, pady=(0, 2))
        self.quality_var = tk.StringVar(value="720p")
        self.quality_box = ttk.Combobox(self.yt_opts_frame, values=["1080p", "720p", "480p", "360p"], textvariable=self.quality_var, state="readonly", width=15)
        self.quality_box.grid(row=1, column=0, sticky="ew", padx=5)
        ttk.Label(self.yt_opts_frame, text="Audio Language").grid(row=0, column=1, sticky="w", padx=5, pady=(0, 2))
        self.lang_var = tk.StringVar(value="default (best quality)")
        self.lang_box = ttk.Combobox(self.yt_opts_frame, values=["default (best quality)"], textvariable=self.lang_var, state="readonly", width=20)
        self.lang_box.grid(row=1, column=1, sticky="ew", padx=5)
        self.shared_opts_frame = ttk.Frame(content_parent, padding=5, relief="groove", borderwidth=1)
        self.shared_opts_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(self.shared_opts_frame, text="Format").grid(row=0, column=0, sticky="w", padx=5, pady=(0, 2))
        self.format_var = tk.StringVar(value=self.settings["download"]["default_format"])
        self.format_box = ttk.Combobox(self.shared_opts_frame, values=["MP4", "MP3"], textvariable=self.format_var, state="readonly", width=15)
        self.format_box.grid(row=1, column=0, sticky="w", padx=5, pady=(0, 5))
        self.out_row = ttk.Frame(content_parent, padding=5, relief="groove", borderwidth=1)
        self.out_row.grid_columnconfigure(0, weight=1)
        self.out_dir_var = tk.StringVar(value=self.settings["download"]["output_dir"])
        self.out_dir_lbl = ttk.Label(self.out_row, text=f"Saving to: {self.out_dir_var.get()}", anchor="w", wraplength=450)
        self.out_dir_lbl.grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        self.choose_folder_btn = ttk.Button(self.out_row, text="Change...", command=self.choose_folder)
        self.choose_folder_btn.grid(row=0, column=1, sticky="e", padx=5, pady=5)
        action = ttk.Frame(download_tab)
        action.grid(row=4, column=0, sticky="sew", pady=(10, 0))
        action.grid_columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(action, orient="horizontal", mode="determinate", maximum=1.0)
        self.progress.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.status_var = tk.StringVar(value="Ready.")
        self.status_lbl = ttk.Label(action, textvariable=self.status_var, anchor="w")
        self.status_lbl.grid(row=1, column=0, sticky="ew")
        self.download_btn = ttk.Button(action, text="Start Download", command=self.start_download)
        self.download_btn.grid(row=2, column=0, sticky="ew", pady=(5, 0), ipady=4)

    def _build_update_tab(self):
        update_tab = ttk.Frame(self.notebook, padding=20)
        self.notebook.add(update_tab, text="Update")
        update_tab.grid_columnconfigure(0, weight=1)
        ttk.Label(update_tab, text="Update MD Lite App", font=("-weight bold", 16)).grid(row=0, column=0, pady=(0, 10), sticky="w")
        ttk.Label(update_tab, text="Click the button below to check for updates.", wraplength=500, justify="left").grid(row=1, column=0, pady=(0, 20), sticky="w")
        self.update_btn = ttk.Button(update_tab, text="Check and Update", command=self.start_update)
        self.update_btn.grid(row=2, column=0, sticky="ew", ipady=5, pady=(0, 10))
        self.update_progress_bar = ttk.Progressbar(update_tab, orient="horizontal", mode="indeterminate")
        self.update_progress_bar.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        self.update_progress_bar.grid_remove()
        self.update_status_var = tk.StringVar(value="Waiting...")
        ttk.Label(update_tab, textvariable=self.update_status_var, anchor="center").grid(row=4, column=0, sticky="ew")

    def start_update(self):
        if not internet_available():
            messagebox.showerror("Error", "No internet connection.")
            return
        yt_dlp_path = get_yt_dlp_path()
        if not os.path.exists(yt_dlp_path):
            messagebox.showerror("Error", f"yt-dlp.exe not found at:\n{yt_dlp_path}")
            return
        self.update_btn.config(state="disabled")
        self.update_status_var.set("Checking for updates...")
        self.update_progress_bar.grid()
        self.update_progress_bar.start()
        threading.Thread(target=self._run_update_thread, daemon=True).start()

    def _run_update_thread(self):
        output_str = ""
        try:
            yt_dlp_path = get_yt_dlp_path()
            command = [yt_dlp_path, "-U"]
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.run(
                command,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=creation_flags
            )
            output_str = process.stdout.strip() + "\n" + process.stderr.strip()
            output_str = output_str.strip()
        except Exception as e:
            output_str = f"Failed to execute update:\n{e}"
        self.after(0, self._finish_update, output_str)

    def _finish_update(self, output: str):
        self.update_progress_bar.stop()
        self.update_progress_bar.grid_remove()
        self.update_btn.config(state="normal")
        if "Updated yt-dlp" in output or "yt-dlp is up to date" in output:
             if "Updated yt-dlp" in output:
                 self.update_status_var.set("MD Lite App updated successfully!")
             else:
                 self.update_status_var.set("MD Lite App is already the latest version.")
        else:
            self.update_status_var.set("An error occurred during update.")
        messagebox.showinfo("Update Result", output)

    def set_status(self, text: str):
        if hasattr(self, 'status_var') and self.status_var:
            try:
                self.after(0, lambda: self.status_var.set(text))
            except tk.TclError:
                pass 

    def update_progress(self, value: float):
        if hasattr(self, 'progress') and self.progress.winfo_exists():
            v = min(max(value, 0.0), 1.0)
            try:
                self.after(0, lambda: self.progress.config(value=v))
            except Exception:
                pass 

    def choose_folder(self):
        initial = self.out_dir_var.get() or os.path.abspath(".")
        path = filedialog.askdirectory(initialdir=initial)
        if path:
            self.out_dir_var.set(path)
            self.out_dir_lbl.configure(text=f"Saving to: {path}")
            self.settings["download"]["output_dir"] = path
            save_settings(self.settings)

    def set_controls_state(self, state: str):
        def _update():
            if not hasattr(self, 'url_entry') or not self.winfo_exists():
                return
            url_state = "disabled" if state == "busy" else "normal"
            fetch_state = "disabled" if state == "busy" else "normal"
            download_state = "normal" if (state == "ready" or state.startswith("generic")) else "disabled"
            folder_state = "normal" if (state == "ready" or state.startswith("generic")) else "disabled"
            shared_opts_state = "normal" if (state == "ready" or state.startswith("generic")) else "disabled"
            yt_opts_state = "normal" if state == "ready" else "disabled"
            playlist_state = "normal" if state == "ready" and self.is_playlist else "disabled"
            update_tab_state = "disabled" if state == "busy" else "normal"
            try:
                self.notebook.tab(1, state=update_tab_state)
            except Exception:
                pass 
            self.url_entry.configure(state=url_state)
            self.fetch_btn.configure(state=fetch_state)
            self.download_btn.configure(state=download_state)
            self.choose_folder_btn.configure(state=folder_state)
            self.format_box.configure(state=shared_opts_state)
            yt_opts_combo_state = "readonly" if yt_opts_state == "normal" else "disabled"
            self.quality_box.configure(state=yt_opts_combo_state)
            self.lang_box.configure(state=yt_opts_combo_state)
            self.playlist_checkbox.configure(state=playlist_state)
            if state == "ready":
                self.details_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            else:
                self.details_frame.grid_remove()
            if playlist_state == "normal":
                self.playlist_frame.grid(row=1, column=0, sticky="w", padx=0, pady=(0, 6))
            else:
                self.playlist_frame.grid_remove()
            if state == "ready":
                self.yt_opts_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
            else:
                self.yt_opts_frame.grid_remove()
            if state == "ready" or state.startswith("generic"):
                self.shared_opts_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))
            else:
                self.shared_opts_frame.grid_remove()
            if state == "ready" or state.startswith("generic"):
                self.out_row.grid(row=4, column=0, sticky="ew", pady=(0, 6))
            else:
                self.out_row.grid_remove()
        if self.winfo_exists():
            self.after(0, _update)

    def fetch_info(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Warning", "Empty URL.")
            return
        if not internet_available():
            messagebox.showerror("Error", "No internet.")
            return
        self.set_status("Analyzing link...")
        self.set_controls_state("busy")
        self.fetched_info = None
        self.is_playlist = False
        if is_youtube_url(url):
            self.set_status("Fetching YouTube info...")
            threading.Thread(target=self._fetch_info_thread, args=(url,), daemon=True).start()
        else:
            self.set_status("Generic link detected. Ready to download.")
            self.set_controls_state("generic")
            self.title_lbl.configure(text="Generic site download")

    def _fetch_info_thread(self, url: str):
        try:
            yt_dlp_path = get_yt_dlp_path()
            if not os.path.exists(yt_dlp_path):
                raise FileNotFoundError(f"yt-dlp.exe not found in {yt_dlp_path}")
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            command = [yt_dlp_path, url, "--dump-json", "--flat-playlist"]
            process = subprocess.run(
                command,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=creation_flags,
            )
            if process.returncode != 0:
                raise Exception(f"yt-dlp failed.\n{process.stderr[:500]}...")
            lines = [ln for ln in process.stdout.strip().split("\n") if ln.strip()]
            if len(lines) > 1 or ('"_type": "playlist"' in lines[0]):
                self.is_playlist = True
                playlist_data = json.loads(lines[0])
                self.fetched_info = playlist_data
                num_videos = playlist_data.get("playlist_count", max(1, len(lines)))
                title = f"Playlist: {playlist_data.get('title', 'N/A')} ({num_videos} videos)"
                def _ui():
                    if not self.winfo_exists(): return
                    self.title_lbl.configure(text=title)
                    self.playlist_checkbox.configure(text=f"Download entire playlist ({num_videos} videos)")
                    self.download_playlist_var.set(True)
                    self.quality_box.configure(values=["Playlist Default"])
                    self.quality_var.set("Playlist Default")
                    self.lang_box.configure(values=["default (best quality)"])
                    self.lang_var.set("default (best quality)")
                if self.winfo_exists(): self.after(0, _ui) 
            else:
                self.is_playlist = False
                data = json.loads(lines[0])
                self.fetched_info = data
                title = data.get("title", "N/A")
                formats = data.get("formats", [])
                resolutions = sorted(list(set(f.get("height") for f in formats if f.get("vcodec") != "none" and f.get("height"))), reverse=True)
                qualities = [f"{h}p" for h in resolutions if h <= 1080] or ([f"{h}p" for h in resolutions] if resolutions else ["720p"])
                audio_formats = [f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"]
                languages = sorted(list(set(f.get("language") for f in audio_formats if f.get("language"))))
                final_lang_list = ["default (best quality)"] + (languages if languages else [])
                def _ui():
                    if not self.winfo_exists(): return
                    self.title_lbl.configure(text=title)
                    self.quality_box.configure(values=qualities)
                    if self.quality_var.get() not in qualities:
                        self.quality_var.set(qualities[0])
                    self.lang_box.configure(values=final_lang_list)
                    default_lang = "en" if "en" in final_lang_list else (languages[0] if languages else "default (best quality)")
                    self.lang_var.set(default_lang)
                if self.winfo_exists(): self.after(0, _ui)
            self.set_status("Ready to download.")
            self.set_controls_state("ready")
        except Exception as e:
            logging.exception("Fetch info failed:")
            if "application has been destroyed" not in str(e):
                messagebox.showerror("Error", f"Fetch failed:\n{e}")
            self.set_status("Failed.")
            self.set_controls_state("initial")

    def start_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Warning", "Empty URL.")
            return
        try:
            ffmpeg_path = get_ffmpeg_path()
            yt_dlp_path = get_yt_dlp_path()
            if not os.path.exists(ffmpeg_path):
                messagebox.showerror("Error", f"FFmpeg not found at:\n{ffmpeg_path}")
                return
            if not os.path.exists(yt_dlp_path):
                messagebox.showerror("Error", f"yt-dlp.exe not found at:\n{yt_dlp_path}")
                return
        except Exception as e:
             messagebox.showerror("Error verifying files", str(e))
             return
        if not internet_available():
            messagebox.showerror("Error", "No internet.")
            return
        out_dir = self.out_dir_var.get().strip() or os.path.abspath(".")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Invalid folder:\n{e}")
            return
        self.settings["download"]["output_dir"] = out_dir
        self.settings["download"]["default_format"] = self.format_var.get()
        save_settings(self.settings)
        out_format = self.format_var.get()
        embed_thumbnail = False 
        self.set_controls_state("busy")
        self.set_status("Starting...")
        self.update_progress(0.0)
        if self.fetched_info and is_youtube_url(url):
            resolution = self.quality_var.get().replace("p", "")
            selected_lang = self.lang_var.get()
            download_playlist = self.is_playlist and self.download_playlist_var.get()
            title = self.fetched_info.get("title", "download")
            t_args = (url, out_dir, out_format, embed_thumbnail, resolution, selected_lang, title, download_playlist, False)
        else:
            t_args = (url, out_dir, out_format, embed_thumbnail, None, None, url, False, True)
        t = threading.Thread(
            target=self.manager.download,
            args=t_args,
            daemon=True,
        )
        t.start()

if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        save_settings(DEFAULT_SETTINGS)
    ensure_history_file() 
    app = App()
    app.mainloop()