#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0


from setuptools import find_packages, setup

install_requirements = [
    'datacube[performance,s3]>=1.9.0-rc11',
    'flask',
    'requests',
    'affine',
    'click',
    'colour',
    'fsspec',
    'lxml',
    'deepdiff',
    'importlib_metadata',
    'matplotlib',
    'pyparsing',
    'antimeridian',
    'numpy>=1.22',
    'scipy',
    'Pillow>=10.2.0',
    'Babel',
    'Flask-Babel>3.0.0',   # New API in 3.x, bug in 3.0.0
    'psycopg2',
    'python_dateutil',
    'pytz',
    'rasterio>=1.3.2',
    'regex',
    'timezonefinder',
    'python_slugify',
    'geoalchemy2',
    'lark',
    'xarray',
    'pyows',
    'prometheus_flask_exporter',
    'setuptools_scm'
]

test_requirements = [
    'pytest', 'pytest_cov', 'pytest_localserver',
    'owslib>0.29.2',
    'pytest_mock', 'pep8',
    'pytest-helpers-namespace', 'flask-cors',
    'fsspec',
]

dev_requirements = [
    'pydevd-pycharm~=242.23339.19',
    'pylint',
    'sphinx_click',
    'pre-commit',
    'mypy',
    'flake8',
    'types-pytz',
    'types-python-dateutil',
    'types-requests',
]

operational_requirements = [
    "gunicorn>=22.0.0", "gunicorn[gevent]", "gevent", "prometheus_client", "sentry_sdk",
    "prometheus_flask_exporter", "blinker"
]
setup_requirements = ['setuptools_scm', 'setuptools']

extras = {
    "dev": dev_requirements + test_requirements + operational_requirements,
    "test": test_requirements,
    "ops": operational_requirements,
    "setup": setup_requirements,
    "all": dev_requirements + test_requirements + operational_requirements,
}

#  Dropped requirements: ruamel.yaml, bottleneck, watchdog

setup(
    name='datacube_ows',
    description="Open Data Cube Open Web Services",
    long_description="""
============
datacube-ows
============

Open Web Services for the Open Datacube.

* Free software: Apache Software License 2.0
* Documentation: https://datacube-ows.readthedocs.io.

Features
--------

* Leverages the power of the Open Data Cube, including support for COGs on S3.
* Supports WMS and WMTS.
* Experimental support for WCS (1.0, 2.0, 2.1).

    """,
    author="Open Data Cube",
    author_email='earth.observation@ga.gov.au',
    url='https://github.com/opendatacube/datacube-ows',
    entry_points={
        'console_scripts': [
            'datacube-ows = datacube_ows.wsgi:main',
            'datacube-ows-update = datacube_ows.update_ranges_impl:main',
            'datacube-ows-cfg = datacube_ows.cfg_parser_impl:main'
        ],
        "datacube_ows.plugins.index": [
            'postgres = datacube_ows.index.postgres.api:ows_index_driver_init',
            'postgis = datacube_ows.index.postgis.api:ows_index_driver_init',
        ]
    },
    python_requires=">=3.10.0",
    packages=find_packages(exclude=["tests", "tests.cfg", "integration_tests", "integration_tests.cfg"]),
    include_package_data=True,
    install_requires=install_requirements,
    license="Apache Software License 2.0",
    zip_safe=False,
    keywords='datacube, wms, wcs',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3.10',
    ],
    setup_requires=setup_requirements,
    use_scm_version={
        "version_scheme": "post-release",
    },
    test_suite='tests',
    tests_require=test_requirements,
    extras_require=extras
)
