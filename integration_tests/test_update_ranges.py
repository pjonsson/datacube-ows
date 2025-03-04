# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0

"""Test update ranges on DB using Click testing
https://click.palletsprojects.com/en/7.x/testing/
"""
from datacube_ows.update_ranges_impl import main


def test_update_ranges_schema_without_roles(runner):
    result = runner.invoke(main, ["--schema"])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0
    result = runner.invoke(main, ["-E", "owspostgis", "--schema"])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0


def test_update_ranges_schema_with_roles(runner, read_role_name, write_role_name):
    result = runner.invoke(main, ["--schema", "--read-role", read_role_name, "--write-role", write_role_name])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0
    result = runner.invoke(main, ["-E", "owspostgis",
                                  "--schema", "--read-role", read_role_name, "--write-role", write_role_name])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0
    result = runner.invoke(main, ["-E", "nonononodontcallanenviornmentthis",
                                  "--schema", "--read-role", read_role_name, "--write-role", write_role_name])
    assert "Unable to connect to the nonono" in result.output
    assert result.exit_code == 1


def test_update_ranges_roles_only(runner, read_role_name, write_role_name):
    result = runner.invoke(main, ["--read-role", read_role_name, "--write-role", write_role_name])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0
    result = runner.invoke(main, ["-E", "owspostgis", "--read-role", read_role_name, "--write-role", write_role_name])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0


def test_update_ranges_cleanup(runner):
    result = runner.invoke(main, ["--cleanup"])
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert "Cannot find SQL resource" not in result.output
    assert result.exit_code == 0


def test_update_ranges_views(runner):
    result = runner.invoke(main, ["--views"])
    assert "Cannot find SQL resource" not in result.output
    assert "appear to be missing" not in result.output
    assert "Insufficient Privileges" not in result.output
    assert result.exit_code == 0


def test_update_version(runner):
    result = runner.invoke(main, ["--version"])
    assert "Open Data Cube Open Web Services (datacube-ows) version" in result.output
    assert result.exit_code == 0


def test_update_ranges_product(runner, product_name):
    result = runner.invoke(main, [product_name])
    assert "ERROR" not in result.output
    assert result.exit_code == 0


def test_update_ranges_bad_product(runner, product_name):
    result = runner.invoke(main, ["not_a_real_product_name"])
    assert "not_a_real_product_name" in result.output
    assert "does not exist in the OWS configuration - skipping" in result.output
    assert result.exit_code == 1


def test_update_ranges(runner):
    result = runner.invoke(main)
    assert "ERROR" not in result.output
    assert result.exit_code == 0
