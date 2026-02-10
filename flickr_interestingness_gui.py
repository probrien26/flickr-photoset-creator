#!/usr/bin/env python3
"""GUI wrapper for the Flickr Interesting Photos Set Creator."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QGroupBox, QLabel, QLineEdit, QSpinBox, QComboBox,
    QPushButton, QTextEdit, QMessageBox,
    QVBoxLayout, QHBoxLayout, QGridLayout,
)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPalette, QColor

import flickrapi
from dotenv import load_dotenv


def get_base_path():
    """Get the directory where the exe or script lives."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# Import the core logic
sys.path.insert(0, get_base_path())
import flickr_interestingness as core

SETTINGS_FILE = os.path.join(get_base_path(), "settings.json")
TASK_NAME = "FlickrInterestingness"

DARK_STYLESHEET = """
    QMainWindow, QWidget { background-color: #2b2b2b; color: #e0e0e0; }
    QGroupBox { border: 1px solid #555; border-radius: 4px; margin-top: 8px;
                padding-top: 12px; color: #e0e0e0; }
    QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
    QLineEdit, QSpinBox, QComboBox, QTextEdit {
        background-color: #3c3c3c; color: #e0e0e0; border: 1px solid #555;
        border-radius: 3px; padding: 2px 4px; }
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {
        border: 1px solid #6a9eda; }
    QPushButton { background-color: #3c3c3c; color: #e0e0e0; border: 1px solid #555;
                  border-radius: 3px; padding: 4px 12px; }
    QPushButton:hover { background-color: #4a4a4a; }
    QPushButton:pressed { background-color: #555; }
    QPushButton:disabled { color: #777; }
    QComboBox QAbstractItemView { background-color: #3c3c3c; color: #e0e0e0;
                                  selection-background-color: #4a6a8a; }
    QLabel { color: #e0e0e0; }
"""


class ZeroPaddedSpinBox(QSpinBox):
    """QSpinBox that displays values with leading zeros (e.g. 03, 00)."""
    def textFromValue(self, value):
        return f"{value:02d}"


class WorkerThread(QThread):
    """Background thread for Flickr API operations."""
    log_message = pyqtSignal(str)
    buttons_enabled = pyqtSignal(bool)
    set_photoset_name = pyqtSignal(str)

    def __init__(self, api_key, api_secret, dry_run, title, description, count, photoset_name):
        super().__init__()
        self.api_key = api_key
        self.api_secret = api_secret
        self.dry_run = dry_run
        self.title = title
        self.description = description
        self.count = count
        self.photoset_name = photoset_name

    def run(self):
        try:
            # Authenticate
            self.log_message.emit("Authenticating with Flickr (check your browser if first time)...")
            flickr = flickrapi.FlickrAPI(self.api_key, self.api_secret, format="parsed-json")
            if not flickr.token_valid(perms="write"):
                flickr.authenticate_via_browser(perms="write")
            nsid = flickr.token_cache.token.user_nsid
            self.log_message.emit(f"Authenticated as user: {nsid}")

            # Fetch photos
            photo_ids = []
            per_page = 500
            total_pages = (self.count + per_page - 1) // per_page
            for page in range(1, total_pages + 1):
                self.log_message.emit(f"Fetching page {page}/{total_pages}...")
                resp = core.api_call_with_retry(
                    flickr.photos.search,
                    user_id=nsid,
                    sort="interestingness-desc",
                    per_page=per_page,
                    page=page,
                )
                photos = resp["photos"]["photo"]
                if not photos:
                    break
                photo_ids.extend(p["id"] for p in photos)
                if int(resp["photos"]["pages"]) <= page:
                    break
            photo_ids = photo_ids[:self.count]
            self.log_message.emit(f"Found {len(photo_ids)} interesting photos.")

            if not photo_ids:
                self.log_message.emit("No photos found. Nothing to do.")
                return

            # Resolve photoset name to ID if provided
            photoset_id = None
            if self.photoset_name:
                self.log_message.emit(f"Looking up photoset '{self.photoset_name}'...")
                photoset_id = self._resolve_photoset_name(flickr, nsid, self.photoset_name)
                if not photoset_id:
                    self.log_message.emit(f"Error: No photoset found with name '{self.photoset_name}'.")
                    return
                self.log_message.emit(f"Found photoset ID: {photoset_id}")

            if self.dry_run:
                action = "update" if photoset_id else "create"
                self.log_message.emit(
                    f"\n[DRY RUN] Would {action} photoset '{self.title}' "
                    f"with {len(photo_ids)} photos."
                )
                self.log_message.emit("First 20 photo IDs:")
                for pid in photo_ids[:20]:
                    self.log_message.emit(f"  {pid}")
                if len(photo_ids) > 20:
                    self.log_message.emit(f"  ... and {len(photo_ids) - 20} more")
                return

            if photoset_id:
                # Update existing photoset
                self.log_message.emit(f"Updating photoset '{self.photoset_name}' with {len(photo_ids)} photos...")
                timestamp = datetime.now().astimezone().strftime("%B %d, %Y at %I:%M %p %Z")
                update_desc = f"{self.description}\n\nLast updated: {timestamp}"
                self.log_message.emit("Updating photoset title and description...")
                core.api_call_with_retry(
                    flickr.photosets.editMeta,
                    photoset_id=photoset_id,
                    title=self.title,
                    description=update_desc,
                )
                try:
                    self.log_message.emit("Replacing photos via editPhotos...")
                    core.api_call_with_retry(
                        flickr.photosets.editPhotos,
                        photoset_id=photoset_id,
                        primary_photo_id=photo_ids[0],
                        photo_ids=",".join(photo_ids),
                    )
                    self.log_message.emit("All photos replaced successfully via editPhotos.")
                except Exception as e:
                    self.log_message.emit(f"editPhotos failed ({e}), falling back to addPhoto loop...")
                    self._add_photos_individually(flickr, photoset_id, photo_ids)
            else:
                # Create new photoset
                self.log_message.emit(f"Creating photoset '{self.title}' with {len(photo_ids)} photos...")
                resp = core.api_call_with_retry(
                    flickr.photosets.create,
                    title=self.title,
                    description=self.description,
                    primary_photo_id=photo_ids[0],
                )
                photoset_id = resp["photoset"]["id"]
                self.log_message.emit(f"Photoset created with ID: {photoset_id}")
                # Auto-fill the name field so future runs update this set
                self.set_photoset_name.emit(self.title)

                try:
                    self.log_message.emit("Attempting bulk add via editPhotos...")
                    core.api_call_with_retry(
                        flickr.photosets.editPhotos,
                        photoset_id=photoset_id,
                        primary_photo_id=photo_ids[0],
                        photo_ids=",".join(photo_ids),
                    )
                    self.log_message.emit("All photos added successfully via editPhotos.")
                except Exception as e:
                    self.log_message.emit(f"editPhotos failed ({e}), falling back to addPhoto loop...")
                    self._add_photos_individually(flickr, photoset_id, photo_ids)

            owner = nsid.replace("@", "%40")
            url = f"https://www.flickr.com/photos/{owner}/sets/{photoset_id}"
            self.log_message.emit(f"\nDone! View your photoset at:\n  {url}")

        except Exception as e:
            self.log_message.emit(f"\nError: {e}")
        finally:
            self.buttons_enabled.emit(True)

    def _resolve_photoset_name(self, flickr, nsid, name):
        """Look up a photoset ID by name from the user's photosets."""
        page = 1
        while True:
            resp = core.api_call_with_retry(
                flickr.photosets.getList,
                user_id=nsid,
                per_page=500,
                page=page,
            )
            photosets = resp["photosets"]["photoset"]
            for ps in photosets:
                if ps["title"]["_content"] == name:
                    return ps["id"]
            if page >= int(resp["photosets"]["pages"]):
                break
            page += 1
        return None

    def _add_photos_individually(self, flickr, photoset_id, photo_ids):
        remaining = photo_ids[1:]
        added, failed = 0, 0
        for i, pid in enumerate(remaining, start=1):
            try:
                core.api_call_with_retry(
                    flickr.photosets.addPhoto,
                    photoset_id=photoset_id,
                    photo_id=pid,
                )
                added += 1
            except Exception as ex:
                failed += 1
                self.log_message.emit(f"  Failed to add {pid}: {ex}")
            if i % 50 == 0 or i == len(remaining):
                self.log_message.emit(
                    f"  Progress: {i}/{len(remaining)} "
                    f"(added: {added}, failed: {failed})"
                )
            time.sleep(0.1)


class FlickrApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Flickr Interesting Photos Set Creator")
        self.setFixedSize(620, 720)

        icon_path = os.path.join(get_base_path(), "flickr_icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.worker = None
        self.dark_mode = False

        self._build_ui()
        self._load_credentials()
        self._load_settings()
        self._check_schedule_status()

    def _build_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(5)

        # --- Credentials ---
        cred_group = QGroupBox("Flickr API Credentials")
        cred_layout = QGridLayout()
        cred_group.setLayout(cred_layout)

        cred_layout.addWidget(QLabel("API Key:"), 0, 0)
        self.api_key_edit = QLineEdit()
        cred_layout.addWidget(self.api_key_edit, 0, 1)

        cred_layout.addWidget(QLabel("API Secret:"), 1, 0)
        self.api_secret_edit = QLineEdit()
        self.api_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        cred_layout.addWidget(self.api_secret_edit, 1, 1)

        main_layout.addWidget(cred_group)

        # --- Photoset Settings ---
        settings_group = QGroupBox("Photoset Settings")
        settings_layout = QGridLayout()
        settings_group.setLayout(settings_layout)

        settings_layout.addWidget(QLabel("Title:"), 0, 0)
        self.title_edit = QLineEdit("Top 1000 Most Interesting")
        settings_layout.addWidget(self.title_edit, 0, 1, 1, 2)

        settings_layout.addWidget(QLabel("Description:"), 1, 0)
        self.desc_edit = QLineEdit("Auto-generated set of my most interesting photos.")
        settings_layout.addWidget(self.desc_edit, 1, 1, 1, 2)

        settings_layout.addWidget(QLabel("Photo count:"), 2, 0)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 5000)
        self.count_spin.setValue(1000)
        settings_layout.addWidget(self.count_spin, 2, 1)

        settings_layout.addWidget(QLabel("Existing Photoset:"), 3, 0)
        self.photoset_name_edit = QLineEdit()
        settings_layout.addWidget(self.photoset_name_edit, 3, 1)
        hint_label = QLabel("(optional \u2014 name of set to update)")
        hint_font = hint_label.font()
        hint_font.setPointSize(8)
        hint_label.setFont(hint_font)
        settings_layout.addWidget(hint_label, 3, 2)

        main_layout.addWidget(settings_group)

        # --- Action Buttons ---
        btn_layout = QHBoxLayout()
        self.dry_run_btn = QPushButton("Dry Run")
        self.dry_run_btn.clicked.connect(lambda: self._start(dry_run=True))
        btn_layout.addWidget(self.dry_run_btn)

        self.create_btn = QPushButton("Create Photoset")
        self.create_btn.clicked.connect(lambda: self._start(dry_run=False))
        btn_layout.addWidget(self.create_btn)

        btn_layout.addStretch()

        self.theme_btn = QPushButton("Dark Mode")
        self.theme_btn.clicked.connect(self._toggle_theme)
        btn_layout.addWidget(self.theme_btn)

        main_layout.addLayout(btn_layout)

        # --- Scheduling ---
        sched_group = QGroupBox("Scheduling")
        sched_layout = QGridLayout()
        sched_group.setLayout(sched_layout)

        sched_layout.addWidget(QLabel("Frequency:"), 0, 0)
        self.freq_combo = QComboBox()
        self.freq_combo.addItems(["Daily", "Weekly"])
        self.freq_combo.currentIndexChanged.connect(self._on_freq_change)
        sched_layout.addWidget(self.freq_combo, 0, 1)

        sched_layout.addWidget(QLabel("Time:"), 0, 2)
        time_widget = QWidget()
        time_layout = QHBoxLayout(time_widget)
        time_layout.setContentsMargins(0, 0, 0, 0)

        self.hour_spin = ZeroPaddedSpinBox()
        self.hour_spin.setRange(0, 23)
        self.hour_spin.setValue(3)
        self.hour_spin.setWrapping(True)
        self.hour_spin.setFixedWidth(50)
        time_layout.addWidget(self.hour_spin)

        time_layout.addWidget(QLabel(":"))

        self.minute_spin = ZeroPaddedSpinBox()
        self.minute_spin.setRange(0, 59)
        self.minute_spin.setValue(0)
        self.minute_spin.setWrapping(True)
        self.minute_spin.setFixedWidth(50)
        time_layout.addWidget(self.minute_spin)

        tz_name = datetime.now().astimezone().strftime("%Z")
        tz_label = QLabel(f"  ({tz_name})")
        tz_font = tz_label.font()
        tz_font.setPointSize(8)
        tz_label.setFont(tz_font)
        time_layout.addWidget(tz_label)

        sched_layout.addWidget(time_widget, 0, 3)

        # Day of week (hidden by default for Daily)
        self.day_label = QLabel("Day:")
        sched_layout.addWidget(self.day_label, 1, 0)
        self.day_combo = QComboBox()
        self.day_combo.addItems(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"])
        sched_layout.addWidget(self.day_combo, 1, 1)
        self.day_label.setVisible(False)
        self.day_combo.setVisible(False)

        # Schedule buttons
        sched_btn_widget = QWidget()
        sched_btn_layout = QHBoxLayout(sched_btn_widget)
        sched_btn_layout.setContentsMargins(0, 0, 0, 0)
        self.schedule_btn = QPushButton("Schedule Task")
        self.schedule_btn.clicked.connect(self._schedule_task)
        sched_btn_layout.addWidget(self.schedule_btn)
        self.remove_sched_btn = QPushButton("Remove Schedule")
        self.remove_sched_btn.clicked.connect(self._remove_schedule)
        sched_btn_layout.addWidget(self.remove_sched_btn)
        sched_btn_layout.addStretch()
        sched_layout.addWidget(sched_btn_widget, 2, 0, 1, 4)

        # Schedule status
        self.sched_status_label = QLabel("Checking schedule status...")
        status_font = self.sched_status_label.font()
        status_font.setPointSize(8)
        self.sched_status_label.setFont(status_font)
        sched_layout.addWidget(self.sched_status_label, 3, 0, 1, 4)

        main_layout.addWidget(sched_group)

        # --- Output Log ---
        log_group = QGroupBox("Output")
        log_layout = QVBoxLayout()
        log_group.setLayout(log_layout)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)

        main_layout.addWidget(log_group, 1)  # stretch factor so log expands

    def _on_freq_change(self):
        is_weekly = self.freq_combo.currentText() == "Weekly"
        self.day_label.setVisible(is_weekly)
        self.day_combo.setVisible(is_weekly)

    # --- Theme ---

    def _apply_theme(self, dark):
        self.dark_mode = dark
        if dark:
            QApplication.instance().setStyleSheet(DARK_STYLESHEET)
            self.theme_btn.setText("Light Mode")
        else:
            QApplication.instance().setStyleSheet("")
            self.theme_btn.setText("Dark Mode")

    def _toggle_theme(self):
        self._apply_theme(not self.dark_mode)
        self._save_settings()

    # --- Settings persistence ---

    def _save_settings(self):
        data = {
            "title": self.title_edit.text(),
            "description": self.desc_edit.text(),
            "count": self.count_spin.value(),
            "photoset_name": self.photoset_name_edit.text(),
            "dark_mode": self.dark_mode,
        }
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # non-critical

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            if "title" in data:
                self.title_edit.setText(data["title"])
            if "description" in data:
                self.desc_edit.setText(data["description"])
            if "count" in data:
                self.count_spin.setValue(data["count"])
            if "photoset_name" in data:
                self.photoset_name_edit.setText(data["photoset_name"])
            elif "photoset_id" in data:
                # backward compat with old settings
                self.photoset_name_edit.setText(data["photoset_id"])
            if data.get("dark_mode", False):
                self._apply_theme(True)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _load_credentials(self):
        env_path = os.path.join(get_base_path(), ".env")
        load_dotenv(env_path)
        self.api_key_edit.setText(os.environ.get("FLICKR_API_KEY", ""))
        self.api_secret_edit.setText(os.environ.get("FLICKR_API_SECRET", ""))

    # --- Scheduling ---

    def _get_script_path(self):
        """Get the path to the CLI script."""
        return os.path.join(get_base_path(), "flickr_interestingness.py")

    def _get_python_path(self):
        """Get the path to the Python interpreter."""
        return sys.executable

    def _check_schedule_status(self):
        """Check if a scheduled task exists and update the status label."""
        try:
            result = subprocess.run(
                ["schtasks", "/query", "/tn", TASK_NAME],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self.sched_status_label.setText("Scheduled task is active.")
            else:
                self.sched_status_label.setText("No scheduled task found.")
        except Exception:
            self.sched_status_label.setText("Could not check schedule status.")

    def _schedule_task(self):
        photoset_name = self.photoset_name_edit.text().strip()
        if not photoset_name:
            QMessageBox.critical(
                self,
                "Error",
                "Existing Photoset name is required for scheduling.\n"
                "Create a photoset first, then enter its name here.",
            )
            return

        self._save_settings()

        python_path = self._get_python_path()
        script_path = self._get_script_path()
        title = self.title_edit.text()
        count = self.count_spin.value()
        hour = str(self.hour_spin.value()).zfill(2)
        minute = str(self.minute_spin.value()).zfill(2)
        start_time = f"{hour}:{minute}"

        tr = (
            f'"{python_path}" "{script_path}" '
            f'--photoset-name "{photoset_name}" '
            f'--title "{title}" '
            f'--count {count}'
        )

        cmd = [
            "schtasks", "/create",
            "/tn", TASK_NAME,
            "/tr", tr,
            "/st", start_time,
            "/f",
        ]

        freq = self.freq_combo.currentText()
        if freq == "Daily":
            cmd.extend(["/sc", "DAILY"])
        else:
            cmd.extend(["/sc", "WEEKLY", "/d", self.day_combo.currentText()])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                QMessageBox.information(self, "Success", "Scheduled task created successfully.")
                self.sched_status_label.setText("Scheduled task is active.")
            else:
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to create scheduled task:\n{result.stderr or result.stdout}",
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create scheduled task:\n{e}")

    def _remove_schedule(self):
        try:
            result = subprocess.run(
                ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                QMessageBox.information(self, "Success", "Scheduled task removed.")
                self.sched_status_label.setText("No scheduled task found.")
            else:
                QMessageBox.critical(
                    self,
                    "Error",
                    f"Failed to remove scheduled task:\n{result.stderr or result.stdout}",
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to remove scheduled task:\n{e}")

    # --- Logging ---

    def _append_log(self, msg):
        self.log_text.append(msg)
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_buttons(self, enabled):
        self.dry_run_btn.setEnabled(enabled)
        self.create_btn.setEnabled(enabled)

    # --- Main actions ---

    def _start(self, dry_run):
        api_key = self.api_key_edit.text().strip()
        api_secret = self.api_secret_edit.text().strip()
        if not api_key or not api_secret:
            QMessageBox.critical(self, "Error", "API Key and API Secret are required.")
            return

        if self.worker and self.worker.isRunning():
            return

        self.log_text.clear()

        if not dry_run:
            self._save_settings()

        self._set_buttons(False)
        self.worker = WorkerThread(
            api_key=api_key,
            api_secret=api_secret,
            dry_run=dry_run,
            title=self.title_edit.text(),
            description=self.desc_edit.text(),
            count=self.count_spin.value(),
            photoset_name=self.photoset_name_edit.text().strip(),
        )
        self.worker.log_message.connect(self._append_log)
        self.worker.buttons_enabled.connect(self._set_buttons)
        self.worker.set_photoset_name.connect(self.photoset_name_edit.setText)
        self.worker.start()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.quit()
            self.worker.wait(5000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = FlickrApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
