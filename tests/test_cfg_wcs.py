# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import patch

import pytest

from datacube_ows.config_utils import ConfigException
from datacube_ows.ows_configuration import WCSFormat, parse_ows_layer


def test_zero_grid(minimal_global_cfg, minimal_layer_cfg, minimal_dc, mock_range, empty_driver_cache):
    minimal_global_cfg.wcs = True
    minimal_layer_cfg["native_crs"] = "EPSG:4326"
    minimal_layer_cfg["product_name"] = "foo_nativeres"
    lyr = parse_ows_layer(minimal_layer_cfg,
                          global_cfg=minimal_global_cfg)
    mock_range.bboxes["EPSG:4326"] = {
        "top": 0.1, "bottom": 0.1,
        "left": -0.1, "right": 0.1,
    }
    assert mock_range.bboxes["EPSG:4326"]["bottom"] > 0
    assert not lyr.ready
    with patch("datacube_ows.index.postgres.api.get_ranges_impl") as get_rng:
        get_rng.return_value = mock_range
        with pytest.raises(ConfigException) as excinfo:
            lyr.make_ready(minimal_dc)
            get_rng.assert_called()
    assert not lyr.ready
    assert "but vertical resolution is " in str(excinfo.value)
    assert "a_layer" in str(excinfo.value)
    assert "EPSG:4326" in str(excinfo.value)
    minimal_global_cfg.layer_index = {}
    lyr = parse_ows_layer(minimal_layer_cfg,
                          global_cfg=minimal_global_cfg)
    mock_range.bboxes["EPSG:4326"] = {
        "top": 0.1, "bottom": -0.1,
        "left": -0.1, "right": -0.1,
    }
    with patch("datacube_ows.index.postgres.api.get_ranges_impl") as get_rng:
        get_rng.return_value = mock_range
        with pytest.raises(ConfigException) as excinfo:
            lyr.make_ready(minimal_dc)
    assert "but horizontal resolution is " in str(excinfo.value)
    assert "a_layer" in str(excinfo.value)
    assert "EPSG:4326" in str(excinfo.value)


def test_wcs_renderer_detection():
    fmt = WCSFormat(
        "GeoTIFF",
        "image/geotiff",
        "tif",
        {
            "1": "datacube_ows.wcs1_utils.get_tiff",
            "2": "datacube_ows.wcs2_utils.get_tiff",
        },
        False
    )
    r = fmt.renderer("2.1.0")
    assert r == fmt.renderers[2]
