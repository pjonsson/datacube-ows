# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0

import math
from datetime import datetime, date

import numpy
import regex as re
from affine import Affine
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
from matplotlib import pyplot as plt
from odc.geo import geom
from pytz import utc
from rasterio.warp import Resampling

from datacube_ows.config_utils import ConfigException
from datacube_ows.ogc_exceptions import WMSException
from datacube_ows.ogc_utils import create_geobox
from datacube_ows.ows_configuration import OWSNamedLayer, get_config
from datacube_ows.resource_limits import RequestScale
from datacube_ows.styles import StyleDef
from datacube_ows.styles.expression import ExpressionException
from datacube_ows.utils import default_to_utc, find_matching_date


RESAMPLING_METHODS = {
    'nearest': Resampling.nearest,
    'cubic': Resampling.cubic,
    'bilinear': Resampling.bilinear,
    'cubic_spline': Resampling.cubic_spline,
    'lanczos': Resampling.lanczos,
    'average': Resampling.average,
}


def _bounding_pts(minx: int, miny: int, maxx: int, maxy: int, src_crs, dst_crs=None):
    # pylint: disable=too-many-locals
    p1 = geom.point(minx, maxy, src_crs)
    p2 = geom.point(minx, miny, src_crs)
    p3 = geom.point(maxx, maxy, src_crs)
    p4 = geom.point(maxx, miny, src_crs)

    conv = dst_crs is not None
    gp1 = p1.to_crs(dst_crs) if conv else p1
    gp2 = p2.to_crs(dst_crs) if conv else p2
    gp3 = p3.to_crs(dst_crs) if conv else p3
    gp4 = p4.to_crs(dst_crs) if conv else p4

    minx = min(gp1.points[0][0], gp2.points[0][0], gp3.points[0][0], gp4.points[0][0])
    maxx = max(gp1.points[0][0], gp2.points[0][0], gp3.points[0][0], gp4.points[0][0])
    miny = min(gp1.points[0][1], gp2.points[0][1], gp3.points[0][1], gp4.points[0][1])
    maxy = max(gp1.points[0][1], gp2.points[0][1], gp3.points[0][1], gp4.points[0][1])

    # miny-maxy for negative scale factor and maxy in the translation, includes inversion of Y axis.

    return minx, miny, maxx, maxy


def _get_geobox_xy(args, crs):
    if get_config().published_CRSs[str(crs)]["vertical_coord_first"]:
        miny, minx, maxy, maxx = map(float, args['bbox'].split(','))
    else:
        minx, miny, maxx, maxy = map(float, args['bbox'].split(','))
    return minx, miny, maxx, maxy


def _get_geobox(args, crs):
    width = int(args['width'])
    height = int(args['height'])
    minx, miny, maxx, maxy = _get_geobox_xy(args, crs)

    if minx == maxx or miny == maxy:
        raise WMSException("Bounding box must enclose a non-zero area")

    if crs.epsg == 3857 and (maxx < -13_000_000 or minx > 13_000_000):
            # EPSG:3857 query AND closer to the anti-meridian than the prime meridian:
            # re-project to epsg:3832 (Pacific Web-Mercator)
            ll = geom.point(x=minx, y=miny, crs=crs).to_crs("epsg:3832")
            ur = geom.point(x=maxx, y=maxy, crs=crs).to_crs("epsg:3832")
            minx, miny = ll.coords[0]
            maxx, maxy = ur.coords[0]
            crs = geom.CRS("epsg:3832")

    return create_geobox(
        crs,
        minx, miny, maxx, maxy,
        width, height
    )


def _get_polygon(args, crs):
    minx, miny, maxx, maxy = _get_geobox_xy(args, crs)
    poly = geom.polygon([(minx, maxy), (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)], crs)
    return poly


def zoom_factor(args, crs):
    # Determine the geographic "zoom factor" for the request.
    # (Larger zoom factor means deeper zoom.  Smaller zoom factor means larger area.)
    # Extract request bbox and crs
    width = int(args['width'])
    height = int(args['height'])
    minx, miny, maxx, maxy = _get_geobox_xy(args, crs)

    # Project to a geographic coordinate system
    # This is why we can't just use the regular geobox.  The scale needs to be
    # "standardised" in some sense, not dependent on the CRS of the request.
    # TODO: can we do better in polar regions?
    minx, miny, maxx, maxy = _bounding_pts(
        minx, miny,
        maxx, maxy,
        crs, dst_crs="epsg:4326"
    )
    # Create geobox affine transformation (N.B. Don't need an actual Geobox)
    affine = Affine.translation(minx, miny) * Affine.scale((maxx - minx) / width, (maxy - miny) / height)
    # Zoom factor is the reciprocal of the square root of the transform determinant
    # (The determinant is x scale factor multiplied by the y scale factor)
    return 1.0 / math.sqrt(affine.determinant)


def img_coords_to_geopoint(geobox, i, j):
    cfg = get_config()
    h_coord = cfg.published_CRSs[str(geobox.crs)]["horizontal_coord"]
    v_coord = cfg.published_CRSs[str(geobox.crs)]["vertical_coord"]
    return geom.point(geobox.coordinates[h_coord].values[int(i)],
                          geobox.coordinates[v_coord].values[int(j)],
                          geobox.crs)


def get_layer_from_arg(args, argname="layers") -> OWSNamedLayer:
    layers = args.get(argname, "").split(",")
    if len(layers) != 1:
        raise WMSException("Multi-layer requests not supported")
    lyr = layers[0]
    layer_chunks = lyr.split("__")
    lyr = layer_chunks[0]
    cfg = get_config()
    layer = cfg.layer_index.get(lyr)
    if not layer:
        raise WMSException("Layer %s is not defined" % lyr,
                           WMSException.LAYER_NOT_DEFINED,
                           locator="Layer parameter",
                           valid_keys=list(cfg.layer_index))
    return layer


def get_arg(args, argname, verbose_name, lower=False,
            errcode=None, permitted_values=None):
    fmt = args.get(argname, "")
    if lower:
        fmt = fmt.lower()
    if not fmt:
        raise WMSException("No %s specified" % verbose_name,
                           errcode,
                           locator="%s parameter" % argname,
                           valid_keys=permitted_values)

    if permitted_values:
        if fmt not in permitted_values:
            raise WMSException("%s %s is not supported" % (verbose_name, fmt),
                               errcode,
                               locator="%s parameter" % argname,
                               valid_keys=permitted_values)
    return fmt


def get_times_for_layer(layer: OWSNamedLayer) -> list[datetime | date]:
    return layer.ranges.times


def get_times(args, layer: OWSNamedLayer) -> list[datetime | date]:
    # Time parameter
    times_raw = args.get('time', '')
    times = times_raw.split(',')

    return list([parse_time_item(item, layer) for item in times])


def parse_time_item(item: str, layer: OWSNamedLayer) -> datetime | date:
    times = item.split('/')
    # Time range handling follows the implementation described by GeoServer
    # https://docs.geoserver.org/stable/en/user/services/wms/time.html

    # If all times are equal we can proceed
    if len(times) > 1:
        # TODO WMS Time range selections (/ notation) are poorly and incompletely implemented.
        start, end = parse_wms_time_strings(times, with_tz=layer.time_resolution.is_subday())
        if layer.time_resolution.is_subday():
            matching_times: list[datetime | date] = [t for t in layer.ranges.times if start <= t <= end]
        else:
            start, end = start.date(), end.date()
            matching_times = [t for t in layer.ranges.times if start <= t <= end]
        if matching_times:
            # default to the first matching time
            return matching_times[0]
        elif layer.regular_time_axis:
            raise WMSException(
                "No data available for time dimension range '%s'-'%s' for this layer" % (start, end),
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
        else:
            raise WMSException(
                "Time dimension range '%s'-'%s' not valid for this layer" % (start, end),
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
    elif not times[0]:
        # default to last available time if not supplied.
        product_times = get_times_for_layer(layer)
        return product_times[-1]
    try:
        time = parse(times[0])
        if not layer.time_resolution.is_subday():
            time = time.date()  # type: ignore[assignment]
    except ValueError:
        raise WMSException(
            "Time dimension value '%s' not valid for this layer" % times[0],
            WMSException.INVALID_DIMENSION_VALUE,
            locator="Time parameter")

    # Validate time parameter for requested layer.
    if layer.regular_time_axis:
        start, end = layer.time_range()
        if time < start:
            raise WMSException(
                "Time dimension value '%s' not valid for this layer" % times[0],
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
        if time > end:
            raise WMSException(
                "Time dimension value '%s' not valid for this layer" % times[0],
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
        if (time - start).days % layer.time_axis_interval != 0:
            raise WMSException(
                "Time dimension value '%s' not valid for this layer" % times[0],
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
    elif layer.time_resolution.is_subday():
        if not find_matching_date(time, layer.ranges.times):
            raise WMSException(
                "Time dimension value '%s' not valid for this layer" % times[0],
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
    else:
        if time not in layer.ranges.time_set:
            raise WMSException(
                "Time dimension value '%s' not valid for this layer" % times[0],
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
    return time


def parse_time_delta(delta_str):
    pattern = (r'P((?P<years>\d+)Y)?((?P<months>\d+)M)?((?P<days>\d+)D)?'
               r'(T(((?P<hours>\d+)H)?((?P<minutes>\d+)M)?((?P<seconds>\d+)S)?)?)?')
    parts = re.search(pattern, delta_str).groupdict()
    return relativedelta(**{k: float(v) for k, v in parts.items() if v is not None})


def parse_wms_time_string(t, start=True):
    if t.upper() == 'PRESENT':
        return datetime.utcnow()
    elif t.startswith('P'):
        return parse_time_delta(t)
    else:
        default = datetime(1970, 1, 1) if start else datetime(1970, 12, 31, 23, 23, 59, 999999)  # default year ignored
        return parse(t, default=default)


def parse_wms_time_strings(parts, with_tz=False):
    start = parse_wms_time_string(parts[0])
    end = parse_wms_time_string(parts[-1], start=False)

    a_tiny_bit = relativedelta(microseconds=1)
    # Follows GeoServer https://docs.geoserver.org/stable/en/user/services/wms/time.html#reduced-accuracy-times

    if isinstance(start, relativedelta):
        if isinstance(end, relativedelta):
            raise WMSException(
                "Could not understand time value '%s'" % parts,
                WMSException.INVALID_DIMENSION_VALUE,
                locator="Time parameter")
        fuzzy_end = parse_wms_time_string(parts[-1], start=True)
        return fuzzy_end - start + a_tiny_bit, end
    if isinstance(end, relativedelta):
        end = start + end - a_tiny_bit
    if with_tz:
        start = default_to_utc(start)
        end = default_to_utc(end)
    return start, end


class GetParameters():
    def __init__(self, args):
        self.cfg = get_config()
        # Version
        self.version = get_arg(args, "version", "WMS version",
                               permitted_values=['1.1.1', '1.3.0'])
        # CRS
        if self.version == '1.1.1':
            crs_arg = "srs"
        else:
            crs_arg = "crs"
        self.crsid = get_arg(args, crs_arg, "Coordinate Reference System",
                             errcode=WMSException.INVALID_CRS,
                             permitted_values=list(self.cfg.published_CRSs))
        self.crs = self.cfg.crs(self.crsid)
        # Layers
        self.layer = self.get_layer(args)

        self.geometry = _get_polygon(args, self.crs)
        # BBox, height and width parameters
        self.geobox = _get_geobox(args, self.crs)
        # Web-merc antimeridian hack:
        if self.geobox.crs != self.crs:
            self.crs = self.geobox.crs
            self.geometry = self.geometry.to_crs(self.crs)

        # Time parameter
        self.times = get_times(args, self.layer)

        self.method_specific_init(args)

    def method_specific_init(self, args):
        pass

    def get_layer(self, args) -> OWSNamedLayer:
        return get_layer_from_arg(args)


def single_style_from_args(layer, args, required=True):
    # User Band Math (overrides style if present).
    if layer.user_band_math and "code" in args and "colorscheme" in args:
        code = args["code"]
        mpl_ramp = args["colorscheme"]
        try:
            plt.get_cmap(mpl_ramp)
        except:
            raise WMSException(f"Invalid Matplotlib ramp name: {mpl_ramp}",
                               locator="Colorscalerange parameter")
        colorscalerange = args.get("colorscalerange", "0,1").split(",")
        if len(colorscalerange) != 2:
            raise WMSException(f"Colorscale range must be two numbers, sorted and separated by a comma.",
                               locator="Colorscalerange parameter")
        try:
            colorscalerange = [float(r) for r in colorscalerange]
        except ValueError:
            raise WMSException(f"Colorscale range must be two numbers, sorted and separated by a comma.",
                               locator="Colorscalerange parameter")
        if colorscalerange[0] >= colorscalerange[1]:
            raise WMSException(f"Colorscale range must be two numbers, sorted and separated by a comma.",
                               locator="Colorscalerange parameter")
        try:
            style = StyleDef(layer, {
                "name": "custom_user_style",
                "index_expression": code,
                "mpl_ramp": mpl_ramp,
                "range": colorscalerange,
                "legend": {
                    "title": "User-Custom Index",
                    "show_legend": True,
                    "begin": str(colorscalerange[0]),
                    "end": str(colorscalerange[1]),
                }
            }, stand_alone=True, user_defined=True)
        except ExpressionException as e:
            raise WMSException(f"Code expression invalid: {e}",
                               locator="Code parameter")
        except ConfigException as e:
            raise WMSException(f"Code invalid: {e}",
                               locator="Code parameter")
    else:
        # Regular WMS Styles
        styles = args.get("styles", "").split(",")
        if len(styles) != 1:
            raise WMSException("Multi-layer GetMap requests not supported")
        style_r = styles[0]
        if not style_r and not required:
            return None
        if not style_r:
            style_r = layer.default_style.name
        style = layer.style_index.get(style_r)
        if not style:
            raise WMSException("Style %s is not defined" % style_r,
                               WMSException.STYLE_NOT_DEFINED,
                               locator="Style parameter",
                               valid_keys=list(layer.style_index))
    return style

class GetLegendGraphicParameters():
    def __init__(self, args):
        self.layer = get_layer_from_arg(args, 'layer')

        # Validate Format parameter
        self.format = get_arg(args, "format", "image format",
                              errcode=WMSException.INVALID_FORMAT,
                              lower=True,
                              permitted_values=["image/png"])
        self.style = single_style_from_args(self.layer, args)
        self.styles = [self.style]
        # Time parameter
        self.times = get_times(args, self.layer)


class GetMapParameters(GetParameters):
    def method_specific_init(self, args):
        # Validate Format parameter
        self.format = get_arg(args, "format", "image format",
                              errcode=WMSException.INVALID_FORMAT,
                              lower=True,
                              permitted_values=["image/png"])

        self.style = single_style_from_args(self.layer, args)
        cfg = get_config()
        if self.geobox.width > cfg.wms_max_width:
            raise WMSException(f"Width {self.geobox.width} exceeds supported maximum {self.cfg.wms_max_width}.",
                               locator="Width parameter")
        if self.geobox.height > cfg.wms_max_height:
            raise WMSException(f"Width {self.geobox.height} exceeds supported maximum {self.cfg.wms_max_height}.",
                               locator="Height parameter")

        # Zoom factor
        self.zf = zoom_factor(args, self.crs)

        self.ows_stats = bool(args.get("ows_stats"))

        # TODO: Do we need to make resampling method configurable?
        self.resampling = Resampling.nearest

        self.resources = RequestScale(
            native_crs=geom.CRS(self.layer.native_CRS),
            native_resolution=(self.layer.resolution_x, self.layer.resolution_y),
            geobox=self.geobox,
            n_dates=len(self.times),
            request_bands=self.style.odc_needed_bands(),
        )


class GetFeatureInfoParameters(GetParameters):
    def get_layer(self, args):
        return get_layer_from_arg(args, "query_layers")

    def method_specific_init(self, args):
        # Validate Formata parameter
        self.format = get_arg(args, "info_format", "info format", lower=True,
                              errcode=WMSException.INVALID_FORMAT,
                              permitted_values=["application/json", "text/html"])
        # Point coords
        if self.version == "1.1.1":
            coords = ["x", "y"]
        else:
            coords = ["i", "j"]
        i = args.get(coords[0])
        j = args.get(coords[1])
        if i is None:
            raise WMSException("HorizontalCoordinate not supplied", WMSException.INVALID_POINT,
                               "%s parameter" % coords[0])
        if j is None:
            raise WMSException("Vertical coordinate not supplied", WMSException.INVALID_POINT,
                               "%s parameter" % coords[0])
        self.i = int(i)
        self.j = int(j)
        self.style = single_style_from_args(self.layer, args, required=False)


# Solar angle correction functions
def declination_rad(dt):
    # Estimate solar declination from a datetime.  (value returned in radians).
    # Formula taken from https://en.wikipedia.org/wiki/Position_of_the_Sun#Declination_of_the_Sun_as_seen_from_Earth
    timedel = dt - datetime(dt.year, 1, 1, 0, 0, 0, tzinfo=utc)
    day_count = timedel.days + timedel.seconds / (60.0 * 60.0 * 24.0)
    return -1.0 * math.radians(23.44) * math.cos(2 * math.pi / 365 * (day_count + 10))


def cosine_of_solar_zenith(lat, lon, utc_dt):
    # Estimate cosine of solar zenith angle
    # (angle between sun and local zenith) at requested latitude, longitude and datetime.
    # Formula taken from https://en.wikipedia.org/wiki/Solar_zenith_angle
    utc_seconds_since_midnight = ((utc_dt.hour * 60) + utc_dt.minute) * 60 + utc_dt.second
    utc_hour_deg_angle = (utc_seconds_since_midnight / (60 * 60 * 24) * 360.0) - 180.0
    local_hour_deg_angle = utc_hour_deg_angle + lon
    local_hour_angle_rad = math.radians(local_hour_deg_angle)
    latitude_rad = math.radians(lat)
    solar_decl_rad = declination_rad(utc_dt)
    result = math.sin(latitude_rad) * math.sin(solar_decl_rad) \
             + math.cos(latitude_rad) * math.cos(solar_decl_rad) * math.cos(local_hour_angle_rad)
    return result


def solar_correct_data(data, dataset):
    # Apply solar angle correction to the data for a dataset.
    # See for example http://gsp.humboldt.edu/olm_2015/Courses/GSP_216_Online/lesson4-1/radiometric.html
    native_x = (dataset.bounds.right + dataset.bounds.left) / 2.0
    native_y = (dataset.bounds.top + dataset.bounds.bottom) / 2.0
    pt = geom.point(native_x, native_y, dataset.crs)
    geo_pt = pt.to_crs("epsg:4326")
    data_time = dataset.center_time.astimezone(utc)
    data_lon, data_lat = geo_pt.coords[0]

    csz = cosine_of_solar_zenith(data_lat, data_lon, data_time)

    return data / csz


def wofls_fuser(dest, src):
    where_nodata = (src & 1) == 0
    numpy.copyto(dest, src, where=where_nodata)
    return dest


def item_fuser(dest, src):
    where_combined = numpy.isnan(dest) | (dest == -6666.)
    numpy.copyto(dest, src, where=where_combined)
    return dest
