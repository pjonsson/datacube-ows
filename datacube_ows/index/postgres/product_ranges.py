# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import math
import click
from datetime import date, datetime, timezone
from typing import cast, Callable

import datacube
import odc.geo
import sqlalchemy.exc
from psycopg2.extras import Json
from sqlalchemy import text

from odc.geo.geom import Geometry

from datacube_ows.ows_configuration import OWSNamedLayer
from datacube_ows.index.postgres.mv_index import MVSelectOpts, mv_search
from datacube_ows.utils import get_sqlconn
from datacube_ows.index.api import CoordRange, LayerSignature, LayerExtent

_LOG = logging.getLogger(__name__)


def jsonise_bbox(bbox: odc.geo.geom.BoundingBox) -> dict[str, float]:
    if isinstance(bbox, dict):
        return bbox
    else:
        return {
            "top": bbox.top,
            "bottom": bbox.bottom,
            "left": bbox.left,
            "right": bbox.right,
        }


def create_range_entry(layer: OWSNamedLayer, cache: dict[LayerSignature, list[str]]) -> None:
    meta = LayerSignature(time_res=layer.time_resolution.value,
                          products=tuple(layer.product_names),
                          env=layer.local_env._name,
                          datasets=layer.dc.index.datasets.count(product=layer.product_names))

    click.echo(f"Postgres Updating range for layer {layer.name}")
    click.echo(f"(signature: {meta.as_json()!r})")
    conn = get_sqlconn(layer.dc)
    txn = conn.begin()
    if meta in cache:
        template = cache[meta][0]
        click.echo(f"Layer {template} has same signature - reusing")
        cache[meta].append(layer.name)
        try:
            conn.execute(text("""
            INSERT INTO ows.layer_ranges
                (layer, lat_min, lat_max, lon_min, lon_max, dates, bboxes, meta, last_updated)
            SELECT :layer_id, lat_min, lat_max, lon_min, lon_max, dates, bboxes, meta, last_updated
            FROM ows.layer_ranges lr2
            WHERE lr2.layer = :template_id"""),
                         {
                                      "layer_id": layer.name,
                                      "template_id": template
                                   })
        except sqlalchemy.exc.IntegrityError:
            conn.execute(text("""
            UPDATE ows.layer_ranges lr1
            SET lat_min = lr2.lat_min,
                lat_max = lr2.lat_max,
                lon_min = lr2.lon_min,
                lon_max = lr2.lon_max,
                dates = lr2.dates,
                bboxes = lr2.bboxes,
                meta = lr2.meta,
                last_updated = lr2.last_updated
            FROM ows.layer_ranges lr2
            WHERE lr1.layer = :layer_id
            AND   lr2.layer = :template_id"""),
                         {
                             "layer_id": layer.name,
                             "template_id": template
                         })
    else:
        # insert empty row if one does not already exist
        conn.execute(text("""
        INSERT INTO ows.layer_ranges
        (layer, lat_min, lat_max, lon_min, lon_max, dates, bboxes, meta, last_updated)
        VALUES
        (:p_layer, 0, 0, 0, 0, :empty, :empty, :meta, :now)
        ON CONFLICT (layer) DO NOTHING
        """),
        {
            "p_layer": layer.name, "empty": Json(""),
            "meta": Json(meta.as_json()), "now": datetime.now(tz=timezone.utc)
        })

        prodids = [p.id for p in layer.products]
        # Update min/max lat/longs
        conn.execute(text(
          """
          UPDATE ows.layer_ranges lr
          SET lat_min = st_ymin(subq.bbox),
              lat_max = st_ymax(subq.bbox),
              lon_min = st_xmin(subq.bbox),
              lon_max = st_xmax(subq.bbox)
          FROM (
            SELECT st_extent(stv.spatial_extent) as bbox
            FROM ows.space_time_view stv
            WHERE stv.dataset_type_ref = ANY(:prodids)
          ) as subq
          WHERE lr.layer = :layer_id
          """),
          {"layer_id": layer.name, "prodids": prodids})

        # Set default timezone
        conn.execute(text("""set timezone to 'Etc/UTC'"""))

        # Loop over dates
        dates = set()  # Should get to here!
        if layer.time_resolution.is_solar():
          results = conn.execute(text(
              """
              select
                    lower(temporal_extent), upper(temporal_extent),
                    ST_X(ST_Centroid(spatial_extent))
              from ows.space_time_view
              WHERE dataset_type_ref = ANY(:prodids)
              """),
              {"prodids": prodids})
          for result in results:
              dt1, dt2, lon = result
              dt = dt1 + (dt2 - dt1) / 2
              dt = dt.astimezone(timezone.utc)

              solar_day = datacube.api.query._convert_to_solar_time(dt, lon).date()
              dates.add(solar_day)
        else:
          results = conn.execute(text(
              """
              select
                    array_agg(temporal_extent)
              from ows.space_time_view
              WHERE dataset_type_ref = ANY(:prodids)
              """),
              {"prodids": prodids}
          )
          for result in results:
              for dat_ran in result[0]:
                  dates.add(dat_ran.lower)

        if layer.time_resolution.is_subday():
          date_formatter = lambda d: d.isoformat()
        else:
          date_formatter = lambda d: d.strftime("%Y-%m-%d")

        dates = sorted(dates)
        conn.execute(text("""
           UPDATE ows.layer_ranges
           SET dates = :dates
           WHERE layer= :layer_id
        """),
                   {
                       "dates": Json(list(map(date_formatter, dates))),
                       "layer_id": layer.name
                   }
        )
        # calculate bounding boxes
        # Get extent polygon from materialised views

        extent_4386 = cast(Geometry, mv_search(layer.dc.index, MVSelectOpts.EXTENT, products=layer.products))

        all_bboxes = bbox_projections(extent_4386, layer.global_cfg.crses)

        conn.execute(text("""
        UPDATE ows.layer_ranges
        SET bboxes = :bbox
        WHERE layer = :layer_id
        """), {
        "bbox": Json(all_bboxes),
        "layer_id": layer.name})

        cache[meta] = [layer.name]

    txn.commit()
    conn.close()


def bbox_projections(starting_box: odc.geo.Geometry, crses: dict[str, odc.geo.CRS]) -> dict[str, dict[str, float]]:
   result = {}
   for crsid, crs in crses.items():
       if crs.valid_region is not None:
           clipped_crs_region = (starting_box & crs.valid_region)
           if clipped_crs_region.wkt == 'POLYGON EMPTY':
               continue
           clipped_crs_bbox = clipped_crs_region.to_crs(crs).boundingbox
       else:
           clipped_crs_bbox = None
       if clipped_crs_bbox is not None:
           result[crsid] = jsonise_bbox(clipped_crs_bbox)
       else:
           projbbox = starting_box.to_crs(crs).boundingbox
           result[crsid] = sanitise_bbox(projbbox)
   return result


def sanitise_bbox(bbox: odc.geo.geom.BoundingBox) -> dict[str, float]:
    def sanitise_coordinate(coord: float, fallback: float) -> float:
        return coord if math.isfinite(coord) else fallback
    return {
        "top": sanitise_coordinate(bbox.top, float("9.999999999e99")),
        "bottom": sanitise_coordinate(bbox.bottom, float("-9.999999999e99")),
        "left": sanitise_coordinate(bbox.left, float("-9.999999999e99")),
        "right": sanitise_coordinate(bbox.right, float("9.999999999e99")),
    }


def get_ranges(layer: OWSNamedLayer) -> LayerExtent | None:
    cfg = layer.global_cfg
    conn = get_sqlconn(layer.dc)
    results = conn.execute(text("""
        SELECT *
        FROM ows.layer_ranges
        WHERE layer=:pname"""),
                           {"pname": layer.name}
                          )

    for result in results:
        conn.close()
        if layer.time_resolution.is_subday():
            dt_parser: Callable[[str], datetime | date] = lambda dts: datetime.fromisoformat(dts)
        else:
            dt_parser = lambda dts: datetime.strptime(dts, "%Y-%m-%d").date()
        times = [dt_parser(d) for d in result.dates if d is not None]
        if not times:
            return None
        return LayerExtent(
            lat=CoordRange(min=float(result.lat_min), max=float(result.lat_max)),
            lon=CoordRange(min=float(result.lon_min), max=float(result.lon_max)),
            times=times,
            bboxes=result.bboxes
        )
    return None
