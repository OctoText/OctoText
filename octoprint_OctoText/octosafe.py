# -*- coding: utf-8 -*-
import logging

import flask
import octoprint.plugin
import RPi.GPIO as GPIO
import smbus

DEVICE_ADDRESS = 0x40  # 7 bit address for the HDC2080 (0b1000000)
DEVICE_MFG1 = 0xFC
DEVICE_MFG2 = 0xFD
HDC2080_TEMPL = 0x0
HDC2080_TEMPH = 0x1
HDC2080_HUMID_L = 0x2
HDC2080_HUMID_H = 0x3
HDC2080_CONFIG = 0xF
HDC2080_DRDY = 0x4


class OctoSafe:
    def __init__(self, plugin):
        self.plugin = plugin
        # noinspection PyProtectedMember
        self._settings = plugin._settings
        self._logger: logging.Logger = logging.getLogger("octoprint.plugins.OctoText.api")
        relay_state = False
        bus = smbus.SMBus(1)  # the I2C bus is always 1 for the GPIO header /dev/i2c-1
        last_temp = 0
        last_humidity = 0
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)

    def button_callback(self, channel):
        if channel == self._settings.get(["detector_pin"]):
            # self._printer.pause_print
            self._logger.info("----*** Sound detected ***----")

    def SetupSingleGPIO(self, channel):
        try:
            if channel != -1:
                GPIO.setup(channel, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
                GPIO.add_event_detect(
                    channel,
                    GPIO.RISING,
                    callback=self.button_callback,
                    bouncetime=self.debounce,
                )
                self._logger.info("New Event Detect has been added to GPIO # %s", channel)
        except:
            self._logger.exception(
                "Cannot setup GPIO ports %s, check to make sure you don't have the same ports assigned to multiple actions",
                str(channel),
            )

    @property
    def debounce(self):
        return int(self._settings.get(["debounce"]))

    def get_settings_defaults(self):
        return {
            "debounce": 400,
            "relay_pin": 13,
            "detector_pin": 11,
            "inverted_output": False,
        }

    def get_template_configs(self):
        return [
            {"type": "navbar", "custom_bindings": False},
            {"type": "settings", "custom_bindings": False},
        ]

    def on_after_startup(self):
        self._logger.info("--------------------------------------------")
        self._logger.info("OctoSafe started, listening for GET request")
        self._logger.info(
            "Relay pin: {}, inverted_input: {}, detector pin: {}".format(
                self._settings.get(["relay_pin"]),
                self._settings.get(["inverted_output"]),
                self._settings.get(["detector_pin"]),
            )
        )
        self._logger.info("--------------------------------------------")

        # Setting the default state of pin
        self.SetupSingleGPIO(
            self._settings.get(["detector_pin"])
        )  # register this one with a callback (smoke detector)
        GPIO.setup(int(self._settings.get(["relay_pin"])), GPIO.OUT)
        if bool(self._settings.get(["inverted_output"])):
            GPIO.output(int(self._settings.get(["relay_pin"])), GPIO.HIGH)
        else:
            GPIO.output(int(self._settings.get(["relay_pin"])), GPIO.LOW)

        # start the first conversion
        self.bus.write_byte_data(DEVICE_ADDRESS, HDC2080_CONFIG, 0x1)
        # read some status from the temp/humidty sensor
        b = self.bus.read_byte_data(DEVICE_ADDRESS, DEVICE_MFG1)
        c = self.bus.read_byte_data(DEVICE_ADDRESS, DEVICE_MFG2)
        self._logger.info(" Humidity device MFG1 register - should be 0x49, 0x54")
        self._logger.info(f" low byte: {b}, high byte {c}")

    def on_api_get(self, request):
        # Sets the GPIO every time, if user changed it in the settings.
        GPIO.setup(int(self._settings.get(["relay_pin"])), GPIO.OUT)

        self.relay_state = not self.relay_state

        # Sets the relay state depending on the inverted output setting (XOR)
        if self.relay_state ^ self._settings.get(["inverted_output"]):
            GPIO.output(int(self._settings.get(["relay_pin"])), GPIO.HIGH)
        else:
            GPIO.output(int(self._settings.get(["relay_pin"])), GPIO.LOW)

        self._logger.info(f"Got request. Relay state: {self.relay_state}")

        return flask.jsonify(status="ok")

    def get_update_information(self):
        return {
            "octosafe": {
                "displayName": "OctoSafe",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "current": self._plugin_version,
                "user": "berrystephenw",
                "repo": "OctoSafe",
                "pip": "https://github.com/berrystephenw/OctoSafe/archive/{target}.zip",
            }
        }

    def convert_temp(self, byteh, bytel):
        # the equation to convert to temp in C
        # temp = ((byteH[15:8]+byteL[7:0])/2^16)x165-40
        temp = (((byteh << 8) + bytel) / 65536) * 165 - 40
        return temp

    def convert_humid(self, byteh, bytel):
        # the equation to convert to humidity
        # temp = ((byteH[15:8]+byteL[7:0])/2^16)x165-40
        humid = (((byteh << 8) + bytel) / 65536) * 100
        return humid

    def callback(self, comm, parsed_temps):
        temp_l = self.bus.read_byte_data(DEVICE_ADDRESS, HDC2080_TEMPL)
        temp_h = self.bus.read_byte_data(DEVICE_ADDRESS, HDC2080_TEMPH)
        humid_l = self.bus.read_byte_data(DEVICE_ADDRESS, HDC2080_HUMID_L)
        humid_h = self.bus.read_byte_data(DEVICE_ADDRESS, HDC2080_HUMID_H)
        temp = self.convert_temp(temp_h, temp_l)
        humidity = self.convert_humid(humid_h, humid_l)
        parsed_temps.update(ambient=(temp, None))
        parsed_temps.update(humidity=(humidity, None))
        # start the conversion for the next time around
        self.bus.write_byte_data(DEVICE_ADDRESS, HDC2080_CONFIG, 0x1)
        return parsed_temps
