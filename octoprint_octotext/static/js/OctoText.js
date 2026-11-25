/*
 * View model for OctoPrint-Octotext
 *
 * Author: Stephen Berry
 * License: AGPLv3
 */
$(document).ready(function(){
    $('[tool-tip-toggle="tooltip"]').tooltip({
        placement : 'bottom'
    });
});

$(function() {
    function OctoTextViewModel(parameters) {
        var self = this;

        self.settings = parameters[0];

        self.busy = ko.observable(false);

        // Custom notification templates

        var TEMPLATE_EVENTS = [
            {key: "print_started", label: "Print started"},
            {key: "print_done", label: "Print finished"},
            {key: "print_progress", label: "Print progress"},
            {key: "print_paused", label: "Print paused"},
            {key: "print_resumed", label: "Print resumed"},
            {key: "print_cancelled", label: "Print cancelled"},
            {key: "print_failed", label: "Print failed"},
            {key: "upload", label: "File uploaded"},
            {key: "error", label: "Printer error"}
        ];

        function makeSample(values) {
            var sample = {
                printer: "Prusa MK4",
                tool_temp: "T0 210\u00b0C / 210\u00b0C, T1 205\u00b0C / 205\u00b0C",
                bed_temp: "60\u00b0C / 60\u00b0C",
                percent: "50",
                elapsed: "1:15:00",
                remaining: "1:15:00",
                eta: "3:45 PM",
                state: "Printing",
                error: "",
                user: "",
                reason: ""
            };
            Object.keys(values).forEach(function (key) {
                sample[key] = values[key];
            });
            return sample;
        }

        // Keep these values in sync with _get_event_samples() in
        // octoprint_octotext/__init__.py.
        self.templateSamples = {
            print_started: makeSample({
                event: "Print job started",
                message: "benchy.gcode has started printing locally.",
                file: "benchy.gcode",
                percent: "0",
                elapsed: "0:00:00"
            }),
            print_done: makeSample({
                event: "Print job finished",
                message: "benchy.gcode\nfinished printing, elapsed time: 2:30:00.",
                file: "benchy.gcode",
                percent: "100",
                elapsed: "2:30:00",
                remaining: "0:00:00",
                eta: ""
            }),
            print_progress: makeSample({
                event: "Print Progress 50% finished.",
                message: "benchy.gcode",
                file: "benchy.gcode"
            }),
            print_paused: makeSample({
                event: "Print Paused by admin at 2026-11-24 10:30",
                message: "file: benchy.gcode",
                file: "benchy.gcode",
                state: "Paused",
                user: "admin"
            }),
            print_resumed: makeSample({
                event: "Resumed by admin at 2026-11-24 10:35",
                message: "file: benchy.gcode",
                file: "benchy.gcode",
                elapsed: "1:20:00",
                remaining: "1:10:00",
                user: "admin"
            }),
            print_cancelled: makeSample({
                event: "Print canceled by admin",
                message: "file: benchy.gcode",
                file: "benchy.gcode",
                state: "Cancelling",
                user: "admin"
            }),
            print_failed: makeSample({
                event: "Print Fail after 120 seconds",
                message: "error file: benchy.gcode",
                file: "benchy.gcode",
                percent: "2",
                elapsed: "0:02:00",
                remaining: "",
                eta: "",
                state: "Operational",
                reason: "error"
            }),
            upload: makeSample({
                event: "A file was uploaded ",
                message: "benchy.gcode was uploaded locally",
                file: "benchy.gcode",
                percent: "",
                elapsed: "",
                remaining: "",
                eta: "",
                state: "Operational"
            }),
            error: makeSample({
                event: "Printer ERROR!",
                message: " Thermal runaway",
                file: "benchy.gcode",
                percent: "",
                elapsed: "",
                remaining: "",
                eta: "",
                state: "Error",
                error: "Thermal runaway"
            })
        };

        self.templateSampleMeta = {
            "progress.printTimeLeftOrigin": "genius",
            "analysis.estimatedPrintTime": "9000",
            "analysis.filament.tool0.length": "12500.0"
        };

        self.selectedTemplateEvent = ko.observable("");
        self.showVariableReference = ko.observable(false);
        self.templateEventCaption = gettext("-- Select an event to customize --");
        self.emptyTemplate = {
            subject: ko.observable(""),
            body: ko.observable("")
        };

        self.templateEvents = ko.computed(function () {
            var pluginSettings = self.settings.settings &&
                self.settings.settings.plugins &&
                self.settings.settings.plugins.OctoText;
            var templates = pluginSettings && pluginSettings.templates;
            return TEMPLATE_EVENTS.map(function (event) {
                var template = templates && templates[event.key];
                var customized = template && (
                    (template.subject() || "").trim() ||
                    (template.body() || "").trim()
                );
                return {
                    key: event.key,
                    label: (customized ? "\u2605 " : "") + gettext(event.label)
                };
            });
        }, null, {deferEvaluation: true});

        self.selectedTemplate = ko.computed(function () {
            var key = self.selectedTemplateEvent();
            var pluginSettings = self.settings.settings &&
                self.settings.settings.plugins &&
                self.settings.settings.plugins.OctoText;
            var templates = pluginSettings && pluginSettings.templates;
            if (key && templates && templates[key]) {
                return templates[key];
            }
            return self.emptyTemplate;
        }, null, {deferEvaluation: true});

        self.templateVariables = [
            {token: "{event}", description: gettext("Full built-in event title, including dynamic details.")},
            {token: "{message}", description: gettext("Built-in event body/details.")},
            {token: "{printer}", description: gettext("Printer appearance name.")},
            {token: "{file}", description: gettext("Current or event filename.")},
            {token: "{percent}", description: gettext("Completion percentage as a number (add % in the template).")},
            {token: "{elapsed}", description: gettext("Elapsed print time (H:MM:SS).")},
            {token: "{remaining}", description: gettext("Estimated time remaining (H:MM:SS).")},
            {token: "{eta}", description: gettext("Estimated local completion time.")},
            {token: "{tool_temp}", description: gettext("Hotend actual / target temperatures for every tool.")},
            {token: "{bed_temp}", description: gettext("Bed actual / target temperature.")},
            {token: "{state}", description: gettext("Current printer state.")},
            {token: "{error}", description: gettext("Error text (printer error events).")},
            {token: "{user}", description: gettext("User who paused, resumed or cancelled.")},
            {token: "{reason}", description: gettext("Failure reason (print failed events).")}
        ];

        self.variableReferenceLabel = ko.computed(function () {
            return self.showVariableReference()
                ? gettext("Hide variable reference")
                : gettext("Show variable reference");
        });

        self.toggleVariableReference = function () {
            self.showVariableReference(!self.showVariableReference());
            return false;
        };

        self.defaultTemplatePlaceholders = {
            print_started: {
                subject: "Print job started",
                body: "{file} has started printing locally. Message sent from: {printer}"
            },
            print_done: {
                subject: "Print job finished",
                body: "{file}\nfinished printing, elapsed time: {elapsed}. Message sent from: {printer}"
            },
            print_progress: {
                subject: "Print Progress {percent}% finished.",
                body: "{file} Message sent from: {printer}"
            },
            print_paused: {
                subject: "Print Paused by {user}",
                body: "file: {file} Message sent from: {printer}"
            },
            print_resumed: {
                subject: "Resumed by {user}",
                body: "file: {file} Message sent from: {printer}"
            },
            print_cancelled: {
                subject: "Print canceled by {user}",
                body: "file: {file} Message sent from: {printer}"
            },
            print_failed: {
                subject: "Print Fail",
                body: "{reason} file: {file} Message sent from: {printer}"
            },
            upload: {
                subject: "A file was uploaded",
                body: "{file} was uploaded locally Message sent from: {printer}"
            },
            error: {
                subject: "Printer ERROR!",
                body: "{error} Message sent from: {printer}"
            }
        };

        self.selectedTemplatePlaceholder = ko.computed(function () {
            var key = self.selectedTemplateEvent();
            if (key && self.defaultTemplatePlaceholders[key]) {
                return self.defaultTemplatePlaceholders[key];
            }
            return {
                subject: gettext("Leave blank to use default"),
                body: gettext("Leave blank to use default")
            };
        });

        self.insertVariable = function (token, field) {
            var template = self.selectedTemplate();
            if (!template || !template[field]) {
                return;
            }
            var current = template[field]() || "";
            var separator = current && !/\s$/.test(current) ? " " : "";
            template[field](current + separator + token);
        };

        function stringifyTemplateValue(value) {
            if (value === null || value === undefined) {
                return "";
            }
            return String(value);
        }

        function renderTemplate(template, context, preview) {
            if (!template) {
                return "";
            }
            return template.replace(/\{([^{}]+)\}/g, function (match, rawToken) {
                var token = rawToken.trim();
                if (token.indexOf("meta:") === 0) {
                    var expression = token.substring(5).trim();
                    if (Object.prototype.hasOwnProperty.call(self.templateSampleMeta, expression)) {
                        return stringifyTemplateValue(self.templateSampleMeta[expression]);
                    }
                    return preview ? "???" : "";
                }
                if (token.indexOf("plugin:") === 0) {
                    return preview ? "???" : "";
                }
                if (Object.prototype.hasOwnProperty.call(context, token)) {
                    return stringifyTemplateValue(context[token]);
                }
                return match;
            });
        }

        self.selectedTemplatePreview = ko.computed(function () {
            var key = self.selectedTemplateEvent();
            var sample = self.templateSamples[key];
            if (!key || !sample) {
                return {subject: "", body: ""};
            }
            var pluginSettings = self.settings.settings &&
                self.settings.settings.plugins &&
                self.settings.settings.plugins.OctoText;
            if (!pluginSettings) {
                return {subject: "", body: ""};
            }
            var sample = Object.assign({}, self.templateSamples[key]);
            var appName = self.settings.settings &&
                self.settings.settings.appearance &&
                self.settings.settings.appearance.name &&
                self.settings.settings.appearance.name();
            if (appName) {
                sample.printer = appName;
            }
            var template = self.selectedTemplate();
            var rawBody = template.body() || "";
            var subject = renderTemplate(template.subject() || "", sample, true);
            var body = renderTemplate(rawBody, sample, true);
            if (!subject.trim()) {
                subject = stringifyTemplateValue(sample.event);
            }
            var usedCustomBody = !!rawBody.trim();
            if (!usedCustomBody) {
                body = stringifyTemplateValue(sample.message);
            }
            var prefix = pluginSettings.subject_prefix
                ? pluginSettings.subject_prefix() || ""
                : "";
            var appearance = pluginSettings.include_appearance_prefix &&
                pluginSettings.include_appearance_prefix()
                ? sample.printer + ": "
                : "";
            return {
                subject: (prefix + appearance + subject).replace(/[\r\n]+/g, " "),
                body: usedCustomBody ? body : body + " Message sent from: " + sample.printer
            };
        }, null, {deferEvaluation: true});
        
        self.sendTestMessage = function() {
            self._sendTestMessage({});
        };

        self.sendTemplateTestMessage = function() {
            self._sendTestMessage({event: self.selectedTemplateEvent()});
        };

        self._sendTestMessage = function(payload) {
            self.busy(true);
            OctoPrint.simpleApiCommand("OctoText", "test", payload)
                .done(function(response) {
                    self.busy(false);
                    if (response.result) {
                        new PNotify({
                            title: gettext("Congratulations!"),
                            title_escape: true,
                            text: gettext("A test message was sent to OctoText, everything appears good on our side. Give your service a minute to route the text or email to you!"),
                            text_escape: true,
                            type: "success"
                        });
                    } else {
                        var text, notice_type;
                        notice_type = "error";
                        if (response.error === "SNAP") {
                            text = gettext("Test failure in opening your webcam, check your settings!");
                            notice_type = "notice";
                        } else if (response.error === "LOGIN_E") {
                            text = gettext("Failure to login to your email account!");
                            if (response.login) {
                                text += " (" + gettext("Login used:") + " " + response.login + ")";
                            }
                        } else if (response.error === "SENDM_E"){
                            text = gettext("Exception while logging into mail server. Check your login and password.");
                        } else if (response.error === "SMTP_E"){
                            text = gettext("Exception while talking to your mail server, check your SMTP settings.");
                        } else {
                            text = gettext("Test message could not be sent, check log & your settings");
                        }
                        new PNotify({
                            title: gettext("Test message problem!"),
                            title_escape: true,
                            text: text,
                            text_escape: true,
                            type: notice_type
                        });
                    }
                })
                .fail(function() {
                    self.busy(false);
                });
        };
        // assign the injected parameters, e.g.:
        // self.loginStateViewModel = parameters[0];
        // self.settingsViewModel = parameters[1];

        // TODO: Implement your plugin's view model here.
    }

    /* view model class, parameters for constructor, container to bind to
     * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
     * and a full list of the available options.
     */
    OCTOPRINT_VIEWMODELS.push({
        construct: OctoTextViewModel,
        // ViewModels your plugin depends on, e.g. loginStateViewModel, settingsViewModel, ...
        dependencies: [ /* "loginStateViewModel",*/ "settingsViewModel", "navigationViewModel"],
        // Elements to bind to, e.g. #settings_plugin_OctoText, #tab_plugin_OctoText, ...
        elements: ["#settings_plugin_OctoText", "#navbar_plugin_OctoText"]
    });
});
