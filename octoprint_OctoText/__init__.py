# -*- coding: utf-8 -*-
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
import smtplib
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate
from queue import Queue
from threading import Thread

import flask
import octoprint.events
import octoprint.plugin
import sarge
from flask_login import current_user

# a few globals to save time checking for the existence of plugins


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
            self._logger.debug(f"processing email  {email_message['Subject']}")
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

                    self._logger.debug(f"Retrying notification, error {result}")
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
                    f"Retries sending message: {retries}. Time message delayed: {elapsed_time}"
                )
            self._logger.debug(f"Send Message result: {result}")
            self.notifyQ.task_done()
            time.sleep(60)  # make this adjustable?
        pass

    ##~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return {
            "smtp_port": 587,
            "smtp_name": "smtp.office365.com",
            "smtp_alert": "*ALERT from your PRINTER*",
            "smtp_message": "Your printer is creating something wonderful!",
            "username": "YourEmail",
            "servername": "outlook.com",
            "server_login": "YourEmail@outlook.com",  # obsoleted
            "validate_username": False,
            "server_pass": "not a valid password",
            "from_address": "",
            "phone_numb": "8675309",
            "carrier_address": "mypixmessages.com",
            "push_message": None,
            "progress_interval": 10,
            "en_progress_time": False,
            "en_progress": False,
            "en_webcam": True,
            "en_printstart": True,
            "en_printend": True,
            "en_upload": True,
            "en_error": True,
            "en_printfail": False,
            "en_printpaused": True,
            "en_printresumed": False,
            "show_navbar_button": True,
            "show_fail_cancel": False,
            "mmu_timeout": 0,
            "use_ssl": False,
        }

    def get_printer_name(self):
        a_name = self._settings.global_get(["appearance", "name"])
        if a_name == "":
            a_name = "OctoText"
        return a_name

    # ~~ PrintProgressPlugin

    current_path = ""

    def on_print_progress(self, storage, path, progress):

        self.current_path = path
        if not self._settings.get(["en_progress"]):
            return

        if progress == 0:
            return

        if self._settings.get(["en_progress_time"]):
            return

        # if these two events fire at the same time (printend and progress at 100%) we have two threads that are async
        # to each other that try to send notifications at the same time. This has caused both of these threads to fail
        # on a Pi 4 (not so much on a fast laptop). We default to letting the printend message do the work
        if progress == 100 and self._settings.get(["en_printend"]):
            return
        # occasionally we don't get 99% messages from progress intervals (might be on short prints)
        if progress % int(self._settings.get(["progress_interval"])) == 0:
            printer_name = self.get_printer_name()
            title = "Print Progress " + str(progress) + " percent finished."
            description = path
            self._prepare_email_message_and_send(
                title, description, printer_name, None, self._settings.get(["en_webcam"])
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
            login = self._settings.get(["username"])
        else:
            login = (
                self._settings.get(["username"])
                + "@"
                + self._settings.get(["servername"])
            )

        passw = self._settings.get(["server_pass"])
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
                self._logger.debug(f"startttls() {error}")
            self._logger.debug(f"SMTP_server {SMTP_server}")
        except Exception as e:
            self._logger.exception(
                "Exception while talking to your mail server {message}".format(
                    message=str(e)
                )
            )
            return ["SMTP_E", None]

        # login to the mail account
        self._logger.debug(login)
        if (
            passw
        ):  # Only use SMTP auth if the password has been supplied, skip if blank - issue #91
            self._logger.debug("Password supplied, attempting to log into mail server")
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
        else:
            self._logger.debug(
                "Password not supplied, proceeding without SMTP authentication."
            )

        email_addr = phone_numb + "@%s" % carrier_addr
        return [None, email_addr]

    def _prepare_email_message_and_send(
        self, title, body, sender=None, thumbnail=None, send_image=True, direct_send=False
    ):
        """
        Prepare the email for sending and put it into the message queue or the email is directly sent

        :param title: Email title
        :param body: body of email
        :param sender: email of sender
        :param thumbnail: thumbnail image path
        :param send_image: boolean True for thumbnail
        :param direct_send: boolean True for preformatted email
        :return: SNAP - a failure to get an image from the webcam
        FILE_E - filesystem error
        True - no error
        _send_file results
        SMTP_E - for errors setting up the SMTP connection
        LOGIN_E - for errors logging into the host email account
        SENDM_E - error sending email from server
        True - no error
        """
        self._logger.debug(f"Preparing EMail '{title}' and adding to Notification-Queue")
        self._logger.debug(
            "Enable webcam setting {}".format(self._settings.get(["en_webcam"]))
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
            snapshot_url = self._settings.global_get(["webcam", "snapshot"])
            self._logger.debug(f"Snapshot URL is: {snapshot_url}")
            if snapshot_url and send_image:
                # email = self._create_email_with_snapshotimage()
                image_path_dict = self._create_image_path_from_snapshot()
                image_path = image_path_dict["path"]
                result = image_path_dict["result"]
                if result == "DELETE_IMAGE_AFTER_SENT":
                    delete_image_after_sent = True
            pass

        appearance_name = self.get_printer_name()
        self._logger.debug(f"Appearance name (subject): {appearance_name}")

        if body is None:
            body = ""

        if self._settings.get(["from_address"]):
            fromAddr = self._settings.get(["from_address"])
        elif self._settings.get(["validate_username"]):
            fromAddr = self._settings.get(["username"])
        else:
            fromAddr = self._settings.get(["username"]) + "@" + self._settings.get(["servername"])

        phone_numb = self._settings.get(["phone_numb"])
        carrier_addr = self._settings.get(["carrier_address"])
        email_addr = phone_numb + "@%s" % carrier_addr

        # setup email message with all collected data
        email_message = EmailMessage()

        cc_set = self._settings.get(["cc_field"])
        if cc_set is not None:
            cc_set = cc_set.replace("\n", "")
            cc_set = cc_set.replace(" ", "")
            cc_set = cc_set.split(",")
            self._logger.debug(f"Cc: settings - {cc_set}")
            email_message["Cc"] = cc_set

        email_message["Subject"] = appearance_name + ": " + title
        email_message["From"] = fromAddr  # 'OctoText@outlook.com'
        email_message["To"] = email_addr
        email_message["Date"] = formatdate(localtime=True)
        content_string = " Message sent from: " + sender
        email_message.set_content(
            body + content_string, charset="utf-8"
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
                self._logger.exception(
                    "Exception while opening file for snapshot, {message}".format(
                        message=str(e)
                    )
                )
            if delete_image_after_sent:
                try:
                    os.remove(image_path)
                except Exception as e:
                    self._logger.exception(
                        "Could not remove temporary snapshot file {} e:{}".format(
                            image_path, str(e)
                        )
                    )
                    # just logging, sending without image attached: return "FILE_E"

        if direct_send:
            result = self._send_email_message(email_message)
        else:
            self.notifyQ.put(email_message)
        return result

    # load the snapshot image from camera, rotate and store the image into the filesystem. return the image path
    # location return dict( path:thePath, result:"SNAP")
    def _create_image_path_from_snapshot(self):
        try:

            # reading webcam snapshot image
            import tempfile

            from requests import get

            tempFile = tempfile.NamedTemporaryFile(delete=False)
            snapshot_url = self._settings.global_get(["webcam", "snapshot"])

            response = get(snapshot_url, verify=False, timeout=5)  # adding timeout on url
            response.raise_for_status()
            tempFile.write(response.content)
            tempFile.close()
            # ffmpeg can't guess file type it seems
            os.rename(tempFile.name, tempFile.name + ".jpg")
            tempFile.name += ".jpg"

            self._logger.debug(f"Webcam tempfile {tempFile.name}")
            self._process_snapshot(tempFile.name)

            return {"path": tempFile.name, "result": "DELETE_IMAGE_AFTER_SENT"}
        except Exception as e:
            self._logger.exception(
                "Exception while fetching snapshot from webcam: {message}".format(
                    message=str(e)
                )
            )
            # send message without webcam snapshot (enabled but not available)
            path = self._basefolder + "/static/img/offline.jpg"
            return {"path": path, "result": "SNAP"}
        pass

    # Send the email to the smtp-server
    def _send_email_message(self, email_message):
        # login to the SMTP account and mail server
        error, email_addr = self.smtp_login_server()

        if not (error is None):
            return error

        try:
            SMTP_server.send_message(email_message)
            # SMTP_server.sendmail(
            #     email_message["From"], email_message["To"], email_message.as_string()
            # )
            SMTP_server.quit()
        except Exception as e:
            self._logger.exception(
                "Exception while logging into SMTP server(send_email_message) {message}".format(
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
        self._logger.debug("Running: {}".format(" ".join(ffmpeg_command)))
        try:
            p = sarge.run(ffmpeg_command)
        except Exception as e:
            self._logger.debug(f"Exception running ffmpeg {e}")
            return

        if p.returncode == 0:
            self._logger.debug("Rotated/flipped image with ffmpeg")
        else:
            self._logger.warn(
                "Failed to rotate/flip image with ffmpeg, "
                "got return code {}: {}, {}".format(
                    p.returncode, p.stdout.text, p.stderr.text
                )
            )

    def get_api_commands(self):
        return {
            "test": [],
            "data": ["some_parameter"],
        }  # dictionary of acceptable commands

    # Called by OctoPrint upon a POST request to /api/plugin/<plugin identifier>.
    # command will contain one of the commands as specified via get_api_commands(),
    # data will contain the full request body parsed from JSON into a Python dictionary.
    #
    # format of post request from plugin:
    # r = requests.post('/api/plugin/OctoText', json={'param1': 'value1', 'param2': 'value2'})
    def on_api_command(self, command, data):
        self._logger.debug(f"Got an API command: {command}, data: {data}")
        return flask.jsonify(result="ok")

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

        self._logger.debug(f"received a message command: {command}")

        # TODO check the data before we put it on the queue
        # there is no way to notify the caller that there was an error so just log the
        # issues
        # subject and to are required!

        email_message = data

        if email_message["From"] is None:
            email_message["From"] = (
                self._settings.get(["username"])
                + "@"
                + self._settings.get(["servername"])
            )

        if email_message["To"] is None:
            email_message["To"] = (
                self._settings.get(["phone_numb"])
                + "@"
                + self._settings.get(["carrier_address"])
            )

        subject = email_message["Subject"]
        if subject is None:
            return False
        # put it into the notify queue
        self.notifyQ.put(email_message)

        return True

    # called when the user presses the icon in the status bar for testing or the test button in the settings form
    def on_api_get(self, request):

        self._logger.debug("The test button was pressed...")
        self._logger.debug(f"request = {request}")

        try:
            self._logger.debug("Sending text with image")

            # title, body, sender=None, thumbnail=None, send_image=True, direct_send=True
            result = self._prepare_email_message_and_send(
                "Test from the OctoText Plugin.",
                self._settings.get(["smtp_message"]),
                sender="OctoText",
                direct_send=True,
            )
            pass
        except Exception as e:
            self._logger.exception(
                "Exception while sending text, {message}".format(message=str(e))
            )
            return flask.make_response(flask.jsonify(result=False, error="SMTP_E"))

        # result = True
        self._logger.debug(f"String returned from send_message_with_webcam {result}")
        if not (result is True):
            error = result
            result = False
        else:
            error = None

        return flask.make_response(flask.jsonify(result=result, error=error))

    # testing logging and proper startup of passed values in settings forms
    def on_after_startup(self):

        self._logger.info("--------------------------------------------")
        self._logger.info(f"OctoText started: {self._plugin_version}")
        self._logger.info(
            "SMTP Name: {}, SMTP port: {}, SMTP message: {}, server login: {}".format(
                self._settings.get(["smtp_name"]),
                self._settings.get(["smtp_port"]),
                self._settings.get(["smtp_message"]),
                self._settings.get(["username"])
                + "@"
                + self._settings.get(["servername"]),
            )
        )
        # on loading of plugin look for the existence of the prusa or cura thumbnail plugins

        self.prusa_folder = self.get_plugin_data_folder().replace(
            "OctoText", "prusaslicerthumbnails"
        )
        self.cura_folder = self.get_plugin_data_folder().replace(
            "OctoText", "UltimakerFormatPackage"
        )
        if os.path.exists(self.prusa_folder):
            self._logger.info(f"Prusa thumbnail loaded: {self.prusa_folder}")
        if os.path.exists(self.cura_folder):
            self._logger.info(f"Cura thumbnails loaded: {self.cura_folder}")
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
            self._logger.info(f"State ID: {self._printer.get_state_id()}")
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
        self._logger.debug(f"thumbnail filename path is: {thumb_filename}")
        if thumb_filename is not None and os.path.exists(thumb_filename):
            self._logger.debug("thumbnail exists! using image in notifications")
        return thumb_filename

    # ~~ thread for time based notification
    # setting the flag stopme will cause the thread to exit
    def time_thread(self, stopme):
        ptl = None
        while ptl is None:
            time.sleep(1)
            if stopme.is_set():
                return
            progr = self._printer.get_current_data()["progress"]
            ptl = progr["printTimeLeft"]
        self._logger.debug(f"progress: {ptl}")
        while not stopme.is_set():
            progr = self._printer.get_current_data()["progress"]
            total_time = progr["printTime"] + progr["printTimeLeft"]
            interval = (
                int(self._settings.get(["progress_interval"])) / 100 * int(total_time)
            )
            interval = int(interval)
            self._logger.debug(f"interval: {interval}")
            time.sleep(interval)

            # send the message to the queue
            progr = self._printer.get_current_data()["progress"]
            pt_current = progr["printTimeLeft"]
            if pt_current is None or pt_current == 0 or stopme.is_set():
                self._logger.debug("Exiting time thread!")
                return
            # progress = int((pt_current / ptl) * 100)
            time_left = datetime.timedelta(seconds=int(pt_current))
            self._logger.debug(f"Print time left {time_left}")
            printer_name = self.get_printer_name()
            # title = "Print Progress " + str(progress) + " percent left."
            title = "Print Progress " + str(time_left) + " time to finish."
            description = self.current_path

            self._prepare_email_message_and_send(
                title, description, printer_name, None, self._settings.get(["en_webcam"])
            )
        return

    # ~~ Simple function to start and stop the time thread
    stopme = threading.Event()

    def manage_progress_thread(self, stop=False):
        if stop:
            self.stopme.set()
            return
        self.stopme.clear()
        Thread(target=self.time_thread, daemon=True, args=(self.stopme,)).start()
        return

    # ~~ EventPlugin API

    def on_event(self, event, payload):

        noteType = title = description = None
        do_cam_snapshot = True
        thumbnail_filename = None

        if event == octoprint.events.Events.UPLOAD:

            if not self._settings.get(["en_upload"]):
                return

            file = payload["name"]
            target = payload["path"]
            path_to_thumbnail = self.find_thumbnail(file)
            self._logger.debug(f"Upload event - thumbnail filename {path_to_thumbnail}")
            noteType = True
            title = "A file was uploaded "
            description = "{file} was uploaded {targetString}".format(
                file=file, targetString="to SD" if target == "sd" else "locally"
            )
            do_cam_snapshot = False  # don't really want a snapshot for this

        elif event == octoprint.events.Events.PRINT_STARTED:

            if not self._settings.get(["en_printstart"]):
                return

            self._logger.debug(f"Print started event: {payload}")
            file = os.path.basename(payload["name"])
            origin = payload["origin"]

            noteType = True
            title = "Print job started"
            description = "{file} has started printing {originString}.".format(
                file=file, originString="from SD" if origin == "sd" else "locally"
            )
            thumbnail_filename = self.find_thumbnail(file)

            # if we have enabled en_progress_time then we will want to use a different method of sending progress
            # messages. This will mean starting a thread that waits for a specific period of time before sending an
            # notification. we will need to stop the thread on print cancel, error or ending events

            if self._settings.get(["en_progress_time"]):
                self.manage_progress_thread()

        elif event == octoprint.events.Events.PRINT_DONE:

            if not self._settings.get(["en_printend"]):
                return

            file = os.path.basename(payload["name"])
            elapsed_time = datetime.timedelta(seconds=int(payload["time"]))

            self._logger.debug(f"Event received: {event}, print done: {file}")
            noteType = True
            title = "Print job finished"
            description = (
                "{file} \n\rfinished printing, elapsed time: {elapsed_time}.".format(
                    file=file, elapsed_time=elapsed_time
                )
            )
            if self._settings.get(["en_progress_time"]):
                self.manage_progress_thread(stop=True)

        elif event == octoprint.events.Events.ERROR:

            if not self._settings.get(["en_error"]):
                return

            error = payload["error"]

            noteType = True
            title = "Printer ERROR!"
            description = f" {error}"
            self._logger.debug(f"Event received: {event}, print error: {error}")
            if self._settings.get(["en_progress_time"]):
                self.manage_progress_thread(stop=True)

        elif event == octoprint.events.Events.PRINT_CANCELLED:

            if not self._settings.get(["show_fail_cancel"]):
                return
            if self._settings.get(["en_printfail"]) == "Fail":
                return

            settingf = self._settings.get(["en_printfail"])
            self._logger.debug(f"Event received: {event}, print fail: {settingf}")
            name = payload["name"]
            try:
                user = payload["user"]
            except Exception:
                user = "system"

            noteType = True
            title = "Print canceled by " + user
            description = f"file: {name}"
            if self._settings.get(["en_progress_time"]):
                self.manage_progress_thread(stop=True)

        elif event == octoprint.events.Events.PRINT_FAILED:

            if not self._settings.get(["show_fail_cancel"]):
                return
            if self._settings.get(["en_printfail"]) == "Cancel":
                return

            settingf = self._settings.get(["en_printfail"])
            self._logger.debug(f"Event received: {event}, print fail: {settingf}")
            reason = payload["reason"]
            name = payload["name"]
            time = payload["time"]
            time = str(int(time))  # time is a float

            noteType = True
            title = "Print Fail after " + time + " seconds"
            description = f"{reason} file: {name}"

            if self._settings.get(["en_progress_time"]):
                self.manage_progress_thread(stop=True)

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
            self._logger.debug(
                "Print paused args notetype: {}, name:{}, title {}, description {}".format(
                    noteType, pay_name, title, description
                )
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
            self._logger.debug(
                "Print resumed args notetype: {}, name:{}, title {}, description {}".format(
                    noteType, pay_name, title, description
                )
            )

        if noteType is None:
            return

        printer_name = self.get_printer_name()

        self._prepare_email_message_and_send(
            title, description, printer_name, thumbnail_filename, do_cam_snapshot
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
                "user": "berrystephenw",
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
                "pip": "https://github.com/berrystephenw/OctoText/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "OctoText"

__plugin_pythoncompat__ = ">=3,<4"  # only python 3+


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
