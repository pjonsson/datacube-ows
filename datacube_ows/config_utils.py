# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
from importlib import import_module
from itertools import chain
from typing import Any, Callable, Iterable, Optional, Sequence, cast
from urllib.parse import urlparse

import fsspec
from babel.messages import Catalog, Message
from datacube.model import Product
from datacube.utils.masking import make_mask
from flask_babel import gettext as _
from xarray import DataArray

from datacube_ows.config_toolkit import deepinherit

TYPE_CHECKING = False
if TYPE_CHECKING:
    import datacube_ows.ows_configuration.AttributionCfg
    import datacube_ows.ows_configuration.OWSConfig
    import datacube_ows.ows_configuration.OWSNamedLayer
    import datacube_ows.styles.base.StyleMask

_LOG = logging.getLogger(__name__)

RAW_CFG = None | str | int | float | bool | list["RAW_CFG"] | dict[str, "RAW_CFG"]

CFG_DICT = dict[str, RAW_CFG]

F = Callable[..., Any]


# inclusions defaulting to an empty list is dangerous, but note that it is never modified.
# If modification of inclusions is a required, a copy (ninclusions) is made and modified instead.
# pylint: disable=dangerous-default-value
def cfg_expand(cfg_unexpanded: CFG_DICT,
               cwd: str | None = None, inclusions: list[str] = []) -> CFG_DICT:
    """
    Recursively expand config inclusions.

    :param cfg_unexpanded: The unexpanded configuration object
    :param cwd: (optional) the directory relative to which json expansions will be resolved
    :param inclusions: (optional) inclusions already included (prevents infinite recursion)
    :return: The fully expanded configuration object
    """
    if cwd is None:
        cwd = os.getcwd()

    if isinstance(cfg_unexpanded, dict):
        if "include" in cfg_unexpanded:
            if cfg_unexpanded["include"] in inclusions:
                raise ConfigException("Cyclic inclusion: %s" % cfg_unexpanded["include"])
            raw_path = cast(str, cfg_unexpanded["include"])
            ninclusions: list[str] = inclusions.copy()
            ninclusions.append(cast(str, raw_path))
            # Perform expansion
            if "type" not in cfg_unexpanded or cfg_unexpanded["type"] == "json":
                # JSON Expansion
                try:
                    # Try in actual working directory
                    json_obj: Any = load_json_obj(raw_path)
                    abs_path: str = os.path.abspath(raw_path)
                    cwd = os.path.dirname(abs_path)
                # pylint: disable=broad-except
                except Exception:
                    json_obj = None
                if json_obj is None:
                    path: str = os.path.join(cwd, raw_path)
                    try:
                        # Try in inherited working directory
                        json_obj = load_json_obj(path)
                    # pylint: disable=broad-except
                    except Exception:
                        json_obj = None
                if json_obj is None:
                    raise ConfigException(f"Could not find json file {raw_path}")
                return cfg_expand(json_obj, cwd=cwd, inclusions=ninclusions)
            elif cfg_unexpanded["type"] == "python":
                # Python Expansion
                return cfg_expand(import_python_obj(raw_path), cwd=cwd, inclusions=ninclusions)
            else:
                raise ConfigException("Unsupported inclusion type: %s" % str(cfg_unexpanded["type"]))
        else:
            return {
                k: cfg_expand(cast(CFG_DICT, v), cwd=cwd, inclusions=inclusions)
                for k, v in cfg_unexpanded.items()
            }
    elif isinstance(cfg_unexpanded, Sequence) and not isinstance(cfg_unexpanded, str):
        return [cfg_expand(elem, cwd=cwd, inclusions=inclusions) for elem in cfg_unexpanded]
    else:
        return cfg_unexpanded


def get_file_loc(x: str) -> str:
    """Helper function to deal with local / remote "working directory"

    Returns the absolute pathname for a local file
    and the URL location of a remote file.
    """
    xp = urlparse(x)
    if xp.scheme in ("s3",): # NOTE: could add http/s, ...
        enable_s3 = os.environ.get("DATACUBE_OWS_CFG_ALLOW_S3", "no")
        if not enable_s3.lower() in ("yes", "true", "1", "y"):
            raise ConfigException("Please set environment variable 'DATACUBE_OWS_CFG_ALLOW_S3=YES' "
                              + "to enable OWS config from AWS S3")
        cwd = xp.scheme + "://" + xp.netloc +  xp.path.rsplit("/", 1)[0]
    elif xp.scheme:
        raise ConfigException(f"Unsupported URL scheme in config inheritance: {xp.scheme}")
    else:
        abs_path = os.path.abspath(x)
        cwd = os.path.dirname(abs_path)
    return cwd


def load_json_obj(path: str) -> RAW_CFG:
    """
    Load a json object from a file path
    :param path: The file path
    :return: The deserialised json object from the file.
    """
    with fsspec.open(path) as json_file:
        return json.load(json_file)


def import_python_obj(path: str) -> CFG_DICT:
    """Imports a python dictionary by fully-qualified path

    :param: A fully qualified python path.
    :return: a Python object, or None
    """
    mod_name, obj_name = path.rsplit('.', 1)
    try:
        mod = import_module(mod_name)
        obj = getattr(mod, obj_name)
    except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
        raise ConfigException(f"Could not import python object: {path}")
    return cast(CFG_DICT, obj)


class ConfigException(Exception):
    """
    General exception for OWS Configuration issues.
    """


class OWSConfigNotReady(ConfigException):
    """
    Exception raised when someone tries to use an OWSConfigEntry that isn't fully initialised yet.
    """


class ODCInitException(ConfigException):
    """
    Exception raised when a Datacube index could not be created
    """



class OWSConfigEntry:
    """
    Base class for all configuration objects
    """
    # Parse and validate the json but don't access the database.
    def __init__(self, cfg: RAW_CFG, *args, **kwargs) -> None:
        """
        Base Class Constructor.

        Handles unready attributes for two-phase intitialisation and stows the raw configuration away.

        :param cfg: The congfiguration being parsed
        :param args:
        :param kwargs:
        """
        self._unready_attributes: set[str] = set()
        self._raw_cfg: RAW_CFG = cfg
        self.ready: bool = False

    def declare_unready(self, name):
        """
        Declare a parameter that cannot be set in the first (no db) phase of initialisation.

        :param name:
        :return:
        """
        if self.ready:
            raise ConfigException(f"Cannot declare {name} as unready on a ready object")
        self._unready_attributes.add(name)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Expose attributes in a dictionary-like manner.

        :param key: The attribute name
        :param default: The default value to use if the attribute does not exist
        :return: The attribute value.
        """
        try:
            return getattr(self, key)
        except AttributeError:
            return default

    def __getattribute__(self, name: str) -> Any:
        """
        Throw an error if an unready attribute is accessed.

        :param name: attribute name
        :return: attribute value
        :raises: OWSConfigNotReady if entry is not fully initialised.
        """
        if name == "_unready_attributes":
            pass
        elif hasattr(self, "_unready_attributes") and name in self._unready_attributes:
            raise OWSConfigNotReady(f"The following parameters have not been initialised: {self._unready_attributes}")
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, val: Any) -> None:
        """
        Mark unready attributes off as they get set

        :param name: Attribute name
        :param val: Value to set the attribute to.
        """
        if name == "_unready_attributes":
            pass
        elif hasattr(self, "_unready_attributes") and name in self._unready_attributes:
            self._unready_attributes.remove(name)
        super().__setattr__(name, val)

    # Validate against database and prepare for use.
    def make_ready(self, *args, **kwargs) -> None:
        """
        Perform second phase initialisation with a database connection.

        In the base class we just confirm that all declared-unready attributes have been initialised,
        and set the ready flag.

        :param dc:
        :param args:
        :param kwargs:
        :return:
        """
        if self._unready_attributes:
            raise OWSConfigNotReady(f"The following parameters have not been initialised: {self._unready_attributes}")
        self.ready = True

#####################################################
# Metadata separation and translation.

# Label names for metadata separation and translation

FLD_TITLE = "title"
FLD_UNITS = "units"
FLD_ABSTRACT = "abstract"
FLD_KEYWORDS = "local_keywords"
FLD_FEES = "fees"
FLD_ACCESS_CONSTRAINTS = "access_constraints"
FLD_ATTRIBUTION = "attribution_title"
FLD_CONTACT_ORGANISATION = "contact_org"
FLD_CONTACT_POSITION = "contact_position"

class OWSMetadataConfig(OWSConfigEntry):
    """
    Config Entry abstract class that supports attributes that can be over-ridden with a message file or
    translations directory.
    """

    # Supported Metadata types.  Override them in the child classes as required
    # Each metadata type has special handling.

    METADATA_TITLE: bool = True
    METADATA_ABSTRACT: bool = True
    METADATA_KEYWORDS: bool = False
    METADATA_CONTACT_INFO: bool = False
    METADATA_FEES: bool = False
    METADATA_ACCESS_CONSTRAINTS: bool = False
    METADATA_ATTRIBUTION: bool = False
    METADATA_DEFAULT_BANDS: bool = False
    METADATA_VALUE_RULES: bool = False
    METADATA_LEGEND_UNITS: bool = False
    METADATA_TICK_LABELS: bool = False

    # Class registries, mapping metadata paths to their default value and whether the metadata value is
    # unique to that path, or has been inherited from a parent metadata path.
    _metadata_registry: dict[str, str] = {}
    _inheritance_registry: dict[str, bool] = {}

    _msg_src: Catalog | None = None

    # Inaccessible attributes to allow type checking
    abstract: str = ""
    attribution: Optional["datacube_ows.ows_configuration.AttributionCfg"] = None

    def get_obj_label(self) -> str:
        """Return the metadata path prefix for this object."""
        return "global"


    def can_inherit_from(self) -> Optional["OWSMetadataConfig"]:
        """
        The parent config object this object can inherit metadata from.

        :return: An instance of OWSMetadataConfig or None.
        """
        return None

    # Holders for managing inheritance.
    @property
    def default_title(self) -> str | None:
        return None

    @property
    def default_abstract(self) -> str | None:
        return None

    _keywords: set[str] = set()

    def parse_metadata(self, cfg: CFG_DICT) -> None:
        """
        Read some raw configuration for this object, and setup metadata handling.
        Must be called early in __init__() (before super().__init__().)

        :param cfg: The raw configuration for this object
        """
        # can_inherit_from() can be over-ridden by subclasses
        # pylint: disable=assignment-from-none
        inherit_from = self.can_inherit_from()
        if self.METADATA_TITLE:
            if self.default_title is not None:
                self.register_metadata(self.get_obj_label(), FLD_TITLE, cast(str, cfg.get(FLD_TITLE, self.default_title)))
            else:
                try:
                    self.register_metadata(self.get_obj_label(), FLD_TITLE, cast(str, cfg[FLD_TITLE]))
                except KeyError:
                    raise ConfigException(f"Entity {self.get_obj_label()} has no title.")
        if self.METADATA_ABSTRACT:
            local_abstract = cfg.get("abstract")
            if local_abstract is None and inherit_from is not None:
                self.register_metadata(self.get_obj_label(), FLD_ABSTRACT, inherit_from.abstract, inherited=True)
            elif local_abstract is None and self.default_abstract is not None:
                self.register_metadata(self.get_obj_label(), FLD_ABSTRACT, cast(str, self.default_abstract))
            elif local_abstract is None:
                raise ConfigException(f"Entity {self.get_obj_label()} has no abstract")
            else:
                self.register_metadata(self.get_obj_label(), "abstract", cast(str, local_abstract))
        if self.METADATA_KEYWORDS:
            local_keyword_set = set(cast(list[str], cfg.get("keywords", [])))
            self.register_metadata(self.get_obj_label(), FLD_KEYWORDS, ",".join(local_keyword_set))
            if inherit_from:
                keyword_set = inherit_from.keywords
            else:
                keyword_set = set()
            self._keywords = keyword_set.union(local_keyword_set)
        if self.METADATA_ATTRIBUTION:
            inheriting = False
            attrib = cast(dict[str, str], cfg.get("attribution"))
            if attrib is None and inherit_from is not None:
                attrib = inherit_from.attribution
                inheriting = True
            if attrib:
                attrib_title = attrib.get("title")
            else:
                attrib_title = None
            if attrib_title:
                self.register_metadata(self.get_obj_label(), FLD_ATTRIBUTION, attrib_title, inheriting)
        if self.METADATA_FEES:
            fees = cast(str, cfg.get(FLD_FEES))
            if not fees:
                fees = "none"
            self.register_metadata(self.get_obj_label(), FLD_FEES, fees)
        if self.METADATA_ACCESS_CONSTRAINTS:
            acc = cast(str, cfg.get("access_contraints"))
            if not acc:
                acc = "none"
            self.register_metadata(self.get_obj_label(), FLD_ACCESS_CONSTRAINTS, acc)
        if self.METADATA_CONTACT_INFO:
            cfg_contact_info: dict[str, str] = cast(dict[str, str], cfg.get("contact_info", {}))
            org = cfg_contact_info.get("organisation")
            position = cfg_contact_info.get("position")
            if org:
                self.register_metadata(self.get_obj_label(), FLD_CONTACT_ORGANISATION, org)
            if position:
                self.register_metadata(self.get_obj_label(), FLD_CONTACT_POSITION, position)
        if self.METADATA_DEFAULT_BANDS:
            band_map = cast(dict[str, list[str]], cfg)
            for k, v in band_map.items():
                if len(v):
                    self.register_metadata(self.get_obj_label(), k, v[0])
                else:
                    self.register_metadata(self.get_obj_label(), k, k)
        if self.METADATA_VALUE_RULES:
            # Note that parse_metadata must be called after the value map is parsed.
            for patch in self.patches:
                self.register_metadata(self.get_obj_label(), f"rule_{patch.idx}", patch.label)
        if self.METADATA_LEGEND_UNITS:
            units = cast(str, cfg.get(FLD_UNITS))
            if units is not None:
                self.register_metadata(self.get_obj_label(), FLD_UNITS, units)
        if self.METADATA_TICK_LABELS:
            # Note that parse_metadata must be called after legend ticks are parsed.
            for tick, lbl in zip(self.ticks, self.tick_labels):
                if any(c.isalpha() for c in lbl):
                    self.register_metadata(self.get_obj_label(), f"lbl_{tick}", lbl)

    @property
    def keywords(self) -> set[str]:
        """
        Return the keywords for this object (with inheritance, but without metadata separation or translation)
        :return: A set of keywords.
        """
        return self._keywords

    @classmethod
    def set_msg_src(cls, src: Catalog | None) -> None:
        """
        Allow all OWSMetadatConfig subclasses to share a common message catalog.
        :param src: A Message Catalog object
        """
        OWSMetadataConfig._msg_src = src

    def read_metadata(self, lbl: str, fld: str) -> str | None:
        """
        Read a general piece of metadata (potentially from another object).
        Resolution order:

        1) Via gettext translation if internationalisation is enabled.
        2) Via externalised message catalog, if configured.
        3) From raw config.

        :param lbl: Object label
        :param fld: Metadata type label
        :return: Displayable metadata.
        """
        lookup = ".".join([lbl, fld])
        if self.global_config().internationalised:
            trans = _(lookup)
            if trans != lookup:
                return trans
        if self._msg_src is not None:
            msg: Message | None = cast(Catalog, self._msg_src).get(lookup)
            if not msg:
                msg_: str | None = self._metadata_registry.get(lookup)
            else:
                msg_ = cast(str, msg.string)
            return msg_
        return self._metadata_registry.get(lookup)

    def read_inheritance(self, lbl: str, fld: str) -> bool:
        """
        Determine whether an arbitrary piece of metadata is inherited from a parent object.
        :param lbl: Object label
        :param fld: Metadata type label
        :return: True if the metadata is inherited from a parent object, False otherwise.
        """
        lookup = ".".join([lbl, fld])
        return self._inheritance_registry.get(lookup, False)

    def register_metadata(self, lbl: str, fld: str, val: str, inherited: bool = False) -> None:
        """
        Register a piece of metadata at config-parse time with it's raw config default value.

        :param lbl: Object label
        :param fld: Metadata type label
        :param val: The default value, as recorded in the raw config.
        :param inherited: If true, metadata is considered inherited and is not handled independently.
        """
        lookup = ".".join([lbl, fld])
        self._metadata_registry[lookup] = val
        self._inheritance_registry[lookup] = inherited

    def read_local_metadata(self, fld: str) -> str | None:
        """
        Read a general piece of metadata for this object.
        Resolution order:

        1) Via gettext translation if internationalisation is enabled.
        2) Via externalised message catalog, if configured.
        3) From raw config.

        :param fld: Metadata type label
        :return: Displayable metadata.
        """
        return self.read_metadata(self.get_obj_label(), fld)

    def is_inherited(self, fld: str) -> bool:
        """
        Determine whether an piece of metadata for this object is inherited from the parent object.
        :param fld: Metadata type label
        :return: True if the metadata is inherited from the parent object, False otherwise.
        """
        return self.read_inheritance(self.get_obj_label(), fld)

    def global_config(self) -> "datacube_ows.ows_configuration.OWSConfig":
        """
        Return the global config object.

        Should be over-ridden by child classes as appropriate.

        :return: The global (OWSConfig) configuration object.
        """
        return cast("datacube_ows.ows_configuration.OWSConfig", self)

    def __getattribute__(self, name: str) -> Any:
        """"
        Expose separated or internationalised metadata as attributes
        """
        if name in (FLD_TITLE, FLD_ABSTRACT, FLD_FEES,
                    FLD_ACCESS_CONSTRAINTS, FLD_CONTACT_POSITION, FLD_CONTACT_ORGANISATION,
                    FLD_UNITS):
            return self.read_local_metadata(name)
        elif name == FLD_KEYWORDS:
            kw = self.read_local_metadata(FLD_KEYWORDS)
            if kw:
                return set(kw.split(","))
            else:
                return set()
        elif name == FLD_ATTRIBUTION:
            return self.read_local_metadata(FLD_ATTRIBUTION)
        else:
            return super().__getattribute__(name)

###########################
# Inheritable configuration

class OWSEntryNotFound(ConfigException):
    """
    Exception thrown when looking up an indexed config entry that does not exist.
    """


class OWSIndexedConfigEntry(OWSConfigEntry):
    """
    A Config Entry object that can be looked up by name (i.e. so it can be inherited from)
    """
    INDEX_KEYS: list[str] = []

    def __init__(self, cfg: RAW_CFG, keyvals: dict[str, str], *args, **kwargs) -> None:
        """
        Validate and store keyvals for indexed lookup.

        :param cfg: Raw configuration for object.
        :param keyvals: Key values identifying this object
        :param args:
        :param kwargs:
        """
        super().__init__(cfg, *args, **kwargs)

        for k in self.INDEX_KEYS:
            if k not in keyvals:
                raise ConfigException(f"Key value {k} missing from keyvals: {keyvals!r}")
        self.keyvals = keyvals

    @classmethod
    def lookup_impl(cls, cfg: "datacube_ows.ows_configuration.OWSConfig",
                    keyvals: dict[str, str],
                    subs: CFG_DICT | None = None) -> "OWSIndexedConfigEntry":
        """
        Lookup a config entry of this type by identifying label(s)

        :param cfg:  The global config object that the desired object lives under.
        :param keyvals: Keyword dictionary of identifying label(s)
        :param subs:  Dictionary of keyword substitutions.  Used for e.g. looking up a style from a different layer.
        :return: The desired config object
        :raises: OWSEntryNotFound exception if no matching object found.
        """
        raise NotImplementedError()


# pylint: disable=abstract-method
class OWSExtensibleConfigEntry(OWSIndexedConfigEntry):
    """
    A configuration object that can inherit from and extend an existing configuration object of the same type.
    """
    def __init__(self,
                 cfg: RAW_CFG, keyvals: dict[str, str], global_cfg: "datacube_ows.ows_configuration.OWSConfig",
                 *args,
                 keyval_subs: dict[str, Any] | None = None,
                 keyval_defaults: dict[str, str] | None = None,
                 expanded: bool = False,
                 **kwargs) -> None:
        """
        Apply inheritance expansion and overrides to raw config.
        :param cfg: Raw (unexpanded) configuration
        :param keyvals: Dictionary of lookup keys to values
        :param global_cfg: global configuration object
        :param keyval_subs: (optional) Dictionary of keyword substitutions
        :param keyval_defaults: (optional) Dictionary of keyword defaults
        :param expanded: (optional, defaults to False) If true, assume expansion has already been applied.
        """
        if not expanded:
            cfg = self.expand_inherit(cast(CFG_DICT, cfg), global_cfg,
                                      keyval_subs=keyval_subs, keyval_defaults=keyval_defaults)

        super().__init__(cfg, keyvals, global_cfg=global_cfg, *args, **kwargs)

    @classmethod
    def expand_inherit(cls,
                       cfg: CFG_DICT, global_cfg: "datacube_ows.ows_configuration.OWSConfig",
                       keyval_subs: dict[str, Any] | None = None,
                       keyval_defaults: dict[str, str] | None = None) -> RAW_CFG:
        """
        Expand inherited config, and apply overrides.

        :param cfg: Unexpanded configuration
        :param global_cfg: Global config object
        :param keyval_subs: (optional) Dictionary of keyword substitutions
        :param keyval_defaults: (optional) Dictionary of keyword defaults
        :return: Fully expanded config, with inheritance and overrides applied.
        """
        if "inherits" in cfg:
            lookup = True
            # Precludes e.g. defaulting style lookup to current layer.
            lookup_keys: dict[str, str] = {}
            inherits = cast(dict[str, str], cfg["inherits"])
            for k in cls.INDEX_KEYS:
                if k not in inherits and keyval_defaults is not None and k not in keyval_defaults:
                    lookup = False
                    break
                if k in inherits:
                    lookup_keys[k] = inherits[k]
                elif keyval_defaults and k in keyval_defaults:
                    lookup_keys[k] = str(keyval_defaults[k])
            if lookup and lookup_keys:
                parent = cls.lookup_impl(global_cfg, keyvals=lookup_keys, subs=keyval_subs)
                # pylint: disable=protected-access
                parent_cfg = parent._raw_cfg
            else:
                parent_cfg = cfg["inherits"]
            cfg = deepinherit(cast(dict[str, Any], parent_cfg), cfg)
            cfg["inheritance_expanded"] = True
        return cfg

##################################
# Managing multiproduct flag-bands

class OWSFlagBandStandalone:
    """"
    Minimal proxy for OWSFlagBand, for use in StandAlone API which doesn't need anything more than the band name.
    """
    def __init__(self, band: str) -> None:
        self.pq_band = band
        self.canonical_band_name = band
        self.pq_names: list[str] = []
        self.pq_ignore_time = False
        self.pq_manual_merge = False
        self.pq_fuse_func: Optional[FunctionWrapper] = None


class OWSFlagBand(OWSConfigEntry):
    """
    Represents a flag band, which may come from the main product or a parallel secondary product.
    """
    def __init__(self, cfg: CFG_DICT, product_cfg: "datacube_ows.ows_configuration.OWSNamedLayer",
                 **kwargs) -> None:
        """
        Class constructor, first round initialisation

        :param cfg: Raw config
        :param product_cfg:  The OWSNamedLayer object this flag-band is associated with
        """
        super().__init__(cfg, **kwargs)
        cfg = cast(CFG_DICT, self._raw_cfg)
        self.layer = product_cfg
        pq_names = self.layer.parse_pq_names(cfg)
        self.pq_names = cast(list[str], pq_names["pq_names"])
        self.pq_low_res_names = pq_names["pq_low_res_names"]
        self.main_products = pq_names["main_products"]
        self.pq_band = str(cfg["band"])
        self.canonical_band_name = self.pq_band # Update for aliasing on make_ready
        if "fuse_func" in cfg:
            self.pq_fuse_func: Optional[FunctionWrapper] = FunctionWrapper(self.layer, cast(CFG_DICT, cfg["fuse_func"]))
        else:
            self.pq_fuse_func = None
        self.pq_ignore_time = bool(cfg.get("ignore_time", False))
        self.ignore_info_flags = cast(list[str], cfg.get("ignore_info_flags", []))
        self.pq_manual_merge = bool(cfg.get("manual_merge", False))
        self.declare_unready("pq_products")
        self.declare_unready("flags_def")
        self.declare_unready("info_mask")

    # pylint: disable=attribute-defined-outside-init
    def make_ready(self, *args: Any, **kwargs: Any) -> None:
        """
        Second round (db-aware) intialisation.

        :param dc: A Datacube object
        """
        # pyre-ignore[16]
        self.pq_products: list[Product] = []
        # pyre-ignore[16]
        self.pq_low_res_products: list[Product] = []
        for pqn in self.pq_names:
            if pqn is not None:
                pq_product = self.layer.dc.index.products.get_by_name(pqn)
                if pq_product is None:
                    raise ConfigException(f"Could not find flags product {pqn} for layer {self.layer.name} in datacube")
                self.pq_products.append(pq_product)
        for pqn in self.pq_low_res_names:
            if pqn is not None:
                pq_product = self.layer.dc.index.products.get_by_name(pqn)
                if pq_product is None:
                    raise ConfigException(f"Could not find flags low_res product {pqn} for layer {self.layer.name} in datacube")
                self.pq_low_res_products.append(pq_product)

        # Resolve band alias if necessary.
        if self.main_products:
            try:
                self.canonical_band_name = self.layer.band_idx.band(self.pq_band)
            except ConfigException:
                pass

    # pyre-ignore[16]
        self.info_mask: int = ~0
        # A (hopefully) representative product
        product = self.pq_products[0]
        try:
            meas = product.lookup_measurements([str(self.canonical_band_name)])[str(self.canonical_band_name)]
        except KeyError:
            raise ConfigException(
                f"Band {self.pq_band} does not exist in product {product.name} - cannot be used as a flag band for layer {self.layer.name}.")
        if "flags_definition" not in meas:
            raise ConfigException(f"Band {self.pq_band} in product {product.name} has no flags_definition in ODC - cannot be used as a flag band for layer {self.layer.name}.")
        # pyre-ignore[16]
        self.flags_def: dict[str, dict[str, RAW_CFG]] = meas["flags_definition"]
        for bitname in self.ignore_info_flags:
            bit = self.flags_def[bitname]["bits"]
            if not isinstance(bit, int):
                continue
            flag = 1 << bit
            self.info_mask &= ~flag
        super().make_ready(*args, **kwargs)


FlagBand = OWSFlagBand | OWSFlagBandStandalone


class FlagProductBands(OWSConfigEntry):
    """
    A collection of flag bands for a layer that all come from the same product (or multi-product product collection).
    """
    def __init__(self, flag_band: FlagBand,
                 layer: "datacube_ows.ows_configuration.OWSNamedLayer") -> None:
        """
        Class constructor, first round initialisation

        :param flag_band: A Flag-band object
        :param layer: the named layer these flag-bands are associated with.
        """
        super().__init__({})
        self.layer = layer
        self.bands: set[str] = set()
        self.bands.add(str(flag_band.canonical_band_name))
        self.flag_bands = {flag_band.pq_band: flag_band}
        self.product_names = tuple(flag_band.pq_names)
        self.ignore_time = flag_band.pq_ignore_time
        self.declare_unready("products")
        self.declare_unready("low_res_products")
        self.manual_merge = bool(flag_band.pq_manual_merge)
        self.fuse_func = flag_band.pq_fuse_func
        # pyre-ignore[16]
        self.main_product = self.products_match(layer.product_names)

    def products_match(self, product_names: Iterable[str]) -> bool:
        """
        Compare a list of product names to this objects list of product names.

        :param product_names: The product names to compare to this object
        :return: True if the product names lists match, False otherwise.
        """
        return tuple(product_names) == self.product_names

    def add_flag_band(self, fb: FlagBand) -> None:
        """
        Add an additional flag band to this collection

        :param fb:  A flag-band object.  Must match product_names with this collection.
        """
        self.flag_bands[fb.pq_band] = fb
        self.bands.add(fb.pq_band)
        if fb.pq_manual_merge:
            fb.pq_manual_merge = True
        if fb.pq_fuse_func and self.fuse_func and fb.pq_fuse_func != self.fuse_func:
            raise ConfigException(f"Fuse functions for flag bands in product set {self.product_names} do not match")
        if fb.pq_ignore_time != self.ignore_time:
            raise ConfigException(f"ignore_time option for flag bands in product set {self.product_names} do not match")
        elif fb.pq_fuse_func and not self.fuse_func:
            self.fuse_func = fb.pq_fuse_func
        self.declare_unready("products")
        self.declare_unready("low_res_products")

    # pylint: disable=attribute-defined-outside-init
    def make_ready(self, *args, **kwargs) -> None:
        """
        Second round (db-aware) intialisation.

        :param dc: A Datacube object
        """
        for fb in self.flag_bands.values():
            fb = cast(OWSFlagBand, fb)
            # pyre-ignore [16]
            self.products: list[Product] = fb.pq_products
            # pyre-ignore [16]
            self.low_res_products: list[Product] = fb.pq_low_res_products
            break
        if self.main_product:
            self.bands = set(self.layer.band_idx.band(b) for b in self.bands)
        super().make_ready(*args, **kwargs)

    @classmethod
    def build_list_from_masks(cls, masks: Iterable["datacube_ows.styles.base.StyleMask"],
                              layer: "datacube_ows.ows_configuration.OWSNamedLayer") -> list["FlagProductBands"]:
        """
        Class method to instantiate a list of FlagProductBands from a list of style masks.

        Bands from the same product (or multi-product collection) will be grouped into the same FlagProductBand

        :param masks: A list of StyleMask objects.
        :param layer: A named layer object
        :return: A list of FlagProductBands objects
        """
        flag_products: list["FlagProductBands"] = []
        for mask in masks:
            handled = False
            for fp in flag_products:
                if fp.products_match(mask.flag_band.pq_names):
                    fp.add_flag_band(mask.flag_band)
                    handled = True
                    break
            if not handled:
                flag_products.append(cls(mask.flag_band, layer))
        return flag_products

    @classmethod
    def build_list_from_flagbands(cls, flagbands: Iterable[OWSFlagBand],
                                  layer: "datacube_ows.ows_configuration.OWSNamedLayer") -> list["FlagProductBands"]:
        """
        Class method to instantiate a list of FlagProductBands from a list of OWS Flag Bands.

        Bands from the same product (or multi-product collection) will be grouped into the same FlagProductBand

        :param masks: A list of OWSFlagBand objects.
        :param layer: A named layer object
        :return: A list of FlagProductBands objects
        """
        flag_products: list["FlagProductBands"] = []
        for fb in flagbands:
            handled = False
            for fp in flag_products:
                if fp.products_match(fb.pq_names):
                    fp.add_flag_band(fb)
                handled = True
                break
            if not handled:
                flag_products.append(cls(fb, layer))
        return flag_products


FlagSpec = dict[str, bool | str]


class AbstractMaskRule(OWSConfigEntry):
    def __init__(self, band: str, cfg: CFG_DICT, mapper: Callable[[str], str] = lambda x: x) -> None:
        super().__init__(cfg)
        self.band = mapper(band)
        self.parse_rule_spec(cfg)

    @property
    def context(self) -> str:
        return "a mask rule"

    VALUES_LABEL = "values"
    def parse_rule_spec(self, cfg: CFG_DICT) -> None:
        self.flags: list[FlagSpec] | FlagSpec | None = None
        self.or_flags: bool | list[bool] = False
        self.values: list[list[int]] | list[int] | None = None
        self.invert: bool | list[bool] = bool(cfg.get("invert", False))
        if "flags" in cfg:
            flags = cast(FlagSpec, cfg["flags"])
            if "or" in flags and "and" in flags:
                raise ConfigException(
                    f"ValueMap rule in {self.context} combines 'and' and 'or' rules")
            elif "or" in flags:
                self.or_flags = True
                flags = cast(FlagSpec, flags["or"])
            elif "and" in flags:
                flags = cast(FlagSpec, flags["and"])
            self.flags = flags
        else:
            self.flags = None

        if "values" in cfg:
            val: Any = cfg["values"]
        else:
            val = None
        if val is None:
            self.values = None
        else:
            if isinstance(val, int):
                self.values = [cast(int, val)]
            else:
                self.values = cast(list[int], val)

        if not self.flags and not self.values:
            raise ConfigException(
                f"Mask rule in {self.context} must have a non-empty 'flags' or 'values' section.")
        if self.flags and self.values:
            raise ConfigException(
                f"Mask rule in {self.context} has both a 'flags' and a 'values' section - choose one.")

    def create_mask(self, data: DataArray) -> DataArray | None:
        """
        Create a mask from raw flag band data.

        :param data: Raw flag data, assumed to be for this rule's flag band.
        :return: A boolean DataArray, True where the data matches this rule
        """
        if self.values:
            mask: DataArray | None = None
            for v in cast(list[int], self.values):
                vmask = data == v
                if mask is None:
                    mask = vmask
                else:
                    mask |= vmask
        elif self.or_flags:
            mask = None
            for f in cast(CFG_DICT, self.flags).items():
                d = {f[0]: f[1]}
                if mask is None:
                    mask = make_mask(data, **d)
                else:
                    mask |= make_mask(data, **d)
        else:
            mask = make_mask(data, **cast(CFG_DICT, self.flags))
        if mask is not None and self.invert:
            mask = ~mask # pylint: disable=invalid-unary-operand-type
        return mask


# Function wrapper for configurable functional elements
class FunctionWrapper:
    """
    Function wrapper for configurable functional elements
    """

    def __init__(self,
                 product_or_style_cfg: OWSExtensibleConfigEntry | None,
                 func_cfg: str | CFG_DICT | F,
                 stand_alone: bool = False) -> None:
        """

        :param product_or_style_cfg: An instance of either NamedLayer or Style,
                the context in which the wrapper operates.
        :param func_cfg: A function or a configuration dictionary representing a function.
        :param stand_alone: Optional boolean.
                If False (the default) then only configuration dictionaries will be accepted.
        """
        self.style_or_layer_cfg = product_or_style_cfg
        if callable(func_cfg):
            if not stand_alone:
                raise ConfigException(
                    "Directly including callable objects in configuration is no longer supported. Please reference callables by fully qualified name.")
            self._func: Callable = func_cfg
            self._args: list[RAW_CFG] = []
            self._kwargs: CFG_DICT = {}
            self.band_mapper: Callable[[str], str] | None = None
            self.pass_layer_cfg = False
        elif isinstance(func_cfg, str):
            self._func = get_function(func_cfg)
            self._args = []
            self._kwargs = {}
            self.band_mapper = None
            self.pass_layer_cfg = False
        else:
            if stand_alone and callable(func_cfg["function"]):
                self._func = func_cfg["function"]
            elif callable(func_cfg["function"]):
                raise ConfigException(
                    "Directly including callable objects in configuration is no longer supported. Please reference callables by fully qualified name.")
            else:
                self._func = get_function(cast(str, func_cfg["function"]))
            self._args = cast(list[RAW_CFG], func_cfg.get("args", []))
            self._kwargs = cast(CFG_DICT, func_cfg.get("kwargs", {})).copy()
            self.pass_layer_cfg = bool(func_cfg.get("pass_layer_cfg", False))
            if "pass_product_cfg" in func_cfg:
                _LOG.warning("WARNING: pass_product_cfg in function wrapper definitions has been renamed "
                             "'mapped_bands'.  Please update your config accordingly")
            if func_cfg.get("mapped_bands", func_cfg.get("pass_product_cfg", False)):
                if hasattr(product_or_style_cfg, "band_idx"):
                    # NamedLayer
                    from datacube_ows.ows_configuration import OWSNamedLayer
                    named_layer = cast(OWSNamedLayer, product_or_style_cfg)
                    b_idx = named_layer.band_idx
                    self.band_mapper = b_idx.band
                else:
                    # Style
                    from datacube_ows.styles import StyleDef
                    style = cast(StyleDef, product_or_style_cfg)
                    b_idx = style.product.band_idx
                    delocaliser = style.local_band
                    self.band_mapper = lambda b: b_idx.band(delocaliser(b))
            else:
                self.band_mapper = None

    def __call__(self, *args, **kwargs) -> Any:
        if args and self._args:
            calling_args: Iterable[Any] = chain(args, self._args)
        elif args:
            calling_args = args
        else:
            calling_args = self._args
        if kwargs and self._kwargs:
            calling_kwargs: dict[str, Any] = self._kwargs.copy()
            calling_kwargs.update(kwargs)
        elif kwargs:
            calling_kwargs = kwargs.copy()
        else:
            calling_kwargs = self._kwargs.copy()

        if self.band_mapper:
            calling_kwargs["band_mapper"] = self.band_mapper

        if self.pass_layer_cfg:
            calling_kwargs['layer_cfg'] = self.style_or_layer_cfg

        return self._func(*calling_args, **calling_kwargs)


def get_function(func: F | str) -> F:
    """Converts a config entry to a function, if necessary

    :param func: Either a Callable object or a fully qualified function name str, or None
    :return: a Callable object, or None
    """
    if func is not None and not callable(func):
        mod_name, func_name = func.rsplit('.', 1)
        try:
            mod = import_module(mod_name)
            func = getattr(mod, func_name)
        except (ImportError, ModuleNotFoundError, ValueError, AttributeError):
            raise ConfigException(f"Could not import python object: {func}")
        assert callable(func)
    return cast(F, func)
