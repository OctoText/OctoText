# This is the release candidate branch - changes to this version include:
# updates to the notifications reported:
# 	print paused, resumed
# 	time base notifications instead of % based
# 	more robust sending of notifications if the network fails temporarily (a queue?)
# 	make the icon in the status bar go away, and smaller when it is there
# 	insert the printer name into the reporting
#
# 	need a way of making sure asynchronous events execute serially through the mail queue
# 		maybe combine multiple events?
import datetime
import os
import smtplib
from email.message import EmailMessage

import flask
import octoprint.events
import octoprint.plugin
import sarge
from flask_login import current_user

# from octoprint.printer.estimation import PrintTimeEstimator


class OctoTextPlugin(
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.ProgressPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.TemplatePlugin,
):

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return dict(
            smtp_port=587,  # most default to this
            smtp_name="smtp.office365.com",  # mail server name
            smtp_alert="*ALERT from your PRINTER*",
            smtp_message="Your printer is creating something wonderful!",  # the message to send
            server_login="YourEmail@outlook.com",  # mail account to use
            server_pass="not a valid password",  # password for that account
            phone_numb="8675309",  # sorry jenny!
            carrier_address="mypixmessages.com",
            push_message=None,
            progress_interval=10,  # should we limit this to a reasonable number?
            en_progress=False,
            en_webcam=True,
            en_printstart=True,
            en_printend=True,
            en_upload=True,
            en_error=True,
            en_printfail=True,
            en_printpaused=True,
            en_printresumed=False,
            show_navbar_button=True,
        )

    def get_api_commands(self):
        return dict(test=["token"])

    # ~~ PrintProgressPlugin

    def on_print_progress(self, storage, path, progress):
        if not self._settings.get(["en_progress"]):
            return

        if progress == 0:
            return

        # if these two events fire at the same time (printend and progress at 100%) we have two threads that are async
        # to each other that try to send notifications at the same time. This has caused both of these threads to fail
        # on a Pi 4 (not so much on a fast laptop). We default to letting the printend message do the work
        if progress == 100 and self._settings.get(["en_printend"]):
            return

        if progress % int(self._settings.get(["progress_interval"])) == 0:
            printer_name = self._settings.global_get(["appearance", "name"])
            title = "Print Progress"
            description = str(progress) + " percent finished"
            noteType = "Status from: " + printer_name
            if self._settings.get(["en_webcam"]):
                self._send_message_with_webcam_image(
                    title, description, sender=printer_name
                )
            else:
                self.smtp_send_message(noteType, title, description)

    ##~~ AssetPlugin mixin

    def get_assets(self):
        # Define your plugin's asset files to automatically include in the
        # core UI here.
        return dict(js=["js/OctoText.js"])

    def get_template_configs(self):
        return [
            dict(type="navbar", name="OctoText", custom_bindings=True),
            dict(type="settings", name="OctoText", custom_bindings=True),
        ]

    # access restrictions for sensitive data
    def on_settings_load(self):
        data = octoprint.plugin.SettingsPlugin.on_settings_load(self)

        # only return our restricted settings to admin users - this is only needed for OctoPrint <= 1.2.16
        restricted = ("server_pass", "server_login")
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
        return dict(admin=[["server_pass"], ["server_login"]])

    def on_settings_save(self, data):

        if "server_pass" in data and not data["server_pass"]:
            data["server_pass"] = None

        if "server_login" in data and not data["server_login"]:
            data["server_login"] = None

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

    # Login to the mail server
    def smtp_login_server(self):
        global SMTP_server
        name = self._settings.get(["smtp_name"])
        port = self._settings.get(["smtp_port"])
        # message = self._settings.get(["smtp_message"])
        login = self._settings.get(["server_login"])
        passw = self._settings.get(["server_pass"])
        phone_numb = self._settings.get(["phone_numb"])
        carrier_addr = self._settings.get(["carrier_address"])
        # alert = self._settings.get(["smtp_alert"])
        self._logger.info(name)
        self._logger.info(port)

        # setup the server with the SMTP address/port
        try:
            self._logger.info("before server smtplib")
            SMTP_server = smtplib.SMTP(name, port)
            self._logger.info(f"SMTP_server {SMTP_server}")
            error = SMTP_server.starttls()
            self._logger.info(f"startttls() {error}")
        except Exception as e:
            self._logger.exception(
                "Exception while talking to your mail server {message}".format(
                    message=str(e)
                )
            )
            return ["SMTP", None]

        # login to the mail account
        self._logger.info(login)
        # self._logger.info(passw) # not very secure putting this in the logs
        try:
            SMTP_server.login(login, passw)
        except Exception as e:
            self._logger.exception(
                "Exception while logging into mail server {message}".format(
                    message=str(e)
                )
            )
            SMTP_server.quit()
            return ["LOGIN_E", None]

        email_addr = phone_numb + "@%s" % carrier_addr
        return [None, email_addr]

    # initializes the SMTP service, logs into the email server and sends the message to the destination address
    def smtp_send_message(self, subject, title, description):

        # login to the SMTP account and mail server
        error, email_addr = self.smtp_login_server()
        login = self._settings.get(["server_login"])

        if not (error is None):
            return error

        # Send text message through SMS gateway of destination number
        # format the message like an email
        self._logger.info(email_addr)
        message = (
            "From: %s\r\n" % login
            + "To: %s" % email_addr
            + "\r\n"
            + "Subject: %s\r\n" % subject
            + "\r\n"
            + title
            + "\r\n"
            + description
        )

        self._logger.info(f"Notetype: {subject}")
        self._logger.info(f"Message: {message}")

        try:
            SMTP_server.sendmail(login, email_addr, message)
            SMTP_server.quit()
        except Exception as e:
            self._logger.exception(
                "Exception while logging into SMTP server {message}".format(
                    message=str(e)
                )
            )
            return "SENDM_E"
        return True

    # send an image with the message. have to watch for errors connecting to the camera
    def _send_message_with_webcam_image(self, title, body, filename=None, sender=None):

        self._logger.info(
            "Enable webcam setting {}".format(self._settings.get(["en_webcam"]))
        )
        if not self._settings.get(["en_webcam"]):
            return self.smtp_send_message(sender, title, body)

        if filename is None:
            import random
            import string

            filename = "test-{}.jpg".format(
                "".join([random.choice(string.ascii_letters) for _ in range(16)])
            )

        if sender is None:
            sender = "OctoText"

        snapshot_url = self._settings.global_get(["webcam", "snapshot"])
        webcam_stream_url = self._settings.global_get(["webcam", "stream"])
        result = True
        self._logger.info(f"filename for image: {filename}")
        self._logger.info(
            f"Webcam URL is: {webcam_stream_url}, Snapshot URL is: {snapshot_url}"
        )
        if snapshot_url:
            try:
                import tempfile

                from requests import get

                tempFile = tempfile.NamedTemporaryFile(delete=False)
                response = get(snapshot_url, verify=True)  # False
                response.raise_for_status()
                tempFile.write(response.content)
                tempFile.close()
            except Exception as e:
                self._logger.exception(
                    "Exception while fetching snapshot from webcam: {message}".format(
                        message=str(e)
                    )
                )
                return "SNAP"
            else:
                # ffmpeg can't guess file type it seems
                os.rename(tempFile.name, tempFile.name + ".jpg")
                tempFile.name += ".jpg"

                self._logger.info(f"Webcam tempfile {tempFile.name}")
                self._process_snapshot(tempFile.name)
                result = self._send_file(sender, tempFile.name, title, body)
                if result is True:
                    try:
                        os.remove(tempFile.name)
                    except Exception as e:
                        self._logger.exception(
                            "Could not remove temporary snapshot file {} e:{}".format(
                                tempFile.name, str(e)
                            )
                        )
                        return False

        return result

    # format the MMS message - both text and image.
    def _send_file(self, sender, path, title, body):

        # login to the SMTP account and mail server
        error, email_addr = self.smtp_login_server()

        if not (error is None):
            return error

        if body is None:
            body = self._settings.global_get(["appearance", "name"])

        self._logger.info(f"Appearance name (subject): {body}")

        login = self._settings.get(["server_login"])
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = login  # 'OctoText@outlook.com'
        msg["To"] = email_addr
        msg.preamble = "You will not see this in a MIME-aware mail reader.\n"
        content_string = " Message sent from: " + self._settings.global_get(
            ["appearance", "name"]
        )
        msg.set_content(body + content_string)
        # msg.set_content("""\
        # 	Message sent from OctoText!""")

        self._logger.info(f"path for image: {path}")

        if path != "":
            try:
                fp = open(path, "rb")
            except Exception as e:
                self._logger.exception(
                    "Exception while opening file for snapshot, {message}".format(
                        message=str(e)
                    )
                )

            filename = datetime.datetime.now().isoformat(timespec="minutes") + ".jpg"
            msg.add_attachment(
                fp.read(), maintype="image", subtype="jpg", filename=filename
            )

            fp.close()

        # Send text message through SMS gateway of destination number/address
        try:
            SMTP_server.sendmail(login, email_addr, msg.as_string())
            SMTP_server.quit()
        except Exception as e:
            self._logger.exception(
                "Exception while logging into SMTP server(send_file) {message}".format(
                    message=str(e)
                )
            )
            return "SENDM_E"
        return True

    # this code will rotate or flip the image based on the webcam settings. borrowed from foosel
    def _process_snapshot(self, snapshot_path, pixfmt="yuv420p"):
        hflip = self._settings.global_get_boolean(["webcam", "flipH"])
        vflip = self._settings.global_get_boolean(["webcam", "flipV"])
        rotate = self._settings.global_get_boolean(["webcam", "rotate90"])
        ffmpeg = self._settings.global_get(["webcam", "ffmpeg"])

        if (
            not ffmpeg
            or not os.access(ffmpeg, os.X_OK)
            or (not vflip and not hflip and not rotate)
        ):
            return

        ffmpeg_command = [ffmpeg, "-y", "-i", snapshot_path]

        rotate_params = [f"format={pixfmt}"]  # workaround for foosel/OctoPrint#1317
        if rotate:
            rotate_params.append("transpose=2")  # 90 degrees counter clockwise
        if hflip:
            rotate_params.append("hflip")  # horizontal flip
        if vflip:
            rotate_params.append("vflip")  # vertical flip

        ffmpeg_command += [
            "-vf",
            sarge.shell_quote(",".join(rotate_params)),
            snapshot_path,
        ]
        self._logger.info("Running: {}".format(" ".join(ffmpeg_command)))
        try:
            p = sarge.run(ffmpeg_command)
        except Exception as e:
            self._logger.info(f"Exception running ffmpeg {e}")
            return

        if p.returncode == 0:
            self._logger.info("Rotated/flipped image with ffmpeg")
        else:
            self._logger.warn(
                "Failed to rotate/flip image with ffmpeg, "
                "got return code {}: {}, {}".format(
                    p.returncode, p.stdout.text, p.stderr.text
                )
            )

    # called when the user presses the icon in the status bar for testing or the test button in the settings form
    def on_api_get(self, request):

        self._logger.info("The test button was pressed...")
        self._logger.info(f"request = {request}")

        try:
            self._logger.info("Sending text with image")
            result = self._send_message_with_webcam_image(
                "Test from the OctoText Plugin.",
                self._settings.get(["smtp_message"]),
                sender="OctoText",
            )
        except Exception as e:
            self._logger.exception(
                "Exception while sending text, {message}".format(message=str(e))
            )
            return flask.make_response(flask.jsonify(result=False, error="SMTP"))

        # result = True
        self._logger.info(f"String returned from send_message_with_webcam {result}")
        if not (result is True):
            error = result
            result = False
        else:
            error = None

        return flask.make_response(flask.jsonify(result=result, error=error))

    #    def printer_status_callback(self, _):
    #        result = True
    #        # result = octoprint.printer.PrinterInterface.is_paused(self)
    #        if result:
    #            self._logger.info("*** Printer callback paused ***")
    #        return

    # testing logging and proper startup of passed values in settings forms
    def on_after_startup(self):
        self._logger.info("--------------------------------------------")
        self._logger.info(f"OctoText started: {self._plugin_version}")
        self._logger.info(
            "SMTP Name: {}, SMTP port: {}, SMTP message: {}, server login: {}".format(
                self._settings.get(["smtp_name"]),
                self._settings.get(["smtp_port"]),
                self._settings.get(["smtp_message"]),
                self._settings.get(["server_login"]),
            )
        )
        self._logger.info("--------------------------------------------")
        # octoprint.printer.PrinterInterface.register_callback(
        #    self, callback=self.printer_status_callback(self)
        # )
        # self._printer.pause_print()

    # ~~ EventPlugin API

    def on_event(self, event, payload):

        noteType = title = description = None
        printer_name = self._settings.global_get(["appearance", "name"])

        do_cam_snapshot = True

        # self._logger.info(f"event received: {event}")

        if event == octoprint.events.Events.UPLOAD:

            if not self._settings.get(["en_upload"]):
                return

            file = payload["name"]
            target = payload["path"]

            noteType = "File uploaded from" + printer_name
            title = "A new file was uploaded"
            description = "{file} was uploaded {targetString}".format(
                file=file, targetString="to SD" if target == "sd" else "locally"
            )
            do_cam_snapshot = False  # don't really want a snapshot for this

        elif event == octoprint.events.Events.PRINT_STARTED:

            if not self._settings.get(["en_printstart"]):
                return

            self._logger.info(payload)
            file = os.path.basename(payload["name"])
            origin = payload["origin"]

            noteType = "Print job started on: " + printer_name
            title = "A new print job was started"
            description = "{file} has started printing {originString}".format(
                file=file, originString="from SD" if origin == "sd" else "locally"
            )

        elif event == octoprint.events.Events.PRINT_DONE:

            if not self._settings.get(["en_printend"]):
                return

            file = os.path.basename(payload["name"])
            elapsed_time = payload["time"]

            noteType = "Print job finished on: " + printer_name
            title = "Print job finished"
            description = "{file} finished printing, took {elapsed_time} seconds".format(
                file=file, elapsed_time=int(elapsed_time)
            )

        elif event == octoprint.events.Events.ERROR:

            if not self._settings.get(["en_error"]):
                return

            error = payload["error"]

            noteType = "Printer ERROR: " + printer_name
            title = "Unrecoverable Error!"
            description = f"{noteType} {error}"

        elif event == octoprint.events.Events.PRINT_FAILED:

            if not self._settings.get(["en_printfail"]):
                return

            reason = payload["reason"]
            time = payload["time"]
            time = str(int(time))

            noteType = "Print failed on: " + printer_name
            title = "Print Fail after " + time + " seconds"
            description = f"{noteType} {reason}"

        elif event == octoprint.events.Events.PRINT_PAUSED:

            if not self._settings.get(["en_printpaused"]):
                return

            reason = payload["name"]
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

            noteType = "Print Paused on: " + printer_name
            title = "Print Paused " + user + " at " + time
            description = f"{noteType} {reason}"
            self._logger.info(
                "Print paused args notetype: {}, name:{}, title {}, description {}".format(
                    noteType, reason, title, description
                )
            )

        elif event == octoprint.events.Events.PRINT_RESUMED:

            if not self._settings.get(["en_printresumed"]):
                return

            reason = payload["name"]
            try:
                user = payload["user"]
            except Exception:
                try:
                    user = payload["owner"]
                except Exception:
                    user = "System"

            time = datetime.datetime.now().isoformat(sep=" ", timespec="minutes")

            noteType = "Print resumed on: " + printer_name
            title = "Print resumed by " + user + " at " + time
            description = f"{noteType} {reason}"
            self._logger.info(
                "Print resumed args notetype: {}, name:{}, title {}, description {}".format(
                    noteType, reason, title, description
                )
            )

        if noteType is None:
            return

        if do_cam_snapshot:
            self._send_message_with_webcam_image(title, description, sender=printer_name)
        else:
            self.smtp_send_message(noteType, title, description)

    ##~~ Softwareupdate hook

    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return dict(
            OctoText=dict(
                displayName="OctoText",  # should this be self._plugin_name ??
                displayVersion=self._plugin_version,
                # version check: github repository
                type="github_release",
                user="berrystephenw",
                repo="Octotext",
                current=self._plugin_version,
                stable_branch=dict(name="Stable", branch="main", comittish=["main"]),
                prerelease_branches=[
                    dict(
                        name="Release Candidate",
                        branch="rc",
                        comittish=["rc", "main"],
                    )
                ],
                # update method: pip
                pip="https://github.com/berrystephenw/OctoText/archive/{target_version}.zip",
            )
        )


# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "OctoText"

__plugin_pythoncompat__ = ">=3,<4"  # only python 3


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = OctoTextPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
