Environment  Variables and Datacube_OWS
=======================================

The behaviour of datacube_ows can be modified by a number of environment
variables.

.. contents:: Table of Contents

Datacube_ows configuration
--------------------------

The location of the `datacube configuration object <configuration.rst>`_
is set via the ``$DATACUBE_OWS_CFG`` environment variable as described
`here <configuration.rst>`_. To enable the retrieval of a json configuration file from AWS S3,
the ``$DATACUBE_OWS_CFG_ALLOW_S3`` environment variable needs to be set to ``YES``.

Open DataCube Database Connection
---------------------------------

The preferred method of configuring the ODC database is with the ``$ODC_DEFAULT_DB_URL``
environment variable. The format of postgres connection URL is::

    postgresql://<username>:<password>@<hostname>:<port>/<database>

If you are using an ODC environment other than ``default`` or are using multiple ODC environments,
you can specify the url for other environments in the same fashion, e.g. for environment ``myenv``
use ``$ODC_MYENV_DB_URL``.

If you want to use a ``postgis`` based ODC index, you should also specify the index driver by
setting e.g. ``$ODC_MYENV_INDEX_DRIVER`` to ``postgis``.

Other valid methods for configuring an OpenDatacube instance (e.g. a ``.datacube.conf`` file)
should also work.  Note that OWS currently only works with legacy/postgres index driver.
Postgis support is hopefully coming soon.

The old `$DB_HOSTNAME`, `$DB_DATABASE` etc. environment variables are now STRONGLY DEPRECATED as they
only work in a single-index environment.

An ODC environment other than ``default`` can be used by setting the ``env`` option in the global OWS
configuration.

For Running Integration Tests
-----------------------------

The integration tests need to be able to call a running a OWS server connected the test database
and running the version of the OWS codebase being tested.

SERVER_URL:
    The URL of the test server.  Defaults to ``http://localhost:5000``

SERVER_DB_USERNAME:
    This is the database username used by the test server to connect to the test database.  Defaults to
    the same database username being used by the integration tests themselves.

Note that ``docker-compose`` arrangement used for integration testing on github


Configuring AWS Access
----------------------

Environment variables for AWS access are mostly read through the boto3 library - please
refer to their documentation for details.

Of particular note are:

AWS_DEFAULT_REGION:
    S3 access by datacube_ows will be disabled unless this is set.

AWS_NO_SIGN_REQUEST:
    S3 access will be unsigned if this environment variable is set
    to "y", "t", "yes", "true" or "1".

    If requests are signed then you will also need to ensure that
    boto3 has access to appropriate AWS credentials - typically
    the ``$AWS_ACCESS_KEY_ID`` and ``$AWS_SECRET_ACCESS_KEY`` environment
    variables.

    N.B. Signed requests are the default behaviour - explicitly
    set ``$AWS_NO_SIGN_REQUEST`` to 'yes' to use unsigned request.
    The default behaviour for this variable changed in version 1.8.17.

AWS_REQUEST_PAYER:
    Set to "requester" if accessing requester-pays S3 buckets.
    Default behaviour is to prevent access to requester-pays buckets.

AWS_S3_ENDPOINT:
    Set to the DNS host name of the S3 endpoint.  Required for accessing
    non-Amazon implementations of the S3 protocol, and for some newer AWS regions
    (e.g. Africa).

Configuring Flask
-----------------

Datacube_ows uses the
`Flask web application framework <https://palletsprojects.com/p/flask>`_
which can read from several environment variables, most notably:

FLASK_APP:
      Should point to the ``datacube_ows/ogc.py`` file in your deployment.

The ``$FLASK_ENV`` environment variable also has a significant
effect on the way datacube_ows runs. Refer to the Flask documentation
for further details.

Dev-ops Tools
-------------

The following deployment tools are configured via environment variables:

SENTRY_DSN:
    The `Sentry application monitoring and error tracking system`_
    system is activated and configured with the ``$SENTRY_DSN``
    environment variables.

prometheus_multiproc_dir:
    The `Prometheus event monitoring system <https://prometheus.io>`_ is activated by
    setting this lower case environment variable.

PROXY_FIX:
    If ``$PROXY_FIX`` is set to "true", "yes", "on" or "1", the Flask application will trust the
    X-Forwarded-For and other headers from a proxy server.

    This is useful when running behind a reverse proxy server such as Nginx or CloudFront.

    NEVER use in production without a reverse proxy server.

Dev Tools
---------

PYDEV_DEBUG:
    If set to anything other than "n", "f", "no" or "false" (case insensitive), activates PyDev remote debugging.

    NEVER use in production.

DEFER_CFG_PARSE:
    If set, the configuration file is not read and parsed at startup.  This
    is mostly useful for creating test fixtures.

Docker and Docker-compose
-------------------------

The provided ``Dockerfile`` and ``docker-compose.yaml`` read additional
environment variables at build time.  Please refer to the :doc:`README <readme>`
for further details.

Environment variables exclusive for docker-compose
--------------------------------------------------

OWS_CFG_DIR:
    path to a folder containing ows config files anywhere on the local machine

OWS_CFG_MOUNT_DIR:
    path the OWS_CFG_FOLDER will mount to inside docker container

PYTHONPATH:
    PYTHONPATH to ows config file

POSTGRES_DB:
POSTGRES_USER:
POSTGRES_PASSWORD:
    The db superuser name and password for the postgis database container.
    If multiple databases are required, use a comma-separated list of database names

POSTGRES_HOSTNAME:
    The name of the database server/container.

READY_PROBE_DB:
    The (single) database to use for the startup database readiness probe.  Should be set to one of the
    values in ``$POSTGRES_DB``
