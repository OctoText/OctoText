import http.server
import io
import os
import queue
import re
import socketserver
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import flask
import octoprint.events
import pytest
from PIL import Image

import octoprint_octotext
from octoprint_octotext import OctoTextPlugin


def make_test_image(mode="RGB", size=(40, 20), fmt="JPEG"):
    img = Image.new(mode, size, (0, 0, 0, 255) if mode == "RGBA" else (0, 0, 0))
    for x in range(10):
        for y in range(10):
            img.putpixel((x, y), (255, 0, 0, 255) if mode == "RGBA" else (255, 0, 0))
            img.putpixel((size[0] - 1 - x, y), (0, 255, 0, 255) if mode == "RGBA" else (0, 255, 0))
            img.putpixel((x, size[1] - 1 - y), (0, 0, 255, 255) if mode == "RGBA" else (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def settings_value(defaults, path):
    value = defaults
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def make_plugin(settings_overrides=None, global_overrides=None):
    plugin = OctoTextPlugin()
    plugin._identifier = "OctoText"
    plugin._plugin_version = "0.3.3.1"
    plugin._basefolder = os.path.dirname(octoprint_octotext.__file__)
    plugin._logger = MagicMock()

    defaults = plugin.get_settings_defaults()
    if settings_overrides:
        defaults.update(settings_overrides)
    global_settings = {
        ("appearance", "name"): "TestPrinter",
        ("webcam", "snapshot"): "",
        ("webcam", "flipH"): False,
        ("webcam", "flipV"): False,
        ("webcam", "rotate90"): False,
    }
    if global_overrides:
        global_settings.update(global_overrides)

    settings = MagicMock()
    settings.get.side_effect = lambda path, **kw: settings_value(defaults, path)
    settings.global_get.side_effect = lambda path, **kw: global_settings.get(tuple(path))
    settings.global_get_boolean.side_effect = lambda path, **kw: bool(global_settings.get(tuple(path), False))
    plugin._settings = settings
    plugin._file_manager = MagicMock()
    plugin._plugin_manager = MagicMock()
    plugin.notifyQ = queue.Queue()
    return plugin


TEMPLATE_EVENT_KEYS = [
    "print_started",
    "print_done",
    "print_progress",
    "print_paused",
    "print_resumed",
    "print_cancelled",
    "print_failed",
    "upload",
    "error",
]


def make_templates(**overrides):
    templates = {key: {"subject": "", "body": ""} for key in TEMPLATE_EVENT_KEYS}
    for key, values in overrides.items():
        templates[key].update(values)
    return templates


def queue_notification(plugin, title, body, event_key, extras=None, sender="TestPrinter"):
    plugin.notifyQ = queue.Queue()
    plugin._prepare_email_message_and_send(
        title,
        body,
        sender=sender,
        send_image=False,
        event_key=event_key,
        template_extras=extras,
    )
    return plugin.notifyQ.get_nowait()


def test_metadata_and_security_mixins():
    plugin = make_plugin()
    assert octoprint_octotext.__plugin_name__ == "OctoText"
    assert octoprint_octotext.__plugin_pythoncompat__ == ">=3.7,<4"
    assert plugin.is_template_autoescaped() is True
    assert plugin.is_api_protected() is True
    assert plugin.get_api_commands() == {"test": []}
    assert "on_api_get" not in OctoTextPlugin.__dict__


def test_snapshot_source_new_webcam_system_iter_chunks():
    plugin = make_plugin()
    jpeg_bytes = make_test_image("RGB", (40, 20), "JPEG")

    def fake_take_snapshot(name):
        assert name == "classic"
        yield jpeg_bytes[:100]
        yield jpeg_bytes[100:]

    fake_webcam = SimpleNamespace(
        config=SimpleNamespace(name="classic", canSnapshot=True, flipH=False, flipV=False, rotate90=False),
        providerPlugin=SimpleNamespace(take_webcam_snapshot=fake_take_snapshot),
    )

    with patch.object(octoprint_octotext, "get_snapshot_webcam", return_value=fake_webcam):
        res = plugin._create_image_path_from_snapshot()

    assert res["result"] == "DELETE_IMAGE_AFTER_SENT"
    assert res["path"].endswith(".jpg")
    with Image.open(res["path"]) as img:
        assert img.size == (40, 20)
    os.remove(res["path"])


def test_snapshot_source_no_webcam_or_cannot_snapshot():
    plugin = make_plugin()

    with patch.object(octoprint_octotext, "get_snapshot_webcam", return_value=None):
        assert plugin._create_image_path_from_snapshot() is None

    fake_webcam = SimpleNamespace(
        config=SimpleNamespace(name="classic", canSnapshot=False, flipH=False, flipV=False, rotate90=False),
        providerPlugin=MagicMock(),
    )
    with patch.object(octoprint_octotext, "get_snapshot_webcam", return_value=fake_webcam):
        assert plugin._create_image_path_from_snapshot() is None


def test_snapshot_source_exception_falls_back_to_offline_jpg():
    plugin = make_plugin()

    with patch.object(octoprint_octotext, "get_snapshot_webcam", side_effect=RuntimeError("no webcam provider")):
        res = plugin._create_image_path_from_snapshot()

    assert res["result"] == "SNAP"
    assert res["path"].endswith("/static/img/offline.jpg")
    assert os.path.exists(res["path"])


def test_snapshot_take_exception_falls_back_to_offline_jpg():
    plugin = make_plugin()

    def failing_snapshot(name):
        raise ConnectionError("camera unreachable")

    fake_webcam = SimpleNamespace(
        config=SimpleNamespace(name="classic", canSnapshot=True, flipH=False, flipV=False, rotate90=False),
        providerPlugin=SimpleNamespace(take_webcam_snapshot=failing_snapshot),
    )
    with patch.object(octoprint_octotext, "get_snapshot_webcam", return_value=fake_webcam):
        res = plugin._create_image_path_from_snapshot()

    assert res["result"] == "SNAP"
    assert os.path.exists(res["path"])


@pytest.mark.parametrize(
    "hflip,vflip,rotate,expected_size",
    [
        (True, False, False, (40, 20)),
        (False, True, False, (40, 20)),
        (False, False, True, (20, 40)),
        (True, True, True, (20, 40)),
    ],
)
def test_process_snapshot_transforms(tmp_path, hflip, vflip, rotate, expected_size):
    plugin = make_plugin()

    jpg_path = str(tmp_path / "snap.jpg")
    with open(jpg_path, "wb") as f:
        f.write(make_test_image("RGB", (40, 20), "JPEG"))
    plugin._process_snapshot(jpg_path, hflip, vflip, rotate)
    plugin._logger.exception.assert_not_called()
    with Image.open(jpg_path) as img:
        assert img.size == expected_size

    rgba_path = str(tmp_path / "snap_rgba.jpg")
    with open(rgba_path, "wb") as f:
        f.write(make_test_image("RGBA", (40, 20), "PNG"))
    plugin._process_snapshot(rgba_path, hflip, vflip, rotate)
    plugin._logger.exception.assert_not_called()
    with Image.open(rgba_path) as img:
        assert img.size == expected_size
        assert img.mode == "RGB"


def test_legacy_octoprint_lt_1_9_snapshot_http():
    jpeg_bytes = make_test_image("RGB", (40, 20), "JPEG")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(jpeg_bytes)

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        plugin = make_plugin(
            global_overrides={
                ("webcam", "snapshot"): f"http://127.0.0.1:{port}/snapshot",
                ("webcam", "rotate90"): True,
            }
        )
        with patch.object(octoprint_octotext, "get_snapshot_webcam", None):
            res = plugin._create_image_path_from_snapshot()
        assert res["result"] == "DELETE_IMAGE_AFTER_SENT"
        with Image.open(res["path"]) as img:
            assert img.size == (20, 40)
        os.remove(res["path"])
    finally:
        server.shutdown()


def test_on_api_command_test_success_and_errors():
    app = flask.Flask(__name__)
    plugin = make_plugin()

    with app.app_context():
        with patch.object(plugin, "_prepare_email_message_and_send", return_value=True):
            resp = plugin.on_api_command("test", {})
            assert resp.get_json() == {"result": True, "error": None}

        with patch.object(plugin, "_prepare_email_message_and_send", return_value="LOGIN_E"):
            resp = plugin.on_api_command("test", {})
            assert resp.get_json() == {
                "result": False,
                "error": "LOGIN_E",
                "login": "YourEmail@outlook.com",
            }

        with patch.object(plugin, "_prepare_email_message_and_send", side_effect=RuntimeError("boom")):
            resp = plugin.on_api_command("test", {})
            assert resp.get_json() == {"result": False, "error": "SMTP_E"}


def test_classicwebcam_integration():
    try:
        from octoprint.plugins.classicwebcam import ClassicWebcamPlugin
    except ImportError:
        pytest.skip("classicwebcam not present on OctoPrint < 1.9")

    jpeg_bytes = make_test_image("RGB", (40, 20), "JPEG")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(jpeg_bytes)

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cw = ClassicWebcamPlugin()
        cw._logger = MagicMock()
        cw_settings = MagicMock()
        cw_vals = {
            "snapshot": f"http://127.0.0.1:{port}/snapshot",
            "snapshotTimeout": 5,
            "snapshotSslValidation": False,
            "flipH": False,
            "flipV": False,
            "rotate90": True,
        }
        cw_settings.get.side_effect = lambda p, **kw: cw_vals.get(p[0])
        cw_settings.get_int.side_effect = lambda p, **kw: int(cw_vals.get(p[0]))
        cw_settings.get_boolean.side_effect = lambda p, **kw: bool(cw_vals.get(p[0]))
        cw._settings = cw_settings

        fake_webcam = SimpleNamespace(
            config=SimpleNamespace(name="classic", canSnapshot=True, flipH=False, flipV=False, rotate90=True),
            providerPlugin=cw,
        )
        plugin = make_plugin()
        with patch.object(octoprint_octotext, "get_snapshot_webcam", return_value=fake_webcam):
            res = plugin._create_image_path_from_snapshot()

        assert res["result"] == "DELETE_IMAGE_AFTER_SENT"
        with Image.open(res["path"]) as img:
            assert img.size == (20, 40)
        os.remove(res["path"])
    finally:
        server.shutdown()


def test_gmail_app_password_strips_spaces_only_for_gmail():
    import smtplib

    gmail_plugin = make_plugin(
        settings_overrides={
            "smtp_name": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "user",
            "servername": "gmail.com",
            "server_pass": "abcd efgh ijkl mnop",
        }
    )
    mock_smtp = MagicMock()
    with patch.object(smtplib, "SMTP", return_value=mock_smtp):
        err, _ = gmail_plugin.smtp_login_server()
    assert err is None
    mock_smtp.login.assert_called_once_with("user@gmail.com", "abcdefghijklmnop")

    other_plugin = make_plugin(
        settings_overrides={
            "smtp_name": "smtp.office365.com",
            "smtp_port": 587,
            "username": "user",
            "servername": "outlook.com",
            "server_pass": "pass with spaces",
        }
    )
    mock_smtp_other = MagicMock()
    with patch.object(smtplib, "SMTP", return_value=mock_smtp_other):
        err, _ = other_plugin.smtp_login_server()
    assert err is None
    mock_smtp_other.login.assert_called_once_with("user@outlook.com", "pass with spaces")
def test_duplicate_progress_emails_deduplicated():
    plugin = make_plugin(
        settings_overrides={
            "en_progress": True,
            "en_progress_time": False,
            "progress_interval": 25,
        }
    )
    with patch.object(plugin, "_prepare_email_message_and_send") as mock_send:
        for _ in range(4):
            plugin.on_print_progress("local", "benchy.gcode", 25)
        assert mock_send.call_count == 1

        plugin.on_print_progress("local", "benchy.gcode", 50)
        assert mock_send.call_count == 2

        # New print resets last progress sent
        plugin.on_print_progress("local", "benchy.gcode", 0)
        plugin.on_print_progress("local", "benchy.gcode", 25)
        assert mock_send.call_count == 3

def test_time_progress_starts_and_stops_independently_of_other_events():
    import octoprint.events

    plugin = make_plugin(
        settings_overrides={
            "en_progress": True,
            "en_progress_time": True,
            "en_printstart": False,
            "en_printend": False,
        }
    )
    with patch.object(plugin, "manage_progress_thread") as mock_mgr:
        plugin.on_event(
            octoprint.events.Events.PRINT_STARTED,
            {"name": "test.gcode", "origin": "local"},
        )
        mock_mgr.assert_called_once_with()

        mock_mgr.reset_mock()
        plugin.on_event(
            octoprint.events.Events.PRINT_DONE,
            {"name": "test.gcode", "time": 120},
        )
        mock_mgr.assert_called_once_with(stop=True)


def test_time_thread_handles_none_print_time():
    import threading

    plugin = make_plugin(
        settings_overrides={
            "en_progress": True,
            "en_progress_time": True,
            "progress_interval": 1,
        }
    )
    plugin._printer = MagicMock()
    # First poll: printTime is None, printTimeLeft is 100; second poll: printTimeLeft is 0 -> clean exit
    plugin._printer.get_current_data.side_effect = [
        {"progress": {"printTime": None, "printTimeLeft": 100}},
        {"progress": {"printTime": None, "printTimeLeft": 100}},
        {"progress": {"printTime": 100, "printTimeLeft": 0}},
    ]
    stop_event = threading.Event()
    # Should not raise TypeError when printTime is None
    with patch.object(stop_event, "wait", return_value=False):
        plugin.time_thread(stop_event)

def test_blank_smtp_hostname_does_not_crash():
    import smtplib

    plugin = make_plugin(
        settings_overrides={
            "smtp_name": "smtp.example.com",
            "smtp_port": 587,
            "username": "myuser",
            "servername": None,
            "server_pass": "secret",
            "validate_username": False,
            "from_address": "",
        }
    )
    assert plugin._get_from_address() == "myuser"

    mock_smtp = MagicMock()
    with patch.object(smtplib, "SMTP", return_value=mock_smtp):
        err, _ = plugin.smtp_login_server()
    assert err is None
    mock_smtp.login.assert_called_once_with("myuser", "secret")

    # on_after_startup should also not crash when servername is None
    plugin.get_plugin_data_folder = MagicMock(return_value="/tmp/OctoText")
    with patch("octoprint_octotext.Thread") as mock_thread:
        plugin.on_after_startup()
        mock_thread.assert_called_once()



def test_suppress_idle_printer_errors():
    import octoprint.events

    plugin = make_plugin(
        settings_overrides={"en_error": True, "en_error_printing_only": True}
    )
    plugin._printer = MagicMock()
    plugin._printer.is_printing.return_value = False
    plugin._printer.is_paused.return_value = False

    with patch.object(plugin, "_prepare_email_message_and_send") as mock_send:
        plugin.on_event(
            octoprint.events.Events.ERROR, {"error": "Offline after error"}
        )
        mock_send.assert_not_called()

        plugin._printer.is_printing.return_value = True
        plugin.on_event(
            octoprint.events.Events.ERROR, {"error": "Thermal runaway"}
        )
        assert mock_send.call_count == 1

        plugin._printer.is_printing.return_value = False
        plugin._printer.is_paused.return_value = True
        plugin.on_event(
            octoprint.events.Events.ERROR, {"error": "Thermal runaway while paused"}
        )
        assert mock_send.call_count == 2


def test_error_sent_when_octoprint_transitions_to_offline_before_event():
    import octoprint.events

    plugin = make_plugin(
        settings_overrides={
            "en_error": True,
            "en_error_printing_only": True,
            "en_printstart": False,
        }
    )
    plugin._printer = MagicMock()
    plugin._printer.is_printing.return_value = False
    plugin._printer.is_paused.return_value = False

    with patch.object(plugin, "_prepare_email_message_and_send") as mock_send:
        plugin.on_event(
            octoprint.events.Events.PRINT_STARTED,
            {"name": "part.gcode", "origin": "local"},
        )
        # OctoPrint already transitioned to Error/Offline before firing Events.ERROR
        plugin.on_event(
            octoprint.events.Events.ERROR, {"error": "Printer halted"}
        )
        assert mock_send.call_count == 1
        # Subsequent error while now idle should be suppressed
        plugin.on_event(
            octoprint.events.Events.ERROR, {"error": "Offline after error"}
        )
        assert mock_send.call_count == 1
def test_login_error_returns_attempted_login_username():
    app = flask.Flask(__name__)
    plugin = make_plugin(
        settings_overrides={
            "username": "alice",
            "servername": "fastmail.com",
            "validate_username": False,
        }
    )
    with app.app_context():
        with patch.object(plugin, "_prepare_email_message_and_send", return_value="LOGIN_E"):
            resp = plugin.on_api_command("test", {})
            assert resp.get_json() == {
                "result": False,
                "error": "LOGIN_E",
                "login": "alice@fastmail.com",
            }
        with patch.object(plugin, "_prepare_email_message_and_send", return_value=True):
            resp = plugin.on_api_command("test", {})
            assert resp.get_json() == {"result": True, "error": None}

def test_fixed_time_progress_interval():
    import threading
    import octoprint.events

    plugin = make_plugin(
        settings_overrides={
            "en_progress": True,
            "en_progress_fixed_time": True,
            "en_progress_time": False,
            "progress_fixed_minutes": 45,
            "en_printstart": False,
            "en_printend": False,
        }
    )
    # Percentage progress should be skipped when fixed time mode is active
    with patch.object(plugin, "_prepare_email_message_and_send") as mock_send:
        plugin.on_print_progress("local", "part.gcode", 50)
        mock_send.assert_not_called()

    # PRINT_STARTED / PRINT_DONE start and stop the progress thread
    with patch.object(plugin, "manage_progress_thread") as mock_mgr:
        plugin.on_event(
            octoprint.events.Events.PRINT_STARTED,
            {"name": "part.gcode", "origin": "local"},
        )
        mock_mgr.assert_called_once_with()
        mock_mgr.reset_mock()
        plugin.on_event(
            octoprint.events.Events.PRINT_DONE,
            {"name": "part.gcode", "time": 120},
        )
        mock_mgr.assert_called_once_with(stop=True)

    # time_thread waits for progress_fixed_minutes * 60 seconds and sends update
    plugin._printer = MagicMock()
    plugin._printer.get_current_data.return_value = {
        "progress": {"printTime": 2700, "printTimeLeft": 3600}
    }
    stop_event = threading.Event()
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            return False
        stop_event.set()
        return True

    with patch.object(stop_event, "wait", side_effect=fake_wait):
        with patch.object(plugin, "_prepare_email_message_and_send") as mock_send:
            plugin.time_thread(stop_event)
            assert waits[0] == 45 * 60
            assert mock_send.call_count == 1
            assert "1:00:00" in mock_send.call_args[0][0]


def test_notification_throttle_default_and_configurable():
    plugin = make_plugin()
    assert plugin.get_settings_defaults()["notification_throttle"] == 60


def test_custom_template_defaults_are_disabled_and_cover_all_events():
    defaults = OctoTextPlugin().get_settings_defaults()
    assert defaults["en_custom_templates"] is False
    assert set(defaults["templates"]) == set(TEMPLATE_EVENT_KEYS)
    for template in defaults["templates"].values():
        assert template == {"subject": "", "body": ""}


def test_disabled_custom_templates_keep_builtin_wording():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": False,
            "templates": make_templates(
                print_started={"subject": "CUSTOM {file}", "body": "CUSTOM BODY"}
            ),
        }
    )
    message = queue_notification(
        plugin,
        "Print job started",
        "benchy.gcode has started printing locally.",
        "print_started",
        {"file": "benchy.gcode"},
    )
    assert message["Subject"] == "TestPrinter: Print job started"
    body = message.get_content()
    assert "benchy.gcode has started printing locally." in body
    assert "CUSTOM" not in body


def test_blank_fields_fall_back_and_overrides_stay_per_event():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "templates": make_templates(
                print_started={"subject": "Started {file}", "body": "Start body"},
                print_done={"subject": "", "body": ""},
            ),
        }
    )
    started = queue_notification(
        plugin,
        "Print job started",
        "benchy.gcode has started printing locally.",
        "print_started",
        {"file": "benchy.gcode"},
    )
    assert started["Subject"] == "TestPrinter: Started benchy.gcode"
    assert started.get_content().startswith("Start body")

    done = queue_notification(
        plugin,
        "Print job finished",
        "benchy.gcode \n\rfinished printing.",
        "print_done",
        {"file": "benchy.gcode", "percent": 100},
    )
    assert done["Subject"] == "TestPrinter: Print job finished"
    assert "finished printing" in done.get_content()


def test_all_core_placeholders_render_from_printer_data():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "include_appearance_prefix": False,
            "subject_prefix": "PFX ",
            "templates": make_templates(
                print_progress={
                    "subject": "{event} {percent} {file}",
                    "body": (
                        "{message}|{printer}|{file}|{percent}|{elapsed}|{remaining}|"
                        "{eta}|{tool_temp}|{bed_temp}|{state}|{error}|{user}|{reason}"
                    ),
                }
            ),
        }
    )
    plugin._printer = MagicMock()
    plugin._printer.get_current_data.return_value = {
        "job": {
            "file": {
                "name": "folder/benchy.gcode",
                "origin": "local",
                "path": "folder/benchy.gcode",
            }
        },
        "progress": {
            "completion": 42.7,
            "printTime": 4500,
            "printTimeLeft": 3600,
            "printTimeLeftOrigin": "genius",
        },
        "state": {"text": "Printing"},
    }
    plugin._printer.get_current_temperatures.return_value = {
        "tool0": {"actual": 210.4, "target": 210.0},
        "tool1": {"actual": 205.2, "target": 205.0},
        "bed": {"actual": 60.1, "target": 60.0},
    }
    plugin._file_manager.get_metadata.return_value = {
        "analysis": {"estimatedPrintTime": 9000}
    }

    message = queue_notification(
        plugin,
        "Print Progress 50% finished.",
        "benchy.gcode",
        "print_progress",
        {
            "file": "folder/benchy.gcode",
            "percent": 50,
            "error": "Thermal runaway",
            "user": "admin",
            "reason": "error",
        },
    )
    assert message["Subject"] == "PFX Print Progress 50% finished. 50 benchy.gcode"
    parts = message.get_content().rstrip("\n").split("|")
    assert parts[0] == "benchy.gcode"
    assert parts[1] == "TestPrinter"
    assert parts[2] == "benchy.gcode"
    assert parts[3] == "50"
    assert parts[4] == "1:15:00"
    assert parts[5] == "1:00:00"
    assert re.match(r"^\d{1,2}:\d{2} [AP]M$", parts[6])
    assert parts[7] == "T0 210°C / 210°C, T1 205°C / 205°C"
    assert parts[8] == "60°C / 60°C"
    assert parts[9] == "Printing"
    assert parts[10] == "Thermal runaway"
    assert parts[11] == "admin"
    assert parts[12] == "error"


def test_meta_paths_resolve_dicts_and_fall_back_safely():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "include_appearance_prefix": False,
            "templates": make_templates(
                print_progress={
                    "subject": "",
                    "body": (
                        "{meta:progress.printTimeLeftOrigin}|"
                        "{meta:analysis.estimatedPrintTime}|"
                        "{meta:analysis.filament.tool0.length}|"
                        "{meta:missing.path}"
                    ),
                }
            ),
        }
    )
    plugin._printer = MagicMock()
    plugin._printer.get_current_data.return_value = {
        "progress": {"printTimeLeftOrigin": "linear"},
        "job": {
            "file": {
                "name": "benchy.gcode",
                "origin": "local",
                "path": "benchy.gcode",
            }
        },
    }
    plugin._file_manager.get_metadata.return_value = {
        "analysis": {
            "estimatedPrintTime": 9000.0,
            "filament": {"tool0": {"length": 12500.0}},
        }
    }

    message = queue_notification(
        plugin,
        "Print Progress",
        "benchy.gcode",
        "print_progress",
        {"file": "benchy.gcode", "origin": "local", "path": "benchy.gcode"},
    )
    assert message.get_content().rstrip("\n") == "linear|9000|12500|"

    plugin._file_manager.get_metadata.return_value = {"analysis": "not-a-dict"}
    message = queue_notification(
        plugin,
        "Print Progress",
        "benchy.gcode",
        "print_progress",
        {"file": "benchy.gcode", "origin": "local", "path": "benchy.gcode"},
    )
    assert message.get_content().rstrip("\n") == "linear|||"


def test_plugin_helpers_resolve_and_fail_safely():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "include_appearance_prefix": False,
            "templates": make_templates(
                print_progress={
                    "subject": (
                        "{plugin:foo.estimate}|{plugin:foo.details.eta}|"
                        "{plugin:foo.missing}|{plugin:bar.nope}"
                    ),
                    "body": "",
                }
            ),
        }
    )

    def failing_helper():
        raise RuntimeError("boom")

    plugin._plugin_manager.get_helpers.side_effect = lambda plugin_id: {
        "foo": {
            "estimate": lambda: 1234,
            "details": lambda: {"eta": "3:45 PM"},
            "broken": failing_helper,
        }
    }.get(plugin_id, {})

    message = queue_notification(
        plugin, "Print Progress", "benchy.gcode", "print_progress", {}
    )
    assert message["Subject"] == "1234|3:45 PM||"

    plugin._plugin_manager.get_helpers.return_value = {"broken": failing_helper}
    assert plugin._resolve_plugin_helper("foo.broken") is octoprint_octotext._MISSING
    plugin._plugin_manager.get_helpers.side_effect = KeyError("missing")
    assert plugin._resolve_plugin_helper("bar.helper") is octoprint_octotext._MISSING


def test_unknown_variables_and_stray_braces_are_left_literal():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "templates": make_templates(
                print_started={
                    "subject": "{unknown} {oops {printer.__class__}",
                    "body": "",
                }
            ),
        }
    )
    message = queue_notification(
        plugin,
        "Print job started",
        "benchy.gcode has started printing locally.",
        "print_started",
        {"file": "benchy.gcode"},
    )
    assert message["Subject"] == "TestPrinter: {unknown} {oops {printer.__class__}"


def test_subject_line_breaks_are_collapsed_and_empty_render_falls_back():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "templates": make_templates(
                print_done={"subject": "{message}", "body": ""}
            ),
        }
    )
    message = queue_notification(
        plugin,
        "Print job finished",
        "benchy.gcode \n\rfinished printing.",
        "print_done",
        {"file": "benchy.gcode", "percent": 100},
    )
    assert "\n" not in message["Subject"]
    assert "\r" not in message["Subject"]
    assert message["Subject"] == "TestPrinter: benchy.gcode  finished printing."

    fallback_defaults = plugin.get_settings_defaults()
    fallback_defaults["en_custom_templates"] = True
    fallback_defaults["templates"]["print_done"]["subject"] = "{meta:missing.path}"
    plugin._settings.get.side_effect = lambda path, **kw: settings_value(
        fallback_defaults, path
    )
    message = queue_notification(
        plugin,
        "Print job finished",
        "benchy.gcode \n\rfinished printing.",
        "print_done",
        {"file": "benchy.gcode", "percent": 100},
    )
    assert message["Subject"] == "TestPrinter: Print job finished"


def test_missing_printer_data_renders_empty_values():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "include_appearance_prefix": False,
            "templates": make_templates(
                print_progress={
                    "subject": "",
                    "body": (
                        "{percent}|{elapsed}|{remaining}|{eta}|"
                        "{tool_temp}|{bed_temp}|{state}"
                    ),
                }
            ),
        }
    )
    plugin._printer = MagicMock()
    plugin._printer.get_current_data.side_effect = RuntimeError("printer offline")
    plugin._printer.get_current_temperatures.side_effect = RuntimeError("offline")
    plugin._printer.get_state_id.return_value = None

    message = queue_notification(
        plugin, "Print Progress", "benchy.gcode", "print_progress", {}
    )
    assert message.get_content().rstrip("\n") == "||||||"


def test_print_done_extras_drive_elapsed_percent_and_remaining():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "include_appearance_prefix": False,
            "templates": make_templates(
                print_done={
                    "subject": "",
                    "body": "{elapsed}|{percent}|{remaining}|{eta}",
                }
            ),
        }
    )
    message = queue_notification(
        plugin,
        "Print job finished",
        "ignored",
        "print_done",
        {"elapsed": 9000, "percent": 100, "remaining": 0},
    )
    assert message.get_content().rstrip("\n") == "2:30:00|100|0:00:00|"


def test_custom_body_omits_sent_from_footer_and_keeps_prefixes():
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "subject_prefix": "PFX ",
            "include_appearance_prefix": True,
            "templates": make_templates(
                print_started={"subject": "S", "body": "B"}
            ),
        }
    )
    message = queue_notification(
        plugin,
        "Print job started",
        "ignored",
        "print_started",
        {"file": "benchy.gcode"},
    )
    assert message["Subject"] == "PFX TestPrinter: S"
    assert message.get_content().rstrip("\n") == "B"


def test_preview_marks_unresolved_advanced_lookups():
    plugin = make_plugin()
    assert plugin._render_template("{meta:a.b} {plugin:x.y}", {}, preview=True) == "??? ???"
    assert plugin._render_template("{meta:a.b} {plugin:x.y}", {}) == " "
    assert (
        plugin._render_template(
            "{meta:progress.printTimeLeftOrigin}",
            {},
            preview=True,
            sample_meta=octoprint_octotext._SAMPLE_META_VALUES,
        )
        == "genius"
    )


def test_test_message_uses_selected_event_override_with_samples():
    app = flask.Flask(__name__)
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "templates": make_templates(
                print_done={"subject": "Done: {file}", "body": "Percent {percent}"}
            ),
        }
    )
    with app.app_context():
        with patch.object(
            plugin, "_prepare_email_message_and_send", return_value=True
        ) as mock_send:
            plugin.on_api_command("test", {"event": "print_done"})
    assert mock_send.call_args[0][0] == "Done: benchy.gcode"
    assert mock_send.call_args[0][1] == "Percent 100"
    assert mock_send.call_args[1]["sender"] == "TestPrinter"


@pytest.mark.parametrize("event_data", [{}, {"event": ""}, {"event": "unknown"}])
def test_test_message_keeps_builtin_wording_without_selected_override(event_data):
    app = flask.Flask(__name__)
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": True,
            "templates": make_templates(
                print_done={"subject": "", "body": ""}
            ),
        }
    )
    with app.app_context():
        with patch.object(
            plugin, "_prepare_email_message_and_send", return_value=True
        ) as mock_send:
            plugin.on_api_command("test", event_data)
    assert mock_send.call_args[0][0] == "Test from the OctoText Plugin."
    assert mock_send.call_args[0][1] == "Your printer is creating something wonderful!"
    assert mock_send.call_args[1]["sender"] == "OctoText"


def test_test_message_ignores_overrides_when_custom_templates_disabled():
    app = flask.Flask(__name__)
    plugin = make_plugin(
        settings_overrides={
            "en_custom_templates": False,
            "templates": make_templates(
                print_done={"subject": "Done: {file}", "body": "Percent {percent}"}
            ),
        }
    )
    with app.app_context():
        with patch.object(
            plugin, "_prepare_email_message_and_send", return_value=True
        ) as mock_send:
            plugin.on_api_command("test", {"event": "print_done"})
    assert mock_send.call_args[0][0] == "Test from the OctoText Plugin."
    assert mock_send.call_args[1]["sender"] == "OctoText"


def test_event_samples_match_builtin_wording():
    plugin = make_plugin(settings_overrides={"en_webcam": False})
    plugin._printer = MagicMock()
    plugin._printer.is_printing.return_value = False
    plugin._printer.is_paused.return_value = False
    samples = plugin._get_event_samples()

    cases = [
        (
            octoprint.events.Events.UPLOAD,
            {"name": "benchy.gcode", "path": "local", "origin": "local"},
            "upload",
        ),
        (
            octoprint.events.Events.PRINT_STARTED,
            {"name": "benchy.gcode", "origin": "local"},
            "print_started",
        ),
        (
            octoprint.events.Events.PRINT_DONE,
            {"name": "benchy.gcode", "time": 9000},
            "print_done",
        ),
        (
            octoprint.events.Events.ERROR,
            {"error": "Thermal runaway"},
            "error",
        ),
    ]
    for event, payload, key in cases:
        with patch.object(plugin, "_prepare_email_message_and_send") as mock_send:
            plugin.on_event(event, payload)
        assert mock_send.call_args[0][0] == samples[key]["event"]
        assert mock_send.call_args[0][1] == samples[key]["message"]

    cancel_plugin = make_plugin(
        settings_overrides={
            "en_webcam": False,
            "show_fail_cancel": True,
            "en_printfail": "Cancel",
        }
    )
    with patch.object(cancel_plugin, "_prepare_email_message_and_send") as mock_send:
        cancel_plugin.on_event(
            octoprint.events.Events.PRINT_CANCELLED,
            {"name": "benchy.gcode", "user": "admin"},
        )
    assert mock_send.call_args[0][0] == samples["print_cancelled"]["event"]
    assert mock_send.call_args[0][1] == samples["print_cancelled"]["message"]

    fail_plugin = make_plugin(
        settings_overrides={
            "en_webcam": False,
            "show_fail_cancel": True,
            "en_printfail": "Fail",
        }
    )
    with patch.object(fail_plugin, "_prepare_email_message_and_send") as mock_send:
        fail_plugin.on_event(
            octoprint.events.Events.PRINT_FAILED,
            {"name": "benchy.gcode", "reason": "error", "time": 120},
        )
    assert mock_send.call_args[0][0] == samples["print_failed"]["event"]
    assert mock_send.call_args[0][1] == samples["print_failed"]["message"]

    pause_plugin = make_plugin(settings_overrides={"en_webcam": False})
    with patch.object(pause_plugin, "_prepare_email_message_and_send") as mock_send:
        pause_plugin.on_event(
            octoprint.events.Events.PRINT_PAUSED,
            {"name": "benchy.gcode", "user": "admin"},
        )
    assert mock_send.call_args[0][0].startswith("Print Paused by admin at ")
    assert samples["print_paused"]["event"].startswith("Print Paused by admin at ")
    assert mock_send.call_args[0][1] == samples["print_paused"]["message"]

    resume_plugin = make_plugin(
        settings_overrides={"en_webcam": False, "en_printresumed": True}
    )
    with patch.object(resume_plugin, "_prepare_email_message_and_send") as mock_send:
        resume_plugin.on_event(
            octoprint.events.Events.PRINT_RESUMED,
            {"name": "benchy.gcode", "user": "admin"},
        )
    assert mock_send.call_args[0][0].startswith("Resumed by admin at ")
    assert samples["print_resumed"]["event"].startswith("Resumed by admin at ")
    assert mock_send.call_args[0][1] == samples["print_resumed"]["message"]
