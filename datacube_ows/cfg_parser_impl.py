#!/usr/bin/env python3
# This file is part of datacube-ows, part of the Open Data Cube project.
# See https://opendatacube.org for more information.
#
# Copyright (c) 2017-2024 OWS Contributors
# SPDX-License-Identifier: Apache-2.0


import json
import os
import sys
from typing import cast

import click
from babel.messages.pofile import write_po
from datacube import Datacube
from deepdiff import DeepDiff

from datacube_ows import __version__
from datacube_ows.config_utils import ConfigException
from datacube_ows.ows_configuration import (OWSConfig, OWSFolder, OWSLayer,
                                            OWSNamedLayer, read_config)


@click.group(invoke_without_command=True)
@click.option(
    "--version", is_flag=True, default=False, help="Show OWS version number and exit"
)
def main(version: bool):  # type: ignore[return]
    # --version
    if version:
        click.echo(f"Open Data Cube Open Web Services (datacube-ows) version {__version__}")
        return 0


@main.command()
@click.argument("paths", nargs=-1)
@click.option(
    "-p",
    "--parse-only",
    is_flag=True,
    default=False,
    help="Only parse the syntax of the config file - do not validate against database",
)
@click.option(
    "-f",
    "--folders",
    is_flag=True,
    default=False,
    help="Print the folder/layer heirarchy(ies) to stdout.",
)
@click.option(
    "-s",
    "--styles",
    is_flag=True,
    default=False,
    help="Print the styles for each layer to stdout (format depends on --folders flag).",
)
@click.option(
    "-i",
    "--input-file",
    help="Provide a file path for the input inventory json file to be compared with config file",
)
@click.option(
    "-o",
    "--output-file",
    help="Provide an output inventory file name with extension .json",
)
def check(parse_only: bool, folders: bool, styles: bool, input_file: str, output_file: str, paths: list[str]) -> int:
    """Check configuration files

    Takes a list of configuration specifications which are each loaded and validated in turn,
    with each specification being interpreted as per the $DATACUBE_OWS_CFG environment variable.

    If no specification is provided, the $DATACUBE_OWS_CFG environment variable is used.
    """
    if parse_only and (folders or styles):
        click.echo(
            "The --folders (-f) and --styles (-s) flags cannot be used in conjunction with the --parser-only (-p) flag."
        )
        sys.exit(1)
    all_ok = True
    if not paths:
        if parse_path(None, parse_only, folders, styles, input_file, output_file):
            return 0
        else:
            sys.exit(1)
    for path in paths:
        if not parse_path(path, parse_only, folders, styles, input_file, output_file):
            all_ok = False

    if not all_ok:
        sys.exit(1)
    return 0


def parse_path(path: str | None, parse_only: bool, folders: bool, styles: bool, input_file: str, output_file: str) -> bool:
    try:
        raw_cfg = read_config(path)
        cfg = OWSConfig(refresh=True, cfg=raw_cfg)
        if not parse_only:
            with Datacube() as dc:
                cfg.make_ready(dc)
    except ConfigException as e:
        click.echo(f"Config exception for path {str(e)}")
        return False
    click.echo("Configuration parsed OK")
    click.echo(f"Configured message file location: {cfg.msg_file_name}")
    click.echo(f"Configured translations directory location: {cfg.translations_dir}")
    if folders:
        click.echo()
        click.echo("Folder/Layer Hierarchy")
        click.echo("======================")
        print_layers(cfg.layers, styles, depth=0)
        click.echo()
    elif styles:
        click.echo()
        click.echo("Layers and Styles")
        click.echo("=================")
        for lyr in cfg.layer_index.values():
            click.echo(f"{lyr.name} [{','.join(lyr.product_names)}]")
            print_styles(lyr)
        click.echo()
    if input_file or output_file:
        layers_report(cfg.layer_index, input_file, output_file)
    return True


@main.command()
@click.option(
    "-c",
    "--cfg-only",
    is_flag=True,
    default=False,
    help="Read metadata from config only - ignore configured metadata message file.",
)
@click.option(
    "-m",
    "--msg-file",
    default="messages.po",
    help="Write to a message file with the translatable metadata from the configuration. (Defaults to 'messages.po')"
)
@click.argument("path", nargs=1, required=False)
def extract(path: str, cfg_only: bool, msg_file: str) -> int:
    """Extract metadata from existing configuration into a message file template.

    Takes a configuration specification which is loaded as per the $DATACUBE_OWS_CFG environment variable.

    If no specification is provided, the $DATACUBE_OWS_CFG environment variable is used.
    """
    try:
        raw_cfg = read_config(path)
        cfg = OWSConfig(refresh=True, cfg=raw_cfg, ignore_msgfile=cfg_only)
        with Datacube() as dc:
            cfg.make_ready(dc)
    except ConfigException as e:
        click.echo(f"Config exception for path {str(e)}")
        return False
    click.echo("Configuration parsed OK")
    click.echo(f"Configured message file location: {cfg.msg_file_name}")
    click.echo(f"Configured translations directory location: {cfg.translations_dir}")
    write_msg_file(msg_file, cfg)
    click.echo(f"Message file {msg_file} written")
    return 0


@main.command()
@click.option(
    "-n",
    "--new",
    is_flag=True,
    default=False,
    help="Create a new translation template. (Default is to update an existing one.)"
)
@click.option(
    "-m",
    "--msg-file",
    default=None,
    help="Use this message file as the template for translation files. (defaults to message filename from configuration)"
)
@click.option(
    "-d",
    "--translations-dir",
    default=None,
    help="Path to the output translations directory. Defaults to value from configuration"
)
@click.option(
    "-D",
    "--domain",
    default=None,
    help="The domain of the translation files. Defaults to value from configuration"
)
@click.option(
    "-c",
    "--cfg",
    default=None,
    help="Configuration specification to use to determine translations directory and domain (defaults to environment $DATACUBE_OWS_CFG)"
)
@click.argument("languages", nargs=-1)
def translation(languages: list[str], msg_file: str | None, new: bool,
                domain: str | None, translations_dir: str | None, cfg: str | None) -> int:
    """Generate a new translations catalog based on the specified message file.

    Takes a list of languages to generate catalogs for. "all" can be included as a shorthand
    for all languages listed as supported in the configuration.
    """
    if len(languages) == 0:
        click.echo("No language(s) specified.")
        sys.exit(1)
    if msg_file is None or domain is None or translations_dir is None or "all" in languages:
        try:
            raw_cfg = read_config(cfg)
            config = OWSConfig(refresh=True, cfg=raw_cfg)
        except ConfigException as e:
            click.echo(f"Config exception for path: {str(e)}")
            sys.exit(1)
        if domain is None:
            click.echo(f"Using message domain '{config.message_domain}' from configuration")
            domain = config.message_domain
        if translations_dir is None and config.translations_dir is None:
            click.echo("No translations directory was supplied or is configured")
            sys.exit(1)
        elif translations_dir is None:
            click.echo(f"Using translations directory '{config.translations_dir}' from configuration")
            translations_dir = config.translations_dir
        if msg_file is None and config.msg_file_name is None:
            click.echo("No message file name was supplied or is configured")
            sys.exit(1)
        elif msg_file is None:
            click.echo(f"Using message file location '{config.msg_file_name}' from configuration")
            msg_file = config.msg_file_name
        all_langs = config.locales
    else:
        all_langs = []
    assert msg_file is not None  # For type checker
    assert domain is not None  # For type checker
    assert translations_dir is not None  # For type checker
    try:
        fp = open(msg_file, "rb")
        fp.close()
    except IOError:
        click.echo("Message file {msg_file} does not exist or cannot be read.")
        sys.exit(1)
    for language in languages:
        if language == "all":
            for supp_lang in all_langs:
                if new:
                    create_translation(msg_file, translations_dir, domain, supp_lang)
                else:
                    update_translation(msg_file, translations_dir, domain, supp_lang)
        else:
            if new:
                create_translation(msg_file, translations_dir, domain, language)
            else:
                update_translation(msg_file, translations_dir, domain, language)
    click.echo("Language templates created.")
    return 0


def create_translation(msg_file: str, translations_dir: str, domain: str, locale: str) -> bool:
    click.echo(f"Creating template for language: {locale}")
    os.system(f"pybabel init -i {msg_file} -d {translations_dir} -D {domain} -l {locale}")
    return True


def update_translation(msg_file: str, translations_dir: str, domain: str, locale: str) -> bool:
    click.echo(f"Updating template for language: {locale}")
    os.system(f"pybabel update --no-fuzzy-matching --ignore-obsolete "
              f"-i {msg_file} -d {translations_dir} -D {domain} -l {locale}")
    return True


@main.command(name="compile")
@click.option(
    "-d",
    "--translations-dir",
    default=None,
    help="Path to the output translations directory. Defaults to value from configuration"
)
@click.option(
    "-D",
    "--domain",
    default=None,
    help="The domain of the translation files. Defaults to value from configuration"
)
@click.option(
    "-c",
    "--cfg",
    default=None,
    help="Configuration specification to use to determine translations directory and domain (defaults to environment $DATACUBE_OWS_CFG)"
)
@click.argument("languages", nargs=-1)
def compile_cmd(languages: list[str], domain: str | None, translations_dir: str | None, cfg: str | None) -> int:
    """Compile completed translation files.

    Takes a list of languages to generate catalogs for. "all" can be included as a shorthand
    for all languages listed as supported in the configuration.
    """
    if len(languages) == 0:
        click.echo("No language(s) specified.")
        sys.exit(1)
    if domain is None or translations_dir is None or "all" in languages:
        try:
            raw_cfg = read_config(cfg)
            config = OWSConfig(refresh=True, cfg=raw_cfg)
        except ConfigException as e:
            click.echo(f"Config exception for path: {str(e)}")
            sys.exit(1)
        if domain is None:
            click.echo(f"Using message domain '{config.message_domain}' from configuration")
            domain = config.message_domain
        if translations_dir is None and config.translations_dir is None:
            click.echo("No translations directory was supplied or is configured")
            sys.exit(1)
        elif translations_dir is None:
            click.echo(f"Using translations directory '{config.translations_dir}' from configuration")
            translations_dir = config.translations_dir
        all_langs = config.locales
    else:
        all_langs = []
    assert translations_dir is not None
    for language in languages:
        if language == "all":
            for supp_lang in all_langs:
                compile_translation(translations_dir, domain, supp_lang)
        else:
            compile_translation(translations_dir, domain, language)
    click.echo("Language templates created.")
    return 0


def compile_translation(translations_dir: str, domain: str, language: str) -> bool:
    click.echo(f"Compiling template for language: {language}")
    os.system(f"pybabel compile -d {translations_dir} -D {domain} -l {language}")
    return True


def write_msg_file(msg_file: str, cfg: OWSConfig) -> None:
    with open(msg_file, "wb") as fp:
        write_po(fp, cfg.export_metadata())


def layers_report(config_values: dict[str, OWSNamedLayer], input_file: str, output_file: str):
    report = {"total_layers_count": len(config_values.values()), "layers": []}
    for lyr in config_values.values():
        layer = {
            "layer": lyr.name,
            "product": list(lyr.product_names),
            "styles_count": len(lyr.styles),
            "styles_list": [styl.name for styl in lyr.styles],
        }
        cast(list[dict], report["layers"]).append(layer)
    if input_file:
        with open(input_file) as f:
            input_file_data = json.load(f)
        ddiff = DeepDiff(input_file_data, report, ignore_order=True)
        if len(ddiff) == 0:
            return True
        else:
            click.echo(ddiff)
            sys.exit(1)
    if output_file:
        with open(output_file, 'w') as reportfile:
            json.dump(report, reportfile, indent=4)
        return True


def print_layers(layers: list[OWSLayer], styles: bool, depth: int):
    for lyr in layers:
        if isinstance(lyr, OWSFolder):
            indent(depth)
            click.echo(f"* {lyr.title}")
            click.echo(f"{lyr.child_layers} {styles} {depth + 1}")
        else:
            indent(depth)
            click.echo(f"{lyr.name} [{','.join(lyr.product_names)}]")
            if styles:
                click.echo(f"{lyr} {depth}")


def print_styles(lyr: OWSNamedLayer, depth: int = 0):
    for styl in lyr.styles:
        indent(0, for_styles=True)
        print(f". {styl.name}")


def indent(depth: int, for_styles: bool = False):
    for i in range(depth):
        click.echo("  ", nl=False)
    if for_styles:
        click.echo("      ", nl=False)
