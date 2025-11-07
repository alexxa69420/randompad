#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HotRandomPad — a tiny open‑source Soundpad‑style app with a twist:
- Map a single global hotkey to MULTIPLE audio files.
- On hotkey press, the app picks one sound based on the chosen mode and plays it.
- Simple GUI to add/edit/remove mappings and import/export a JSON preset.
"""

import json
import os
import random
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set, Any

from PySide6 import QtCore, QtGui, QtWidgets

# Try to import dependencies with friendly errors
try:
    import pygame
except Exception as e:
    QtWidgets.QMessageBox.critical(None, "Dependency error", f"Failed to import pygame: {e}\n\nRun: pip install pygame")
    raise

try:
    from pynput import keyboard as pynput_keyboard
except Exception as e:
    QtWidgets.QMessageBox.critical(None, "Dependency error", f"Failed to import pynput: {e}\n\nRun: pip install pynput")
    raise

try:
    import sounddevice as sd
except Exception as e:
    QtWidgets.QMessageBox.critical(None, "Dependency error", f"Failed to import sounddevice: {e}\n\nRun: pip install sounddevice")
    raise


APP_NAME = "HotRandomPad"
DEFAULT_CONFIG_DIR = (
    Path(os.getenv("APPDATA")) / APP_NAME if os.name == "nt" else Path.home() / f".config/{APP_NAME}"
)
DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"

SUPPORTED_EXTS = {".wav", ".ogg", ".mp3", ".flac", ".aac", ".m4a"}


def key_to_canonical_str(key: Any) -> Optional[str]:
    """Converts a pynput key object to a canonical, comparable string."""
    if isinstance(key, pynput_keyboard.Key):
        return f"key:{key.name}"
    if isinstance(key, pynput_keyboard.KeyCode):
        if hasattr(key, 'vk') and key.vk is not None:
            return f"vk:{key.vk}"
    return None  # Ignore keys we can't reliably identify

# ------------------------------ Data model ------------------------------ #

class HotkeyMapping:
    """Class to store all hotkey information."""
    def __init__(self, hotkey: str, files: List[str], volume: float = 1.0, allow_overlap: bool = True, mode: str = "random", key_combo_strs: Optional[Set[str]] = None,
                 saved_rr_index: int = 0, saved_shuffled_files: Optional[List[str]] = None, saved_shuffled_index: int = 0):
        self.hotkey = hotkey
        self.files = files
        self.volume = volume
        self.allow_overlap = allow_overlap
        self.mode = mode
        self.key_combo_strs = key_combo_strs if key_combo_strs is not None else set()

        # Runtime state, initialized from saved values
        self._rr_index = saved_rr_index
        self._shuffled_files = saved_shuffled_files if saved_shuffled_files is not None else []
        self._shuffled_index = saved_shuffled_index
        self._is_active = False

    def pick_file(self) -> Optional[str]:
        if not self.files: return None
        if self.mode == "round_robin":
            f = self.files[self._rr_index % len(self.files)]; self._rr_index += 1; return f
        elif self.mode == "shuffle":
            if not self._shuffled_files or self._shuffled_index >= len(self._shuffled_files):
                self._shuffled_files, self._shuffled_index = self.files[:], 0
                random.shuffle(self._shuffled_files)
            f = self._shuffled_files[self._shuffled_index]; self._shuffled_index += 1; return f
        else:  # "random" mode
            return random.choice(self.files)

class AppState:
    """Class to manage all application state."""
    def __init__(self, data: Dict[str, HotkeyMapping], selected_device: Optional[str] = None):
        self.data = data
        self.selected_device = selected_device

    @staticmethod
    def empty() -> "AppState": return AppState(data={})

    def to_json(self) -> dict:
        """Serializes the state to a JSON-ready dictionary."""
        mappings_dict = {}
        for hk, mapping in self.data.items():
            mappings_dict[hk] = {
                "hotkey": mapping.hotkey,
                "files": mapping.files,
                "volume": mapping.volume,
                "allow_overlap": mapping.allow_overlap,
                "mode": mapping.mode,
                "key_combination": list(mapping.key_combo_strs),
                # Save playback state
                "saved_rr_index": mapping._rr_index,
                "saved_shuffled_files": mapping._shuffled_files,
                "saved_shuffled_index": mapping._shuffled_index
            }
        return {"selected_device": self.selected_device, "mappings": mappings_dict}

    @staticmethod
    def from_json(data: dict) -> "AppState":
        """Restores the state from a JSON dictionary."""
        mappings = {}
        for hk_key, m_data in data.get("mappings", {}).items():
            key_combo_set = set(m_data.get('key_combination', []))
            
            # Load saved state or use defaults for backward compatibility
            saved_rr_index = m_data.get("saved_rr_index", 0)
            saved_shuffled_files = m_data.get("saved_shuffled_files")
            saved_shuffled_index = m_data.get("saved_shuffled_index", 0)

            mapping = HotkeyMapping(
                hotkey=m_data.get('hotkey', hk_key),
                files=m_data.get('files', []),
                volume=float(m_data.get('volume', 1.0)),
                allow_overlap=bool(m_data.get('allow_overlap', True)),
                mode=m_data.get('mode', 'random'),
                key_combo_strs=key_combo_set,
                # Pass loaded state to the constructor
                saved_rr_index=saved_rr_index,
                saved_shuffled_files=saved_shuffled_files,
                saved_shuffled_index=saved_shuffled_index
            )
            mappings[hk_key] = mapping
            
        return AppState(data=mappings, selected_device=data.get("selected_device"))

# ------------------------------ Audio engine ------------------------------ #
class AudioEngine:
    def __init__(self, devicename: Optional[str] = None):
        try: pygame.mixer.init(devicename=devicename)
        except pygame.error as e: print(f"Audio init error: {e}. Falling back."); pygame.mixer.init()
        self._cache: Dict[str, pygame.mixer.Sound] = {}; self._playing_channels: Dict[str, pygame.mixer.Channel] = {}; self._lock = threading.Lock()
    def reinit(self, devicename: Optional[str] = None):
        self.stop_all(); pygame.mixer.quit()
        try: pygame.mixer.init(devicename=devicename)
        except pygame.error as e: print(f"Audio re-init error: {e}. Falling back."); pygame.mixer.init()
    @staticmethod
    def get_output_devices() -> List[str]:
        try: return [dev['name'] for dev in sd.query_devices() if dev['max_output_channels'] > 0]
        except Exception as e: print(f"Could not query audio devices: {e}"); return []
    def _load(self, path: str) -> pygame.mixer.Sound:
        if path not in self._cache: self._cache[path] = pygame.mixer.Sound(path)
        return self._cache[path]
    def play(self, key: str, path: str, volume: float, allow_overlap: bool):
        def _do():
            try:
                snd = self._load(path); snd.set_volume(max(0.0, min(1.0, volume)))
                with self._lock:
                    if not allow_overlap and (ch := self._playing_channels.get(key)) and ch.get_busy(): return
                    self._playing_channels[key] = snd.play()
            except Exception as e: print(f"Playback error for {path}: {e}")
        threading.Thread(target=_do, daemon=True).start()
    def stop_all(self): pygame.mixer.stop()

# ------------------------------ GUI ------------------------------ #

class HotkeyEditDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, existing_hotkeys: Optional[List[str]] = None, mapping: Optional[HotkeyMapping] = None):
        super().__init__(parent)
        self.setWindowTitle("Hotkey mapping"); self.setModal(True); self.resize(520, 420)
        self.existing_hotkeys = set(existing_hotkeys or []); self.mapping = mapping
        self.key_combination_strs: Set[str] = mapping.key_combo_strs if mapping else set()
        self.setLayout(QtWidgets.QVBoxLayout())
        
        hk_layout = QtWidgets.QHBoxLayout()
        self.hk_edit = QtWidgets.QLineEdit(); self.hk_edit.setPlaceholderText("Press 'Capture...'"); self.hk_edit.setReadOnly(True)
        capture_btn = QtWidgets.QPushButton("Capture…"); capture_btn.clicked.connect(self.capture_hotkey)
        hk_layout.addWidget(QtWidgets.QLabel("Hotkey:")); hk_layout.addWidget(self.hk_edit, 1); hk_layout.addWidget(capture_btn)
        self.layout().addLayout(hk_layout)
        
        files_group = QtWidgets.QGroupBox("Audio files"); files_v = QtWidgets.QVBoxLayout(files_group)
        self.files_list = QtWidgets.QListWidget()
        add_btn = QtWidgets.QPushButton("Add…"); rm_btn = QtWidgets.QPushButton("Remove"); clear_btn = QtWidgets.QPushButton("Clear")
        add_btn.clicked.connect(self.add_files); rm_btn.clicked.connect(self.remove_selected); clear_btn.clicked.connect(self.files_list.clear)
        btns = QtWidgets.QHBoxLayout(); btns.addWidget(add_btn); btns.addWidget(rm_btn); btns.addWidget(clear_btn)
        files_v.addWidget(self.files_list); files_v.addLayout(btns); self.layout().addWidget(files_group)

        opts_group = QtWidgets.QGroupBox("Options"); opts_v = QtWidgets.QFormLayout(opts_group)
        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal); self.volume_slider.setRange(0, 100)
        self.allow_overlap_cb = QtWidgets.QCheckBox("Allow overlapping playback")
        self.mode_combo = QtWidgets.QComboBox(); self.mode_combo.addItems(["Random", "Round-Robin", "Shuffle"])
        opts_v.addRow("Volume:", self.volume_slider); opts_v.addRow("Playback mode:", self.mode_combo); opts_v.addRow("", self.allow_overlap_cb)
        self.layout().addWidget(opts_group)

        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept); box.rejected.connect(self.reject); self.layout().addWidget(box)

        if self.mapping:
            self.hk_edit.setText(self.mapping.hotkey); self.volume_slider.setValue(int(self.mapping.volume * 100))
            self.allow_overlap_cb.setChecked(self.mapping.allow_overlap)
            self.mode_combo.setCurrentText(self.mapping.mode.replace("_", "-").title())
            self.files_list.addItems(self.mapping.files)
        else: self.volume_slider.setValue(100); self.allow_overlap_cb.setChecked(True)

    def add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select audio files", str(Path.home()), "Audio files (*.wav *.ogg *.mp3 *.flac *.aac *.m4a);;All files (*.*)")
        for p in paths:
            if Path(p).suffix.lower() in SUPPORTED_EXTS and not self.files_list.findItems(p, QtCore.Qt.MatchFlag.MatchExactly): self.files_list.addItem(p)
    def remove_selected(self):
        for it in self.files_list.selectedItems(): self.files_list.takeItem(self.files_list.row(it))

    def capture_hotkey(self):
        info = QtWidgets.QMessageBox(self); info.setWindowTitle("Capture hotkey"); info.setIcon(QtWidgets.QMessageBox.Icon.Information)
        info.setText("Press and hold your desired key combination.\nRelease the main key to finish.\n\nPress ESC to cancel."); info.show()
        pressed_keys, main_key = set(), None
        vk_map = {96:"Numpad 0",97:"Numpad 1",98:"Numpad 2",99:"Numpad 3",100:"Numpad 4",101:"Numpad 5",102:"Numpad 6",103:"Numpad 7",104:"Numpad 8",105:"Numpad 9",106:"Numpad *",107:"Numpad +",109:"Numpad -",110:"Numpad .",111:"Numpad /"}
        mod_keys = {pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r, pynput_keyboard.Key.alt_l, pynput_keyboard.Key.alt_r, pynput_keyboard.Key.alt_gr, pynput_keyboard.Key.shift_l, pynput_keyboard.Key.shift_r, pynput_keyboard.Key.cmd_l, pynput_keyboard.Key.cmd_r}
        mod_name_map = {'ctrl_l':'ctrl','ctrl_r':'ctrl','alt_l':'alt','alt_r':'alt','alt_gr':'alt_gr','shift_l':'shift','shift_r':'shift','cmd_l':'cmd','cmd_r':'cmd'}
        def on_press(key):
            nonlocal main_key
            if key == pynput_keyboard.Key.esc: return False
            if main_key is None and key not in mod_keys and not hasattr(key, 'is_modifier'): main_key = key
            pressed_keys.add(key)
        def on_release(key):
            if key == main_key: return False
        with pynput_keyboard.Listener(on_press=on_press, on_release=on_release, suppress=True) as listener: listener.join()
        info.close()
        if main_key:
            canonical_keys = {key_to_canonical_str(k) for k in pressed_keys}
            self.key_combination_strs = {s for s in canonical_keys if s is not None}
            
            mods_display = sorted(list(set(mod_name_map.get(k.name, k.name) for k in pressed_keys if k in mod_keys)))
            if isinstance(main_key, pynput_keyboard.KeyCode) and hasattr(main_key, 'vk'): main_key_display = vk_map.get(main_key.vk, main_key.char or f"<{main_key.vk}>")
            else: main_key_display = main_key.name
            self.hk_edit.setText("+".join(mods_display + [main_key_display]))

    def get_mapping(self) -> Optional[HotkeyMapping]:
        hk = self.hk_edit.text().strip()
        if not hk or not self.key_combination_strs: QtWidgets.QMessageBox.warning(self, "Missing hotkey", "Please capture a valid hotkey."); return None
        if (not self.mapping or hk != self.mapping.hotkey) and hk in self.existing_hotkeys: QtWidgets.QMessageBox.warning(self, "Duplicate hotkey", "This hotkey is already in use."); return None
        files = [self.files_list.item(i).text() for i in range(self.files_list.count())]
        if not files: QtWidgets.QMessageBox.warning(self, "No files", "Please add at least one audio file."); return None
        
        mode = self.mode_combo.currentText().lower().replace("-", "_")
        
        # If editing an existing mapping, carry over its state
        if self.mapping:
            return HotkeyMapping(
                hotkey=hk, key_combo_strs=self.key_combination_strs, files=files, 
                volume=self.volume_slider.value()/100.0, 
                allow_overlap=self.allow_overlap_cb.isChecked(), 
                mode=mode,
                saved_rr_index=self.mapping._rr_index,
                saved_shuffled_files=self.mapping._shuffled_files,
                saved_shuffled_index=self.mapping._shuffled_index
            )
        else:
            # For a new mapping, state will be initialized to defaults
             return HotkeyMapping(
                hotkey=hk, key_combo_strs=self.key_combination_strs, files=files, 
                volume=self.volume_slider.value()/100.0, 
                allow_overlap=self.allow_overlap_cb.isChecked(), 
                mode=mode
            )

class MainWindow(QtWidgets.QMainWindow):
    trigger_signal = QtCore.Signal(HotkeyMapping)
    def __init__(self):
        super().__init__(); self.setWindowTitle(APP_NAME); self.resize(800, 500)
        self.state = self.load_state()
        self.audio = AudioEngine(devicename=self.state.selected_device)
        self.hotkey_listener = None
        self.pressed_keys_str: Set[str] = set()
        self.lock = threading.Lock()
        self.trigger_signal.connect(self.trigger_mapping)
        central = QtWidgets.QWidget(); self.setCentralWidget(central); v = QtWidgets.QVBoxLayout(central)
        toolbar = QtWidgets.QHBoxLayout()
        self.add_btn=QtWidgets.QPushButton("Add"); self.edit_btn=QtWidgets.QPushButton("Edit"); self.remove_btn=QtWidgets.QPushButton("Remove"); self.test_btn=QtWidgets.QPushButton("Test"); self.import_btn=QtWidgets.QPushButton("Import…"); self.export_btn=QtWidgets.QPushButton("Export…"); self.stop_all_btn=QtWidgets.QPushButton("Stop All")
        toolbar.addWidget(self.add_btn); toolbar.addWidget(self.edit_btn); toolbar.addWidget(self.remove_btn); toolbar.addStretch(1); toolbar.addWidget(self.test_btn); toolbar.addWidget(self.stop_all_btn); toolbar.addStretch(1); toolbar.addWidget(self.import_btn); toolbar.addWidget(self.export_btn)
        device_layout = QtWidgets.QHBoxLayout(); self.device_combo = QtWidgets.QComboBox()
        device_layout.addWidget(QtWidgets.QLabel("Audio Output:")); device_layout.addWidget(self.device_combo, 1)
        self.table = QtWidgets.QTableWidget(0, 4); self.table.setHorizontalHeaderLabels(["Hotkey", "# Sounds", "Volume", "Mode"]); self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows); self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addLayout(toolbar); v.addLayout(device_layout); v.addWidget(self.table)
        self.setup_tray(); self.connect_signals()
        self.populate_devices(); self.refresh_table(); self.start_listener()
    def setup_tray(self):
        self.tray=QtWidgets.QSystemTrayIcon(self); self.tray.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MediaPlay))
        menu = QtWidgets.QMenu(); menu.addAction("Show").triggered.connect(self.showNormal); menu.addAction("Quit").triggered.connect(self.close_app)
        self.tray.setContextMenu(menu); self.tray.show()
    def connect_signals(self):
        self.add_btn.clicked.connect(self.add_mapping); self.edit_btn.clicked.connect(self.edit_mapping); self.remove_btn.clicked.connect(self.remove_mapping)
        self.test_btn.clicked.connect(self.test_play); self.stop_all_btn.clicked.connect(self.audio.stop_all); self.import_btn.clicked.connect(self.import_preset)
        self.export_btn.clicked.connect(self.export_preset); self.device_combo.currentTextChanged.connect(self.on_device_changed)
    def load_state(self) -> AppState:
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f: return AppState.from_json(json.load(f))
            except Exception as e: print(f"Failed to load config: {e}")
        return AppState.empty()
    def save_state(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f: json.dump(self.state.to_json(), f, indent=2)
        except Exception as e: print(f"Failed to save config: {e}")
    def populate_devices(self):
        self.device_combo.blockSignals(True); self.device_combo.clear(); devices = self.audio.get_output_devices()
        self.device_combo.addItems(devices)
        if self.state.selected_device in devices: self.device_combo.setCurrentText(self.state.selected_device)
        elif devices: self.on_device_changed(devices[0])
        self.device_combo.blockSignals(False)
    def on_device_changed(self, device_name: str):
        if not device_name or device_name == self.state.selected_device: return
        self.state.selected_device = device_name; self.audio.reinit(device_name); self.save_state()
    def refresh_table(self):
        self.table.setRowCount(0)
        for hk, m in self.state.data.items():
            row = self.table.rowCount(); self.table.insertRow(row)
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(hk)); self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(len(m.files))))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{int(m.volume*100)}%"))
            mode = m.mode.replace("_", " ").title() + ("" if m.allow_overlap else ", No-Overlap")
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(mode))
    def selected_hotkey(self) -> Optional[str]: return self.table.item(self.table.currentRow(), 0).text() if self.table.currentRow() >= 0 else None
    
    def _on_press(self, key):
        with self.lock:
            canon_key = key_to_canonical_str(key)
            if not canon_key: return

            self.pressed_keys_str.add(canon_key)
            
            for mapping in self.state.data.values():
                if not mapping._is_active and mapping.key_combo_strs and mapping.key_combo_strs.issubset(self.pressed_keys_str):
                    mapping._is_active = True
                    self.trigger_signal.emit(mapping)

    def _on_release(self, key):
        with self.lock:
            canon_key = key_to_canonical_str(key)
            if not canon_key: return

            self.pressed_keys_str.discard(canon_key)

            for mapping in self.state.data.values():
                if canon_key in mapping.key_combo_strs:
                    mapping._is_active = False

    def start_listener(self):
        if self.hotkey_listener and self.hotkey_listener.is_alive(): return
        try:
            self.hotkey_listener = pynput_keyboard.Listener(on_press=self._on_press, on_release=self._on_release); self.hotkey_listener.start()
            print("Hotkey listener started.")
        except Exception as e: QtWidgets.QMessageBox.warning(self, "Listener error", f"Failed to start listener:\n{e}")
    def stop_listener(self):
        if self.hotkey_listener: self.hotkey_listener.stop(); self.hotkey_listener.join(); self.hotkey_listener = None; print("Hotkey listener stopped.")

    @QtCore.Slot(HotkeyMapping)
    def trigger_mapping(self, mapping: HotkeyMapping):
        path = mapping.pick_file()
        if path and Path(path).exists():
            self.audio.play(mapping.hotkey, path, mapping.volume, mapping.allow_overlap)
            self.save_state()  # Persist state (e.g., new rr_index) after every play
        elif path: 
            QtWidgets.QMessageBox.warning(self, "Missing file", f"Audio file not found:\n{path}")
            
    def add_mapping(self):
        dlg = HotkeyEditDialog(self, existing_hotkeys=list(self.state.data.keys()))
        if dlg.exec() and (m := dlg.get_mapping()): self.state.data[m.hotkey] = m; self.save_and_refresh()
    def edit_mapping(self):
        if not (hk := self.selected_hotkey()): return
        dlg = HotkeyEditDialog(self, existing_hotkeys=[k for k in self.state.data if k != hk], mapping=self.state.data.get(hk))
        if dlg.exec() and (m := dlg.get_mapping()):
            if m.hotkey != hk: del self.state.data[hk]
            self.state.data[m.hotkey] = m; self.save_and_refresh()
    def remove_mapping(self):
        if (hk := self.selected_hotkey()) and QtWidgets.QMessageBox.question(self, "Remove", f"Delete '{hk}'?") == QtWidgets.QMessageBox.StandardButton.Yes:
            self.state.data.pop(hk, None); self.save_and_refresh()
    def test_play(self):
        if (hk := self.selected_hotkey()) and (m := self.state.data.get(hk)): self.trigger_mapping(m)
    def import_preset(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import preset", "", "JSON (*.json)")
        if not path: return
        try:
            with open(path, "r", encoding="utf-8") as f: self.state = AppState.from_json(json.load(f))
            self.populate_devices(); self.audio.reinit(self.state.selected_device); self.save_and_refresh()
        except Exception as e: QtWidgets.QMessageBox.critical(self, "Import failed", str(e))
    def export_preset(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export preset", "hotrandompad.json", "JSON (*.json)")
        if not path: return
        try:
            with open(path, "w", encoding="utf-8") as f: json.dump(self.state.to_json(), f, indent=2)
        except Exception as e: QtWidgets.QMessageBox.critical(self, "Export failed", str(e))
    def save_and_refresh(self): self.save_state(); self.refresh_table()
    def close_app(self): self.stop_listener(); pygame.mixer.quit(); QtWidgets.QApplication.instance().quit()
    def closeEvent(self, event: QtGui.QCloseEvent):
        if self.tray.isVisible(): self.hide(); event.ignore()
        else: self.close_app()

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()