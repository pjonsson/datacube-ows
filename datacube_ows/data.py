from __future__ import absolute_import

import json
from datetime import datetime

import numpy
import numpy.ma
import xarray
from rasterio.io import MemoryFile
from rasterio.warp import Resampling
from skimage.draw import polygon as skimg_polygon
from itertools import chain
import re

import datacube
from datacube.utils import geometry
from datacube.utils.masking import mask_to_dict

from datacube_ows.cube_pool import cube
from datacube_ows.ogc_exceptions import WMSException

from datacube_ows.ows_configuration import get_config
from datacube_ows.query_profiler import QueryProfiler
from datacube_ows.wms_utils import img_coords_to_geopoint, GetMapParameters, \
    GetFeatureInfoParameters, solar_correct_data
from datacube_ows.ogc_utils import dataset_center_time, ConfigException, tz_for_geometry, \
    solar_date
from datacube_ows.mv_index import MVSelectOpts, mv_search
from datacube_ows.utils import log_call

import logging

_LOG = logging.getLogger(__name__)

class ProductBandQuery:
    def __init__(self, products, bands, main=False, manual_merge=False, fuse_func=None):
        self.products = products
        self.bands = bands
        self.manual_merge = manual_merge
        self.fuse_func = fuse_func
        self.main = main
        self.key = (
            tuple((p.id for p in self.products)),
            tuple(bands)
        )

    def __str__(self):
        return f"Query bands {self.bands} from products {self.products}"

    def  __hash__(self):
        return hash(self.key)

    @classmethod
    def style_queries(cls, style, resource_limited=False):
        queries = [
            cls.simple_layer_query(style.product, style.needed_bands,
                                   manual_merge=style.product.data_manual_merge,
                                   fuse_func=style.product.fuse_func,
                                   resource_limited=resource_limited)
        ]
        for fp in style.flag_products:
            if fp.products_match(style.product.product_names):
                for band in fp.bands:
                    assert band in style.needed_bands
            else:
                if resource_limited:
                    pq_products = fp.low_res_products
                else:
                    pq_products = fp.products
                queries.append(cls(
                    pq_products,
                    tuple(fp.bands),
                    manual_merge=fp.manual_merge,
                    fuse_func=fp.fuse_func
                ))
        return queries

    @classmethod
    def simple_layer_query(cls, layer, bands, manual_merge=False, fuse_func=None, resource_limited=False):
        if resource_limited:
            main_products = layer.low_res_products
        else:
            main_products = layer.products
        return cls(main_products, bands, manual_merge=manual_merge, main=True, fuse_func=fuse_func)


class DataStacker:
    @log_call
    def __init__(self, product, geobox, times, resampling=None, style=None, bands=None, **kwargs):
        super(DataStacker, self).__init__(**kwargs)
        self._product = product
        self.cfg = product.global_cfg
        self._geobox = geobox
        self._resampling = resampling if resampling is not None else Resampling.nearest
        self.style = style
        if style:
            self._needed_bands = style.needed_bands
        elif bands:
            self._needed_bands = [ self._product.band_idx.band(b) for b in bands ]
        else:
            self._needed_bands = self._product.band_idx.native_bands.index

        self.raw_times = times
        self._times = [
                self._product.search_times(
                        t, self._geobox)
                for t in times
        ]
        self.group_by = self._product.dataset_groupby()
        self.resource_limited = False

    def needed_bands(self):
        return self._needed_bands

    @log_call
    def n_datasets(self, index, all_time=False, point=None):
        return self.datasets(index,
                             all_time=all_time, point=point,
                             mode=MVSelectOpts.COUNT)

    def datasets(self, index,
                 main_only=True,
                 all_time=False, point=None,
                 mode=MVSelectOpts.DATASETS):
        if self.style and not main_only:
            queries = ProductBandQuery.style_queries(
                self.style,
                self.resource_limited
            )
        else:
            queries = [
                ProductBandQuery.simple_layer_query(
                    self._product,
                    self.needed_bands(),
                    self.resource_limited)
            ]
        if point:
            geom = point
        else:
            geom = self._geobox.extent
        if all_time:
            times = None
        else:
            times = self._times
        results = {}
        for query in queries:
            result = mv_search(index,
                               sel=mode,
                               times=times,
                               geom=geom,
                               products=query.products)
            if mode == MVSelectOpts.DATASETS:
                result = datacube.Datacube.group_datasets(result, self.group_by)
            if main_only:
                return result
            results[query] = result
        return results

    @log_call
    def data(self, datasets_by_query, skip_corrections=False):
        # pylint: disable=too-many-locals, consider-using-enumerate
        # datasets is an XArray DataArray of datasets grouped by time.
        data = None
        for pbq, datasets in datasets_by_query.items():
            measurements = pbq.products[0].lookup_measurements(pbq.bands)
            fuse_func = pbq.fuse_func
            if pbq.manual_merge:
                qry_result = self.manual_data_stack(datasets, measurements, pbq.bands, skip_corrections, fuse_func=fuse_func)
            else:
                qry_result = self.read_data(datasets, measurements, self._geobox, self._resampling, fuse_func=fuse_func)
            if data is None:
                data = qry_result
            else:
                for band in pbq.bands:
                    data.assign({
                        band: qry_result[band]
                        for band in pbq.bands
                    })

        return data

    @log_call
    def manual_data_stack(self, datasets, measurements, bands, skip_corrections, fuse_func):
        # pylint: disable=too-many-locals, too-many-branches
        # manual merge
        if self.style:
            flag_bands = set(filter(lambda b: b in self.style.flag_bands, bands))
            non_flag_bands = set(filter(lambda b: b not in self.style.flag_bands, bands))
        else:
            non_flag_bands = bands
            flag_bands = set()
        time_slices = []
        for dt in datasets.time.values:
            tds = datasets.sel(time=dt)
            merged = None
            for ds in tds.values.item():
                d = self.read_data_for_single_dataset(ds, measurements, self._geobox, fuse_func=fuse_func)
                # Squeeze upconverts uints to int32
                d = d.squeeze(["time"], drop=True)
                extent_mask = None
                for band in non_flag_bands:
                    for f in self._product.extent_mask_func:
                        if extent_mask is None:
                            extent_mask = f(d, band)
                        else:
                            extent_mask &= f(d, band)
                dm = d.where(extent_mask)
                if self._product.solar_correction and not skip_corrections:
                    for band in non_flag_bands:
                        dm[band] = solar_correct_data(dm[band], ds)
                if merged is None:
                    merged = dm
                else:
                    merged = merged.combine_first(dm)
            for band in flag_bands:
                # REVISIT: not sure about type converting one band like this?
                merged[band] = merged[band].astype('uint16', copy=True)
                merged[band].attrs = d[band].attrs
            time_slices.append(merged)

        result = xarray.concat(time_slices, datasets.time)
        return result

    # Read data for given datasets and measurements per the output_geobox
    @log_call
    def read_data(self, datasets, measurements, geobox, resampling=Resampling.nearest, fuse_func=None):
        return datacube.Datacube.load_data(
                datasets,
                geobox,
                measurements=measurements,
                fuse_func=fuse_func)

    # Read data for single datasets and measurements per the output_geobox
    @log_call
    def read_data_for_single_dataset(self, dataset, measurements, geobox, resampling=Resampling.nearest, fuse_func=None):
        datasets = [dataset]
        if self._product.is_raw_time_res:
            dc_datasets = datacube.Datacube.group_datasets(datasets, 'solar_day')
        else:
            dc_datasets = datacube.Datacube.group_datasets(datasets, 'time')
        return datacube.Datacube.load_data(
            dc_datasets,
            geobox,
            measurements=measurements,
            fuse_func=fuse_func)


def datasets_in_xarray(xa):
    if xa is None:
        return 0
    return sum(len(xa.values[i]) for i in range(0, len(xa.values)))


def bbox_to_geom(bbox, crs):
    return datacube.utils.geometry.box(bbox.left, bbox.bottom, bbox.right, bbox.top, crs)


@log_call
def get_map(args):
    # pylint: disable=too-many-nested-blocks, too-many-branches, too-many-statements, too-many-locals
    # Parse GET parameters
    params = GetMapParameters(args)
    qprof = QueryProfiler(params.ows_stats)
    n_dates = len(params.times)
    if n_dates == 1:
        mdh = None
    else:
        mdh = params.style.get_multi_date_handler(n_dates)
        if mdh is None:
            raise WMSException("Style %s does not support GetMap requests with %d dates" % (params.style.name, n_dates),
                               WMSException.INVALID_DIMENSION_VALUE, locator="Time parameter")
    qprof["n_dates"] = n_dates
    with cube() as dc:
        if not dc:
            raise WMSException("Database connectivity failure")
        # Tiling.
        stacker = DataStacker(params.product, params.geobox, params.times, params.resampling, style=params.style)
        zoomed_out = params.zf < params.product.min_zoom
        qprof["zoom_factor"] = params.zf
        qprof.start_event("count-datasets")
        n_datasets = stacker.datasets(dc.index, mode=MVSelectOpts.COUNT)
        qprof.end_event("count-datasets")
        qprof["n_datasets"] = n_datasets
        too_many_datasets = (params.product.max_datasets_wms > 0
                             and n_datasets > params.product.max_datasets_wms
                             )
        if qprof.active:
            qprof["datasets"] = stacker.datasets(dc.index, mode=MVSelectOpts.IDS)
        if too_many_datasets or zoomed_out:
            stacker.resource_limited = True
            qprof["too_many_datasets"] = too_many_datasets
            qprof["zoomed_out"] = zoomed_out

        if stacker.resource_limited and not params.product.low_res_product_names:
            qprof.start_event("extent-in-query")
            extent = stacker.datasets(dc.index, mode=MVSelectOpts.EXTENT)
            qprof.end_event("extent-in-query")
            if extent is None:
                qprof["write_action"] = "No extent: Write Empty"
                qprof.start_event("write")
                body = _write_empty(params.geobox)
                qprof.end_event("write")
            else:
                qprof["write_action"] = "Polygon"
                qprof.start_event("write")
                body = _write_polygon(
                    params.geobox,
                    extent,
                    params.product.zoom_fill,
                    params.product)
                qprof.end_event("write")
        elif n_datasets == 0:
            qprof["write_action"] = "No datsets: Write Empty"
            qprof.start_event("write")
            body = _write_empty(params.geobox)
            qprof.end_event("write")
        else:
            if stacker.resource_limited:
                qprof.start_event("count-summary-datasets")
                qprof["n_summary_datasets"] = stacker.datasets(dc.index, mode=MVSelectOpts.COUNT)
                qprof.end_event("count-summary-datasets")
            qprof.start_event("fetch-datasets")
            datasets = stacker.datasets(dc.index, main_only=False)
            for flagband, dss in datasets.items():
                if not dss.any():
                    _LOG.warning("Flag band %s returned no data", str(flagband))
            qprof.end_event("fetch-datasets")
            _LOG.debug("load start %s %s", datetime.now().time(), args["requestid"])
            qprof.start_event("load-data")
            data = stacker.data(datasets)
            qprof.end_event("load-data")
            _LOG.debug("load stop %s %s", datetime.now().time(), args["requestid"])
            qprof.start_event("build-masks")
            td_masks = []
            for npdt in data.time.values:
                td = data.sel(time=npdt)
                td_ext_mask = None
                for band in params.style.needed_bands:
                    if band in params.style.flag_bands:
                        if params.product.data_manual_merge:
                            if td_ext_mask is None:
                                td_ext_mask = ~numpy.isnan(td[band])
                            else:
                                td_ext_mask &= ~numpy.isnan(td[band])
                        else:
                            for f in params.product.extent_mask_func:
                                if td_ext_mask is None:
                                    td_ext_mask = f(td, band)
                                else:
                                    td_ext_mask &= f(td, band)
                if params.product.data_manual_merge:
                    td_ext_mask = xarray.DataArray(td_ext_mask)
                td_masks.append(td_ext_mask)
            extent_mask = xarray.concat(td_masks, dim=data.time)
            qprof.end_event("build-masks")

            if not data:
                qprof["write_action"] = "No Data: Write Empty"
                body = _write_empty(params.geobox)
            else:
                qprof["write_action"] = "Write Data"
                body = _write_png(data, params.style, extent_mask, params.geobox, qprof)

    if params.ows_stats:
        return json_response(qprof.profile())
    else:
        return png_response(body, extra_headers=params.product.wms_cache_rules.cache_headers(n_datasets))


def png_response(body, cfg=None, extra_headers=None):
    if not cfg:
        cfg = get_config()
    if extra_headers is None:
        extra_headers = {}
    headers = {"Content-Type": "image/png"}
    headers.update(extra_headers)
    headers = cfg.response_headers(headers)
    return body, 200, cfg.response_headers(headers)


@log_call
def _write_png(data, style, extent_mask, geobox, qprof):
    qprof.start_event("combine-masks")
    mask = style.to_mask(data, extent_mask)
    qprof.end_event("combine-masks")
    qprof.start_event("apply-style")
    img_data = style.transform_data(data, mask)
    qprof.end_event("apply-style")
    width = geobox.width
    height = geobox.height
    band_index = {
        "red": 1,
        "green": 2,
        "blue": 3,
        "alpha": 4,
    }

    with MemoryFile() as memfile:
        with memfile.open(driver='PNG',
                          width=width,
                          height=height,
                          count=4,
                          transform=None,
                          dtype='uint8') as thing:
            masked = False
            last_band = None
            qprof.start_event("write")
            for band in img_data.data_vars:
                idx = band_index[band]
                band_data = img_data[band].values
                if band == "alpha" and mask is not None:
                    band_data = numpy.where(mask, band_data, 0)
                    masked = True
                thing.write_band(idx, band_data)
                last_band = band_data
            if not masked:
                if mask is None:
                    alpha_mask = numpy.empty(last_band.shape)
                    alpha_mask.fill(255)
                else:
                    alpha_mask = numpy.where(mask, 255, 0).astype('uint8')
                thing.write_band(4, alpha_mask)
            qprof.end_event("write")
        return memfile.read()


@log_call
def _write_empty(geobox):
    with MemoryFile() as memfile:
        with memfile.open(driver='PNG',
                          width=geobox.width,
                          height=geobox.height,
                          count=1,
                          transform=None,
                          nodata=0,
                          dtype='uint8') as thing:
            pass
        return memfile.read()

def get_coordlist(geo, layer_name):
    if geo.type == 'Polygon':
        coordinates_list = [geo.json["coordinates"]]
    elif geo.type == 'MultiPolygon':
        coordinates_list = geo.json["coordinates"]
    elif geo.type == 'GeometryCollection':
        coordinates_list = []
        for geom in geo.json["geometries"]:
            if geom["type"] == "Polygon":
                coordinates_list.append(geom["coordinates"])
            elif geom["type"] == "MultiPolygon":
                coordinates_list.extend(geom["coordinates"])
            else:
                _LOG.warning(
                    "Extent contains non-polygon GeometryType (%s in GeometryCollection - ignoring), layer: %s",
                    geom["type"],
                    layer_name)
    else:
        raise Exception("Unexpected extent/geobox polygon geometry type: %s in layer %s" % (geo.type, layer_name))
    return coordinates_list


@log_call
def _write_polygon(geobox, polygon, zoom_fill, layer):
    geobox_ext = geobox.extent
    if geobox_ext.within(polygon):
        data = numpy.full([geobox.height, geobox.width], fill_value=1, dtype="uint8")
    else:
        data = numpy.zeros([geobox.height, geobox.width], dtype="uint8")
        coordinates_list = get_coordlist(polygon, layer.name)
        for polygon_coords in coordinates_list:
            pixel_coords = [~geobox.transform * coords for coords in polygon_coords[0]]
            rs, cs = skimg_polygon([c[1] for c in pixel_coords], [c[0] for c in pixel_coords],
                                   shape=[geobox.width, geobox.height])
            data[rs, cs] = 1

    with MemoryFile() as memfile:
        with memfile.open(driver='PNG',
                          width=geobox.width,
                          height=geobox.height,
                          count=len(zoom_fill),
                          transform=None,
                          nodata=0,
                          dtype='uint8') as thing:
            for idx, fill in enumerate(zoom_fill, start=1):
                thing.write_band(idx, data * fill)
        return memfile.read()


@log_call
def get_s3_browser_uris(datasets, pt=None, s3url="", s3bucket=""):
    uris = []
    last_crs = None
    for pbq, dss in datasets.items():
        if pbq.main:
            for tds in dss:
                for ds in tds.values.item():
                    if pt and ds.extent:
                        if ds.crs != last_crs:
                            pt_native = pt.to_crs(ds.crs)
                            last_crs = ds.crs
                        if ds.extent.contains(pt_native):
                            uris.append(ds.uris)
                    else:
                        uris.append(ds.uris)
            break

    uris = list(chain.from_iterable(uris))
    unique_uris = set(uris)

    regex = re.compile(r"s3:\/\/(?P<bucket>[a-zA-Z0-9_\-\.]+)\/(?P<prefix>[\S]+)/[a-zA-Z0-9_\-\.]+.yaml")

    # convert to browsable link
    def convert(uri):
        uri_format = "http://{bucket}.s3-website-ap-southeast-2.amazonaws.com/?prefix={prefix}"
        uri_format_prod = str(s3url) + "/?prefix={prefix}"
        result = regex.match(uri) 
        if result is not None:
            if result.group("bucket") == str(s3bucket):
                new_uri = uri_format_prod.format(prefix=result.group("prefix"))
            else:
                new_uri = uri_format.format(bucket=result.group("bucket"),
                                            prefix=result.group("prefix"))
        else:
            new_uri = uri
        return new_uri

    formatted = {convert(uri) for uri in unique_uris}

    return formatted


@log_call
def _make_band_dict(prod_cfg, pixel_dataset, band_list, flag_bands):
    band_dict = {}
    for band in band_list:
        if band in flag_bands:
            continue
        try:
            band_lbl = prod_cfg.band_idx.band_label(band)
            band_val = pixel_dataset[band].item()
            if band_val == pixel_dataset[band].nodata or numpy.isnan(band_val):
                band_dict[band_lbl] = "n/a"
            else:
                band_dict[band_lbl] = band_val
        except ConfigException:
            pass
    for band in flag_bands:
        band_val = pixel_dataset[band].item()
        flag_def = pixel_dataset[band].attrs['flags_definition']
        # HACK: Work around bands with floating point values
        try:
            flag_dict = mask_to_dict(flag_def, band_val)
        except TypeError as te:
            logging.warning('Working around for float bands')
            flag_dict = mask_to_dict(flag_def, int(band_val))
        try:
            ret_val = [flag_def[flag]['description'] for flag, val in flag_dict.items() if val]
        except KeyError:
            # Weirdly formatted flag definition.  Hacky workaround for USGS data in DEAfrica demo.
            ret_val = [val for flag, val in flag_dict.items() if val]
        band_dict[band] = ret_val

    return band_dict


@log_call
def _make_derived_band_dict(pixel_dataset, style_index):
    """Creates a dict of values for bands derived by styles.
    This only works for styles with an `index_function` defined.

    :param xarray.Dataset pixel_dataset: A 1x1 pixel dataset containing band arrays
    :param dict(str, StyleCfg) style_index: dict of style configuration dicts
    :return: dict of style names to derived value
    """
    derived_band_dict = {}
    for style_name, style in style_index.items():
        if not style.include_in_feature_info:
            continue

        if any(pixel_dataset[band] == pixel_dataset[band].nodata for band in style.needed_bands):
            continue

        value = style.index_function(pixel_dataset).item()
        derived_band_dict[style_name] = value if not numpy.isnan(value) else "n/a"
    return derived_band_dict


def geobox_is_point(geobox):
    # TODO: Not 100% sure why this function is needed.
    return geobox.height == 1 and geobox.width == 1

@log_call
def feature_info(args):
    # pylint: disable=too-many-nested-blocks, too-many-branches, too-many-statements, too-many-locals
    # Parse GET parameters
    params = GetFeatureInfoParameters(args)
    feature_json = {}

    geo_point = img_coords_to_geopoint(params.geobox, params.i, params.j)
    # shrink geobox to point
    # Prepare to extract feature info
    if geobox_is_point(params.geobox):
        geo_point_geobox = params.geobox
    else:
        geo_point_geobox = datacube.utils.geometry.GeoBox.from_geopolygon(
            geo_point, params.geobox.resolution, crs=params.geobox.crs)
    tz = tz_for_geometry(geo_point_geobox.geographic_extent)
    stacker = DataStacker(params.product, geo_point_geobox, params.times)
    # --- Begin code section requiring datacube.
    cfg = get_config()
    with cube() as dc:
        if not dc:
            raise WMSException("Database connectivity failure")
        all_time_datasets = stacker.datasets(dc.index, all_time=True, point=geo_point)

        # Taking the data as a single point so our indexes into the data should be 0,0
        h_coord = cfg.published_CRSs[params.crsid]["horizontal_coord"]
        v_coord = cfg.published_CRSs[params.crsid]["vertical_coord"]
        s3_bucket = cfg.s3_bucket
        s3_url = cfg.s3_url
        isel_kwargs = {
            h_coord: 0,
            v_coord: 0
        }
        if any(all_time_datasets):
            # Group datasets by time, load only datasets that match the idx_date
            global_info_written = False
            feature_json["data"] = []
            fi_date_index = {}
            time_datasets = stacker.datasets(dc.index, main_only=False, point=geo_point)
            data = stacker.data(time_datasets, skip_corrections=True)
            for dt in data.time.values:
                td = data.sel(time=dt)
                # Global data that should apply to all dates, but needs some data to extract
                if not global_info_written:
                    global_info_written = True
                    # Non-geographic coordinate systems need to be projected onto a geographic
                    # coordinate system.  Why not use EPSG:4326?
                    # Extract coordinates in CRS
                    data_x = getattr(td, h_coord)
                    data_y = getattr(td, v_coord)

                    x = data_x[isel_kwargs[h_coord]].item()
                    y = data_y[isel_kwargs[v_coord]].item()
                    pt = geometry.point(x, y, params.crs)

                    # Project to EPSG:4326
                    crs_geo = geometry.CRS("EPSG:4326")
                    ptg = pt.to_crs(crs_geo)

                    # Capture lat/long coordinates
                    feature_json["lon"], feature_json["lat"] = ptg.coords[0]

                date_info = {}

                ds = None
                for pbq, dss in time_datasets.items():
                    if pbq.main:
                        ds = dss.sel(time=dt).values.tolist()[0]
                        break
                if params.product.multi_product:
                    date_info["source_product"] = "%s (%s)" % (ds.type.name, ds.metadata_doc["platform"]["code"])

                # Extract data pixel
                pixel_ds = td.isel(**isel_kwargs)

                # Get accurate timestamp from dataset
                if params.product.is_raw_time_res:
                    date_info["time"] = dataset_center_time(ds).strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    date_info["time"] = ds.time.begin.strftime("%Y-%m-%d")
                # Collect raw band values for pixel and derived bands from styles
                date_info["bands"] = _make_band_dict(params.product, pixel_ds, stacker.needed_bands(),
                                                     params.product.all_flag_band_names)
                derived_band_dict = _make_derived_band_dict(pixel_ds, params.product.style_index)
                if derived_band_dict:
                    date_info["band_derived"] = derived_band_dict
                # Add any custom-defined fields.
                for k, f in params.product.feature_info_custom_includes.items():
                    date_info[k] = f(date_info["bands"])

                feature_json["data"].append(date_info)
                fi_date_index[dt] = feature_json["data"][-1]
# REVISIT: There were two very different flag intepreters
            # keeping this commented out for now in case I want to reuse this code in the other interpreter
#                for dt in pq_data.time.values:
#                    pqd =pq_data.sel(time=dt)
#                    date_info = fi_date_index.get(dt)
#                    if date_info:
#                        if "flags" not in date_info:
#                            date_info["flags"] = {}
#                    else:
#                        date_info = {"flags": {}}
#                        feature_json["data"].append(date_info)
#                    pq_pixel_ds = pqd.isel(**isel_kwargs)
#                    # PQ flags
#                    flags = pq_pixel_ds[params.product.pq_band].item()
#                    if not flags & ~params.product.info_mask:
#                        my_flags = my_flags | flags
#                    else:
#                        continue
#                    for mk, mv in params.product.flags_def.items():
#                        if mk in params.product.ignore_info_flags:
#                            continue
#                        bits = mv["bits"]
#                        values = mv["values"]
#                        if isinstance(bits, int):
#                            flag = 1 << bits
#                            if my_flags & flag:
#                                val = values['1']
#                            else:
#                                val = values['0']
#                            date_info["flags"][mk] = val
#                        else:
#                            try:
#                                for i in bits:
#                                    if not isinstance(i, int):
#                                        raise TypeError()
#                                # bits is a list of ints try to do it alos way
#                                for key, desc in values.items():
#                                    if (isinstance(key, str) and key == str(my_flags)) or (isinstance(key, int) and key==my_flags):
#                                        date_info["flags"][mk] = desc
#                                        break
#                            except TypeError:
#                                pass
            feature_json["data_available_for_dates"] = []
            for d in all_time_datasets.coords["time"].values:
                dt_datasets = all_time_datasets.sel(time=d)
                dt = datetime.utcfromtimestamp(d.astype(int) * 1e-9)
                if params.product.is_raw_time_res:
                    dt = solar_date(dt, tz)
                pt_native = None
                for ds in dt_datasets.values.item():
                    if pt_native is None:
                        pt_native = geo_point.to_crs(ds.crs)
                    elif pt_native.crs != ds.crs:
                        pt_native = geo_point.to_crs(ds.crs)
                    if ds.extent and ds.extent.contains(pt_native):
                        feature_json["data_available_for_dates"].append(dt.strftime("%Y-%m-%d"))
                        break
            if time_datasets:
                feature_json["data_links"] = sorted(get_s3_browser_uris(time_datasets, pt, s3_url, s3_bucket))
            else:
                feature_json["data_links"] = []
            if params.product.feature_info_include_utc_dates:
                unsorted_dates = []
                for tds in all_time_datasets:
                    for ds in tds.values.item():
                        if params.product.time_resolution.is_raw_time_res:
                            unsorted_dates.append(ds.center_time.strftime("%Y-%m-%d"))
                        else:
                            unsorted_dates.append(ds.time.begin.strftime("%Y-%m-%d"))
                feature_json["data_available_for_utc_dates"] = sorted(
                    d.center_time.strftime("%Y-%m-%d") for d in all_time_datasets)
    # --- End code section requiring datacube.

    result = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": feature_json
            }
        ]
    }
    return json_response(result, cfg)

def json_response(result, cfg=None):
    if not cfg:
        cfg = get_config()
    return json.dumps(result), 200, cfg.response_headers({"Content-Type": "application/json"})
