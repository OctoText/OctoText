# coding=utf-8
from __future__ import absolute_import
from octoprint.printer.estimation import PrintTimeEstimator

import os
import requests
import octoprint.plugin
import flask
import smtplib
import sarge
import imghdr
from email.message import EmailMessage
from flask_login import current_user

class SMTPMessages(object):
	TEST = "Connection test"
	FILE_UPLOADED = "File uploaded"
	PRINT_STARTED = "Printjob started"
	PRINT_DONE = "Printjob done"
	TIMELAPSE_DONE = "Timelapse done"
	ERROR = "Unrecoverable Printer ERROR"
	PRINT_FAILED = "Print Fail"

class OctoTextPlugin(octoprint.plugin.EventHandlerPlugin,
					octoprint.plugin.ProgressPlugin,
					octoprint.plugin.StartupPlugin,
					octoprint.plugin.SettingsPlugin,
                    octoprint.plugin.AssetPlugin,
					octoprint.plugin.SimpleApiPlugin,
                    octoprint.plugin.TemplatePlugin):

	##~~ SettingsPlugin mixin

	def get_settings_defaults(self):
		return dict(
			smtp_port = 587, 					# most default to this
			smtp_name = "smtp.office365.com", 	# mail server name 
			smtp_alert = "*ALERT from your PRINTER*",
			smtp_message = "Your printer is creating something wonderful!", # the message to send
			server_login = "YourEmail@outlook.com", 	# mail account to use
			server_pass = "not a valid password", 			# password for that account
			phone_numb = "8675309", 				# sorry jenny!
			carrier_address = "mypixmessages.com",
			push_message = None,
			progress_interval = 10,				# should we limit this to a reasonable number?
			en_progress = False,
			en_webcam = True,
			en_printstart = True,
			en_printend = True,
			en_upload = True,
			en_error = True
		)

	def get_api_commands(self):
    		return dict(test=["token"])

	#~~ PrintProgressPlugin

	def on_print_progress(self, storage, path, progress):
		if not self._settings.get(["en_progress"]):
			return

		if progress == 0:
			return
		
		if progress % int(self._settings.get(["progress_interval"])) == 0:
			title = "Print Progress"
			description = str(progress) + " percent finished"
			noteType = "Status"
			if self._settings.get(["do_cam_snapshot"]):
				status = self._send_message_with_webcam_image(title, description)
				if not status:
					self.smtp_send_message(noteType, title, description)
			else:
				self.smtp_send_message(noteType, title, description)

	##~~ AssetPlugin mixin

	def get_assets(self):
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(
			js=["js/OctoText.js"]
#			css=["css/OctoText.css"],
#			less=["less/OctoText.less"]
		)

	def get_template_configs(self):
		return [
#			dict(type="navbar", name = "OctoText", custom_bindings=True),
			dict(type="settings", name = "OctoText", custom_bindings=True)
		]

	# access restrictions for sensitive data
	def on_settings_load(self):
		data = octoprint.plugin.SettingsPlugin.on_settings_load(self)

		# only return our restricted settings to admin users - this is only needed for OctoPrint <= 1.2.16
		restricted = ("server_pass", "server_login")
		for r in restricted:
			if r in data and (current_user is None or current_user.is_anonymous or not current_user.has_permission):
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
			message = self._settings.get(["smtp_message"])
			login = self._settings.get(["server_login"])
			passw = self._settings.get(["server_pass"])
			phone_numb = self._settings.get(["phone_numb"])
			carrier_addr = self._settings.get(["carrier_address"])
			alert = self._settings.get(["smtp_alert"])
			self._logger.info(name)
			self._logger.info(port)

			# setup the server with the SMTP address/port
			SMTP_server = smtplib.SMTP(name, port)
			SMTP_server.starttls()
			# login to the mail account
			self._logger.info(login)
			self._logger.info(passw)
			try:
				SMTP_server.login(login, passw)
			except Exception as e:
				self._logger.exception(
					"Exception while logging into mail server {message}".format(
						message=str(e)))
				SMTP_server.quit()

			email_addr = phone_numb + "@%s" % carrier_addr
			return email_addr

	# initializes the SMTP service, logs into the email server and sends the message to the destination address 
	def smtp_send_message(self, notetype, title, description):
    		
			# login to the SMTP account and mail server
			email_addr = self.smtp_login_server()

			login = self._settings.get(["server_login"])
			
			# Send text message through SMS gateway of destination number
			# format the message like an email - can we send emails too?
			self._logger.info(email_addr)
			message = ("From: %s\r\n" % login
				+ "To: %s" % email_addr +"\r\n"
				+ "Subject: %s\r\n" % notetype
				+ "\r\n"
				+ title + "\r\n" + description)

			login = self._settings.get(["server_login"])
			SMTP_server.sendmail(login, email_addr, message) # works for Xfinity
			SMTP_server.quit()
			return True

	# send an image with the message. have to watch for errors connecting to the camera
	def _send_message_with_webcam_image(self, title, body, filename=None, sender=None):

		self._logger.info("Enable webcam setting {}".format(self._settings.get(["en_webcam"])))
		if not self._settings.get(["en_webcam"]):
			return self.smtp_send_message(sender, title, body)
		
		if filename is None:
			import random, string
			filename = "test-{}.jpg".format("".join([random.choice(string.ascii_letters) for _ in range(16)]))

		if sender is None:
			sender = "OctoText"

		snapshot_url = self._settings.global_get(["webcam", "snapshot"])
		webcam_stream_url = self._settings.global_get(["webcam", "stream"])

		self._logger.info("filename for image: {}".format(filename))
		self._logger.info("Webcam URL is: {}, Snapshot URL is: {}".format(webcam_stream_url, snapshot_url))
		if snapshot_url:
			try:
				from requests import get
				import tempfile
				tempFile = tempfile.NamedTemporaryFile(delete=False)
				response = get(
					snapshot_url,
					verify=False
                )
				response.raise_for_status()
				tempFile.write(response.content)
				tempFile.close()
			except Exception as e:
				self._logger.exception(
					"Exception while fetching snapshot from webcam, sending only a note: {message}".format(
						message=str(e)))
				return False
			else:
				# ffmpeg can't guess file type it seems
				os.rename(tempFile.name, tempFile.name + ".jpg")
				tempFile.name += ".jpg"

				self._logger.info("Webcam tempfile {}".format(tempFile.name))
				# flip or rotate as needed - *** commented out for now *****
				#self._process_snapshot(tempFile)

				if not self._send_file(sender, tempFile.name, filename, title + " " + body):
					self._logger.warn("Could not send a file message with the webcam image, sending only a note")
					return False
    					
				try:
					os.remove(tempFile.name)
				except:
					self._logger.exception("Could not remove temporary snapshot file {}".format(tempFile.name))
					return False

		return True

	# format the MMS message - both text and image. As an email there is something not right with the format
	# the received email doens't have a subject and the attachment is not known to be a jpg...
	def _send_file(self, sender, path, filename, body):

			# login to the SMTP account and mail server
			email_addr = self.smtp_login_server()

			login = self._settings.get(["server_login"])

			msg = EmailMessage()
			msg['Subject'] = body
			msg['From'] = login # 'OctoText@outlook.com'
			msg['To'] = email_addr
			msg.preamble = 'You will not see this in a MIME-aware mail reader.\n'
			
			self._logger.info("path for image: {}".format(path))

			if filename != '':
				try:
					fp = open( path, 'rb' )
				except Exception as e:
					self._logger.exception("Exception while opening file for snapshot, {message}".format(message=str(e)))
				msg_img = fp.read()
				fp.close()

				# debug ended here - the subtype must be returning as None only on the Rpi. **********************
				# not sure that it matters since we force 'jpg' in the filename
				#msg.add_attachment(msg_img, maintype='image', subtype=imghdr.what(None, msg_img))
				msg.add_attachment(msg_img, maintype='image', subtype='jpg')
			
			# Send text message through SMS gateway of destination number
			# format the message like an email
			SMTP_server.sendmail(email_addr, email_addr, msg.as_string() )
			SMTP_server.quit()
			return True

	# this is currently not called but should be tested on a Pi (was disabled for debug)
	def _process_snapshot(self, snapshot_path, pixfmt="yuv420p"):
			hflip  = self._settings.global_get_boolean(["webcam", "flipH"])
			vflip  = self._settings.global_get_boolean(["webcam", "flipV"])
			rotate = self._settings.global_get_boolean(["webcam", "rotate90"])
			ffmpeg = self._settings.global_get(["webcam", "ffmpeg"])
		
			if not ffmpeg or not os.access(ffmpeg, os.X_OK) or (not vflip and not hflip and not rotate):
				return

			ffmpeg_command = [ffmpeg, "-y", "-i", snapshot_path]

			rotate_params = ["format={}".format(pixfmt)] # workaround for foosel/OctoPrint#1317
			if rotate:
				rotate_params.append("transpose=2") # 90 degrees counter clockwise
			if hflip:
				rotate_params.append("hflip") 		# horizontal flip
			if vflip:
				rotate_params.append("vflip")		# vertical flip

			ffmpeg_command += ["-vf", sarge.shell_quote(",".join(rotate_params)), snapshot_path]
			self._logger.info("Running: {}".format(" ".join(ffmpeg_command)))

			p = sarge.run(ffmpeg_command, stdout=sarge.Capture(), stderr=sarge.Capture())
			if p.returncode == 0:
				self._logger.info("Rotated/flipped image with ffmpeg")
			else:
				self._logger.warn("Failed to rotate/flip image with ffmpeg, "
								"got return code {}: {}, {}".format(p.returncode,
																	p.stdout.text,
																	p.stderr.text))

	# called when the user presses the icon in the status bar for testing or the test button in the settings form
	def on_api_get(self, request):
			
			self._logger.info("The test button was pressed...")
			self._logger.info("request = {}".format(request))

			try:
				self._logger.info("Sending text with image")
				result = self._send_message_with_webcam_image("Test from the OctoText Plugin.", self._settings.get(["smtp_message"]), 
					sender="OctoText")
				#self.smtp_send_message("Testing", "Only a test", "of the emergency broadcast system")
			except Exception as e:
				self._logger.exception("Exception while sending text, {message}".format(message=str(e)))
				return flask.make_response(flask.jsonify(result=False, error="SMTP"))

			#result = True
			return flask.make_response(flask.jsonify(result=result))

	## testing logging and proper startup of passed values in settings forms
	def on_after_startup(self):
			self._logger.info("--------------------------------------------")
			self._logger.info("OctoText started")
			self._logger.info("SMTP Name: {}, SMTP port: {}, SMTP message: {}, server login: {}".format(
						self._settings.get(["smtp_name"]),
						self._settings.get(["smtp_port"]),
						self._settings.get(["smtp_message"]),
						self._settings.get(["server_login"])
			))
			self._logger.info("--------------------------------------------")

		#~~ EventPlugin API

	def on_event(self, event, payload):

		import os

		noteType = title = description = None

		do_cam_snapshot = True

		if event == octoprint.events.Events.UPLOAD:
			file = payload["name"]
			target = payload["path"]

			noteType = SMTPMessages.FILE_UPLOADED
			title = "A new file was uploaded"
			description = "{file} was uploaded {targetString}".format(file=file, targetString="to SD" if target == "sd" else "locally")
			do_cam_snapshot = False # don't really want a snapshot for this

		elif event == octoprint.events.Events.PRINT_STARTED:
			self._logger.info(payload)
			file = os.path.basename(payload["name"])
			origin = payload["origin"]

			noteType = SMTPMessages.PRINT_STARTED
			title = "A new print job was started"
			description = "{file} has started printing {originString}".format(file=file, originString="from SD" if origin == "sd" else "locally")

		elif event == octoprint.events.Events.PRINT_DONE:
			file = os.path.basename(payload["name"])
			elapsed_time = payload["time"]

			noteType = SMTPMessages.PRINT_DONE
			title = "Print job finished"
			description = "{file} finished printing, took {elapsed_time} seconds".format(file=file, elapsed_time=int(elapsed_time))
		
		elif event == octoprint.events.Events.ERROR:
			error = payload["error"]

			noteType = SMTPMessages.ERROR
			title = "Unrecoverable Error!"
			description = "{file} {error}".format(file=title, error=error)
		
		elif event == octoprint.events.Events.PRINT_FAILED:
			reason = payload["reason"]
			time = payload["time"]
			time = str(int(time))

			noteType = SMTPMessages.PRINT_FAILED
			title = "Print Fail " + time
			description = "{file} {error}".format(file=title, error=reason)

		if noteType is None:
			return

		if do_cam_snapshot:
			status = self._send_message_with_webcam_image(title, description)
			if not status:
				self.smtp_send_message(noteType, title, description)
		else:
			self.smtp_send_message(noteType, title, description)

	##~~ Softwareupdate hook

	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
		# for details.
		return dict(
			OctoText=dict(
				displayName="Octotext Plugin", # should this be self._plugin_name ??
				displayVersion=self._plugin_version,

				# version check: github repository
				type="github_release",
				user="berrystephenw",
				repo="Octotext",
				current=self._plugin_version,

				# update method: pip
				pip="https://github.com/berrystephenw/Octotext/archive/{target_version}.zip"
			)
		)

# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "OctoText"

# __plugin_pythoncompat__ = ">=2.7,<3" # only python 2
__plugin_pythoncompat__ = ">=3,<4" # only python 3
#__plugin_pythoncompat__ = ">=2.7,<4"  # python 2 and 3


def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = OctoTextPlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}