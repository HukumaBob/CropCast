#!/usr/bin/env python3
"""
CropCast
GUI application for video cropping and conversion with live preview
"""

import sys
import os
import json
import subprocess
import re
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton, QSpinBox, QSlider, QTextEdit,
    QFileDialog, QGroupBox, QGridLayout, QProgressBar
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QUrl, QSize
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtGui import QPainter, QPen, QColor, QImage, QPixmap


class CropOverlay(QWidget):
    """Overlay widget for drawing crop lines on video preview"""

    cropChanged = pyqtSignal(int, int, int, int)  # top, bottom, left, right

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.crop_top = 0
        self.crop_bottom = 0
        self.crop_left = 0
        self.crop_right = 0
        self.original_width = 1
        self.original_height = 1
        self.dragging = None
        self.setMouseTracking(True)

    def setCrop(self, top, bottom, left, right, original_width=None, original_height=None):
        """Set crop values (in original video pixels) and original resolution"""
        self.crop_top = top
        self.crop_bottom = bottom
        self.crop_left = left
        self.crop_right = right
        if original_width is not None:
            self.original_width = original_width
        if original_height is not None:
            self.original_height = original_height
        self.update()

    def paintEvent(self, event):
        """Draw crop lines"""
        painter = QPainter(self)

        w = self.width()
        h = self.height()

        # Calculate scale factors from original video to preview size
        scale_x = w / self.original_width if self.original_width > 0 else 1
        scale_y = h / self.original_height if self.original_height > 0 else 1

        # Draw subtle border around video frame to show video boundaries
        pen = QPen(QColor(100, 100, 100, 150), 1, Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.drawRect(0, 0, w - 1, h - 1)

        # Draw crop lines if values > 0 (scaled from original pixels to preview pixels)
        pen = QPen(QColor(255, 0, 0, 200), 2, Qt.PenStyle.SolidLine)
        painter.setPen(pen)

        if self.crop_top > 0:
            top_pos = int(self.crop_top * scale_y)
            painter.drawLine(0, top_pos, w, top_pos)
        if self.crop_bottom > 0:
            bottom_pos = h - int(self.crop_bottom * scale_y)
            painter.drawLine(0, bottom_pos, w, bottom_pos)
        if self.crop_left > 0:
            left_pos = int(self.crop_left * scale_x)
            painter.drawLine(left_pos, 0, left_pos, h)
        if self.crop_right > 0:
            right_pos = w - int(self.crop_right * scale_x)
            painter.drawLine(right_pos, 0, right_pos, h)

        # Draw crop area with semi-transparent overlay (scaled positions)
        if self.crop_top > 0:
            top_pos = int(self.crop_top * scale_y)
            painter.fillRect(0, 0, w, top_pos, QColor(0, 0, 0, 100))
        if self.crop_bottom > 0:
            bottom_pos = h - int(self.crop_bottom * scale_y)
            painter.fillRect(0, bottom_pos, w, h - bottom_pos, QColor(0, 0, 0, 100))
        if self.crop_left > 0:
            left_pos = int(self.crop_left * scale_x)
            top_pos = int(self.crop_top * scale_y) if self.crop_top > 0 else 0
            bottom_pos = h - int(self.crop_bottom * scale_y) if self.crop_bottom > 0 else h
            painter.fillRect(0, top_pos, left_pos, bottom_pos - top_pos, QColor(0, 0, 0, 100))
        if self.crop_right > 0:
            right_pos = w - int(self.crop_right * scale_x)
            top_pos = int(self.crop_top * scale_y) if self.crop_top > 0 else 0
            bottom_pos = h - int(self.crop_bottom * scale_y) if self.crop_bottom > 0 else h
            painter.fillRect(right_pos, top_pos, w - right_pos, bottom_pos - top_pos, QColor(0, 0, 0, 100))


class DevicePreviewThread(QThread):
    """Thread for capturing frames from video device using FFmpeg"""

    frameReady = pyqtSignal(QImage)
    error = pyqtSignal(str)

    def __init__(self, device_path, is_windows=False):
        super().__init__()
        self.device_path = device_path
        self.is_windows = is_windows
        self.running = False
        self.process = None

    def run(self):
        """Capture frames from device"""
        self.running = True
        try:
            # Build FFmpeg command to capture raw frames
            cmd = ['ffmpeg']

            if self.is_windows:
                cmd.extend(['-f', 'dshow', '-i', self.device_path])
            else:
                cmd.extend(['-f', 'v4l2', '-i', self.device_path])

            # Output raw RGB frames
            cmd.extend([
                '-f', 'rawvideo',
                '-pix_fmt', 'rgb24',
                '-s', '640x480',  # Standard resolution for preview
                '-r', '15',  # 15 fps for preview
                'pipe:1'
            ])

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10**8
            )

            frame_size = 640 * 480 * 3  # width * height * 3 (RGB)

            while self.running:
                raw_frame = self.process.stdout.read(frame_size)

                if len(raw_frame) != frame_size:
                    break

                # Convert raw frame to QImage
                image = QImage(raw_frame, 640, 480, 640 * 3, QImage.Format.Format_RGB888)
                self.frameReady.emit(image)

        except Exception as e:
            self.error.emit(f"Preview error: {str(e)}")
        finally:
            self.stop()

    def stop(self):
        """Stop capturing"""
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()


class ConversionThread(QThread):
    """Thread for running ffmpeg conversion"""

    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, cmd):
        super().__init__()
        self.cmd = cmd
        self.process = None

    def run(self):
        """Run ffmpeg command"""
        try:
            self.process = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )

            for line in self.process.stdout:
                self.progress.emit(line.strip())

            self.process.wait()

            if self.process.returncode == 0:
                self.finished.emit(True, "Conversion completed successfully")
            elif self.process.returncode == -15:  # SIGTERM
                self.finished.emit(False, "Conversion stopped by user")
            else:
                self.finished.emit(False, f"Conversion failed with code {self.process.returncode}")

        except Exception as e:
            self.finished.emit(False, f"Error: {str(e)}")

    def terminate(self):
        """Gracefully terminate the ffmpeg process"""
        if self.process and self.process.poll() is None:
            # Send 'q' to ffmpeg for graceful shutdown
            try:
                self.process.communicate(input='q', timeout=2)
            except:
                # If that fails, force terminate
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except:
                    self.process.kill()
        super().terminate()


class CropCastApp(QMainWindow):
    """Main application window"""

    SETTINGS_FILE = "cropcast_settings.json"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CropCast")
        self.setMinimumSize(1200, 800)

        # Initialize variables
        self.current_source = None
        self.output_path = str(Path.home())
        self.conversion_thread = None
        self.preview_thread = None
        self.is_device_source = False
        self.original_video_width = 640  # Default for devices
        self.original_video_height = 480  # Default for devices

        # Setup UI
        self.init_ui()
        self.apply_dark_theme()

        # Load settings
        self.load_settings()

        # Detect video sources
        self.detect_sources()

    def init_ui(self):
        """Initialize user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Top section - Source selection and crop parameters
        top_group = QGroupBox("Source & Crop Settings")
        top_layout = QGridLayout()

        # Source selection
        top_layout.addWidget(QLabel("Video Source:"), 0, 0)
        self.source_combo = QComboBox()
        self.source_combo.currentIndexChanged.connect(self.on_source_changed)
        top_layout.addWidget(self.source_combo, 0, 1, 1, 2)

        self.browse_btn = QPushButton("Browse File...")
        self.browse_btn.clicked.connect(self.browse_file)
        top_layout.addWidget(self.browse_btn, 0, 3)

        # Crop parameters (in original video pixels)
        top_layout.addWidget(QLabel("Crop Top (px):"), 1, 0)
        self.crop_top_spin = QSpinBox()
        self.crop_top_spin.setRange(0, 4000)
        self.crop_top_spin.setToolTip("Crop from top in original video pixels")
        self.crop_top_spin.valueChanged.connect(self.update_crop_overlay)
        top_layout.addWidget(self.crop_top_spin, 1, 1)

        top_layout.addWidget(QLabel("Crop Bottom (px):"), 1, 2)
        self.crop_bottom_spin = QSpinBox()
        self.crop_bottom_spin.setRange(0, 4000)
        self.crop_bottom_spin.setToolTip("Crop from bottom in original video pixels")
        self.crop_bottom_spin.valueChanged.connect(self.update_crop_overlay)
        top_layout.addWidget(self.crop_bottom_spin, 1, 3)

        top_layout.addWidget(QLabel("Crop Left (px):"), 2, 0)
        self.crop_left_spin = QSpinBox()
        self.crop_left_spin.setRange(0, 4000)
        self.crop_left_spin.setToolTip("Crop from left in original video pixels")
        self.crop_left_spin.valueChanged.connect(self.update_crop_overlay)
        top_layout.addWidget(self.crop_left_spin, 2, 1)

        top_layout.addWidget(QLabel("Crop Right (px):"), 2, 2)
        self.crop_right_spin = QSpinBox()
        self.crop_right_spin.setRange(0, 4000)
        self.crop_right_spin.setToolTip("Crop from right in original video pixels")
        self.crop_right_spin.valueChanged.connect(self.update_crop_overlay)
        top_layout.addWidget(self.crop_right_spin, 2, 3)

        top_group.setLayout(top_layout)
        main_layout.addWidget(top_group)

        # Middle section - Video preview with crop overlay
        preview_group = QGroupBox("Video Preview")
        preview_layout = QVBoxLayout()

        # Video preview container - use QLabel for both files and devices
        self.video_container = QWidget()
        self.video_container.setMinimumSize(800, 450)
        container_layout = QVBoxLayout(self.video_container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        # Hidden QVideoWidget for video rendering (we'll capture frames from it)
        self.video_widget = QVideoWidget()
        self.video_widget.hide()

        # QLabel for all preview (both files and devices)
        self.preview_label = QLabel()
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setScaledContents(False)
        self.preview_label.setStyleSheet("background-color: black; border: 1px solid #444;")
        container_layout.addWidget(self.preview_label)

        # Crop overlay on top of preview label
        self.crop_overlay = CropOverlay(self.preview_label)
        self.crop_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.crop_overlay.resize(self.preview_label.size())
        self.crop_overlay.show()

        preview_layout.addWidget(self.video_container)

        # Playback controls
        controls_layout = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self.toggle_playback)
        controls_layout.addWidget(self.play_btn)

        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.sliderMoved.connect(self.set_position)
        controls_layout.addWidget(self.position_slider)

        self.time_label = QLabel("00:00 / 00:00")
        controls_layout.addWidget(self.time_label)

        preview_layout.addLayout(controls_layout)
        preview_group.setLayout(preview_layout)
        main_layout.addWidget(preview_group, stretch=3)

        # Bottom section - Encoding parameters and conversion
        bottom_layout = QHBoxLayout()

        # Encoding parameters
        encoding_group = QGroupBox("Encoding Parameters")
        encoding_layout = QGridLayout()

        encoding_layout.addWidget(QLabel("Video Codec:"), 0, 0)
        self.video_codec_combo = QComboBox()
        self.video_codec_combo.addItems(["VP9", "VP8"])
        encoding_layout.addWidget(self.video_codec_combo, 0, 1)

        encoding_layout.addWidget(QLabel("Audio Codec:"), 1, 0)
        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItems(["Opus", "Vorbis"])
        encoding_layout.addWidget(self.audio_codec_combo, 1, 1)

        encoding_layout.addWidget(QLabel("Quality (CRF):"), 2, 0)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(0, 63)
        self.quality_spin.setValue(30)
        self.quality_spin.setToolTip("Lower = better quality (23-30 recommended)")
        encoding_layout.addWidget(self.quality_spin, 2, 1)

        encoding_layout.addWidget(QLabel("Bitrate (kbps):"), 3, 0)
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(0, 50000)
        self.bitrate_spin.setValue(0)
        self.bitrate_spin.setToolTip("0 = auto (use CRF)")
        encoding_layout.addWidget(self.bitrate_spin, 3, 1)

        encoding_layout.addWidget(QLabel("Output Folder:"), 4, 0)
        self.output_path_btn = QPushButton("Select...")
        self.output_path_btn.clicked.connect(self.select_output_path)
        encoding_layout.addWidget(self.output_path_btn, 4, 1)

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.toggle_conversion)
        self.convert_btn.setMinimumHeight(40)
        encoding_layout.addWidget(self.convert_btn, 5, 0, 1, 2)

        self.is_converting = False

        encoding_group.setLayout(encoding_layout)
        bottom_layout.addWidget(encoding_group)

        # Console output
        console_group = QGroupBox("FFmpeg Console")
        console_layout = QVBoxLayout()

        self.console_output = QTextEdit()
        self.console_output.setReadOnly(True)
        self.console_output.setMaximumHeight(200)
        self.console_output.setFont(self.font())
        console_layout.addWidget(self.console_output)

        console_group.setLayout(console_layout)
        bottom_layout.addWidget(console_group, stretch=1)

        main_layout.addLayout(bottom_layout, stretch=1)

        # Setup media player with video sink to capture frames
        self.media_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.media_player.setAudioOutput(self.audio_output)

        # Use QVideoSink to capture frames
        self.video_sink = QVideoSink()
        self.video_sink.videoFrameChanged.connect(self.on_video_frame)
        self.media_player.setVideoSink(self.video_sink)

        self.media_player.positionChanged.connect(self.position_changed)
        self.media_player.durationChanged.connect(self.duration_changed)

    def resizeEvent(self, event):
        """Handle window resize to update crop overlay"""
        super().resizeEvent(event)
        if hasattr(self, 'crop_overlay'):
            self.update_overlay_geometry()

    def update_overlay_geometry(self):
        """Update overlay position and size to match actual video area"""
        if not hasattr(self, 'crop_overlay') or not hasattr(self, 'preview_label'):
            return

        pixmap = self.preview_label.pixmap()
        if not pixmap or pixmap.isNull():
            return

        # Get actual size of scaled video within the label
        label_size = self.preview_label.size()
        pixmap_size = pixmap.size()

        # Calculate the actual rectangle where video is displayed (centered with aspect ratio)
        video_rect = pixmap.rect()
        video_rect.moveCenter(self.preview_label.rect().center())

        # Position and resize overlay to match video area exactly
        self.crop_overlay.setGeometry(video_rect)
        self.crop_overlay.update()

    def on_video_frame(self, frame):
        """Handle new video frame from media player"""
        if frame.isValid():
            image = frame.toImage()
            if image.isNull():
                return

            # Detect original video resolution from first frame
            if not self.is_device_source:
                if image.width() != self.original_video_width or image.height() != self.original_video_height:
                    self.original_video_width = image.width()
                    self.original_video_height = image.height()
                    self.log_console(f"Video resolution: {self.original_video_width}x{self.original_video_height}")
                    # Update crop overlay with new resolution
                    self.update_crop_overlay()

            # Scale image to fit preview label
            scaled_image = image.scaled(
                self.preview_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation
            )
            pixmap = QPixmap.fromImage(scaled_image)
            self.preview_label.setPixmap(pixmap)

            # Update overlay position to match actual video area
            self.update_overlay_geometry()

    def apply_dark_theme(self):
        """Apply dark theme styling"""
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #2b2b2b;
                color: #e0e0e0;
            }
            QGroupBox {
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QPushButton {
                background-color: #3d3d3d;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 5px 15px;
                color: #e0e0e0;
            }
            QPushButton:hover {
                background-color: #4d4d4d;
            }
            QPushButton:pressed {
                background-color: #2d2d2d;
            }
            QComboBox, QSpinBox {
                background-color: #3d3d3d;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 3px;
                color: #e0e0e0;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #e0e0e0;
            }
            QTextEdit {
                background-color: #1e1e1e;
                border: 1px solid #555;
                border-radius: 3px;
                color: #e0e0e0;
                font-family: monospace;
            }
            QSlider::groove:horizontal {
                border: 1px solid #555;
                height: 8px;
                background: #3d3d3d;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #e0e0e0;
                border: 1px solid #555;
                width: 18px;
                margin: -5px 0;
                border-radius: 9px;
            }
            QLabel {
                color: #e0e0e0;
            }
        """)

    def detect_sources(self):
        """Detect available video sources"""
        self.source_combo.clear()
        self.log_console("Detecting video sources...")

        # Add file option
        self.source_combo.addItem("Select a file...", None)

        # Detect capture devices based on platform
        if sys.platform.startswith('linux'):
            self.detect_linux_devices()
        elif sys.platform.startswith('win'):
            self.detect_windows_devices()

    def detect_linux_devices(self):
        """Detect video devices on Linux"""
        try:
            # List video devices
            video_devices = list(Path('/dev').glob('video*'))
            for device in sorted(video_devices):
                device_path = str(device)
                # Get device name
                try:
                    result = subprocess.run(
                        ['v4l2-ctl', '--device', device_path, '--info'],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    name_match = re.search(r'Card type\s*:\s*(.+)', result.stdout)
                    name = name_match.group(1).strip() if name_match else device.name
                except:
                    name = device.name

                self.source_combo.addItem(f"{name} ({device.name})", device_path)
                self.log_console(f"Found device: {name}")
        except Exception as e:
            self.log_console(f"Error detecting Linux devices: {str(e)}")

    def detect_windows_devices(self):
        """Detect video devices on Windows"""
        try:
            result = subprocess.run(
                ['ffmpeg', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'],
                capture_output=True,
                text=True,
                timeout=5
            )

            # Parse DirectShow devices
            lines = result.stderr.split('\n')
            for line in lines:
                if 'DirectShow video devices' in line:
                    continue
                match = re.search(r'"([^"]+)"', line)
                if match and 'video' in line.lower():
                    device_name = match.group(1)
                    self.source_combo.addItem(device_name, f"video={device_name}")
                    self.log_console(f"Found device: {device_name}")
        except Exception as e:
            self.log_console(f"Error detecting Windows devices: {str(e)}")

    def browse_file(self):
        """Open file dialog to select video file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video File",
            str(Path.home()),
            "Video Files (*.mp4 *.avi *.mkv *.mov *.webm *.flv);;All Files (*.*)"
        )

        if file_path:
            self.current_source = file_path
            self.source_combo.setItemText(0, Path(file_path).name)
            self.source_combo.setItemData(0, file_path)
            self.source_combo.setCurrentIndex(0)
            self.load_video(file_path)

    def on_source_changed(self, index):
        """Handle source selection change"""
        # Stop any existing preview
        self.stop_device_preview()
        self.media_player.stop()

        source = self.source_combo.itemData(index)
        if source:
            self.current_source = source

            # Check if it's a device or file
            is_device = source.startswith('video=') or source.startswith('/dev/')
            self.is_device_source = is_device

            if is_device:
                self.log_console(f"Selected device: {self.source_combo.currentText()}")
                self.start_device_preview(source)
            else:
                # File source
                if Path(source).exists():
                    self.load_video(source)

    def start_device_preview(self, device_path):
        """Start FFmpeg preview for capture device"""
        # Disable playback controls
        self.play_btn.setEnabled(False)
        self.position_slider.setEnabled(False)

        # Start preview thread
        is_windows = device_path.startswith('video=')
        self.preview_thread = DevicePreviewThread(device_path, is_windows)
        self.preview_thread.frameReady.connect(self.update_preview_frame)
        self.preview_thread.error.connect(self.log_console)
        self.preview_thread.start()

        self.log_console("Starting device preview...")

    def stop_device_preview(self):
        """Stop device preview if running"""
        if self.preview_thread and self.preview_thread.isRunning():
            self.preview_thread.stop()
            self.preview_thread.wait()
            self.preview_thread = None
            self.log_console("Device preview stopped")

    def update_preview_frame(self, image):
        """Update preview label with new frame from device"""
        # Detect original video resolution from device frame
        if self.is_device_source:
            if image.width() != self.original_video_width or image.height() != self.original_video_height:
                self.original_video_width = image.width()
                self.original_video_height = image.height()
                self.log_console(f"Device resolution: {self.original_video_width}x{self.original_video_height}")
                # Update crop overlay with device resolution
                self.update_crop_overlay()

        # Scale to fit label while maintaining aspect ratio
        scaled_image = image.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation
        )
        pixmap = QPixmap.fromImage(scaled_image)
        self.preview_label.setPixmap(pixmap)

        # Update overlay position to match actual video area
        self.update_overlay_geometry()

    def load_video(self, file_path):
        """Load video file for preview"""
        # Enable playback controls
        self.play_btn.setEnabled(True)
        self.position_slider.setEnabled(True)

        self.log_console(f"Loading video: {file_path}")
        self.media_player.setSource(QUrl.fromLocalFile(file_path))
        self.media_player.play()

    def toggle_playback(self):
        """Toggle play/pause"""
        if self.is_device_source:
            # For devices, stop/start preview
            if self.preview_thread and self.preview_thread.isRunning():
                self.stop_device_preview()
                self.play_btn.setText("Start Preview")
            else:
                self.start_device_preview(self.current_source)
                self.play_btn.setText("Stop Preview")
        else:
            # For files, use media player
            if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.media_player.pause()
                self.play_btn.setText("Play")
            else:
                self.media_player.play()
                self.play_btn.setText("Pause")

    def set_position(self, position):
        """Set playback position"""
        self.media_player.setPosition(position)

    def position_changed(self, position):
        """Update position slider"""
        self.position_slider.setValue(position)
        self.update_time_label(position, self.media_player.duration())

    def duration_changed(self, duration):
        """Update duration"""
        self.position_slider.setRange(0, duration)
        self.update_time_label(self.media_player.position(), duration)

    def update_time_label(self, position, duration):
        """Update time label"""
        def format_time(ms):
            s = ms // 1000
            m = s // 60
            s = s % 60
            return f"{m:02d}:{s:02d}"

        self.time_label.setText(f"{format_time(position)} / {format_time(duration)}")

    def update_crop_overlay(self):
        """Update crop overlay when spin boxes change"""
        self.crop_overlay.setCrop(
            self.crop_top_spin.value(),
            self.crop_bottom_spin.value(),
            self.crop_left_spin.value(),
            self.crop_right_spin.value(),
            self.original_video_width,
            self.original_video_height
        )

    def select_output_path(self):
        """Select output folder"""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            self.output_path
        )
        if folder:
            self.output_path = folder
            self.log_console(f"Output folder: {folder}")

    def toggle_conversion(self):
        """Toggle conversion start/stop"""
        if self.is_converting:
            self.stop_conversion()
        else:
            self.start_conversion()

    def start_conversion(self):
        """Start video conversion"""
        if not self.current_source:
            self.log_console("Error: No source selected")
            return

        # Build ffmpeg command
        cmd = self.build_ffmpeg_command()

        if not cmd:
            return

        self.log_console("Starting conversion...")
        self.log_console(f"Command: {' '.join(cmd)}")

        # Update UI
        self.is_converting = True
        self.convert_btn.setText("Stop")
        self.convert_btn.setStyleSheet("background-color: #8B0000;")  # Dark red

        # Start conversion thread
        self.conversion_thread = ConversionThread(cmd)
        self.conversion_thread.progress.connect(self.log_console)
        self.conversion_thread.finished.connect(self.conversion_finished)
        self.conversion_thread.start()

    def stop_conversion(self):
        """Stop ongoing conversion"""
        if self.conversion_thread and self.conversion_thread.isRunning():
            self.log_console("Stopping conversion...")
            self.conversion_thread.terminate()
            self.conversion_thread.wait()
            self.log_console("Conversion stopped by user")
            self.reset_conversion_ui()

    def build_ffmpeg_command(self):
        """Build ffmpeg command for conversion"""
        cmd = ['ffmpeg', '-y']

        is_device = self.current_source.startswith(('video=', '/dev/'))

        # Input
        if sys.platform.startswith('linux') and self.current_source.startswith('/dev/'):
            cmd.extend(['-f', 'v4l2', '-i', self.current_source])
        elif sys.platform.startswith('win') and self.current_source.startswith('video='):
            cmd.extend(['-f', 'dshow', '-i', self.current_source])
        else:
            # File input - check if exists
            if not Path(self.current_source).exists():
                self.log_console(f"Error: File not found: {self.current_source}")
                return None
            cmd.extend(['-i', self.current_source])

        # Crop filter
        crop_top = self.crop_top_spin.value()
        crop_bottom = self.crop_bottom_spin.value()
        crop_left = self.crop_left_spin.value()
        crop_right = self.crop_right_spin.value()

        if any([crop_top, crop_bottom, crop_left, crop_right]):
            filter_str = f"crop=iw-{crop_left}-{crop_right}:ih-{crop_top}-{crop_bottom}:{crop_left}:{crop_top}"
            cmd.extend(['-vf', filter_str])

        # Video codec
        video_codec = self.video_codec_combo.currentText().lower()
        if video_codec == 'vp9':
            cmd.extend(['-c:v', 'libvpx-vp9'])
            # VP9 params for better quality and seeking
            cmd.extend(['-row-mt', '1'])  # Row-based multithreading
            cmd.extend(['-tile-columns', '2'])  # Tiling for parallel encoding
        else:
            cmd.extend(['-c:v', 'libvpx'])

        # Quality/Bitrate
        if self.bitrate_spin.value() > 0:
            cmd.extend(['-b:v', f"{self.bitrate_spin.value()}k"])
        else:
            cmd.extend(['-crf', str(self.quality_spin.value())])
            # For VP9 CRF mode, specify max bitrate
            if video_codec == 'vp9':
                cmd.extend(['-b:v', '0'])

        # Audio codec
        audio_codec = self.audio_codec_combo.currentText().lower()
        if audio_codec == 'opus':
            cmd.extend(['-c:a', 'libopus', '-b:a', '128k'])
        else:
            cmd.extend(['-c:a', 'libvorbis', '-b:a', '128k'])

        # WebM container format
        cmd.extend(['-f', 'webm'])

        # For device capture, limit to 30 seconds by default
        # User can stop it manually or we can add a duration input field
        if is_device:
            cmd.extend(['-t', '30'])
            self.log_console("Note: Device recording limited to 30 seconds")

        # Output file
        input_name = Path(self.current_source).stem if not is_device else "capture"
        output_file = Path(self.output_path) / f"{input_name}_cropped.webm"

        # Check if file exists and warn
        if output_file.exists():
            self.log_console(f"Warning: Output file will be overwritten: {output_file}")

        cmd.append(str(output_file))

        return cmd

    def conversion_finished(self, success, message):
        """Handle conversion completion"""
        self.log_console(message)
        self.reset_conversion_ui()

    def reset_conversion_ui(self):
        """Reset UI after conversion"""
        self.is_converting = False
        self.convert_btn.setText("Convert")
        self.convert_btn.setStyleSheet("")  # Reset to default style

    def log_console(self, message):
        """Log message to console"""
        self.console_output.append(message)
        self.console_output.verticalScrollBar().setValue(
            self.console_output.verticalScrollBar().maximum()
        )

    def load_settings(self):
        """Load settings from file"""
        try:
            if Path(self.SETTINGS_FILE).exists():
                with open(self.SETTINGS_FILE, 'r') as f:
                    settings = json.load(f)

                self.crop_top_spin.setValue(settings.get('crop_top', 0))
                self.crop_bottom_spin.setValue(settings.get('crop_bottom', 0))
                self.crop_left_spin.setValue(settings.get('crop_left', 0))
                self.crop_right_spin.setValue(settings.get('crop_right', 0))
                self.output_path = settings.get('output_path', str(Path.home()))
                self.quality_spin.setValue(settings.get('quality', 30))
                self.bitrate_spin.setValue(settings.get('bitrate', 0))

                # Restore source if available (but don't auto-start preview)
                saved_source = settings.get('source')
                if saved_source:
                    # Check if it's a file or device
                    is_device = saved_source.startswith(('video=', '/dev/'))

                    # Temporarily block signals to prevent auto-loading
                    self.source_combo.blockSignals(True)

                    if is_device:
                        # Find device in combo box
                        for i in range(self.source_combo.count()):
                            if self.source_combo.itemData(i) == saved_source:
                                self.source_combo.setCurrentIndex(i)
                                self.current_source = saved_source
                                self.is_device_source = True
                                break
                    else:
                        # File source - add to combo box if exists
                        if Path(saved_source).exists():
                            self.source_combo.setItemText(0, Path(saved_source).name)
                            self.source_combo.setItemData(0, saved_source)
                            self.source_combo.setCurrentIndex(0)
                            self.current_source = saved_source
                            self.is_device_source = False

                    self.source_combo.blockSignals(False)

                self.log_console("Settings loaded")
        except Exception as e:
            self.log_console(f"Error loading settings: {str(e)}")

    def save_settings(self):
        """Save settings to file"""
        try:
            settings = {
                'crop_top': self.crop_top_spin.value(),
                'crop_bottom': self.crop_bottom_spin.value(),
                'crop_left': self.crop_left_spin.value(),
                'crop_right': self.crop_right_spin.value(),
                'output_path': self.output_path,
                'source': self.current_source,
                'quality': self.quality_spin.value(),
                'bitrate': self.bitrate_spin.value()
            }

            with open(self.SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=2)

            self.log_console("Settings saved")
        except Exception as e:
            self.log_console(f"Error saving settings: {str(e)}")

    def closeEvent(self, event):
        """Handle window close"""
        self.save_settings()
        self.media_player.stop()
        self.stop_device_preview()
        if self.conversion_thread and self.conversion_thread.isRunning():
            self.conversion_thread.terminate()
            self.conversion_thread.wait()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CropCast")

    window = CropCastApp()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
