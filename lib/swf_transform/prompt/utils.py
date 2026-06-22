#!/usr/bin/env python
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Wen Guan, <wen.guan@cern.ch>, 2026

import logging
import os
import re
import sys
import time


def setup_logging(name, stream=None, log_file=None, loglevel=None):
    """
    Setup logging
    """
    if loglevel is None:
        loglevel = logging.INFO

        if os.environ.get("PROMPT_LOG_LEVEL", None):
            prompt_log_level = os.environ.get("PROMPT_LOG_LEVEL", None)
            prompt_log_level = prompt_log_level.upper()
            if prompt_log_level in ["DEBUG", "CRITICAL", "ERROR", "WARNING", "INFO"]:
                loglevel = getattr(logging, prompt_log_level)
    if type(loglevel) in [str]:
        loglevel = loglevel.upper()
        loglevel = getattr(logging, loglevel)

    if log_file is not None:
        logging.basicConfig(
            filename=log_file,
            level=loglevel,
            format="%(asctime)s\t%(threadName)s\t%(name)s\t%(levelname)s\t%(message)s",
        )
    elif stream is None:
        if os.environ.get("PROMPT_LOG_FILE", None):
            prompt_log_file = os.environ.get("PROMPT_LOG_FILE", None)
            logging.basicConfig(
                filename=prompt_log_file,
                level=loglevel,
                format="%(asctime)s\t%(threadName)s\t%(name)s\t%(levelname)s\t%(message)s",
            )
        else:
            logging.basicConfig(
                stream=sys.stdout,
                level=loglevel,
                format="%(asctime)s\t%(threadName)s\t%(name)s\t%(levelname)s\t%(message)s",
            )
    else:
        logging.basicConfig(
            stream=stream,
            level=loglevel,
            format="%(asctime)s\t%(threadName)s\t%(name)s\t%(levelname)s\t%(message)s",
        )
    logging.Formatter.converter = time.gmtime


def extract_version_from_filename(filename):
    """
    Extract the EPIC campaign version string from the XRootD / local path.

    Example path:
        root://dtn-eic.jlab.org:1094//volatile/eic/EPIC//FULL/26.03.0/...
    Returns the version string (e.g. '26.03.0'), or the fallback '26.03.0'.
    """
    match = re.search(r'/FULL/(\d+\.\d+\.\d+)/', filename)
    if match:
        return match.group(1)
    return "26.03.0"


def extract_payload_fields(payload, logger=None):
    """
    Extract, validate, and derive all standard fields from a slice payload dict.

    Parameters
    ----------
    payload : dict
        Broker ``slice`` message ``content`` dict.
    logger : logging.Logger, optional
        Logger to use for informational messages.  Defaults to a logger named
        after this module.

    Returns
    -------
    fields : dict or None
        Extracted and derived fields on success, ``None`` on validation error.
    error : str or None
        Human-readable error message on failure, ``None`` on success.

    Fields in the returned dict
    ---------------------------
    filename, start, end, execution_id, slice_id, run_id,
    input_filename, tf_filename, input_tf_filename,
    version, nevents, nskip,
    workdir, output_filename, output_file,
    eicrecon_timeout
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    filename = payload.get("filename")
    start = payload.get("start")
    end = payload.get("end")
    execution_id = payload.get("execution_id", "unknown")
    slice_id = payload.get("slice_id", 0)
    run_id = payload.get("run_id", "unknown")
    input_filename = os.path.splitext(os.path.basename(filename))[0] if filename else "unknown"
    tf_filename = payload.get("tf_filename")
    input_tf_filename = (
        os.path.splitext(os.path.basename(tf_filename))[0] if tf_filename else input_filename
    )

    if not filename:
        return None, "Missing 'filename' in payload"
    if start is None or end is None:
        return None, "Missing 'start' or 'end' in payload"

    try:
        start = int(start)
        end = int(end)
    except (ValueError, TypeError) as exc:
        return None, f"Invalid 'start' or 'end' values: {exc}"

    if end < start:
        return None, f"'end' ({end}) must be >= 'start' ({start})"

    version = payload.get("epic_version") or extract_version_from_filename(filename)
    logger.info(
        f"EPIC version: {version} "
        f"(source: {'payload' if payload.get('epic_version') else 'filename'})"
    )

    nevents = end - start + 1
    nskip = start

    workdir = os.environ.get("WORKDIR") or payload.get("workdir") or os.getcwd()
    os.makedirs(workdir, exist_ok=True)

    output_filename = (
        f"{input_tf_filename}_run_{run_id}_slice_{slice_id}_s{start}_e{end}.edm4eic.root"
    )
    output_file = os.path.join(workdir, output_filename)

    try:
        eicrecon_timeout = int(os.environ.get("EICRECON_TIMEOUT", "3600"))
    except (ValueError, TypeError):
        eicrecon_timeout = 3600

    logger.info(
        f"eicrecon plan: file={filename}, nskip={nskip}, nevents={nevents}, "
        f"output={output_file}"
    )

    return {
        "filename": filename,
        "start": start,
        "end": end,
        "execution_id": execution_id,
        "slice_id": slice_id,
        "run_id": run_id,
        "input_filename": input_filename,
        "tf_filename": tf_filename,
        "input_tf_filename": input_tf_filename,
        "version": version,
        "nevents": nevents,
        "nskip": nskip,
        "workdir": workdir,
        "output_filename": output_filename,
        "output_file": output_file,
        "eicrecon_timeout": eicrecon_timeout,
    }, None
