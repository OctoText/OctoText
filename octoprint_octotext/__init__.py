# This is the working branch - changes to this version include:
# Redoing how notifications are sent.
#
# The current plan is to implement a thread-queue method
#  New notifications will be put on a queue, which feeds a thread that is running
#  consuming FIFO events to be sent
#  The reason to do it this way is to allow for retries when the network goes away
#  Network (internet interruptions) happen frequently on Starlink, so this should be
#  easy to test.
#
import datetime
import os
import re
import smtplib
import tempfile
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from queue import Queue
from threading import Thread

import flask
import octoprint.events
import octoprint.plugin
import requests
from flask_login import current_user

try:
    from PIL import Image, ImageOps
except ImportError:  # Pillow is optional, snapshots then stay unprocessed
    Image = None
    ImageOps = None

try:
    from octoprint.webcams import get_snapshot_webcam
except ImportError:  # OctoPrint < 1.9.0
    get_snapshot_webcam = None

# a few globals to save time checking for the existence of plugins

_TEMPLATE_EVENT_KEYS = (
    "print_started",
    "print_done",
    "print_progress",
    "print_paused",
    "print_resumed",
    "print_cancelled",
    "print_failed",
    "upload",
    "error",
)

# Sample values for the documented third-party metadata examples.  These are
# used when a test message renders a custom template while the printer is idle.
_SAMPLE_META_VALUES = {
    "progress.printTimeLeftOrigin": "genius",
    "analysis.estimatedPrintTime": "9000",
    "analysis.filament.tool0.length": "12500.0",
}

# Marker for a template value that could not be resolved (distinct from a
# resolved ``None``/empty value).
_MISSING = object()


class OctoTextPlugin(
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.ProgressPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.TemplatePlugin,
):
    notifyQ = Queue()
    last_fired = None
    prusa_folder = ""
    cura_folder = ""

    def email_message_queue_worker(self):
        """
        This function is started as a thread and blocks on a queue looking for work. It sends all
        of the notifications except for the test message (because we want the error code to be reported
        to the user by an error notice).
        Occasionally I've seen delays of up to 15 minutes with text messages due to poor cell connections
        or just carrier issues - but this code seems to retry correctly on internet connection issues.
        Retry-able error codes:
            SMTP_E - for errors setting up the SMTP connection

            LOGIN_E - for errors logging into the host email account

            SENDM_E - error sending email from server
        :return: None
        """
        while True:
            self._logger.debug("NO Work being done")
            email_message = self.notifyQ.get()
            # do the work
            # self._logger.debug(f"processing email  {email_message}")
            self._logger.debug("processing email  %s", email_message["Subject"])
            result = False
            retries = 0
            first_time = datetime.datetime.now()
            orig_subject = email_message["Subject"]
            while result is False:
                if retries > 0:
                    retry_str = " retries: " + str(retries)
                else:
                    retry_str = ""

                del email_message["Subject"]
                email_message["Subject"] = orig_subject + retry_str
                result = self._send_email_message(email_message)

                retries += 1
                if retries > 5:
                    break
                if result in ["SMTP_E", "LOGIN_E", "SENDM_E"]:

                    self._logger.debug("Retrying notification, error %s", result)
                    time.sleep(30)
                    result = False
                else:
                    break
            now_time = datetime.datetime.now()
            elapsed_time = now_time - first_time
            if (
                elapsed_time.seconds > 29
            ):  # >= time.sleep(30) says we had at least one delayed notice
                self._logger.debug(
                    "Retries sending message: %s. Time message delayed: %s",
                    retries,
                    elapsed_time,
                )
            self._logger.debug("Send Message result: %s", result)
            self.notifyQ.task_done()
            throttle = max(
                0, int(self._settings.get(["notification_throttle"]) or 0)
            )
            if throttle > 0:
                time.sleep(throttle)
        pass

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return {
            "smtp_port": 587,
            "smtp_name": "smtp.office365.com",
            "smtp_alert": "*ALERT from your PRINTER*",
            "smtp_message": "Your printer is creating something wonderful!",
            "from_address": "",
            "username": "YourEmail",
            "servername": "outlook.com",
            "server_login": "YourEmail@outlook.com",  # obsoleted
            "validate_username": False,
            "server_pass": "not a valid password",
            "phone_numb": "8675309",
            "carrier_address": "mypixmessages.com",
            "push_message": None,
            "progress_interval": 10,
            "en_progress_time": False,
            "en_progress_fixed_time": False,
            "progress_fixed_minutes": 60,
            "en_progress": False,
            "en_webcam": True,
            "en_printstart": True,
            "en_printend": True,
            "en_upload": True,
            "en_error": True,
            "en_error_printing_only": False,
            "en_printfail": False,
            "en_printpaused": True,
            "en_printresumed": False,
            "subject_prefix": "",
            "include_appearance_prefix": True,
            "show_navbar_button": True,
            "show_fail_cancel": False,
            "mmu_timeout": 0,
            "notification_throttle": 60,
            "use_ssl": False,
            "en_custom_templates": False,
            "templates": {
                event_key: {"subject": "", "body": ""}
                for event_key in _TEMPLATE_EVENT_KEYS
            },
        }

    def get_printer_name(self):
        a_name = self._settings.global_get(["appearance", "name"])
        if a_name == "":
            a_name = "OctoText"
        return a_name

    # ~~ PrintProgressPlugin

    current_path = ""
    _last_progress_sent = None
    _is_printing = False

    def on_print_progress(self, storage, path, progress):

        self.current_path = path
        if not self._settings.get(["en_progress"]):
            return

        if progress == 0:
            self._last_progress_sent = None
            return

        if self._settings.get(["en_progress_time"]) or self._settings.get(
            ["en_progress_fixed_time"]
        ):
            return

        # if these two events fire at the same time (printend and progress at 100%) we have two threads that are async
        # to each other that try to send notifications at the same time. This has caused both of these threads to fail
        # on a Pi 4 (not so much on a fast laptop). We default to letting the printend message do the work
        if progress == 100 and self._settings.get(["en_printend"]):
            return
        # occasionally we don't get 99% messages from progress intervals (might be on short prints)
        if progress % int(self._settings.get(["progress_interval"])) == 0:
            if progress == self._last_progress_sent:
                return
            self._last_progress_sent = progress
            printer_name = self.get_printer_name()
            title = f"Print Progress {progress}% finished."
            description = path
            self._prepare_email_message_and_send(
                title,
                description,
                printer_name,
                None,
                self._settings.get(["en_webcam"]),
                event_key="print_progress",
                template_extras={
                    "file": path,
                    "percent": progress,
                    "origin": storage,
                    "path": path,
                },
            )

    ##~~ AssetPlugin mixin

    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return {"js": ["js/OctoText.js"]}

    def get_template_configs(self):
        return [
            {"type": "navbar", "name": "OctoText", "custom_bindings": True},
            {"type": "settings", "name": "OctoText", "custom_bindings": True},
        ]

    def is_template_autoescaped(self):
        return True

    # access restrictions for sensitive data
    def on_settings_load(self):
        data = octoprint.plugin.SettingsPlugin.on_settings_load(self)

        # only return our restricted settings to admin users - this is only needed for OctoPrint <= 1.2.16
        restricted = ("server_pass", "username", "servername")
        for r in restricted:
            if r in data and (
                current_user is None
                or current_user.is_anonymous
                or not current_user.has_permission
            ):
                data[r] = None

        return data

    def get_settings_restricted_paths(self):
        # only used in OctoPrint versions > 1.2.16
        return {"admin": [["server_pass"], ["username"], ["servername"]]}

    def on_settings_save(self, data):

        if "server_pass" in data and not data["server_pass"]:
            data["server_pass"] = None

        if "servername" in data and not data["servername"]:
            data["servername"] = None

        if "username" in data and not data["username"]:
            data["username"] = None

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

    def smtp_login_server(self):
        """
        Login to the mail server

        :return:
            first position:
                SMTP_E for errors setting up the SMTP connection
                LOGIN_E for errors logging into the host email account
                None for no error found.
            second position:
                email address of the recipient when there is no error, None otherwise
        """
        global SMTP_server
        name = self._settings.get(["smtp_name"])
        port = self._settings.get(["smtp_port"])
        # message = self._settings.get(["smtp_message"])
        validate = self._settings.get(["validate_username"])
        if validate:
            login = self._settings.get(["username"]) or ""
        else:
            login = self._format_address(
                self._settings.get(["username"]),
                self._settings.get(["servername"]),
            )

        passw = self._settings.get(["server_pass"])
        if passw and (name or "").strip().lower() == "smtp.gmail.com":
            passw = passw.replace(" ", "")
        phone_numb = self._settings.get(["phone_numb"])
        carrier_addr = self._settings.get(["carrier_address"])
        # alert = self._settings.get(["smtp_alert"])
        self._logger.debug(name)
        self._logger.debug(port)

        # setup the server with the SMTP address/port
        try:
            self._logger.debug("before server smtplib")
            if self._settings.get(["use_ssl"]):
                SMTP_server = smtplib.SMTP_SSL(name, port, timeout=5)
                SMTP_server.ehlo()
            else:
                SMTP_server = smtplib.SMTP(name, port, timeout=5)
                error = SMTP_server.starttls()
                self._logger.debug("startttls() %s", error)
            self._logger.debug("SMTP_server %s", SMTP_server)
        except Exception as e:
            self._logger.exception("Exception while talking to your mail server %s", e)
            return ["SMTP_E", None]

        # login to the mail account
        self._logger.debug("Attempting SMTP login as: %s", login)
        if (
            passw
        ):  # Only use SMTP auth if the password has been supplied, skip if blank - issue #91
            self._logger.debug("Password supplied, attempting to log into mail server")
            try:
                SMTP_server.login(login, passw)
            except Exception as e:
                self._logger.exception(
                    "Exception while logging into mail server as '%s': %s", login, e
                )
                SMTP_server.quit()
                return ["LOGIN_E", None]
        else:
            self._logger.debug(
                "Password not supplied, proceeding without SMTP authentication."
            )

        email_addr = f"{phone_numb}@{carrier_addr}"
        return [None, email_addr]

    @staticmethod
    def _format_address(local_part, domain):
        local_part = local_part or ""
        domain = domain or ""
        if local_part and domain:
            return f"{local_part}@{domain}"
        return local_part

    def _get_from_address(self):
        from_addr = (self._settings.get(["from_address"]) or "").strip()
        if from_addr:
            return from_addr
        if self._settings.get(["validate_username"]):
            return self._settings.get(["username"]) or ""
        return self._format_address(
            self._settings.get(["username"]),
            self._settings.get(["servername"]),
        )

    ##~~ Custom notification templates

    @staticmethod
    def _stringify_template_value(value):
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @staticmethod
    def _walk_dict(root, keys):
        value = root
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return _MISSING
            value = value[key]
        return value

    def _safe_current_data(self):
        try:
            data = self._printer.get_current_data()
        except Exception:
            self._logger.debug("Could not read current printer data", exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def _safe_current_temperatures(self):
        try:
            temperatures = self._printer.get_current_temperatures()
        except Exception:
            self._logger.debug("Could not read current temperatures", exc_info=True)
            return {}
        return temperatures if isinstance(temperatures, dict) else {}

    def _safe_file_metadata(self, origin, path):
        if not origin or not path:
            return {}
        try:
            metadata = self._file_manager.get_metadata(origin, path)
        except Exception:
            self._logger.debug(
                "Could not read file metadata for %s:%s", origin, path, exc_info=True
            )
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def _current_job_file(self):
        job = self._safe_current_data().get("job") or {}
        file_info = job.get("file") or {}
        if not isinstance(file_info, dict):
            return {"name": "", "origin": "", "path": ""}
        name = file_info.get("name") or ""
        return {
            "name": os.path.basename(name) if name else "",
            "origin": file_info.get("origin") or "",
            "path": file_info.get("path") or name,
        }

    @staticmethod
    def _format_duration(seconds):
        if seconds is None:
            return ""
        try:
            total = int(float(seconds))
        except (TypeError, ValueError):
            return ""
        if total < 0:
            return ""
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _format_clock(seconds):
        if seconds is None:
            return ""
        try:
            total = int(float(seconds))
        except (TypeError, ValueError):
            return ""
        if total <= 0:
            return ""
        try:
            completion = datetime.datetime.now() + datetime.timedelta(seconds=total)
        except (OverflowError, OSError):
            return ""
        return completion.strftime("%I:%M %p").lstrip("0")

    @staticmethod
    def _format_temperature_value(value):
        if value is None:
            return "?"
        try:
            return str(int(round(float(value))))
        except (TypeError, ValueError):
            return "?"

    def _format_temperature_pair(self, entry):
        if not isinstance(entry, dict):
            return ""
        actual = entry.get("actual")
        target = entry.get("target")
        if actual is None and target is None:
            return ""
        return (
            f"{self._format_temperature_value(actual)}°C / "
            f"{self._format_temperature_value(target)}°C"
        )

    def _tool_temperatures(self):
        temperatures = self._safe_current_temperatures()
        tools = []
        for key in temperatures:
            if isinstance(key, str) and key.startswith("tool") and key[4:].isdigit():
                tools.append((int(key[4:]), key))
        tools.sort()
        parts = []
        for number, key in tools:
            pair = self._format_temperature_pair(temperatures.get(key))
            if pair:
                parts.append(f"T{number} {pair}")
        return ", ".join(parts)

    def _bed_temperature(self):
        return self._format_temperature_pair(
            self._safe_current_temperatures().get("bed")
        )

    def _printer_state_text(self):
        state = self._safe_current_data().get("state") or {}
        if isinstance(state, dict) and state.get("text"):
            return str(state["text"])
        try:
            state_id = self._printer.get_state_id()
        except Exception:
            return ""
        return state_id.replace("_", " ").title() if state_id else ""

    def _resolve_meta_path(self, dotted_path, origin=None, path=None):
        keys = [key for key in (dotted_path or "").split(".") if key]
        if not keys:
            return _MISSING
        roots = [self._safe_current_data()]
        metadata = self._safe_file_metadata(origin, path)
        if metadata:
            roots.append(metadata)
        for root in roots:
            value = self._walk_dict(root, keys)
            if value is not _MISSING:
                return value
        return _MISSING

    def _resolve_plugin_helper(self, expression):
        parts = [part for part in (expression or "").split(".") if part]
        if len(parts) < 2:
            return _MISSING
        plugin_id, helper_name, key_path = parts[0], parts[1], parts[2:]
        try:
            helpers = self._plugin_manager.get_helpers(plugin_id)
        except Exception:
            self._logger.debug(
                "Could not load helpers for plugin %s", plugin_id, exc_info=True
            )
            return _MISSING
        if not isinstance(helpers, dict):
            return _MISSING
        helper = helpers.get(helper_name)
        if not callable(helper):
            return _MISSING
        try:
            value = helper()
        except Exception:
            self._logger.debug(
                "Plugin helper %s.%s failed", plugin_id, helper_name, exc_info=True
            )
            return _MISSING
        if key_path:
            return self._walk_dict(value, key_path)
        return value

    _TEMPLATE_TOKEN = re.compile(r"\{([^{}]+)\}")

    def _render_template(
        self,
        template,
        context,
        preview=False,
        origin=None,
        path=None,
        sample_meta=None,
    ):
        if not template:
            return ""

        def replace(match):
            token = match.group(1).strip()
            if token.startswith("meta:"):
                expression = token[len("meta:") :].strip()
                if sample_meta and expression in sample_meta:
                    return self._stringify_template_value(sample_meta[expression])
                value = self._resolve_meta_path(expression, origin, path)
            elif token.startswith("plugin:"):
                value = self._resolve_plugin_helper(token[len("plugin:") :].strip())
            elif token in context:
                return self._stringify_template_value(context[token])
            else:
                # Leave unknown variables and stray braces untouched instead of
                # raising like str.format() would.
                return match.group(0)
            if value is _MISSING:
                return "???" if preview else ""
            return self._stringify_template_value(value)

        return self._TEMPLATE_TOKEN.sub(replace, str(template))

    def _resolve_template(
        self,
        event_key,
        field,
        context,
        preview=False,
        origin=None,
        path=None,
        sample_meta=None,
    ):
        if not self._settings.get(["en_custom_templates"]):
            return None
        template = self._settings.get(["templates", event_key, field])
        if not isinstance(template, str) or not template.strip():
            return None
        return self._render_template(
            template,
            context,
            preview=preview,
            origin=origin,
            path=path,
            sample_meta=sample_meta,
        )

    def _event_has_custom_template(self, event_key):
        for field in ("subject", "body"):
            value = self._settings.get(["templates", event_key, field])
            if isinstance(value, str) and value.strip():
                return True
        return False

    def _template_origin_and_path(self, extras):
        origin = extras.get("origin")
        path = extras.get("path")
        if not origin or not path:
            job_file = self._current_job_file()
            origin = origin or job_file.get("origin")
            path = path or job_file.get("path")
        return origin, path

    def _get_template_context(self, title, body, extras=None):
        extras = extras or {}
        progress = self._safe_current_data().get("progress") or {}
        if not isinstance(progress, dict):
            progress = {}
        job_file = self._current_job_file()
        file_name = extras.get("file") or job_file.get("name") or self.current_path or ""
        percent = extras.get("percent")
        if percent is None:
            percent = progress.get("completion")
        try:
            percent_value = str(int(float(percent))) if percent is not None else ""
        except (TypeError, ValueError):
            percent_value = ""
        elapsed = extras.get("elapsed")
        if elapsed is None:
            elapsed = progress.get("printTime")
        remaining = extras.get("remaining")
        if remaining is None:
            remaining = progress.get("printTimeLeft")
        return {
            "event": title or "",
            "message": body or "",
            "printer": self.get_printer_name(),
            "file": os.path.basename(file_name) if file_name else "",
            "percent": percent_value,
            "elapsed": self._format_duration(elapsed),
            "remaining": self._format_duration(remaining),
            "eta": self._format_clock(remaining),
            "tool_temp": self._tool_temperatures(),
            "bed_temp": self._bed_temperature(),
            "state": self._printer_state_text(),
            "error": extras.get("error") or "",
            "user": extras.get("user") or "",
            "reason": extras.get("reason") or "",
        }

    def _get_event_samples(self):
        def sample(
            event,
            message,
            file="benchy.gcode",
            percent="50",
            elapsed="1:15:00",
            remaining="1:15:00",
            eta="3:45 PM",
            state="Printing",
            error="",
            user="",
            reason="",
        ):
            return {
                "event": event,
                "message": message,
                "printer": self.get_printer_name(),
                "file": file,
                "percent": percent,
                "elapsed": elapsed,
                "remaining": remaining,
                "eta": eta,
                "tool_temp": "T0 210°C / 210°C, T1 205°C / 205°C",
                "bed_temp": "60°C / 60°C",
                "state": state,
                "error": error,
                "user": user,
                "reason": reason,
            }

        return {
            "print_started": sample(
                "Print job started",
                "benchy.gcode has started printing locally.",
                percent="0",
                elapsed="0:00:00",
            ),
            "print_done": sample(
                "Print job finished",
                "benchy.gcode \n\rfinished printing, elapsed time: 2:30:00.",
                percent="100",
                elapsed="2:30:00",
                remaining="0:00:00",
                eta="",
            ),
            "print_progress": sample(
                "Print Progress 50% finished.",
                "benchy.gcode",
            ),
            "print_paused": sample(
                "Print Paused by admin at 2026-11-24 10:30",
                "file: benchy.gcode",
                state="Paused",
                user="admin",
            ),
            "print_resumed": sample(
                "Resumed by admin at 2026-11-24 10:35",
                "file: benchy.gcode",
                elapsed="1:20:00",
                remaining="1:10:00",
                user="admin",
            ),
            "print_cancelled": sample(
                "Print canceled by admin",
                "file: benchy.gcode",
                state="Cancelling",
                user="admin",
            ),
            "print_failed": sample(
                "Print Fail after 120 seconds",
                "error file: benchy.gcode",
                percent="2",
                elapsed="0:02:00",
                remaining="",
                eta="",
                state="Operational",
                reason="error",
            ),
            "upload": sample(
                "A file was uploaded ",
                "benchy.gcode was uploaded locally",
                percent="",
                elapsed="",
                remaining="",
                eta="",
                state="Operational",
            ),
            "error": sample(
                "Printer ERROR!",
                " Thermal runaway",
                percent="",
                elapsed="",
                remaining="",
                eta="",
                state="Error",
                error="Thermal runaway",
            ),
        }

    def _render_event_sample(self, event_key):
        sample = self._get_event_samples()[event_key]
        context = dict(sample)
        subject = self._resolve_template(
            event_key,
            "subject",
            context,
            preview=True,
            sample_meta=_SAMPLE_META_VALUES,
        )
        body = self._resolve_template(
            event_key,
            "body",
            context,
            preview=True,
            sample_meta=_SAMPLE_META_VALUES,
        )
        if subject is None or not subject.strip():
            subject = sample["event"]
        used_custom_body = body is not None
        if not used_custom_body:
            body = sample["message"]
        return subject, body, used_custom_body

    def _prepare_email_message_and_send(
        self,
        title,
        body,
        sender=None,
        thumbnail=None,
        send_image=True,
        direct_send=False,
        event_key=None,
        template_extras=None,
        append_sender=True,
    ):
        """
        Prepare the email for sending and put it into the message queue or the email is directly sent

        :param title: Email title
        :param body: body of email
        :param sender: email of sender
        :param thumbnail: thumbnail image path
        :param send_image: boolean True for thumbnail
        :param direct_send: boolean True for preformatted email
        :param event_key: notification event key used to look up custom templates
        :param template_extras: per-event values exposed to the template renderer
        :return: SNAP - a failure to get an image from the webcam
        FILE_E - filesystem error
        True - no error
        _send_file results
        SMTP_E - for errors setting up the SMTP connection
        LOGIN_E - for errors logging into the host email account
        SENDM_E - error sending email from server
        True - no error
        """
        self._logger.debug("Preparing EMail '%s' and adding to Notification-Queue", title)
        self._logger.debug(
            "Enable webcam setting %s", self._settings.get(["en_webcam"])
        )

        result = True
        delete_image_after_sent = False
        # collect all data for an email
        if sender is None:
            sender = "OctoText"

        image_path = ""
        if thumbnail is not None:
            # prepare email with thumbnail
            image_path = thumbnail
            pass
        elif self._settings.get(["en_webcam"]) is False or send_image is False:
            image_path = ""
            pass
        else:
            image_path_dict = (
                self._create_image_path_from_snapshot() if send_image else None
            )
            if image_path_dict:
                image_path = image_path_dict["path"]
                result = image_path_dict["result"]
                if result == "DELETE_IMAGE_AFTER_SENT":
                    delete_image_after_sent = True
            pass

        appearance_name_prefix = ""
        if self._settings.get(["include_appearance_prefix"]):
            appearance_name = self.get_printer_name()
            appearance_name_prefix = appearance_name + ": "
            self._logger.debug("Appearance name (subject): %s", appearance_name)

        if body is None:
            body = ""

        subject_text = title
        body_text = body
        used_custom_body = False
        if (
            event_key
            and self._settings.get(["en_custom_templates"])
            and self._event_has_custom_template(event_key)
        ):
            extras = template_extras or {}
            context = self._get_template_context(title, body_text, extras)
            origin, path = self._template_origin_and_path(extras)
            custom_subject = self._resolve_template(
                event_key, "subject", context, origin=origin, path=path
            )
            if custom_subject is not None and custom_subject.strip():
                subject_text = custom_subject
            custom_body = self._resolve_template(
                event_key, "body", context, origin=origin, path=path
            )
            if custom_body is not None:
                body_text = custom_body
                used_custom_body = True

        fromAddr = self._get_from_address()

        phone_numb = self._settings.get(["phone_numb"])
        carrier_addr = self._settings.get(["carrier_address"])
        email_addr = f"{phone_numb}@{carrier_addr}"

        # setup email message with all collected data
        email_message = EmailMessage()

        cc_set = self._settings.get(["cc_field"])
        if cc_set is not None:
            cc_set = cc_set.replace("\n", "")
            cc_set = cc_set.replace(" ", "")
            cc_set = cc_set.split(",")
            self._logger.debug("Cc: settings - %s", cc_set)
            email_message["Cc"] = cc_set

        subject = (
            (self._settings.get(["subject_prefix"]) or "")
            + appearance_name_prefix
            + subject_text
        )
        # Header values may not contain line breaks; {message} and third-party
        # metadata can, so collapse them instead of losing the notification.
        email_message["Subject"] = re.sub(r"[\r\n]+", " ", subject)
        email_message["From"] = fromAddr  # 'OctoText@outlook.com'
        email_message["To"] = email_addr
        email_message["Date"] = formatdate(localtime=True)
        final_body = (
            body_text
            if (used_custom_body or not append_sender)
            else body_text + " Message sent from: " + sender
        )
        email_message.set_content(
            final_body, charset="utf-8"
        )  # utf-8 allows non ascii characters in the test string
        if image_path != "":
            try:
                fp = open(image_path, "rb")
                filename = datetime.datetime.now().isoformat(timespec="minutes") + ".jpg"
                email_message.add_attachment(
                    fp.read(), maintype="image", subtype="jpg", filename=filename
                )
                fp.close()
            except Exception as e:
                self._logger.exception("Exception while opening file for snapshot, %s", e)
            if delete_image_after_sent:
                try:
                    os.remove(image_path)
                except Exception as e:
                    self._logger.exception(
                        "Could not remove temporary snapshot file %s e:%s", image_path, e
                    )
                    # just logging, sending without image attached: return "FILE_E"

        if direct_send:
            result = self._send_email_message(email_message)
        else:
            self.notifyQ.put(email_message)
        return result

    def _get_snapshot_source(self):
        if get_snapshot_webcam is None:  # OctoPrint < 1.9.0
            snapshot_url = self._settings.global_get(["webcam", "snapshot"])
            if not snapshot_url:
                return None

            def take_snapshot():
                response = requests.get(
                    snapshot_url, verify=False, stream=True, timeout=5
                )
                response.raise_for_status()
                return response.iter_content(chunk_size=1024)

            return (
                take_snapshot,
                self._settings.global_get_boolean(["webcam", "flipH"]),
                self._settings.global_get_boolean(["webcam", "flipV"]),
                self._settings.global_get_boolean(["webcam", "rotate90"]),
            )

        webcam = get_snapshot_webcam()
        if webcam is None or not webcam.config.canSnapshot:
            return None

        return (
            lambda: webcam.providerPlugin.take_webcam_snapshot(webcam.config.name),
            webcam.config.flipH,
            webcam.config.flipV,
            webcam.config.rotate90,
        )

    # load the snapshot image from camera, rotate and store the image into the filesystem. return the image path
    # location return dict( path:thePath, result:"SNAP")
    def _create_image_path_from_snapshot(self):
        try:
            snapshot_source = self._get_snapshot_source()
            if snapshot_source is None:
                return None

            take_snapshot, hflip, vflip, rotate = snapshot_source

            # reading webcam snapshot image
            tempFile = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            for chunk in take_snapshot():
                tempFile.write(chunk)
            tempFile.close()

            self._logger.debug("Webcam tempfile %s", tempFile.name)

            # flip or rotate as needed
            self._process_snapshot(tempFile.name, hflip, vflip, rotate)

            return {"path": tempFile.name, "result": "DELETE_IMAGE_AFTER_SENT"}
        except Exception as e:
            self._logger.exception("Exception while fetching snapshot from webcam: %s", e)
            # send message without webcam snapshot (enabled but not available)
            path = self._basefolder + "/static/img/offline.jpg"
            return {"path": path, "result": "SNAP"}

    # Send the email to the smtp-server
    def _send_email_message(self, email_message):
        # login to the SMTP account and mail server
        error, email_addr = self.smtp_login_server()

        if error is not None:
            return error

        try:
            SMTP_server.send_message(email_message)
            # SMTP_server.sendmail(
            #     email_message["From"], email_message["To"], email_message.as_string()
            # )
            SMTP_server.quit()
        except Exception as e:
            self._logger.exception(
                "Exception while logging into SMTP server(send_email_message) %s", e
            )
            return "SENDM_E"
        return True

    # this code will rotate or flip the image based on the webcam settings
    def _process_snapshot(self, snapshot_path, hflip, vflip, rotate):
        if not hflip and not vflip and not rotate:
            return

        if Image is None:
            self._logger.warning("Pillow is not installed, sending the snapshot unprocessed")
            return

        try:
            with Image.open(snapshot_path) as image:
                processed = image
                if hflip:
                    processed = ImageOps.mirror(processed)
                if vflip:
                    processed = ImageOps.flip(processed)
                if rotate:
                    processed = processed.rotate(90, expand=True)

                if processed.mode not in ("L", "RGB"):
                    processed = processed.convert("RGB")

                processed.save(snapshot_path)
        except Exception:
            self._logger.exception(
                "Could not rotate/flip the snapshot, sending it unprocessed"
            )

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {"test": []}  # dictionary of acceptable commands

    # Called by OctoPrint upon a POST request to /api/plugin/<plugin identifier>.
    # command will contain one of the commands as specified via get_api_commands(),
    # data will contain the full request body parsed from JSON into a Python dictionary.
    #
    # format of post request from the frontend:
    # OctoPrint.simpleApiCommand("OctoText", "test", {});
    def on_api_command(self, command, data):
        self._logger.debug("Got an API command: %s, data: %s", command, data)

        if command == "test":
            self._logger.debug("The test button was pressed...")

            event_key = data.get("event") if isinstance(data, dict) else None
            if event_key not in _TEMPLATE_EVENT_KEYS:
                event_key = None
            append_sender = True
            if (
                event_key
                and self._settings.get(["en_custom_templates"])
                and self._event_has_custom_template(event_key)
            ):
                title, body, used_custom_body = self._render_event_sample(event_key)
                append_sender = not used_custom_body
                sender = self.get_printer_name()
                self._logger.debug(
                    "Sending text using the %s template", event_key
                )
            else:
                title = "Test from the OctoText Plugin."
                body = self._settings.get(["smtp_message"])
                sender = "OctoText"
                self._logger.debug("Sending text with image")

            try:
                result = self._prepare_email_message_and_send(
                    title,
                    body,
                    sender=sender,
                    direct_send=True,
                    append_sender=append_sender,
                )
            except Exception as e:
                self._logger.exception("Exception while sending text, %s", e)
                return flask.make_response(flask.jsonify(result=False, error="SMTP_E"))

            self._logger.debug("String returned from send_message_with_webcam %s", result)
            if result is not True:
                error = result
                result = False
            else:
                error = None

            payload = {"result": result, "error": error}
            if error == "LOGIN_E":
                payload["login"] = (
                    (self._settings.get(["username"]) or "")
                    if self._settings.get(["validate_username"])
                    else self._format_address(
                        self._settings.get(["username"]),
                        self._settings.get(["servername"]),
                    )
                )

            return flask.make_response(flask.jsonify(**payload))

    def receive_api_command(self, command, data, permissions=None):
        """
        Api messages from other plugins are received on this callback. A properly formatted EmailMessage is expected
        as a data parameter.

        :param command: "OctoText" string
        :param data: EmailMessage()
        :param permissions: None
        :return: True/False
        """

        if command != self._identifier:
            return

        self._logger.debug("received a message command: %s", command)

        # TODO check the data before we put it on the queue
        # there is no way to notify the caller that there was an error so just log the
        # issues
        # subject and to are required!

        email_message = data

        if email_message["From"] is None:
            email_message["From"] = self._get_from_address()

        if email_message["To"] is None:
            email_message["To"] = self._format_address(
                self._settings.get(["phone_numb"]),
                self._settings.get(["carrier_address"]),
            )

        subject = email_message["Subject"]
        if subject is None:
            return False
        # put it into the notify queue
        self.notifyQ.put(email_message)

        return True

    # testing logging and proper startup of passed values in settings forms
    def on_after_startup(self):

        self._logger.info("--------------------------------------------")
        self._logger.info("OctoText started: %s", self._plugin_version)
        self._logger.info(
            "SMTP Name: %s, SMTP port: %s, SMTP message: %s, server login: %s",
            self._settings.get(["smtp_name"]),
            self._settings.get(["smtp_port"]),
            self._settings.get(["smtp_message"]),
            self._format_address(
                self._settings.get(["username"]),
                self._settings.get(["servername"]),
            ),
        )
        # on loading of plugin look for the existence of the prusa or cura thumbnail plugins

        self.prusa_folder = self.get_plugin_data_folder().replace(
            "OctoText", "prusaslicerthumbnails"
        )
        self.cura_folder = self.get_plugin_data_folder().replace(
            "OctoText", "UltimakerFormatPackage"
        )
        if os.path.exists(self.prusa_folder):
            self._logger.info("Prusa thumbnail loaded: %s", self.prusa_folder)
        if os.path.exists(self.cura_folder):
            self._logger.info("Cura thumbnails loaded: %s", self.cura_folder)
        self._logger.info("--------------------------------------------")
        Thread(target=self.email_message_queue_worker, daemon=True).start()

        # TODO Not needed,  only helper-function allowed: self._plugin_manager.register_message_receiver(self.receive_api_command)

    # ~~ callback for pause initiated by the printer (very specific to Prusa)
    # to test the strings being received by the Pi put this in the console: !!DEBUG:send echo:busy: paused for user

    def AlertWaitingForUser(self, comm, line, *args, **kwargs):
        if self.last_fired is not None:
            right_now = datetime.datetime.now()
            how_long = right_now - self.last_fired
            # self._logger.debug(
            #    f"last fired {last_fired}, right_now {right_now} seconds {how_long.seconds}"
            # )
            mmutimeout = int(self._settings.get(["mmu_timeout"]))
            # any setting less than 30 seconds disables the check
            if mmutimeout < 30:
                return line
            if how_long.seconds < mmutimeout:
                return line
        if "echo:busy: paused for user" in line:
            self._logger.info("State ID: %s", self._printer.get_state_id())
            if self._printer.get_state_id() == "PRINTING":
                self.last_fired = datetime.datetime.now()
                payload = dict([("name", "printer"), ("user", "system")])
                self.on_event(octoprint.events.Events.PRINT_PAUSED, payload)
        return line

    def find_thumbnail(self, filename):

        thumb_filename = None
        prusa_thumb_filename = (
            self.prusa_folder + "/" + filename.replace(".gcode", ".png")
        )
        cura_thumb_filename = self.cura_folder + "/" + filename.replace(".gcode", ".png")

        if os.path.exists(prusa_thumb_filename):
            thumb_filename = prusa_thumb_filename
        elif os.path.exists(cura_thumb_filename):
            thumb_filename = cura_thumb_filename
        self._logger.debug("thumbnail filename path is: %s", thumb_filename)
        if thumb_filename is not None and os.path.exists(thumb_filename):
            self._logger.debug("thumbnail exists! using image in notifications")
        return thumb_filename

    # ~~ thread for time based notification
    # setting the flag stopme will cause the thread to exit
    def _uses_progress_thread(self):
        return bool(
            self._settings.get(["en_progress_time"])
            or self._settings.get(["en_progress_fixed_time"])
        )

    def time_thread(self, stopme):
        if self._settings.get(["en_progress_fixed_time"]):
            minutes = int(self._settings.get(["progress_fixed_minutes"]) or 60)
            interval = max(60, minutes * 60)
            self._logger.debug("Fixed progress interval: %s seconds", interval)
            while not stopme.is_set():
                if stopme.wait(interval):
                    return
                progr = self._printer.get_current_data()["progress"]
                pt_current = progr.get("printTimeLeft")
                printer_name = self.get_printer_name()
                if pt_current is not None and pt_current > 0:
                    time_left = datetime.timedelta(seconds=int(pt_current))
                    title = "Print Progress " + str(time_left) + " time to finish."
                else:
                    title = "Print Progress update."
                description = self.current_path
                self._prepare_email_message_and_send(
                    title,
                    description,
                    printer_name,
                    None,
                    self._settings.get(["en_webcam"]),
                    event_key="print_progress",
                    template_extras={"file": self.current_path},
                )
            return

        ptl = None
        while ptl is None:
            if stopme.wait(1):
                return
            progr = self._printer.get_current_data()["progress"]
            ptl = progr.get("printTimeLeft")
        self._logger.debug("progress: %s", ptl)
        while not stopme.is_set():
            progr = self._printer.get_current_data()["progress"]
            print_time = progr.get("printTime") or 0
            print_time_left = progr.get("printTimeLeft")
            if print_time_left is None or print_time_left <= 0:
                self._logger.debug("Exiting time thread!")
                return
            total_time = print_time + print_time_left
            interval = (
                int(self._settings.get(["progress_interval"])) / 100 * int(total_time)
            )
            interval = max(1, int(interval))
            self._logger.debug("interval: %s", interval)
            if stopme.wait(interval):
                return

            # send the message to the queue
            progr = self._printer.get_current_data()["progress"]
            pt_current = progr.get("printTimeLeft")
            if pt_current is None or pt_current <= 0 or stopme.is_set():
                self._logger.debug("Exiting time thread!")
                return
            # progress = int((pt_current / ptl) * 100)
            time_left = datetime.timedelta(seconds=int(pt_current))
            self._logger.debug("Print time left %s", time_left)
            printer_name = self.get_printer_name()
            # title = "Print Progress " + str(progress) + " percent left."
            title = "Print Progress " + str(time_left) + " time to finish."
            description = self.current_path

            self._prepare_email_message_and_send(
                title,
                description,
                printer_name,
                None,
                self._settings.get(["en_webcam"]),
                event_key="print_progress",
                template_extras={"file": self.current_path},
            )
        return

    # ~~ Simple function to start and stop the time thread
    stopme = threading.Event()

    def manage_progress_thread(self, stop=False):
        self.stopme.set()
        if stop:
            return
        self.stopme = threading.Event()
        Thread(target=self.time_thread, daemon=True, args=(self.stopme,)).start()
        return

    # ~~ EventPlugin API

    def on_event(self, event, payload):

        noteType = title = description = None
        do_cam_snapshot = True
        thumbnail_filename = None
        event_key = None
        template_extras = {}

        if event == octoprint.events.Events.UPLOAD:

            if not self._settings.get(["en_upload"]):
                return

            file = payload["name"]
            target = payload["path"]
            path_to_thumbnail = self.find_thumbnail(file)
            self._logger.debug("Upload event - thumbnail filename %s", path_to_thumbnail)
            noteType = True
            title = "A file was uploaded "
            description = "{file} was uploaded {targetString}".format(
                file=file, targetString="to SD" if target == "sd" else "locally"
            )
            do_cam_snapshot = False  # don't really want a snapshot for this
            event_key = "upload"
            template_extras = {
                "file": file,
                "origin": payload.get("origin"),
                "path": payload.get("path"),
            }

        elif event == octoprint.events.Events.PRINT_STARTED:

            self._is_printing = True
            self._last_progress_sent = None
            # Start time-based progress thread regardless of whether print-start email is enabled
            if self._settings.get(["en_progress"]) and self._uses_progress_thread():
                self.manage_progress_thread()

            if not self._settings.get(["en_printstart"]):
                return

            self._logger.debug("Print started event: %s", payload)
            file = os.path.basename(payload["name"])
            origin = payload["origin"]

            noteType = True
            title = "Print job started"
            description = "{file} has started printing {originString}.".format(
                file=file, originString="from SD" if origin == "sd" else "locally"
            )
            thumbnail_filename = self.find_thumbnail(file)
            event_key = "print_started"
            template_extras = {
                "file": file,
                "percent": 0,
                "origin": origin,
                "path": payload.get("path") or file,
            }

        elif event == octoprint.events.Events.PRINT_DONE:

            self._is_printing = False
            if self._uses_progress_thread():
                self.manage_progress_thread(stop=True)

            if not self._settings.get(["en_printend"]):
                return

            file = os.path.basename(payload["name"])
            elapsed_time = datetime.timedelta(seconds=int(payload["time"]))

            self._logger.debug("Event received: %s, print done: %s", event, file)
            noteType = True
            title = "Print job finished"
            description = (
                f"{file} \n\rfinished printing, elapsed time: {elapsed_time}."
            )
            event_key = "print_done"
            template_extras = {
                "file": file,
                "percent": 100,
                "remaining": 0,
                "elapsed": payload["time"],
                "origin": payload.get("origin"),
                "path": payload.get("path") or file,
            }

        elif event == octoprint.events.Events.ERROR:

            was_printing = (
                self._is_printing
                or self._printer.is_printing()
                or self._printer.is_paused()
            )
            self._is_printing = False
            if self._uses_progress_thread():
                self.manage_progress_thread(stop=True)

            if not self._settings.get(["en_error"]):
                return

            if self._settings.get(["en_error_printing_only"]) and not was_printing:
                self._logger.debug(
                    "Ignoring printer error while not printing: %s",
                    payload.get("error"),
                )
                return

            error = payload["error"]

            noteType = True
            title = "Printer ERROR!"
            description = f" {error}"
            event_key = "error"
            template_extras = {
                "error": error,
                "origin": payload.get("origin"),
                "path": payload.get("path"),
            }
            self._logger.debug("Event received: %s, print error: %s", event, error)

        elif event == octoprint.events.Events.PRINT_CANCELLED:

            self._is_printing = False
            if self._uses_progress_thread():
                self.manage_progress_thread(stop=True)

            if not self._settings.get(["show_fail_cancel"]):
                return
            if self._settings.get(["en_printfail"]) == "Fail":
                return

            settingf = self._settings.get(["en_printfail"])
            self._logger.debug("Event received: %s, print fail: %s", event, settingf)
            name = payload["name"]
            try:
                user = payload["user"]
            except Exception:
                user = "system"

            noteType = True
            title = "Print canceled by " + user
            description = f"file: {name}"
            event_key = "print_cancelled"
            template_extras = {
                "file": os.path.basename(name),
                "user": user,
                "origin": payload.get("origin"),
                "path": payload.get("path") or name,
            }

        elif event == octoprint.events.Events.PRINT_FAILED:

            self._is_printing = False
            if self._uses_progress_thread():
                self.manage_progress_thread(stop=True)

            if not self._settings.get(["show_fail_cancel"]):
                return
            if self._settings.get(["en_printfail"]) == "Cancel":
                return

            settingf = self._settings.get(["en_printfail"])
            self._logger.debug("Event received: %s, print fail: %s", event, settingf)
            reason = payload["reason"]
            name = payload["name"]
            time = payload["time"]
            time = str(int(time))  # time is a float

            noteType = True
            title = "Print Fail after " + time + " seconds"
            description = f"{reason} file: {name}"
            event_key = "print_failed"
            template_extras = {
                "file": os.path.basename(name),
                "reason": reason,
                "elapsed": payload.get("time"),
                "origin": payload.get("origin"),
                "path": payload.get("path") or name,
            }

        elif event == octoprint.events.Events.PRINT_PAUSED:

            if not self._settings.get(["en_printpaused"]):
                return

            pay_name = payload["name"]
            # when a plugin initiates a pause, there is no user and an exception is thrown
            # when accessing the keyword
            try:
                user = payload["user"]
            except Exception:
                try:
                    user = payload["owner"]
                except Exception:
                    user = "System"

            time = datetime.datetime.now().isoformat(sep=" ", timespec="minutes")

            noteType = True
            title = "Print Paused by " + user + " at " + time
            if pay_name == "printer":
                description = "Pause for user detected. Out of Filament?"
            else:
                description = f"file: {pay_name}"
            event_key = "print_paused"
            template_extras = {
                "file": "" if pay_name == "printer" else os.path.basename(pay_name),
                "user": user,
                "origin": payload.get("origin"),
                "path": ""
                if pay_name == "printer"
                else (payload.get("path") or pay_name),
            }
            self._logger.debug(
                "Print paused args notetype: %s, name:%s, title %s, description %s",
                noteType,
                pay_name,
                title,
                description,
            )

        elif event == octoprint.events.Events.PRINT_RESUMED:

            if not self._settings.get(["en_printresumed"]):
                return

            pay_name = payload["name"]
            try:
                user = payload["user"]
            except Exception:
                try:
                    user = payload["owner"]
                except Exception:
                    user = "System"

            time = datetime.datetime.now().isoformat(sep=" ", timespec="minutes")

            noteType = True
            title = "Resumed by " + user + " at " + time
            description = f"file: {pay_name}"
            event_key = "print_resumed"
            template_extras = {
                "file": "" if pay_name == "printer" else os.path.basename(pay_name),
                "user": user,
                "origin": payload.get("origin"),
                "path": ""
                if pay_name == "printer"
                else (payload.get("path") or pay_name),
            }
            self._logger.debug(
                "Print resumed args notetype: %s, name:%s, title %s, description %s",
                noteType,
                pay_name,
                title,
                description,
            )

        if noteType is None:
            return

        printer_name = self.get_printer_name()

        self._prepare_email_message_and_send(
            title,
            description,
            printer_name,
            thumbnail_filename,
            do_cam_snapshot,
            event_key=event_key,
            template_extras=template_extras,
        )

    ##~~ Softwareupdate hook

    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return {
            "OctoText": {
                "displayName": "OctoText",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "OctoText",
                "repo": "Octotext",
                "current": self._plugin_version,
                "stable_branch": {
                    "name": "Stable",
                    "branch": "main",
                    "comittish": ["main"],
                },
                "prerelease_branches": [
                    {
                        "name": "Release Candidate",
                        "branch": "rc",
                        "comittish": ["rc", "main"],
                    }
                ],
                "pip": "https://github.com/OctoText/OctoText/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "OctoText"

__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = OctoTextPlugin()

    # https://docs.octoprint.org/en/master/plugins/helpers.html#helpers
    global __plugin_helpers__
    __plugin_helpers__ = dict(send_email=__plugin_implementation__.receive_api_command)

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.AlertWaitingForUser,
    }

