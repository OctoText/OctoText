import http.server
import io
import os
import socketserver
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import flask
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
    settings.get.side_effect = lambda path, **kw: defaults.get(path[0])
    settings.global_get.side_effect = lambda path, **kw: global_settings.get(tuple(path))
    settings.global_get_boolean.side_effect = lambda path, **kw: bool(global_settings.get(tuple(path), False))
    plugin._settings = settings
    return plugin


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
            assert resp.get_json() == {"result": False, "error": "LOGIN_E"}

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
