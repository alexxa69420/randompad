#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HotRandomPad — a tiny open‑source Soundpad‑style app with a twist:
- Map a single global hotkey to MULTIPLE audio files.
- On hotkey press, the app picks one sound based on the chosen mode and plays it.
- Simple GUI to add/edit/remove mappings and import/export a JSON preset.
"""

import sys
import json
import random
from enum import Enum
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Callable

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTableWidget, QTableWidgetItem, QFileDialog,
    QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QComboBox, QMessageBox, QSystemTrayIcon, QMenu, QAction
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt

from pynput import keyboard
import pygame

# SDL2 устройство, опционально канешн, но без него нету листа
try:
    from pygame._sdl2 import audio as sdl2_audio
except Exception:
    sdl2_audio = None



APP_TITLE = "RandomPad"
CONFIG_NAME = "randompad.json"


# моделька
class PlaybackMode(str, Enum):
    RANDOM = "random"
    ROUND_ROBIN = "round_robin"
    SHUFFLE = "shuffle"


@dataclass
class HotkeyBinding:
    title: str
    hotkey: str
    files: List[str]
    mode: PlaybackMode = PlaybackMode.RANDOM
    device: Optional[str] = None


# property map
BINDING_SCHEMA = {
    "title": "title",
    "hotkey": "hotkey",
    "files": "files",
    "mode": ("mode", lambda m: m.value, lambda s: PlaybackMode(s)),
    "device": "device",
}


def encode_by_schema(obj, schema) -> dict:
    result = {}
    for key, spec in schema.items():
        if isinstance(spec, tuple):
            attr, encoder, _ = spec
            value = getattr(obj, attr)
            result[key] = encoder(value) if encoder else value
            continue
        result[key] = getattr(obj, spec)
    return result


def decode_by_schema(cls, data: dict, schema):
    kwargs = {}
    for key, spec in schema.items():
        if isinstance(spec, tuple):
            attr, _, decoder = spec
            raw = data.get(key)
            kwargs[attr] = decoder(raw) if decoder else raw
            continue
        kwargs[spec] = data.get(key)
    return cls(**kwargs)


# Селекторы 
class SelectorBase:
    def __init__(self, items: List[str]):
        self.items = list(items)

    def next_item(self) -> Optional[str]:
        raise NotImplementedError


class RandomSelector(SelectorBase):
    def next_item(self) -> Optional[str]:
        if not self.items:
            return None
        return random.choice(self.items)


class RoundRobinSelector(SelectorBase):
    def __init__(self, items: List[str]):
        super().__init__(items)
        self.position = 0

    def next_item(self) -> Optional[str]:
        if not self.items:
            return None
        item = self.items[self.position % len(self.items)]
        self.position += 1
        return item


class ShuffleSelector(SelectorBase):
    def __init__(self, items: List[str]):
        super().__init__(items)
        self.queue: List[str] = []

    def refill(self):
        # фикc: пустой список очищает очередь
        if not self.items:
            self.queue = []
            return
        self.queue = list(self.items)
        random.shuffle(self.queue)

    def next_item(self) -> Optional[str]:
        if not self.queue:
            self.refill()
        if not self.queue:
            return None
        return self.queue.pop()


SELECTOR_FACTORY: Dict[PlaybackMode, Callable[[List[str]], SelectorBase]] = {
    PlaybackMode.RANDOM: RandomSelector,
    PlaybackMode.ROUND_ROBIN: RoundRobinSelector,
    PlaybackMode.SHUFFLE: ShuffleSelector,
}


# Audio Engine
class AudioPlayer:
    def __init__(self):
        self.device_name: Optional[str] = None
        self.cache: Dict[str, pygame.mixer.Sound] = {}
        self._ensure_mixer(None)  # изначально вообще не было никакого инита, исправлено ( ДОЛЖНО БЫТЬ )

    def available_devices(self) -> List[str]:
        if sdl2_audio is None:
            return []
        return list(sdl2_audio.get_audio_device_names(False))

    def set_device(self, device: Optional[str]):
        if device == self.device_name:
            return
        self.device_name = device
        self._ensure_mixer(device)

    def _ensure_mixer(self, device: Optional[str]):
        # сейвовый реинициал миксера
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        kwargs = {}
        if device and sdl2_audio:
            kwargs["devicename"] = device
        pygame.mixer.init(**kwargs)

    def _get_sound(self, path: str) -> Optional[pygame.mixer.Sound]:
        # кеш звуков по абсолютному пути
        p = str(Path(path).resolve())
        if p in self.cache:
            return self.cache[p]
        try:
            sound = pygame.mixer.Sound(p)
        except Exception:
            return None
        self.cache[p] = sound
        return sound

    def play(self, path: str):
        sound = self._get_sound(path)
        if not sound:
            return
        channel = pygame.mixer.find_channel(True)
        if not channel:
            return
        channel.play(sound)


# HotKey Eng
class HotkeyEngine:
    def __init__(self, on_hotkey: Callable[[str], None]):
        self.on_hotkey = on_hotkey
        self.listener: Optional[keyboard.GlobalHotKeys] = None
        self.bind_map: Dict[str, str] = {}

    def set_bindings(self, hotkeys: List[HotkeyBinding]):
        self.bind_map = {b.hotkey: b.hotkey for b in hotkeys}
        self.restart()  # вот тут мы делаем пересоздание листенера при ченджах

    def restart(self):
        self.stop()
        mapping = {hk: self._make_handler(hk) for hk in self.bind_map}
        if not mapping:
            return
        self.listener = keyboard.GlobalHotKeys(mapping)
        self.listener.start()

    def _make_handler(self, hotkey: str):
        def _handler():
            self.on_hotkey(hotkey)
        return _handler

    def stop(self):
        if not self.listener:
            return
        try:
            self.listener.stop()
        except Exception:
            pass
        self.listener = None

# AppState
class AppState:
    def __init__(self):
        self.bindings: List[HotkeyBinding] = []
        self.selectors: Dict[str, SelectorBase] = {}
        self.player = AudioPlayer()
        self.config_path = Path(CONFIG_NAME)

    def find_by_hotkey(self, hotkey: str) -> Optional[HotkeyBinding]:
        for b in self.bindings:
            if b.hotkey == hotkey:
                return b
        return None

    def selector_for(self, hotkey: str) -> Optional[SelectorBase]:
        if hotkey in self.selectors:
            return self.selectors[hotkey]
        binding = self.find_by_hotkey(hotkey)
        if not binding:
            return None
        strategy = SELECTOR_FACTORY[binding.mode]
        selector = strategy(binding.files)
        self.selectors[hotkey] = selector
        return selector

    def set_device_for(self, device: Optional[str]):
        self.player.set_device(device)

    def export_json(self, path: Path):
        data = [encode_by_schema(b, BINDING_SCHEMA) for b in self.bindings]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_json(self, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.bindings = [decode_by_schema(HotkeyBinding, d, BINDING_SCHEMA) for d in raw]
        self.selectors.clear()

    # CRUD и конфигыы
    def add_binding(self, b: HotkeyBinding):
        self.bindings.append(b)
        self.selectors.pop(b.hotkey, None)

    def update_binding(self, row: int, b: HotkeyBinding):
        if row < 0 or row >= len(self.bindings):
            return
        old_hotkey = self.bindings[row].hotkey
        self.bindings[row] = b
        if b.hotkey != old_hotkey:
            self.selectors.pop(old_hotkey, None)

    def delete_row(self, row: int):
        if row < 0 or row >= len(self.bindings):
            return
        hotkey = self.bindings[row].hotkey
        del self.bindings[row]
        self.selectors.pop(hotkey, None)

    def save_default(self):
        self.export_json(self.config_path)

    def load_default(self):
        if not self.config_path.exists():
            return
        self.import_json(self.config_path)


class BindingDialog(QDialog):
    def __init__(self, parent=None, binding: Optional[HotkeyBinding] = None, devices: List[str] = None):
        super().__init__(parent)
        self.setWindowTitle("Binding")
        self.resize(600, 420)
        self.devices = devices or []
        self.title_edit = QLineEdit()
        self.hotkey_edit = QLineEdit()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([m.value for m in PlaybackMode])
        self.device_combo = QComboBox()
        self.device_combo.addItem("system default")
        for d in self.devices:
            self.device_combo.addItem(d)
        self.files_list = QListWidget()
        add_file = QPushButton("Add files")
        remove_file = QPushButton("Remove selected")
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")

        layout = QVBoxLayout()
        form = QVBoxLayout()
        form.addWidget(QLabel("Title"))
        form.addWidget(self.title_edit)
        form.addWidget(QLabel("Hotkey (e.g. <ctrl>+<alt>+a)"))
        form.addWidget(self.hotkey_edit)
        form.addWidget(QLabel("Mode"))
        form.addWidget(self.mode_combo)
        form.addWidget(QLabel("Output device"))
        form.addWidget(self.device_combo)
        form.addWidget(QLabel("Files"))
        form.addWidget(self.files_list)
        btns = QHBoxLayout()
        btns.addWidget(add_file)
        btns.addWidget(remove_file)
        layout.addLayout(form)
        layout.addLayout(btns)
        actions = QHBoxLayout()
        actions.addWidget(ok_btn)
        actions.addWidget(cancel_btn)
        layout.addLayout(actions)
        self.setLayout(layout)

        add_file.clicked.connect(self._choose_files)
        remove_file.clicked.connect(self._remove_selected)
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

        if binding:
            self.title_edit.setText(binding.title)
            self.hotkey_edit.setText(binding.hotkey)
            self.mode_combo.setCurrentText(binding.mode.value)
            index = 0 if not binding.device else self.device_combo.findText(binding.device)
            self.device_combo.setCurrentIndex(max(0, index))
            for f in binding.files:
                self.files_list.addItem(QListWidgetItem(f))

    def _choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose audio files", "", "Audio (*.mp3 *.wav *.ogg *.flac);;All (*.*)")
        if not files:
            return
        for f in files:
            self.files_list.addItem(QListWidgetItem(f))

    def _remove_selected(self):
        for i in reversed(range(self.files_list.count())):
            if self.files_list.item(i).isSelected():
                self.files_list.takeItem(i)

    def build_binding(self) -> Optional[HotkeyBinding]:
        # VALID полей
        title = self.title_edit.text().strip()
        hotkey = self.hotkey_edit.text().strip()
        mode = PlaybackMode(self.mode_combo.currentText())
        device = self.device_combo.currentText()
        device = None if device == "system default" else device
        files = [self.files_list.item(i).text() for i in range(self.files_list.count())]
        if not title or not hotkey or not files:
            return None
        return HotkeyBinding(title=title, hotkey=hotkey, files=files, mode=mode, device=device)


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setWindowTitle(APP_TITLE)
        self.resize(820, 520)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Title", "Hotkey", "Mode", "Files"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.btn_add = QPushButton("Add")
        self.btn_edit = QPushButton("Edit")
        self.btn_delete = QPushButton("Delete")
        self.btn_import = QPushButton("Import JSON")
        self.btn_export = QPushButton("Export JSON")
        self.btn_devices = QPushButton("Set Output Device")
        self.btn_listen = QPushButton("Restart Hotkeys")

        top = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.table)
        controls = QHBoxLayout()
        for b in [self.btn_add, self.btn_edit, self.btn_delete, self.btn_import, self.btn_export, self.btn_devices, self.btn_listen]:
            controls.addWidget(b)
        layout.addLayout(controls)
        top.setLayout(layout)
        self.setCentralWidget(top)

        self.btn_add.clicked.connect(self.add_binding)
        self.btn_edit.clicked.connect(self.edit_binding)
        self.btn_delete.clicked.connect(self.delete_binding)
        self.btn_import.clicked.connect(self.import_json)
        self.btn_export.clicked.connect(self.export_json)
        self.btn_devices.clicked.connect(self.choose_device)
        self.btn_listen.clicked.connect(self.restart_hotkeys)

        self.tray = QSystemTrayIcon(QIcon())
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu()
        act_show = QAction("Show")
        act_hide = QAction("Hide")
        act_quit = QAction("Quit")
        menu.addAction(act_show)
        menu.addAction(act_hide)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        act_show.triggered.connect(self.showNormal)
        act_hide.triggered.connect(self.hide)
        act_quit.triggered.connect(self.close)
        self.tray.show()

        self.load_initial()

    def load_initial(self):
        self.state.load_default()
        self.refresh_table()

    def refresh_table(self):
        self.table.setRowCount(len(self.state.bindings))
        for i, b in enumerate(self.state.bindings):
            self.table.setItem(i, 0, QTableWidgetItem(b.title))
            self.table.setItem(i, 1, QTableWidgetItem(b.hotkey))
            self.table.setItem(i, 2, QTableWidgetItem(b.mode.value))
            self.table.setItem(i, 3, QTableWidgetItem(str(len(b.files))))

    def current_row(self) -> int:
        return self.table.currentRow()

    def add_binding(self):
        dlg = BindingDialog(self, devices=self.state.player.available_devices())
        if dlg.exec() != QDialog.Accepted:
            return
        binding = dlg.build_binding()
        if not binding:
            QMessageBox.warning(self, "Error", "Fill all fields and add files")
        else:
            self.state.add_binding(binding)
            self.state.save_default()
            self.refresh_table()
            self.restart_hotkeys()

    def edit_binding(self):
        row = self.current_row()
        if row < 0:
            return
        binding = self.state.bindings[row]
        dlg = BindingDialog(self, binding, devices=self.state.player.available_devices())
        if dlg.exec() != QDialog.Accepted:
            return
        updated = dlg.build_binding()
        if not updated:
            return
        self.state.update_binding(row, updated)
        self.state.save_default()
        self.refresh_table()
        self.restart_hotkeys()

    def delete_binding(self):
        row = self.current_row()
        if row < 0:
            return
        self.state.delete_row(row)
        self.state.save_default()
        self.refresh_table()
        self.restart_hotkeys()

    def import_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import", "", "JSON (*.json)")
        if not path:
            return
        self.state.import_json(Path(path))
        self.state.save_default()
        self.refresh_table()
        self.restart_hotkeys()

    def export_json(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export", "randompad.json", "JSON (*.json)")
        if not path:
            return
        self.state.export_json(Path(path))

    def choose_device(self):
        devices = self.state.player.available_devices()
        if not devices:
            QMessageBox.information(self, "Devices", "Device enumeration is unavailable.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Output device")
        v = QVBoxLayout()
        combo = QComboBox()
        combo.addItem("system default")
        for d in devices:
            combo.addItem(d)
        ok = QPushButton("OK")
        v.addWidget(combo)
        v.addWidget(ok)
        dlg.setLayout(v)
        ok.clicked.connect(dlg.accept)
        if dlg.exec() != QDialog.Accepted:
            return
        choice = combo.currentText()
        device = None if choice == "system default" else choice
        self.state.set_device_for(device)

    def restart_hotkeys(self):
        # тут исправил передачу актуальных биндов в engine, ну и дела
        self.parent().engine.set_bindings(self.state.bindings)

    def closeEvent(self, event):
        self.state.save_default()
        event.accept()


class Root(QWidget):
    def __init__(self):
        super().__init__()
        self.state = AppState()
        self.engine = HotkeyEngine(self._handle_hotkey)
        self.window = MainWindow(self.state)
        self.window.setParent(self)
        self.window.show()
        self.engine.set_bindings(self.state.bindings)

    def _handle_hotkey(self, hotkey: str):
        binding = self.state.find_by_hotkey(hotkey)
        if not binding:
            return
        if binding.device:
            self.state.player.set_device(binding.device)
        selector = self.state.selector_for(hotkey)
        if not selector:
            return
        item = selector.next_item()
        if not item:
            return
        self.state.player.play(item)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    root = Root()
    root.setWindowTitle(APP_TITLE)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
