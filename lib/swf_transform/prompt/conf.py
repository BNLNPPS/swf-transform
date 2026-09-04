#!/usr/bin/env python
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Wen Guan, <wen.guan@cern.ch>, 2026


import configparser
import json
import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)


def get_default_config_path():
    """
    Return the default path to prompt.conf.

    Searches in order:
    1. Environment variable PROMPT_TRANSFORM_CONF
    2. Current working directory
    3. Package conf directory
    4. /etc/swf_transform/prompt.conf
    """
    # Check environment variable first
    env_path = os.environ.get("PROMPT_TRANSFORM_CONF")
    if env_path and os.path.exists(env_path):
        return env_path

    # Check current directory
    cwd_path = Path.cwd() / "prompt.conf"
    if cwd_path.exists():
        return str(cwd_path)

    # Check package conf directory
    pkg_path = Path(__file__).parent.parent / "conf" / "prompt.conf"
    if pkg_path.exists():
        return str(pkg_path)

    # Check system config
    sys_path = Path("/etc/swf_transform/prompt.conf")
    if sys_path.exists():
        return str(sys_path)

    # Return package path as default even if it doesn't exist
    return str(pkg_path)


def load_config(config_path=None):
    """
    Load prompt configuration from a config file.

    The config file uses INI format with JSON values for broker dictionaries.

    :param config_path: Path to config file. If None, uses default search paths.
    :returns: dict with keys 'transformer_broker', 'result_broker', 'transformer_broadcast_broker'
    :raises: FileNotFoundError if config file not found
    """
    if config_path is None:
        config_path = get_default_config_path()

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    logger.info(f"Loading config from: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    broker_config = {}

    if "prompt" in config:
        section = config["prompt"]

        # Parse each broker configuration (they're stored as JSON strings)
        for key in [
            "transformer_broker",
            "result_broker",
            "transformer_broadcast_broker",
            "ejfat",
        ]:
            if key in section:
                try:
                    # The config file stores these as multi-line JSON-like dicts
                    value_str = section[key]
                    # Parse as JSON
                    broker_config[key] = json.loads(value_str)
                    logger.debug(f"Loaded {key}: {broker_config[key]}")
                except json.JSONDecodeError as ex:
                    logger.error(f"Failed to parse {key} as JSON: {ex}")
                    raise

    if not broker_config:
        raise ValueError(f"No broker configuration found in {config_path}")

    return broker_config


def get_broker_config(config_path=None):
    """
    Get broker configuration with proper error handling and fallback.

    :param config_path: Optional path to config file
    :returns: dict with broker configs or None if loading fails
    """
    try:
        return load_config(config_path)
    except FileNotFoundError as ex:
        logger.warning(f"Config file not found: {ex}")
        return None
    except Exception as ex:
        logger.error(f"Failed to load config: {ex}", exc_info=True)
        return None
