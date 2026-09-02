#!/usr/bin/env python
"""EJFAT streaming receiver.

This module provides `EJFATSubscriber`, which uses the e2sar_py
Reassembler to pull slice events off the EJFAT load-balanced data
plane, exposing the same interface as `brokers.activemq.Subscriber`
so `Transformer.run()` can drive it interchangeably.
"""
import base64
import datetime
import json
import logging
import os
import pickle
import re
import socket
import threading
import time

try:
    # prefer local package layout
    from .payload_process import process_payload
except Exception:
    # fallback to installed package layout
    from swf_transform.prompt.payload_process import process_payload


# Try to import e2sar_py (external e2sar Python bindings). The real
# e2sar_py API may differ; we attempt common entry points defensively
# and log helpful messages when the import fails.
try:
    import e2sar_py  # type: ignore
    _HAS_E2SAR = True
except Exception:
    e2sar_py = None  # type: ignore
    _HAS_E2SAR = False


class EJFATSubscriber:
    """Receive slice events over the EJFAT data plane using e2sar_py.

    Exposes the same public interface as `brokers.activemq.Subscriber`
    (monitor/is_idle/idle_elapsed/idle_left/waiting_since/update_selector/stop)
    so `Transformer.run()` can drive it interchangeably with the STOMP-based
    subscriber, while internally using an e2sar_py Reassembler to pull events
    off the EJFAT load-balanced data plane instead of a message broker.
    """

    def __init__(
        self,
        broker=None,
        handler=None,
        handler_kwargs=None,
        selector=None,
        namespace=None,
        name="EJFATSubscriber",
        idle_seconds=5,
        port=0,
        threads=1,
        data_ip=None,
        node_name=None,
        **kwargs,
    ):
        self.broker = broker or {}
        self.handler = handler
        self.handler_kwargs = handler_kwargs if handler_kwargs else {}
        self.selector = selector
        self.namespace = namespace
        self.name = name
        self.logger = logging.getLogger(name)

        self.idle_seconds = int(idle_seconds)
        self.last_message_at = time.time()
        self.is_processing_message = False
        self.has_connection_failures = False

        self.port = port
        self.threads = threads
        self.data_ip = data_ip
        self.node_name = node_name or namespace or name

        self._reas = None
        self._rflags = None
        self.graceful_stop = threading.Event()
        self._thread = None

    def _uri_str(self):
        return self.broker.get("admin_uri") if isinstance(self.broker, dict) else None

    def _connect(self):
        if not _HAS_E2SAR:
            raise ImportError("e2sar_py is required for EJFATSubscriber")

        uri_str = self._uri_str()
        if not uri_str:
            raise ValueError("No 'admin_uri' found in ejfat broker configuration")
        uri = _load_uri({"uri": uri_str})

        RFlags = getattr(e2sar_py.DataPlane.Reassembler, "ReassemblerFlags", None)
        rflags = RFlags() if RFlags else None
        if rflags is not None and hasattr(rflags, "useCP"):
            setattr(rflags, "useCP", True)

        ReassemblerCls = getattr(e2sar_py.DataPlane, "Reassembler", None)
        if ReassemblerCls is None:
            raise RuntimeError("e2sar_py.DataPlane.Reassembler not found")

        if self.data_ip:
            data_ip = e2sar_py.IPAddress.from_string(self.data_ip) if hasattr(e2sar_py, "IPAddress") else self.data_ip
            reas = ReassemblerCls(uri, data_ip, self.port, self.threads, rflags)
        else:
            reas = ReassemblerCls(uri, self.port, self.threads, rflags)

        if rflags is not None and getattr(rflags, "useCP", False):
            try:
                _unwrap(getattr(reas, "registerWorker", lambda *a, **k: None)(self.node_name), "registering worker")
            except Exception:
                self.logger.exception(f"[ejfat] [{self.name}]: failed to register EJFAT worker")

        _unwrap(getattr(reas, "OpenAndStart", lambda: None)(), "starting reassembler")
        self._rflags = rflags
        self._reas = reas
        self.has_connection_failures = False
        self.logger.info(
            f"[ejfat] [{self.name}]: reassembler started (node_name={self.node_name}, port={self.port}, threads={self.threads})"
        )

    def _selector_run_id(self):
        if not self.selector:
            return None
        match = re.search(r"run_id\s*=\s*'([^']*)'", self.selector)
        return match.group(1) if match else None

    def _dispatch(self, recv_bytes, event_num, data_id):
        try:
            msg = json.loads(recv_bytes)
        except Exception:
            self.logger.error(f"[ejfat] [{self.name}]: failed to decode event #{event_num} as JSON; skipping")
            return

        if self.namespace is not None and msg.get("namespace") not in (None, self.namespace):
            self.logger.debug(f"[ejfat] [{self.name}]: skipping event #{event_num}: namespace mismatch")
            return

        expected_run_id = self._selector_run_id()
        if expected_run_id is not None and msg.get("run_id") != expected_run_id:
            self.logger.debug(f"[ejfat] [{self.name}]: skipping event #{event_num}: run_id mismatch")
            return

        if self.handler is not None:
            header = {"event_num": event_num, "data_id": data_id}
            self.handler(header, msg, self.handler_kwargs)

    def _shutdown_reassembler(self):
        if self._reas is None:
            return
        try:
            if self._rflags is not None and getattr(self._rflags, "useCP", False):
                try:
                    self._reas.deregisterWorker()
                except Exception:
                    pass
            self._reas.stopThreads()
        except Exception:
            self.logger.exception(f"[ejfat] [{self.name}]: error during reassembler shutdown")
        finally:
            self._reas = None

    def _run_loop(self):
        try:
            self._connect()
        except Exception:
            self.logger.exception(f"[ejfat] [{self.name}]: failed to initialize EJFAT reassembler")
            self.has_connection_failures = True
            return

        while not self.graceful_stop.is_set():
            try:
                try:
                    recv = self._reas.recvEventBytes(wait_ms=200)
                except TypeError:
                    recv = self._reas.recvEventBytes()
            except Exception:
                self.logger.exception(f"[ejfat] [{self.name}]: error receiving event")
                self.has_connection_failures = True
                time.sleep(1)
                continue

            if not recv:
                continue
            if not (isinstance(recv, tuple) and len(recv) >= 4):
                self.logger.warning(f"[ejfat] [{self.name}]: unexpected recvEventBytes return: {recv}")
                continue

            recv_len, recv_bytes, event_num, data_id = recv[:4]
            if recv_len == -2:
                self.logger.error(f"[ejfat] [{self.name}]: receive error, continuing")
                continue
            if recv_len == -1:
                continue

            self.is_processing_message = True
            try:
                self._dispatch(recv_bytes, event_num, data_id)
            except Exception:
                self.logger.exception(f"[ejfat] [{self.name}]: failed to handle event #{event_num}")
            finally:
                self.last_message_at = time.time()
                self.is_processing_message = False

        self._shutdown_reassembler()

    def update_selector(self, selector):
        """Narrow client-side filtering (e.g. once a run_id becomes known from the
        first received event); EJFAT has no broker-side selector to push down to.
        """
        self.selector = selector
        self.logger.info(f"[ejfat] [{self.name}]: updated selector to: {selector}")

    def idle_elapsed(self):
        return max(0.0, time.time() - self.last_message_at)

    def idle_left(self, idle_seconds=None):
        if idle_seconds is None:
            idle_seconds = self.idle_seconds
        return max(0.0, float(idle_seconds) - self.idle_elapsed())

    def waiting_since(self):
        return datetime.datetime.utcfromtimestamp(self.last_message_at).strftime("%Y-%m-%d %H:%M:%S UTC")

    def is_idle(self, idle_seconds=None):
        if self.is_processing_message:
            return False
        if idle_seconds is None:
            idle_seconds = self.idle_seconds
        return self.idle_elapsed() > float(idle_seconds)

    def fail(self):
        self.has_connection_failures = True

    def monitor(self):
        if self.graceful_stop.is_set():
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_loop, name=self.name, daemon=True)
            self._thread.start()

    def stop(self, timeout=10):
        self.graceful_stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)


def _write_root_events(payload_b64, content, logger):
    """Reconstruct the events pickled into an EJFAT slice payload as a local ROOT file.

    Mirrors `fast_processing_ejfat._read_eic_root_events` on the sending side:
    the sender reads a [tf_first, tf_first+tf_count) event range out of the
    'events' TTree with uproot and pickles the resulting awkward array as the
    EJFAT event payload. This writes that array back to a new 'events' TTree
    in a local ROOT file, so eicrecon has a real file to process without
    needing shared-filesystem access to the original STF file.

    Returns the path to the new ROOT file.
    """
    try:
        import uproot
    except ImportError as exc:
        raise RuntimeError(
            "processing EJFAT slice events requires the uproot package "
            "(https://github.com/scikit-hep/uproot5) to be installed and importable."
        ) from exc

    events = pickle.loads(base64.b64decode(payload_b64))

    workdir = os.environ.get("WORKDIR") or content.get("workdir") or os.getcwd()
    os.makedirs(workdir, exist_ok=True)

    tf_filename = content.get("tf_filename")
    input_tf_filename = (
        os.path.splitext(os.path.basename(tf_filename))[0] if tf_filename else "unknown"
    )
    run_id = content.get("run_id", "unknown")
    slice_id = content.get("slice_id", 0)
    root_filename = f"{input_tf_filename}_run_{run_id}_slice_{slice_id}.ejfat.root"
    root_file = os.path.join(workdir, root_filename)

    with uproot.recreate(root_file) as f:
        f["events"] = events

    logger.info(f"Wrote {len(events)} EJFAT events to ROOT file: {root_file}")
    return root_file


def _ejfat_transformer_handler(transformer, header, msg, handler_kwargs=None):
    """Process a slice message received over the EJFAT data plane.

    For real (non fake/mock) slices, the events travel as a pickled awkward
    array in `content['payload']` rather than a shared-filesystem path,
    since `content['filename']` is the original STF file on the sending
    node. Before dispatching to `process_payload`, those events are written
    out to a local ROOT file (see `_write_root_events`) and `content` is
    updated to point at it, so `process_payload` can hand it to eicrecon
    (typically via the ZeroMQ daemon, per `content['processor_type']`) the
    same way it would for a file received over ActiveMQ.

    Mirrors `Transformer.transformer_handler`, but lives here so EJFAT-specific
    message handling stays alongside the EJFAT receiver implementation. Called
    as `ejfat._ejfat_transformer_handler(self, header, msg, handler_kwargs)`
    from `Transformer.ejfat_transformer_handler`.
    """
    handler_kwargs = handler_kwargs if handler_kwargs else {}
    logger = transformer.logger

    msg_type = msg.get("msg_type")
    run_id = msg.get("run_id")
    logger.debug(f"Received EJFAT message: msg_type={msg_type}, run_id={run_id}, header={header}")

    result_publisher = handler_kwargs.get("result_publisher")

    # First message received without a pre-assigned run_id: lock the subscriber
    # onto this run so further events for other runs are filtered out client-side.
    if run_id and not transformer._run_id:
        if transformer.transformer_subscriber is not None:
            try:
                transformer.transformer_subscriber.update_selector(f"run_id = '{run_id}'")
                logger.info(
                    f"No run_id was pre-assigned; locking EJFAT subscriber to run_id={run_id} from first message"
                )
            except Exception:
                logger.exception(
                    f"Failed to update EJFAT subscriber selector for run_id={run_id}"
                )
        transformer._run_id = run_id

    processing_start_at = datetime.datetime.utcnow().isoformat()
    status = False
    result = None
    error = None

    if msg_type == "slice":
        content = dict(msg.get("content") or {})
        payload_b64 = content.pop("payload", None)
        try:
            if payload_b64 and content.get("file_type") not in ("fake", "mock"):
                root_file = _write_root_events(payload_b64, content, logger)
                content["filename"] = root_file
                content["start"] = 0
                content["end"] = content.get("tf_count", 1) - 1
            status, result, error = process_payload(content)
            if status:
                logger.info(
                    f"Processed slice message successfully: run_id={run_id}, result={result}, error={error}"
                )
            else:
                logger.error(
                    f"Failed to process slice message: run_id={run_id}, result={result}, error={error}"
                )
        except Exception as ex:
            error = str(ex)
            logger.exception(f"Exception while processing payload for run_id={run_id}: {ex}")
    else:
        logger.warning(f"Unknown msg_type received in ejfat_transformer_handler: {msg_type}")

    slice_result_msg = {
        "msg_type": "slice_result",
        "run_id": run_id,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "content": {
            "requested_at": msg.get("created_at"),
            "processing_start_at": processing_start_at,
            "processed_at": datetime.datetime.utcnow().isoformat(),
            "state": "done" if status else "failed",
            "hostname": socket.getfqdn(),
            "panda_server_url": os.environ.get("PANDA_SERVER_URL", None),
            "panda_task_id": os.environ.get("PanDA_TaskID"),
            "panda_id": os.environ.get("PANDAID"),
            "harvester_id": os.environ.get("HARVESTER_WORKER_ID"),
            "result": {"state": status, "result": result, "error": error},
        },
    }

    if result_publisher is None:
        logger.warning("No result_publisher provided in handler_kwargs; skipping publish")
    else:
        try:
            result_publisher.publish(slice_result_msg)
            logger.info(f"Published slice_result message for run_id={run_id}: {slice_result_msg}")
        except Exception:
            logger.exception("Failed to publish slice_result_msg")


def _unwrap(val, desc: str = "operation"):
    """Helper that normalizes return values from e2sar_py calls.

    If `val` is a tuple (result, err) we return it. If it's a single value
    we return (val, None). On None we raise RuntimeError.
    """
    if val is None:
        raise RuntimeError(f"Failed while {desc}: returned None")
    if isinstance(val, tuple):
        return val
    return (val, None)


def _load_uri(args, token_type=None):
    """Construct an EjfatURI from args using common e2sar_py entry points.

    Tries several likely factory functions and raises ImportError if
    e2sar_py is not available.
    """
    if not _HAS_E2SAR:
        raise ImportError("e2sar_py is not installed; cannot load EjfatURI")

    uri_str = getattr(args, "uri", None) or (args.get("uri") if isinstance(args, dict) else None)
    if not uri_str:
        raise ValueError("No URI provided in args (args.uri)")

    # Try common constructors
    try:
        if hasattr(e2sar_py, "EjfatURI"):
            # Some bindings provide a from_string factory
            URI = e2sar_py.EjfatURI
            if hasattr(URI, "from_string"):
                return URI.from_string(uri_str)
            try:
                return URI(uri_str)
            except Exception:
                # fallback: some bindings expose parse_uri
                pass
        if hasattr(e2sar_py, "parse_ejfat_uri"):
            return e2sar_py.parse_ejfat_uri(uri_str)
    except Exception as exc:
        raise RuntimeError(f"Failed to construct EjfatURI from '{uri_str}': {exc}")

    raise RuntimeError("Could not build EjfatURI; e2sar_py API mismatch")
