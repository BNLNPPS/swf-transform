#!/usr/bin/env python
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0OA
#
# Authors:
# - Wen Guan, <wen.guan@cern.ch>, 2025 - 2026

import logging
import os
import re
import subprocess
import tempfile
import time


def _find_script_template():
    """
    Locate eicrecon_process.sh using multiple search strategies so that the
    code works both in a source checkout and after run_prompt_wrapper extracts
    its zip payload.

    Search order
    ------------
    1. ``$SWF_TRANSFORM_WRAPPER`` env var (explicit deployment override).
    2. Relative to *this file* – works for source / editable installs where
       the layout is ``lib/swf_transform/prompt/`` → ``../../../wrapper/``.
    3. Relative to ``os.getcwd()`` – works when ``run_prompt_wrapper`` has
       extracted everything into the current working directory, placing
       ``wrapper/`` alongside ``lib_py/``.
    """
    script_name = "eicrecon_process.sh"

    # 1. Explicit env-var override
    env_dir = os.environ.get("SWF_TRANSFORM_WRAPPER")
    if env_dir:
        candidate = os.path.join(env_dir, script_name)
        if os.path.exists(candidate):
            return candidate

    # 2. Relative to __file__: lib[_py]/swf_transform/prompt/ → ../../../wrapper/
    candidate = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../wrapper", script_name)
    )
    if os.path.exists(candidate):
        return candidate

    # 3. Relative to CWD (run_prompt_wrapper extraction directory)
    candidate = os.path.join(os.getcwd(), "wrapper", script_name)
    if os.path.exists(candidate):
        return candidate

    # Return the __file__-relative path so the caller gets a clear OSError
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../wrapper", script_name)
    )


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


def process_payload(payload):
    """
    Process a slice payload by running eicrecon on the input file inside the
    EIC Singularity container.

    The *payload* dict is the ``content`` sub-field of a broker ``slice``
    message and must contain:

        filename     : str  – XRootD / local path of the input edm4hep ROOT file
        start        : int  – first event index to process (0-based; events
                              before this index are skipped)
        end          : int  – last event index to process (inclusive)
        slice_id     : int  – identifies this slice within the run
        execution_id : str  – used to build the output file name

    Optional environment variables honoured:
        WORKDIR              – directory for output files  (default: cwd)
        EICRECON_TIMEOUT     – subprocess timeout in seconds  (default: 3600)

    Returns
    -------
    status : bool   True on success, False on failure.
    result : dict   Copy of *payload* enriched with processing metadata,
                    or None on failure.
    error  : str    Human-readable error message, or None on success.
    """
    logger = logging.getLogger("PayloadProcessor")
    logger.info(f"Processing payload: {payload}")

    # ------------------------------------------------------------------ #
    # Extract required fields
    # ------------------------------------------------------------------ #
    filename = payload.get("filename")
    start = payload.get("start")
    end = payload.get("end")
    execution_id = payload.get("execution_id", "unknown")
    slice_id = payload.get("slice_id", 0)

    if not filename:
        return False, None, "Missing 'filename' in payload"
    if start is None or end is None:
        return False, None, "Missing 'start' or 'end' in payload"

    try:
        start = int(start)
        end = int(end)
    except (ValueError, TypeError) as exc:
        return False, None, f"Invalid 'start' or 'end' values: {exc}"

    if end < start:
        return False, None, f"'end' ({end}) must be >= 'start' ({start})"

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #
    version = payload.get("epic_version") or extract_version_from_filename(filename)
    logger.info(f"EPIC version: {version} (source: {'payload' if payload.get('epic_version') else 'filename'})")

    nevents = end - start + 1
    nskip = start

    workdir = os.environ.get("WORKDIR") or payload.get("workdir") or os.getcwd()
    os.makedirs(workdir, exist_ok=True)

    output_file = os.path.join(
        workdir, f"{execution_id}_slice_{int(slice_id):03d}.edm4eic.root"
    )

    try:
        eicrecon_timeout = int(os.environ.get("EICRECON_TIMEOUT", "3600"))
    except (ValueError, TypeError):
        eicrecon_timeout = 3600

    logger.info(
        f"eicrecon plan: file={filename}, nskip={nskip}, nevents={nevents}, "
        f"output={output_file}"
    )

    # ------------------------------------------------------------------ #
    # Load the script template and fill in the placeholders
    # ------------------------------------------------------------------ #
    try:
        with open(_find_script_template()) as fh:
            script_content = fh.read()
    except OSError as exc:
        return False, None, f"Cannot read eicrecon script template: {exc}"

    script_content = script_content.replace("{EPIC_VERSION}", version)
    script_content = script_content.replace("{INPUT_FILE}", filename)
    script_content = script_content.replace("{OUTPUT_FILE}", output_file)
    script_content = script_content.replace("{NEVENTS}", str(nevents))
    script_content = script_content.replace("{NSKIP}", str(nskip))

    # ------------------------------------------------------------------ #
    # Write, execute, and clean up the filled-in script
    # ------------------------------------------------------------------ #
    script_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False,
            prefix="eicrecon_", dir=workdir
        ) as fh:
            fh.write(script_content)
            script_path = fh.name

        os.chmod(script_path, 0o755)
        logger.info(f"Processing script written to: {script_path}")
        logger.debug(f"Script content:\n{script_content}")

        t0 = time.time()
        proc = subprocess.run(
            ["/bin/bash", script_path],
            capture_output=True,
            text=True,
            timeout=eicrecon_timeout,
        )
        elapsed = time.time() - t0

        logger.info(
            f"eicrecon finished in {elapsed:.1f}s, returncode={proc.returncode}"
        )
        if proc.stdout:
            logger.info(f"stdout:\n{proc.stdout}")
        if proc.stderr:
            logger.warning(f"stderr:\n{proc.stderr}")

        if proc.returncode != 0:
            error = (
                f"eicrecon exited with returncode={proc.returncode}. "
                f"stderr (last 500 chars): {proc.stderr[-500:]}"
            )
            logger.error(error)
            return False, None, error

        # ------------------------------------------------------------ #
        # Success — enrich result dict
        # ------------------------------------------------------------ #
        processed_payload = payload.copy()
        processed_payload["processed"] = True
        processed_payload["actual_processing_time"] = elapsed
        processed_payload["output_file"] = output_file
        processed_payload["nevents_processed"] = nevents
        processed_payload["epic_version"] = version

        return True, processed_payload, None

    except subprocess.TimeoutExpired:
        error = f"eicrecon processing timed out after {eicrecon_timeout}s"
        logger.error(error)
        return False, None, error

    except Exception as exc:
        error = f"Failed to run eicrecon: {exc}"
        logger.error(error, exc_info=True)
        return False, None, error

    finally:
        if script_path and os.path.exists(script_path):
            try:
                os.unlink(script_path)
            except OSError:
                pass
