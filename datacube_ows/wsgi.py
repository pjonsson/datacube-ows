# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0


#pylint: skip-file
import os
import sys

# This is the directory of the source code that the web app will run from
sys.path.append("/opt")

# The location of the datcube config file.
os.environ.setdefault("DATACUBE_CONFIG_PATH", "/opt/odc/.datacube.conf.local")

from datacube_ows import __version__

from datacube_ows.ogc import app  # isort:skip

application = app


def main():
    if "--version" in sys.argv:
        print("Open Data Cube Open Web Services (datacube-ows) version",
              __version__
              )
        exit(0)
    app.run()


if __name__ == '__main__':
    main()
