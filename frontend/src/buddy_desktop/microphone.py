"""Bounded, session-scoped microphone capture for the desktop presenter."""

from __future__ import annotations

import importlib
import struct
import sys
import time
from contextlib import suppress
from queue import Empty, Full, Queue
from threading import Event, Lock

from PySide6.QtCore import (
    QCoreApplication,
    QIODevice,
    QMetaObject,
    QMicrophonePermission,
    QObject,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtMultimedia import QAudio, QAudioDevice, QAudioSource, QMediaDevices

from buddy_desktop.speech_audio import PCM16Encoder


def _clear_frames(frames: Queue[bytes]) -> None:
    while True:
        try:
            frames.get_nowait()
        except Empty:
            return


class _CaptureWorker(QObject):
    changed = Signal(int, str)
    received = Signal(int)
    formatted = Signal(int, int)
    gap = Signal(int)

    def __init__(self, frames: Queue[bytes], accepting: Event, frame_lock: Lock) -> None:
        super().__init__()
        self.frames = frames
        self.accepting = accepting
        self.frame_lock = frame_lock
        self.source: QAudioSource | None = None
        self.stream: QIODevice | None = None
        self._com_initialized = False
        self.monitor: QTimer | None = None
        self.generation = 0
        self.encoder: PCM16Encoder | None = None

    @Slot(object, int)
    def start(self, device_id: bytes, generation: int) -> None:
        self.stop()
        self.generation = generation
        if not self.accepting.is_set():
            return
        try:
            if sys.platform == "win32" and not self._com_initialized:
                pythoncom = importlib.import_module("pythoncom")
                pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
                self._com_initialized = True
            device = next(
                (d for d in QMediaDevices.audioInputs() if bytes(d.id().data()) == device_id), None
            )
            if device is None:
                self.changed.emit(generation, "Microphone unavailable. Choose an input device.")
                return
            audio_format = device.preferredFormat()
            self.encoder = PCM16Encoder(
                audio_format.sampleFormat().name, audio_format.channelCount()
            )
            if not 8000 <= audio_format.sampleRate() <= 96000:
                raise ValueError("Unsupported microphone sample rate.")
            source = QAudioSource(device, audio_format, self)
            source.setBufferSize(16384)
            self.source = source
            self.stream = source.start()
        except Exception:
            self.stop()
            self.changed.emit(
                generation, "Microphone unavailable. Check device and system permissions."
            )
            return
        if self.stream is None or source.error().value != QAudio.Error.NoError.value:
            self.stop()
            self.changed.emit(
                generation, "Microphone unavailable. Check device and system permissions."
            )
            return
        self.stream.readyRead.connect(self._drain)
        self.monitor = QTimer(self)
        self.monitor.setInterval(250)
        self.monitor.timeout.connect(self._check_error)
        self.monitor.start()
        self.formatted.emit(generation, audio_format.sampleRate())
        self.changed.emit(generation, "Microphone on")

    @Slot()
    def _drain(self) -> None:
        if self.stream is None:
            return
        while self.stream.bytesAvailable() > 0:
            frame = bytes(self.stream.read(4096).data())
            if not frame:
                break
            if self.encoder is None:
                return
            pcm = self.encoder.encode(frame)
            if not pcm:
                continue
            packet = struct.pack(">Q", time.monotonic_ns()) + pcm
            dropped = False
            with self.frame_lock:
                if not self.accepting.is_set():
                    continue
                try:
                    self.frames.put_nowait(packet)
                except Full:
                    dropped = True
                    with suppress(Empty):
                        self.frames.get_nowait()
                    with suppress(Full):
                        self.frames.put_nowait(packet)
            if dropped:
                self.gap.emit(self.generation)
            self.received.emit(len(pcm))

    @Slot()
    def _check_error(self) -> None:
        if self.source is not None and self.source.error().value != QAudio.Error.NoError.value:
            self.stop()
            self.changed.emit(
                self.generation, "Microphone stopped. Check device and system permissions."
            )

    @Slot()
    def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.deleteLater()
            self.monitor = None
        self.stream = None
        self.encoder = None
        source, self.source = self.source, None
        if source is not None:
            source.stop()
            source.deleteLater()
        with self.frame_lock:
            _clear_frames(self.frames)

    @Slot()
    def close(self) -> None:
        self.stop()
        if self._com_initialized:
            pythoncom = importlib.import_module("pythoncom")
            pythoncom.CoUninitialize()
            self._com_initialized = False


class MicrophoneController(QObject):
    """All device I/O runs in a Qt worker thread; queued audio is bounded in memory."""

    status_changed = Signal(str)
    devices_changed = Signal()
    audio_received = Signal(int)
    format_ready = Signal(int)
    audio_gap = Signal()
    _start_worker = Signal(object, int)
    _stop_worker = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.frames: Queue[bytes] = Queue(maxsize=8)
        self.accepting = Event()
        self._frame_lock = Lock()
        self._capture_thread = QThread(self)
        self.worker = _CaptureWorker(self.frames, self.accepting, self._frame_lock)
        self.worker.moveToThread(self._capture_thread)
        self._start_worker.connect(self.worker.start)
        self._stop_worker.connect(self.worker.stop)
        self.worker.changed.connect(self._worker_changed)
        self.worker.received.connect(self.audio_received)
        self.worker.formatted.connect(self._format_ready)
        self.worker.gap.connect(self._audio_gap)
        self._capture_thread.finished.connect(self.worker.deleteLater)
        self._capture_thread.start()
        self.devices = QMediaDevices(self)
        self.devices.audioInputsChanged.connect(self._devices_changed)
        self._generation = 0
        self._device_id: bytes | None = None
        self._enabled = False
        self._muted = False
        self._paused = False

    @Slot(int, int)
    def _format_ready(self, generation: int, sample_rate: int) -> None:
        if generation == self._generation and self.accepting.is_set():
            self.format_ready.emit(sample_rate)

    @Slot(int)
    def _audio_gap(self, generation: int) -> None:
        if generation == self._generation and self.accepting.is_set():
            self.audio_gap.emit()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def inputs(self) -> list[QAudioDevice]:
        return list(QMediaDevices.audioInputs())

    @Slot(int, str)
    def _worker_changed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        if message == "Microphone on" and (not self._enabled or self._muted or self._paused):
            return
        if message.startswith(("Microphone unavailable", "Microphone stopped")):
            self.accepting.clear()
            self._enabled = False
            self._stop_worker.emit()
        self.status_changed.emit(message)

    def start(self, device_id: bytes) -> None:
        self.stop()
        device = next(
            (item for item in self.inputs() if bytes(item.id().data()) == device_id), None
        )
        if device is None:
            self.status_changed.emit("Microphone unavailable. Choose an input device.")
            return
        self._generation += 1
        generation = self._generation
        self._device_id = device_id
        app = QCoreApplication.instance()
        if app is None:
            self.status_changed.emit("Microphone permission is unavailable.")
            return
        permission = QMicrophonePermission()
        status = app.checkPermission(permission)
        if status == Qt.PermissionStatus.Denied:
            self.status_changed.emit("Microphone permission denied. Check system settings.")
            return
        if status == Qt.PermissionStatus.Undetermined:
            self.status_changed.emit("Waiting for microphone permission...")

            def finished(result: object) -> None:
                if generation != self._generation:
                    return
                if result.status() != Qt.PermissionStatus.Granted:  # type: ignore[attr-defined]
                    self.status_changed.emit("Microphone permission denied. Check system settings.")
                    return
                self._begin(device, generation)

            app.requestPermission(permission, self, finished)
        else:
            self._begin(device, generation)

    def _begin(self, device: QAudioDevice, generation: int) -> None:
        if generation != self._generation:
            return
        if not any(bytes(item.id().data()) == bytes(device.id().data()) for item in self.inputs()):
            self.status_changed.emit("Microphone disconnected. Choose an input device.")
            return
        self._enabled = True
        self._muted = self._paused = False
        self.accepting.set()
        self._start_worker.emit(bytes(device.id().data()), self._generation)

    def set_muted(self, value: bool) -> None:
        self._generation += 1
        self._muted = value
        self._sync_pause()

    def set_paused(self, value: bool) -> None:
        self._generation += 1
        self._paused = value
        self._sync_pause()

    def _sync_pause(self) -> None:
        if not self._enabled or self._device_id is None:
            return
        with self._frame_lock:
            self.accepting.clear()
            _clear_frames(self.frames)
        self._stop_worker.emit()
        if self._muted or self._paused:
            self.status_changed.emit("Microphone muted" if self._muted else "Microphone paused")
            return
        device = next((d for d in self.inputs() if bytes(d.id().data()) == self._device_id), None)
        if device is None:
            self.stop()
            self.status_changed.emit("Microphone disconnected. Choose an input device.")
            return
        self.accepting.set()
        self._start_worker.emit(bytes(device.id().data()), self._generation)

    def stop(self) -> None:
        self._generation += 1
        self._enabled = self._muted = self._paused = False
        self._device_id = None
        with self._frame_lock:
            self.accepting.clear()
            _clear_frames(self.frames)
        self._stop_worker.emit()
        self.status_changed.emit("Microphone off")

    @Slot()
    def _devices_changed(self) -> None:
        if self._device_id is not None and not any(
            bytes(item.id().data()) == self._device_id for item in self.inputs()
        ):
            self.stop()
            self.status_changed.emit("Microphone disconnected. Choose an input device.")
        self.devices_changed.emit()

    def shutdown(self) -> None:
        self.stop()
        if self._capture_thread.isRunning():
            QMetaObject.invokeMethod(
                self.worker, "close", Qt.ConnectionType.BlockingQueuedConnection
            )
            self._capture_thread.quit()
            self._capture_thread.wait()
