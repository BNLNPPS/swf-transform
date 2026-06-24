#!/usr/bin/env python
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0OA
#
# Authors:
# - Wen Guan, <wen.guan@cern.ch>, 2025 - 2026

import json
import logging
import os
import shutil
import subprocess
import time

import zmq

from swf_transform.prompt.utils import extract_payload_fields, extract_version_from_filename



_SOCKET_FILENAME = "eicrecon_zmq.sock"

class ZeroMQProcessor:
    """
    Manages a long-running eicrecon process that listens for ZeroMQ requests and
    processes them one at a time via a REQ-REP socket pair.

    The eicrecon daemon is started inside a Singularity container the first time
    a payload arrives (if the socket file does not yet exist).  Subsequent payloads
    reuse the same daemon without restarting it.
    """

    def __init__(
        self,
        socket_path=None,
        startup_timeout=180,
        request_timeout=300,
        epic_version=None,
        epic_image=None,
        stdout=None,
        stderr=None
    ):
        self._socket_path = socket_path
        self._startup_timeout = startup_timeout
        self._request_timeout = request_timeout
        self._proc = None
        self._epic_version = epic_version
        self._epic_image = epic_image
        self._stdout = stdout
        self._stderr = stderr
        self._stdout_fh = None
        self._stderr_fh = None
        self._logger = logging.getLogger("ZeroMQProcessor")

    # ---------------------------------------------------------------------- #
    # Internal helpers
    # ---------------------------------------------------------------------- #

    def _start_eicrecon(self):
        """Launch eicrecon inside Singularity as a background daemon."""
        script = (
            "set -e\n"
            f'SINGULARITY_IMAGE="{self._epic_image}"\n'
            "singularity exec \\\n"
            "  -B /cvmfs:/cvmfs \\\n"
            '  "${SINGULARITY_IMAGE}" \\\n'
            "  /bin/bash -c \"\n"
            "set -e\n"
            f"source /opt/detector/epic-{self._epic_version}/bin/thisepic.sh\n"
            f"eicrecon -Ppodio:managed_socket_path={self._socket_path} -Pjana:timeout=0\"\n"
        )
        self._logger.info(
            f"Starting eicrecon daemon via Singularity (socket: {self._socket_path})"
        )
        self._stdout_fh = open(self._stdout, "w") if self._stdout else subprocess.DEVNULL
        self._stderr_fh = open(self._stderr, "w") if self._stderr else subprocess.DEVNULL
        self._proc = subprocess.Popen(
            ["bash", "-c", script],
            stdout=self._stdout_fh,
            stderr=self._stderr_fh,
        )
        self._logger.info(f"eicrecon daemon started with PID {self._proc.pid}")
        self._logger.info(f"eicrecon daemon script:\n{script}")

    def _wait_for_socket(self):
        """Block until the socket file appears, or until startup_timeout is reached."""
        deadline = time.time() + self._startup_timeout
        while time.time() < deadline:
            if os.path.exists(self._socket_path):
                self._logger.info(f"Socket {self._socket_path} is ready")
                return True
            if self._proc is not None and self._proc.poll() is not None:
                stderr_tail = ""
                if self._stderr and os.path.exists(self._stderr):
                    try:
                        with open(self._stderr) as f:
                            stderr_tail = f.read()[-500:]
                    except Exception:
                        pass
                raise RuntimeError(
                    f"eicrecon exited prematurely (rc={self._proc.returncode}). "
                    f"stderr: {stderr_tail}"
                )
            time.sleep(2)
        return False

    def ensure_running(self):
        """Start the eicrecon daemon if the socket does not already exist."""
        if os.path.exists(self._socket_path):
            self._logger.info(
                f"eicrecon socket {self._socket_path} already exists; "
                "assuming daemon is running"
            )
            return

        self._start_eicrecon()
        if not self._wait_for_socket():
            raise RuntimeError(
                f"eicrecon did not create socket {self._socket_path} "
                f"within {self._startup_timeout}s"
            )

    def soft_terminate(self, hard_timeout=30):
        """Ask the eicrecon daemon to shut down gracefully via ZeroMQ, then hard-kill if needed.

        Sends a ``{"terminate": true}`` request and waits up to *hard_timeout* seconds
        for the process to exit.  Falls back to :meth:`terminate` if it does not.
        """
        if not os.path.exists(self._socket_path):
            self._logger.info("Socket does not exist; skipping soft terminate")
            return

        self._logger.info(f"Sending soft-terminate request to {self._socket_path}")
        context = zmq.Context()
        socket = context.socket(zmq.REQ)
        try:
            socket.setsockopt(zmq.SNDTIMEO, 5000)
            socket.setsockopt(zmq.RCVTIMEO, 10000)
            socket.connect(f"ipc://{self._socket_path}")
            socket.send_string(json.dumps({"terminate": True}))
            try:
                response_str = socket.recv_string()
                self._logger.info(f"Soft-terminate response: {response_str}")
            except zmq.Again:
                self._logger.warning("No response to soft-terminate request")
        except zmq.ZMQError as exc:
            self._logger.warning(f"ZMQ error during soft terminate: {exc}")
        finally:
            try:
                socket.close()
                context.term()
            except Exception:
                pass

        # Wait for the process to exit on its own
        if self._proc is not None:
            try:
                self._proc.wait(timeout=hard_timeout)
                self._logger.info("eicrecon daemon exited after soft terminate")
                # Still clean up the socket file if it remains
                if self._socket_path and os.path.exists(self._socket_path):
                    try:
                        os.unlink(self._socket_path)
                    except OSError:
                        pass
                return
            except subprocess.TimeoutExpired:
                self._logger.warning(
                    f"eicrecon did not exit within {hard_timeout}s after soft terminate; "
                    "falling back to hard terminate"
                )

        self.terminate()

    def terminate(self):
        """Terminate the eicrecon daemon and remove the socket file."""
        if self._proc is not None and self._proc.poll() is None:
            self._logger.info(f"Terminating eicrecon daemon (PID {self._proc.pid})")
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
                self._logger.info("eicrecon daemon exited cleanly")
            except subprocess.TimeoutExpired:
                self._logger.warning("eicrecon did not exit within 10s, sending SIGKILL")
                self._proc.kill()
                self._proc.wait()
                self._logger.info("eicrecon daemon killed")

        if self._socket_path and os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
                self._logger.info(f"Removed socket file: {self._socket_path}")
            except OSError as exc:
                self._logger.warning(f"Could not remove socket file {self._socket_path}: {exc}")

        for fh in (self._stdout_fh, self._stderr_fh):
            try:
                if fh and fh not in (subprocess.DEVNULL,):
                    fh.close()
            except Exception:
                pass
        self._stdout_fh = None
        self._stderr_fh = None

    def print_logs(self):
        """Print the eicrecon stdout and stderr files to the logger."""
        for label, path in (("stdout", self._stdout), ("stderr", self._stderr)):
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path) as f:
                    content = f.read()
                if content:
                    self._logger.info(f"eicrecon {label} ({path}):\n{content}")
                else:
                    self._logger.info(f"eicrecon {label} ({path}): (empty)")
            except Exception as exc:
                self._logger.warning(f"Could not read eicrecon {label} {path}: {exc}")

    # ---------------------------------------------------------------------- #
    # Public API
    # ---------------------------------------------------------------------- #

    def process(self, payload):
        """
        Send *payload* to the eicrecon daemon and return ``(status, result, error)``.

        Parameters
        ----------
        payload : dict
            Broker ``slice`` message ``content`` dict.  Expected keys match those
            of :func:`process_payload_eicrecon` in ``payload_process.py``.

        Returns
        -------
        status : bool
        result : dict or None
        error  : str or None
        """
        # ------------------------------------------------------------------ #
        # Extract and validate required fields
        # ------------------------------------------------------------------ #
        fields, error = extract_payload_fields(payload, self._logger)
        if error:
            return False, None, error

        filename = fields["filename"]
        run_id = fields["run_id"]
        version = fields["version"]
        output_filename = fields["output_filename"]
        output_file = fields["output_file"]

        # ------------------------------------------------------------------ #
        # Ensure daemon is up
        # ------------------------------------------------------------------ #
        try:
            self.ensure_running()
        except RuntimeError as exc:
            return False, None, str(exc)

        # ------------------------------------------------------------------ #
        # Send ZeroMQ request and await response
        # ------------------------------------------------------------------ #
        context = zmq.Context()
        socket = context.socket(zmq.REQ)
        try:
            socket.setsockopt(zmq.RCVTIMEO, self._request_timeout * 1000)
            socket.setsockopt(zmq.SNDTIMEO, 5000)
            socket.connect(f"ipc://{self._socket_path}")

            request = {
                "input_file": filename,
                "output_file": output_file,
                "start": fields["start"],
                "end": fields["end"],
                "run_id": run_id,
            }
            self._logger.info(f"Sending ZeroMQ request: {request}")

            t0 = time.time()
            socket.send_string(json.dumps(request))

            response_str = socket.recv_string()
            elapsed = time.time() - t0

            response = json.loads(response_str)
            self._logger.info(
                f"ZeroMQ response received after {elapsed:.1f}s: status={response.get('status')}"
            )

            if response.get("status") != "completed":
                error = response.get(
                    "message",
                    f"eicrecon returned unexpected status '{response.get('status')}'",
                )
                self._logger.error(f"eicrecon processing error: {error}")
                return False, None, error

        except zmq.Again:
            error = f"ZeroMQ request timed out after {self._request_timeout}s"
            self._logger.error(error)
            return False, None, error

        except zmq.ZMQError as exc:
            error = f"ZeroMQ error: {exc}"
            self._logger.error(error)
            return False, None, error

        except json.JSONDecodeError as exc:
            error = f"Failed to parse eicrecon response: {exc}"
            self._logger.error(error)
            return False, None, error

        except Exception as exc:
            error = f"ZeroMQ processing failed: {exc}"
            self._logger.error(error, exc_info=True)
            return False, None, error

        finally:
            try:
                socket.close()
                context.term()
            except Exception:
                pass

        # ------------------------------------------------------------------ #
        # Copy output to dest_path if requested
        # ------------------------------------------------------------------ #
        dest_path = payload.get("dest_path")
        dest_file = None
        if dest_path:
            dest_dir = os.path.join(dest_path, str(run_id))
            os.makedirs(dest_dir, exist_ok=True)
            dest_file = os.path.join(dest_dir, output_filename)
            try:
                shutil.copy2(output_file, dest_file)
                self._logger.info(f"Copied output file to: {dest_file}")
            except OSError as exc:
                return False, None, f"Failed to copy output file to {dest_file}: {exc}"
        else:
            self._logger.info(
                f"No dest_path in payload; output file left at: {output_file}"
            )

        # ------------------------------------------------------------------ #
        # Return enriched payload
        # ------------------------------------------------------------------ #
        processed_payload = payload.copy()
        processed_payload["processed"] = True
        processed_payload["actual_processing_time"] = elapsed
        processed_payload["output_file"] = dest_file or output_file
        processed_payload["output_filename"] = output_filename
        processed_payload["events_processed"] = response.get("events_processed")
        processed_payload["epic_version"] = version
        processed_payload["state"] = "processed"

        return True, processed_payload, None


# One processor per socket path — each job's workdir gets its own daemon.
_processors = {}


def _get_processor(payload):
    logger = logging.getLogger("ZeroMQProcessor")

    workdir = os.environ.get("WORKDIR") or payload.get("workdir") or os.getcwd()
    socket_path = payload.get("zmq_socket_path", os.path.join(workdir, _SOCKET_FILENAME))

    if socket_path in _processors:
        return _processors[socket_path]

    startup_timeout = int(payload.get("zmq_startup_timeout", 180))
    request_timeout = int(
        os.environ.get("EICRECON_TIMEOUT", payload.get("zmq_request_timeout", 300))
    )
    logger.info(
        f"ZeroMQProcessor config: socket_path={socket_path}, "
        f"startup_timeout={startup_timeout}s, request_timeout={request_timeout}s"
    )

    filename = payload.get("filename", "")
    epic_version = payload.get("epic_version") or extract_version_from_filename(filename)
    logger.info(
        f"EPIC version: {epic_version} "
        f"(source: {'payload' if payload.get('epic_version') else 'filename'})"
    )

    default_epic_image = (
        f"/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:{epic_version}-stable"
    )
    epic_image = payload.get("epic_image") or default_epic_image
    logger.info(
        f"Using Singularity image: {epic_image} "
        f"(source: {'payload' if payload.get('epic_image') else 'default'})"
    )

    workdir = payload.get("workdir") or os.getcwd()
    stdout = os.path.join(workdir, "eicrecon.stdout")
    stderr = os.path.join(workdir, "eicrecon.stderr")

    processor = ZeroMQProcessor(
        socket_path=socket_path,
        startup_timeout=startup_timeout,
        request_timeout=request_timeout,
        epic_version=epic_version,
        epic_image=epic_image,
        stdout=stdout,
        stderr=stderr
    )
    _processors[socket_path] = processor
    return processor


def process_payload_zmq(payload):
    """
    Process a slice payload by sending it to a long-running eicrecon daemon via
    ZeroMQ.  The daemon is started inside a Singularity container if it is not
    already running.

    Parameters
    ----------
    payload : dict
        Broker ``slice`` message ``content`` dict.

    Optional payload keys
    ---------------------
    zmq_socket_path     : str  – IPC socket path (default: ``/tmp/wguan_eicrecon_managed.sock``)
    zmq_startup_timeout : int  – seconds to wait for daemon startup (default: 180)
    zmq_request_timeout : int  – seconds to wait for a single response (default: 300)
                                 Can also be set via ``EICRECON_TIMEOUT`` env var.

    Returns
    -------
    status : bool
    result : dict or None
    error  : str or None
    """
    processor = _get_processor(payload)
    return processor.process(payload)


def terminate_zmq_processors():
    """Gracefully terminate all running ZeroMQ eicrecon daemons and clear the registry."""
    for processor in list(_processors.values()):
        processor.soft_terminate()
        processor.print_logs()
    _processors.clear()
