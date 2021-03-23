/*
 * View model for OctoPrint-Octotext
 *
 * Author: Stephen Berry
 * License: AGPLv3
 */
$(function() {
    function OctoTextViewModel(parameters) {
        var self = this;

        self.settings = parameters[0];

        self.busy = ko.observable(false);
        
        self.sendTestMessage = function() {
            self.busy(true);
            $.ajax({
                url: API_BASEURL + "plugin/OctoText",
                type: "GET",
                dataType: "json",
                data: JSON.stringify({
                    command: "test",
                    /* token: self.settings.settings.plugins.OctoText.access_token(), */
                    channel: self.settings.settings.plugins.OctoText.push_message()
                }),
                contentType: "application/json; charset=UTF-8",
                success: function(response) {
                    self.busy(false);
                    if (response.result) {
                        new PNotify({
                            title: gettext("Congratulations!"),
                            text: gettext("A test message was sent to OctoText, everything appears good on our side. \n\r Give your service a minute to route the text or email to you!"),
                            type: "success"
                        });
                    } else {
                        var text;
                        if (response.error === "SNAP") {
                            text = gettext("Test message could not be sent to email server due to failure in opening your webcam, check your settings!");
                        } else if (response.error === "LOGIN_E") {
                            text = gettext("Failure to login to your email account!");
                        } else if (response.error === "SENDM_E"){
                            text = gettext("Exception while logging into mail server. Check your login and password.");
                        } else if (response.error === "SMTP"){
                            text = gettext("Exception while talking to your mail server, check your SMTP settings.");
                        } else {
                            text = gettext("Test message could not be sent, check log & your settings");
                        }
                        new PNotify({
                            title: gettext("Test message could not be sent"),
                            text: text,
                            type: "error"
                        });
                    }
                },
                error: function() {
                    self.busy(false);
                }
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
