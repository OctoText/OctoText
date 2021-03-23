---
layout: plugin

id: OctoText
title: OctoPrint-Octotext
description: Send text messages on common printer events
authors:
- Stephen Berry
license: AGPLv3

# TODO
date: 2021-03-19

homepage: https://github.com/berrystephenw/Octotext
source: https://github.com/berrystephenw/Octotext
archive: https://github.com/berrystephenw/Octotext/archive/master.zip

# TODO
# Set this to true if your plugin uses the dependency_links setup parameter to include
# library versions not yet published on PyPi. SHOULD ONLY BE USED IF THERE IS NO OTHER OPTION!
#follow_dependency_links: false

# TODO
tags:
- email
- text
- notification

# TODO
screenshots:
- url: /extras/IMG_6013.PNG
  alt: Text phone shot
  caption: Text message received
- url: /extras/IMG_6016.PNG
  alt: Email received
  caption: Email is also possible as well as text


# TODO
featuredimage: url of a featured image for your plugin, /assets/img/...

# TODO
# You only need the following if your plugin requires specific OctoPrint versions or
# specific operating systems to function - you can safely remove the whole
# "compatibility" block if this is not the case.

compatibility:

  # List of compatible versions
  #
  # A single version number will be interpretated as a minimum version requirement,
  # e.g. "1.3.1" will show the plugin as compatible to OctoPrint versions 1.3.1 and up.
  # More sophisticated version requirements can be modelled too by using PEP440
  # compatible version specifiers.
  #
  # You can also remove the whole "octoprint" block. Removing it will default to all
  # OctoPrint versions being supported.

  octoprint:
  - 1.4.0

  # List of compatible operating systems
  #
  # Valid values:
  #
  # - windows
  # - linux
  # - macos
  # - freebsd
  #
  # There are also two OS groups defined that get expanded on usage:
  #
  # - posix: linux, macos and freebsd
  # - nix: linux and freebsd
  #
  # You can also remove the whole "os" block. Removing it will default to all
  # operating systems being supported.

  os:
  - linux
  - windows
  - macos
  - freebsd

  # Compatible Python version
  #
  # Plugins should aim for compatibility for Python 2 and 3 for now, in which case the value should be ">=2.7,<4".
  #
  # Plugins that only wish to support Python 3 should set it to ">=3,<4".
  #
  # If your plugin only supports Python 2 (worst case, not recommended for newly developed plugins since Python 2
  # is EOL), leave at ">=2.7,<3" - be aware that your plugin will not be allowed to register on the
  # plugin repository if it only support Python 2.

  python: ">=3,<4"

---
# Octotext - Simple, Easy to use, Free text or email notifications 
<img width="128" alt="OctoText" src="/assets/img/iconfinder_13_1236350.png">
<img width="326" alt="OctoText1" src="/assets/img/IMG_6013.PNG"> Text to your phone!
<img width="326" alt="OctoText2" src="/assets/img/IMG_6016.PNG"> Email printer events!

Get automatically notified when on printer events:
<ul>
   <li> File uploaded</li>
   <li> Print started</li>
   <li> Print done</li>
   <li> Timelapse done</li>
   <li> Print failure </li>
   <li> Peroidic progress updates </li>
   <li> Error (unrecoverable)</li>
</ul>
