# modules/api_sender.py
import requests
import logging
import time
import threading
import json
from datetime import datetime, timedelta, timezone
from config import (API_TIMEOUT, API_MAX_RETRIES, DB_PATH, CAMERA_NAME, API_ENDPOINT,
                    CAMERA_MAC_ID, ENABLE_PAYLOAD_HASH,
                    SSL_VERIFY, BLACKLIST_OFFLINE_FALLBACK, BLACKLIST_CACHE_FILE,
                    PALLET_MAX_KEG_IDS, PALLET_STRICT, PALLET_DEFAULT_LAYER,
                    PALLET_DEFAULT_RESULT, PALLET_SEND_LOCATION_NAME,
                    RETRY_ATTENTION_AFTER, RETRY_DRAIN_LIMIT)
import config   # identity (location/line/camera) is read live so Settings edits take effect
import sqlite3
import hashlib
import urllib3
from collections import deque
from modules import stack_store
from modules.database import DatabaseManager, retry_backoff_minutes, db_lock
from modules.utils import write_private_json

# Suppress SSL warnings only when verification is disabled
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP_PREFIX = "http://"     # the retry adapter is mounted on both schemes,
HTTPS_PREFIX = "https://"   # so the plain-HTTP test box gets the retry policy too.
CONTENT_TYPE_JSON = "application/json"
BLACKLIST_ENDPOINT_ERR_MSG = "Blacklist endpoint failed."

# Field names the cloud may use for the keg identifier inside a blacklist entry.
# The live API returns "serialNo", so that one must stay; the rest are tolerated
# variants. Defined once and shared by the operator page (which decides what to
# flag) and the Blacklist table (which displays it), so the copies can't drift.
BLACKLIST_ID_KEYS   = ("serialNo", "serial_no", "id", "kegId", "keg_id",
                       "qr", "qrCode", "code", "value", "batch")


def blacklist_entry_code(entry):
    """The keg identifier from one blacklist entry, or "" if it carries none."""
    if not isinstance(entry, dict):
        return str(entry).strip() if entry else ""
    for key in BLACKLIST_ID_KEYS:
        value = entry.get(key)
        if value:
            return str(value).strip()
    return ""


def blacklist_codes(entries):
    """Set of flagged keg codes from a blacklist response, for membership tests."""
    codes = (blacklist_entry_code(e) for e in (entries or []))
    return {c for c in codes if c}

# Per the pallet-report spec these are permanent failures. The payload or the
# cloud-side camera registration is wrong, so retrying the same body never helps:
#   400 bad payload (no macId / no kegIds / >500 codes / unknown paletteId)
#   403 the macId is not the camera the login token belongs to
#   404 camera MAC not registered in the cloud
#   409 strict mode rejected a code, or no code matched (wrong brewery)
NON_RETRYABLE_STATUS = (400, 403, 404, 409)

# Database access uses database.db_lock (imported above), the same lock the
# local-data wipe holds, so the retry monitor never writes while the files go.


def iso_utc(dt: datetime = None) -> str:
    """A timestamp in the exact shape the cloud contract uses.

        2026-08-29T09:15:04.120Z

    UTC, millisecond precision, trailing Z, not a local offset. A string like
    `...+05:30` with microseconds is valid ISO-8601 but differs from the
    contract examples, and the cloud compares these as strings (ordering
    layers, de-duplicating a replayed report).

    A naive datetime is treated as this machine's local time.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.astimezone()          # naive == this machine's local time
    dt = dt.astimezone(timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def to_iso_utc(value) -> str:
    """Normalise a caller-supplied time into iso_utc() form.

    scannedTime comes from the UI as `datetime.isoformat()` output, so it is
    re-parsed and re-rendered here, in one place. An unparseable value is passed
    through unchanged rather than replaced with "now"; reporting the send time
    as the scan time would misdate every keg on the pallet.
    """
    if value is None or value == "":
        return iso_utc()
    if isinstance(value, datetime):
        return iso_utc(value)
    text = str(value).strip()
    try:
        return iso_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        logging.getLogger(__name__).warning(
            f"scannedTime {text!r} is not ISO-8601 — sending it unchanged")
        return text


def clean_keg_ids(keg_ids):
    """Whitespace-trimmed, de-duplicated keg IDs in first-read order.

    A live decoder can return the same label on several frames, and a decode
    can carry stray whitespace/newlines. Sent as-is, the cloud would see
    duplicates and IDs that match no keg. Every report, retry and requeue goes
    through here. Order is kept (not sorted) so the payload reads in scan order.
    """
    seen, cleaned = set(), []
    for raw in (keg_ids or []):
        if raw is None:
            continue                      # don't let None become the text "None"
        code = str(raw).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned.append(code)
    return cleaned


def build_pallet_report(keg_ids,
                        batch_number: str = "",
                        scanned_time: str = None,
                        palette_id: str = None,
                        layer: int = None,
                        strict: bool = None,
                        result: str = None,
                        beer_type: str = None,
                        beer_type_id: str = None,
                        operator_id: str = None,
                        operator_name: str = None,
                        stack: dict = None,
                        positions: dict = None,
                        layout=None,
                        expected_count: int = None) -> dict:
    """Build the body for POST /api/camera/pallet-report.

    The one place the payload shape is defined; process_worker builds its
    report through this too.

    Parameters
    ----------
    keg_ids      : every code read in this layer, exactly as printed on the labels
    batch_number : production batch applied to every keg. Omitted when blank;
                   the spec says a blank must never overwrite the batch a keg
                   already carries.
    scanned_time : ISO-8601, when the camera read the layer (defaults to now)
    palette_id   : a paletteId from an earlier response, to add this layer to
                   that pallet; None starts a new pallet
    layer        : which layer this report covers; pallets stacked on each other
                   are one layer each, with no palette_id
    strict       : True makes the cloud refuse the whole layer if a code is
                   unmatched (defaults to config.PALLET_STRICT)
    result       : "pass" (the default) palletises the layer. Anything else
                   tells the cloud the layer was abandoned and nothing is written.
    beer_type    : the selected beer type name. Omitted when blank (see below).
    beer_type_id : its catalogue _id, sent for our own traceability only.
    operator_id  : the operator's cloud id, from GET /api/camera/{mac}/operators.
                   Preferred over the name. Omitted when blank.
    operator_name: the operator's name, sent when we have no id for them.
    stack        : {layer, layers_planned, stack_complete} from
                   StackTracker.begin_layer, or None when pallets are not
                   stacked. Sent with config.PALLET_SEND_STACKS on; the cloud's
                   stackId is added at send time (stack_store.attach_stack_id).
    positions    : {code: (row, col)}, where each keg stood in the layer
    layout       : (rows, cols) of the layer's grid
    expected_count: kegs the layer should hold. These three are sent with
                   config.PALLET_SEND_POSITIONS on.

    Keys are inserted in the order the cloud contract lists them, so a logged
    body can be diffed against their example line for line.

    The operator's MOVE/KEEP decision for an already-palletized keg is not in
    the body. The cloud reassigns a re-reported keg to the newest pallet itself,
    so both send the same body; MOVE only changes what this device does to its
    own record of the old pallet.
    """
    payload = {
        "macId": CAMERA_MAC_ID,
        # Identity labels. If the camera is registered in the cloud with a
        # name/line, that registration wins; otherwise these are used.
        "cameraName": config.CAMERA_NUMBER,
        "lineNumber": str(config.LINE_NUMBER),
    }
    # Not in the contract; off by default (see config.PALLET_SEND_LOCATION_NAME).
    if PALLET_SEND_LOCATION_NAME:
        payload["locationName"] = config.LOCATION_NAME
    # Omitted when blank: a blank must never overwrite the batch a keg carries.
    batch_number = (batch_number or "").strip()
    if batch_number:
        payload["batchNumber"] = batch_number

    # Beer type (an approved addition to the contract, confirmed 2026-09-21).
    # Omitted when blank, like batchNumber: with the feature off or nothing
    # selected, neither key appears and the body is unchanged.
    # The cloud matches on the name (case-insensitive) and ignores unknown ones;
    # the id is only there so we can trace our own records.
    beer_type = (beer_type or "").strip()
    if beer_type:
        payload[config.PALLET_BEER_TYPE_FIELD] = beer_type
        beer_type_id = (beer_type_id or "").strip()
        if beer_type_id:
            payload[config.PALLET_BEER_TYPE_ID_FIELD] = beer_type_id

    # Operator attribution: who built this layer (part of the cloud contract,
    # confirmed 2026-09-21). Omitted when blank: nobody selected in Settings
    # means neither key appears. Normally both are sent: the cloud matches on
    # the id, and the name lets a human reading the report see who it was.
    operator_id   = (operator_id or "").strip()
    operator_name = (operator_name or "").strip()
    if operator_id:
        payload[config.PALLET_OPERATOR_ID_FIELD] = operator_id
    if operator_name:
        payload[config.PALLET_OPERATOR_NAME_FIELD] = operator_name

    codes = clean_keg_ids(keg_ids)
    if config.PALLET_SEND_POSITIONS and layout:
        # Objects instead of strings: the cloud draws the layer as a grid. A
        # keg with no cell is sent with no position (recorded as absent).
        payload["expectedCount"] = int(expected_count or len(codes))
        payload["layout"] = {"rows": int(layout[0]), "cols": int(layout[1])}
        cells = positions or {}
        payload["kegIds"] = [dict({"code": c}, **({"row": int(cells[c][0]),
                                                   "col": int(cells[c][1])}
                                                  if c in cells else {}))
                             for c in codes]
    else:
        payload["kegIds"] = codes
    payload["scannedTime"] = to_iso_utc(scanned_time)   # when we read the layer
    payload["timestamp"]   = iso_utc()                  # when we sent the report
    payload["layer"]       = PALLET_DEFAULT_LAYER if layer is None else int(layer)
    if stack and config.PALLET_SEND_STACKS:
        payload["layer"] = int(stack["layer"])
        if payload["layer"] == 1:
            # Layer 1 opens the stack: no stackId, the plan instead.
            payload["layersPlanned"] = int(stack["layers_planned"])
        if stack.get("stack_complete"):
            payload["stackComplete"] = True

    # Continue an existing pallet instead of starting a new one.
    if palette_id:
        payload["paletteId"] = palette_id
    # Only sent when True; the cloud defaults to false.
    use_strict = PALLET_STRICT if strict is None else strict
    if use_strict:
        payload["strict"] = True
    # Always sent. Every report this app builds is a completed layer, and an
    # omitted result could be interpreted differently by the cloud.
    payload["result"] = (str(result).strip() if result else "") or PALLET_DEFAULT_RESULT
    return payload


def build_stack_report(stack, report_id, finished_early=False, beer_type="",
                       beer_type_id="", operator_id="", operator_name=""):
    """Build the body for POST /api/camera/stack-report: the whole stack, every
    layer bottom first, from a stack_store stack (layers kept on the camera).

    The stack-level batch, layout and expected count are the defaults; a layer
    that differs carries its own. finished_early (NEW STACK below the plan)
    sends stackComplete: true, so the cloud closes it as short."""
    from modules import keg_layout
    layers = [l for l in stack.get("layers") or [] if l.get("keg_ids")]
    layers.sort(key=lambda l: l["layer"])
    first = layers[0] if layers else {}
    default_layout = keg_layout.parse_layout(first.get("layout"))
    default_expected = first.get("expected") or len(first.get("keg_ids") or [])
    batch = next((l.get("batch") for l in reversed(layers) if l.get("batch")), "")
    payload = {"macId": CAMERA_MAC_ID, "reportId": str(report_id),
               "cameraName": config.CAMERA_NUMBER, "lineNumber": str(config.LINE_NUMBER)}
    if PALLET_SEND_LOCATION_NAME:
        payload["locationName"] = config.LOCATION_NAME
    if batch:
        payload["batchNumber"] = batch
    beer_type = (beer_type or "").strip()
    if beer_type:
        payload[config.PALLET_BEER_TYPE_FIELD] = beer_type
        if (beer_type_id or "").strip():
            payload[config.PALLET_BEER_TYPE_ID_FIELD] = beer_type_id.strip()
    if (operator_id or "").strip():
        payload[config.PALLET_OPERATOR_ID_FIELD] = operator_id.strip()
    if (operator_name or "").strip():
        payload[config.PALLET_OPERATOR_NAME_FIELD] = operator_name.strip()
    planned = max(int(stack.get("layers_target") or len(layers)), len(layers))
    payload["layersPlanned"] = planned if finished_early else len(layers)
    if finished_early and len(layers) < planned:
        payload["stackComplete"] = True
    positions_on = bool(config.PALLET_SEND_POSITIONS)
    if positions_on and default_layout:
        payload["expectedCount"] = int(default_expected)
        payload["layout"] = {"rows": default_layout[0], "cols": default_layout[1]}
    entries = []
    for l in layers:
        codes = clean_keg_ids(l["keg_ids"])
        layout = keg_layout.parse_layout(l.get("layout"))
        entry = {"layer": int(l["layer"])}
        if positions_on and layout:
            cells = l.get("positions") or {}
            entry["kegIds"] = [dict({"code": c}, **({"row": int(cells[c][0]),
                                                     "col": int(cells[c][1])}
                                                    if c in cells else {}))
                               for c in codes]
            if layout != default_layout:
                entry["layout"] = {"rows": layout[0], "cols": layout[1]}
            if (l.get("expected") or len(codes)) != default_expected:
                entry["expectedCount"] = int(l.get("expected") or len(codes))
        else:
            entry["kegIds"] = codes
        if l.get("scanned_time"):
            entry["scannedTime"] = to_iso_utc(l["scanned_time"])
        if l.get("batch") and l.get("batch") != batch:
            entry["batchNumber"] = l["batch"]
        entries.append(entry)
    payload["layers"] = entries
    last = next((e.get("scannedTime") for e in reversed(entries) if e.get("scannedTime")), None)
    payload["scannedTime"] = last or iso_utc()
    payload["timestamp"] = iso_utc()
    if PALLET_STRICT:
        payload["strict"] = True
    payload["result"] = PALLET_DEFAULT_RESULT
    return payload


def _auth_failure_text(response, what):
    """Operator-readable reason for a 401/403 from a camera-API read."""
    try:
        data = response.json()
        code = data.get("error") if isinstance(data, dict) else None
    except Exception:
        code = None
    if response.status_code == 403:
        return (f"403 {code or ''} from the {what} endpoint — the camera MAC "
                f"({CAMERA_MAC_ID}) is not this camera's. Check Settings → Device "
                f"Identity → Camera MAC.").replace("  ", " ")
    return (f"401 {code or ''} from the {what} endpoint — the camera is not signed "
            f"in to the cloud. Check the camera password on the sign-in "
            f"screen.").replace("  ", " ")


def _failure(error: str, retryable: bool = False, response=None) -> dict:
    """Uniform failure result for send_batch()."""
    return {"success": False, "pallet_id": None, "error": error,
            "retryable": retryable, "response": response}


# Event kinds published on the APISender event bus (see APISender.drain_events).
# The retry monitor runs on a plain background thread with no access to Qt, so
# anything the operator must hear about a late delivery goes through here.
EV_NETWORK_DOWN     = "network_down"      # {error}
EV_NETWORK_UP       = "network_up"        # {pending}
EV_DRAIN_START      = "drain_start"       # {total}
EV_ITEM_SENDING     = "item_sending"      # {session_id, index, total}
EV_ITEM_CONFIRMED   = "item_confirmed"    # {session_id, pallet_id, index, total}
EV_ITEM_REJECTED    = "item_rejected"     # {session_id, error}
EV_ITEM_DEFERRED    = "item_deferred"     # {session_id, error, attempts}
EV_DRAIN_DONE       = "drain_done"        # {confirmed, remaining}
EV_STACK_SENT       = "stack_sent"        # {stack_id, cloud_id, pallets, duplicate}
EV_STACK_REJECTED   = "stack_rejected"    # {stack_id, error}
EV_STACK_DEFERRED   = "stack_deferred"    # {stack_id, error}
EV_MAX_EVENTS       = 200                 # ring cap; the UI drains every ~1.5 s

class _QuietRepeats:
    """Logger for a fetch that repeats every few seconds.

    A line identical to one from the previous fetch is logged at DEBUG, so an
    unchanged (or still-failing) refresh adds nothing to app.log. Any change,
    including recovery, is logged at its normal level.
    """
    def __init__(self, logger):
        self._log = logger
        self._prev, self._cur = set(), set()

    def new_cycle(self):
        self._prev, self._cur = self._cur, set()

    def _emit(self, level, msg):
        self._cur.add(msg)
        self._log.log(logging.DEBUG if msg in self._prev else level, msg)

    def info(self, msg):
        self._emit(logging.INFO, msg)

    def warning(self, msg):
        self._emit(logging.WARNING, msg)

    def error(self, msg):
        self._emit(logging.ERROR, msg)


class APISender:
    def __init__(self, api_url=None, timeout=None, max_retries=None, camera_name=None):
        """Set up the HTTP session for cloud communication and start the retry monitor."""
        # Palletising-camera endpoint: POST /api/camera/pallet-report
        self.api_url = api_url or API_ENDPOINT
        self.timeout = timeout or API_TIMEOUT
        self.max_retries = max_retries or API_MAX_RETRIES
        self.camera_name = camera_name or CAMERA_NAME

        # Session with custom SSL verification
        self.session = requests.Session()

        self.session.verify = SSL_VERIFY

        # Retry policy
        from requests.adapters import HTTPAdapter, Retry
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount(HTTP_PREFIX, adapter)
        self.session.mount(HTTPS_PREFIX, adapter)

        # Default headers
        self.session.headers.update({
            'User-Agent': 'KegDetectionSystem/1.0',
            'Accept': CONTENT_TYPE_JSON,
            'Content-Type': CONTENT_TYPE_JSON
        })
        # Camera login: every call on this session carries the Bearer token,
        # and an auth 401 logs in once and repeats the call once.
        from modules.camera_auth import get_camera_auth
        self.session.auth = get_camera_auth()

        self.logger = logging.getLogger(__name__)
        # Blacklist / beer types are refreshed every few seconds: repeat-quiet
        # logs, and the cache file is rewritten only when the data changed.
        self._bl_log = _QuietRepeats(self.logger)
        self._beer_log = _QuietRepeats(self.logger)
        self._cached_blacklist = None
        self._cached_beer_types = None
        self.db_path = str(DB_PATH)
        # Shared persistence layer, for operations that must agree with what
        # process_worker wrote (above all, promoting a delivered report to a real
        # pallet). They share one implementation and the same module-level lock.
        self.db = DatabaseManager()

        # Network monitoring. `network_online` starts True (callers read it as
        # "worth trying?"), but the state is not known yet, so the first probe
        # counts as a transition and is announced. Otherwise an app that booted
        # offline would look online until something failed.
        self.network_online = True
        self._network_known = False
        self.last_network_check = None
        self.network_check_interval = 30  # seconds

        # Retry thread
        self.retry_thread = None
        self._stop_event = threading.Event()
        # Set to cut the monitor's sleep short when a report was just held in
        # the queue and should go out now, not at the next tick.
        self._wake_event = threading.Event()
        self.retry_interval = 60  # seconds

        # Event bus out to the UI (see drain_events). The maxlen keeps it bounded
        # when nothing is draining it, e.g. before the operator page attaches or
        # during shutdown.
        self._events = deque(maxlen=EV_MAX_EVENTS)
        self._events_lock = threading.Lock()

        # Start monitoring
        self.start_retry_monitor()
        self.logger.info(f"API Sender initialized. Endpoint: {self.api_url}")

    # Event bus
    def _publish(self, kind: str, **data):
        """Queue one event for the UI. Never raises, so it can't break a delivery."""
        try:
            with self._events_lock:
                self._events.append({"kind": kind, "at": iso_utc(), **data})
        except Exception:
            self.logger.debug("event publish failed", exc_info=True)

    def drain_events(self):
        """Take every pending event, oldest first, and clear the bus.

        Called from the Qt thread on a short timer. Returns plain dicts, so no
        object owned by the network thread crosses into the GUI.
        """
        with self._events_lock:
            events = list(self._events)
            self._events.clear()
        return events

    def pending_count(self) -> int:
        """Reports still waiting to upload, for the operator's queue badge."""
        try:
            return self.db.get_pending_count()
        except Exception:
            return 0

    @property
    def blacklist_url(self):
        """Blacklist URL for this camera, resolved at call time.

        GET {base}/camera/{mac_id}/blacklist. The MAC is in the path, so the URL
        is rebuilt per call and a MAC change takes effect without a restart.
        """
        return config.blacklist_url()

    def get_blacklist(self):
        """Fetch this camera's blacklist from the cloud.

            GET {CHECKOLOGY_BASE_URL}/camera/{mac_id}/blacklist

        The blacklist is the kegs/batches the cloud has flagged as not allowed.
        A successful fetch is cached to disk. If the cloud is unreachable the
        cached response is served instead, so checking works offline as the
        spec requires.

        Always returns a list of dicts so the UI can render safely.
        """
        url = self.blacklist_url
        self._bl_log.new_cycle()

        try:
            self._bl_log.info(f"Fetching blacklist from: {url}")
            response = self.session.get(url, timeout=5)
            # A few deployments expose this as POST. On 405 (wrong method), retry
            # with the MAC in the body. A 404 is not retried: on this API it means
            # the MAC is not registered, and a POST retry would hide that.
            if response.status_code == 405:
                self._bl_log.info("Blacklist GET not allowed (405); retrying as POST")
                response = self.session.post(url, json={"macId": CAMERA_MAC_ID},
                                             headers={'Content-Type': CONTENT_TYPE_JSON},
                                             timeout=5)
            self._bl_log.info(f"Response status: {response.status_code}")

        except requests.exceptions.SSLError as e:
            self._bl_log.warning(f"SSL error: {e}")
            return self._blacklist_fallback()
        except requests.exceptions.Timeout:
            self._bl_log.warning("Timeout connecting to blacklist endpoint")
            return self._blacklist_fallback()
        except requests.exceptions.RequestException as e:
            self._bl_log.warning(f"Network error: {e}")
            return self._blacklist_fallback()
        except Exception as e:
            self._bl_log.warning(f"Unexpected error: {e}")
            return self._blacklist_fallback()

        # An HTML body means we never reached the API: the Keg Management web
        # app answers any path, with its own page on 200 or an Express error
        # page on 404. Checked before the status codes, or a 200 of HTML would
        # read as an empty blacklist (silently disabling checks) and a 404 of
        # HTML as "MAC not registered".
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype.lower():
            self._bl_log.error(
                f"Blacklist URL returned '{ctype or 'no content-type'}' instead of JSON "
                f"(HTTP {response.status_code}) — {url} is not the camera API. Check "
                f"config.CHECKOLOGY_API_URL / PALLET_API_BASE_URL / API_PREFIX.")
            return self._blacklist_fallback()

        if response.status_code == 404:
            # As on the pallet-report endpoint: this MAC is not registered in
            # the cloud. A setup fault, not a network blip; no retry will fix it.
            self._bl_log.error(f"Blacklist 404: camera MAC {CAMERA_MAC_ID} is not "
                              f"registered in the cloud ({url})")
            return self._blacklist_fallback()

        if response.status_code != 200:
            self._bl_log.warning(f"Unexpected status {response.status_code}: {response.text[:200]}")
            return self._blacklist_fallback()

        try:
            data = response.json()
        except json.JSONDecodeError:
            self._bl_log.warning(f"Invalid JSON response: {response.text[:100]}")
            return self._blacklist_fallback()
        except Exception as e:
            self._bl_log.warning(f"Error parsing response: {e}")
            return self._blacklist_fallback()

        entries = self._process_blacklist_response(data)
        # An empty list is a valid answer (nothing flagged). Cache it too, so
        # going offline does not bring back a stale blacklist.
        self._cache_blacklist(entries)
        self._bl_log.info(f"Successfully fetched {len(entries)} blacklist entries")
        return entries

    def _cache_blacklist(self, entries):
        """Persist the last known good cloud blacklist for offline use.

        Skipped when unchanged, so a refresh every few seconds doesn't rewrite
        the same content (flash wear)."""
        if entries == self._cached_blacklist and BLACKLIST_CACHE_FILE.exists():
            return
        self._cached_blacklist = entries
        try:
            write_private_json(BLACKLIST_CACHE_FILE,
                               {"macId": CAMERA_MAC_ID,
                                "fetched_at": iso_utc(),
                                "blacklist": entries})
        except Exception as e:
            self.logger.warning(f"Could not write blacklist cache: {e}")

    def _blacklist_fallback(self):
        """Cloud fetch failed: serve the last cached cloud response.

        Never invents entries. With no cache yet the caller gets an empty list
        and the log says the blacklist is unavailable.
        """
        self._bl_log.error(BLACKLIST_ENDPOINT_ERR_MSG)
        if not BLACKLIST_OFFLINE_FALLBACK:
            return []
        try:
            if not BLACKLIST_CACHE_FILE.exists():
                self._bl_log.warning("No cached blacklist yet — blacklist unavailable "
                                    "until the cloud can be reached once.")
                return []
            with open(BLACKLIST_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = self._process_blacklist_response(data)
            self._bl_log.info(f"Loaded {len(entries)} blacklist entries from the cached "
                             f"cloud response ({data.get('fetched_at', 'unknown time')})"
                             if isinstance(data, dict) else
                             f"Loaded {len(entries)} blacklist entries from cache")
            return entries
        except Exception as e:
            self._bl_log.warning(f"Failed to load cached blacklist: {e}")
            return []

    def _process_blacklist_response(self, data):
        """Normalise the blacklist response into a list of dicts.

        Accepts a bare list, a list of strings, or a dict wrapping the list
        under one of the common keys. Each entry is returned as a dict so the
        UI can read fields like id / reason / created_at generically.
        """
        if isinstance(data, dict):
            for key in ("blacklist", "blacklisted", "blacklistedKegs",
                        "kegs", "items", "results", "data"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = []
        if not isinstance(data, list):
            return []
        entries = []
        for x in data:
            if isinstance(x, dict):
                entries.append(x)
            else:
                entries.append({"id": str(x)})
        return entries

    # Beer-type catalogue
    @property
    def beer_types_url(self):
        """Resolved at call time so an env switch needs no restart.

        Use `config.API_PREFIX`; a bare `API_PREFIX` is not imported here and
        would raise NameError, which (with the beer-type gate on) stops the line
        from building any pallet.
        """
        return f"{config.PALLET_API_BASE_URL}{config.API_PREFIX}{config.BEER_TYPES_PATH}"

    def get_beer_types(self):
        """Fetch the beer-type catalogue for this camera's brewery.

            POST {base}/api/kegs/cam/beer-types
            Content-Type: application/json
            {"macId": "3C:6D:66:02:5A:F2"}          <- the MAC, and nothing else

        The camera sends only its MAC; the cloud works out the brewery. The
        camera-login Bearer token comes from the session (session.auth).

        Note the shape differs from the other two reads: POST with the MAC in
        the body, under /kegs/ not /camera/, returning a bare array, newest
        first. See the comment on config.BEER_TYPES_PATH.

        Per the contract each element is
        {_id, name, description, expirationDays, breweryId}, and `name` is the
        exact string to send back as `beerType` on a pallet report. Only `_id`
        and `name` are kept.

        Returns (mapping, error): mapping is an ordered {name: _id} dict, error
        is None on success or an operator-readable string. On any failure the
        last cached catalogue is served (the contract says "keep the previous
        list"), so the selector keeps working offline.
        """
        self._beer_log.new_cycle()
        try:
            # Built inside the try, so an error while forming the request also
            # falls back to the cached list instead of raising to the caller.
            url = self.beer_types_url
            self._beer_log.info(f"Fetching beer types from: {url}")
            response = self.session.post(
                url,
                json={"macId": CAMERA_MAC_ID},
                headers={'Content-Type': CONTENT_TYPE_JSON},
                timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            return self._beer_types_fallback(f"SSL error: {e}")
        except requests.exceptions.Timeout:
            return self._beer_types_fallback("Timed out reaching the beer-type endpoint")
        except requests.exceptions.RequestException as e:
            return self._beer_types_fallback(f"Network error: {e}")
        except Exception as e:
            return self._beer_types_fallback(f"Unexpected error: {e}")

        # Same trap as the blacklist: the Keg Management web app answers any
        # path with HTML and a 200. Checked before the status code, or a web
        # page reads as "no beer types" and silently empties the selector.
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype.lower():
            return self._beer_types_fallback(
                f"Beer-type URL returned '{ctype or 'no content-type'}' instead of JSON "
                f"(HTTP {response.status_code}) — {url} is not the camera API. "
                f"Check config.PALLET_API_BASE_URL / BEER_TYPES_PATH.")

        # The three documented statuses, worded as the contract defines them.
        # 400 and 404 are setup faults no retry fixes; 500 is transient
        # ("retry later, keep the last list").
        sc = response.status_code
        if sc == 400:
            return self._beer_types_fallback(
                f"400 from the beer-type endpoint — macId missing from the body "
                f"(sending '{CAMERA_MAC_ID}'). Check Settings → Device Identity → Camera MAC.")
        if sc == 404:
            # Per the contract, 404 here means the camera is unknown to the
            # cloud, or not linked to a brewery (the brewery is resolved from
            # the MAC). It does not mean a wrong URL.
            return self._beer_types_fallback(
                f"404 from the beer-type endpoint — camera {CAMERA_MAC_ID} is not "
                f"registered, or is not linked to a brewery. Register it under "
                f"Scanner > Cameras and check its brewery.")
        if sc >= 500:
            return self._beer_types_fallback(
                f"{sc} from the beer-type endpoint — server error. Keeping the last "
                f"known catalogue; it will be retried.")
        if sc in (401, 403):
            # Camera login: the token was refused and the one re-login failed
            # (401), or the MAC is not this camera's (403 CAMERA_MISMATCH).
            return self._beer_types_fallback(_auth_failure_text(response, "beer-type"))
        if sc != 200:
            return self._beer_types_fallback(
                f"Unexpected status {sc}: {response.text[:200]}")

        try:
            data = response.json()
        except Exception as e:
            return self._beer_types_fallback(f"Invalid JSON from beer-type endpoint: {e}")

        mapping = self._process_beer_types_response(data)
        if not mapping:
            # A 200 with an empty array is not a failure, so the cache is not
            # overwritten. But returning an empty map while a good cache exists
            # would blank the selector and, with the beer-type gate on, stall the
            # line. Per the contract ("keep the previous list"), serve the cache
            # and say so.
            self._beer_log.warning("Beer-type catalogue came back empty")
            if config.BEER_TYPES_CACHE_FILE.exists():
                return self._beer_types_fallback(
                    "The brewery returned no beer types")
            return mapping, ("This brewery has no beer types registered in the "
                             "cloud. Add them there, or turn the beer type off in "
                             "Settings — with it on and nothing to pick, no pallet "
                             "can be sent.")
        self._cache_beer_types(mapping)
        self._beer_log.info(f"Successfully fetched {len(mapping)} beer types")
        return mapping, None

    @staticmethod
    def _process_beer_types_response(data):
        """Reduce the response to an ordered {name: _id} map.

        The documented shape is a bare array, newest first, of
        {_id, name, description, expirationDays, breweryId}; everything except
        _id and name is ignored. Insertion order is kept (newest first); the
        picker sorts by name itself.

        Also tolerates the common wrapper objects and `id` in place of `_id`, so
        a server-side shape change does not blank the selector.
        """
        if isinstance(data, dict):
            for key in ("beerTypes", "beer_types", "items", "data", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = []
        if not isinstance(data, list):
            return {}
        mapping = {}
        for x in data:
            if isinstance(x, dict):
                name = str(x.get("name", "")).strip()
                ident = str(x.get("_id", x.get("id", ""))).strip()
            else:
                name, ident = str(x).strip(), ""
            if name and name not in mapping:
                mapping[name] = ident
        return mapping

    def _cache_beer_types(self, mapping):
        """Persist the last known good catalogue for offline use. Skipped when
        unchanged, like _cache_blacklist."""
        if mapping == self._cached_beer_types and config.BEER_TYPES_CACHE_FILE.exists():
            return
        self._cached_beer_types = dict(mapping)
        try:
            write_private_json(config.BEER_TYPES_CACHE_FILE,
                               {"macId": CAMERA_MAC_ID,
                                "fetched_at": iso_utc(),
                                "beerTypes": mapping})
        except Exception as e:
            self.logger.warning(f"Could not write beer-type cache: {e}")

    def _beer_types_fallback(self, reason):
        """Cloud fetch failed: serve the last cached catalogue.

        Never invents entries. With no cache the caller gets an empty map plus
        the reason, so the operator sees why the list is blank.
        """
        self._beer_log.error(f"Beer-type fetch failed: {reason}")
        try:
            if not config.BEER_TYPES_CACHE_FILE.exists():
                return {}, f"{reason} (no cached catalogue yet)"
            with open(config.BEER_TYPES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            mapping = data.get("beerTypes") if isinstance(data, dict) else None
            if not isinstance(mapping, dict):
                mapping = self._process_beer_types_response(data)
            self._beer_log.info(f"Loaded {len(mapping)} beer types from cache "
                             f"({data.get('fetched_at', 'unknown time')})"
                             if isinstance(data, dict) else
                             f"Loaded {len(mapping)} beer types from cache")
            return mapping, f"{reason} — using the cached catalogue"
        except Exception as e:
            self._beer_log.warning(f"Failed to load cached beer types: {e}")
            return {}, f"{reason} (cache unreadable: {e})"

    # Keg registration (Camera Keg Registration API)
    @staticmethod
    def _keg_reg_url(path):
        return f"{config.PALLET_API_BASE_URL}{config.API_PREFIX}{path}"

    @staticmethod
    def _json_or_none(response):
        """The response body as a dict, or None when it is not JSON (the web app
        answers unknown paths with an HTML page and a 200)."""
        if "json" not in response.headers.get("content-type", "").lower():
            return None
        try:
            data = response.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def check_keg(self, serial, item_type="keg"):
        """Step 1: is this code a keg the brewery knows?

            POST {base}/api/camera/keg-registration/check
            {"macId", "serialNo", "itemType"}

        Returns a dict with `state`:
          "registered" - a normal keg, nothing to do
          "unknown"    - not registered and can be registered
          "blocked"    - not registered and cannot be (`reason` says why)
          "error"      - the cloud could not answer; ask again later
        plus `reason` (operator-readable or "") and `code` (the cloud's code).
        """
        try:
            response = self.session.post(
                self._keg_reg_url(config.KEG_REG_CHECK_PATH),
                json={"macId": CAMERA_MAC_ID, "serialNo": serial, "itemType": item_type},
                headers={'Content-Type': CONTENT_TYPE_JSON},
                timeout=self.timeout)
        except Exception as e:
            return {"state": "error", "reason": f"Network error: {e}", "code": ""}

        data = self._json_or_none(response)
        sc = response.status_code
        if data is None:
            return {"state": "error", "code": "",
                    "reason": f"Keg check returned no JSON (HTTP {sc})"}
        if sc in (401, 403):
            return {"state": "error", "code": data.get("error") or "",
                    "reason": _auth_failure_text(response, "keg-check")}
        if sc != 200 or not data.get("success", True):
            return {"state": "error", "code": data.get("error") or "",
                    "reason": data.get("message") or f"Keg check failed (HTTP {sc})"}
        if data.get("registered"):
            return {"state": "registered", "reason": "", "code": ""}
        if data.get("canRegister"):
            return {"state": "unknown", "reason": "", "code": ""}
        return {"state": "blocked", "code": data.get("code") or "",
                "reason": data.get("reason") or "This keg cannot be registered here."}

    def get_keg_form(self, item_type="keg"):
        """Step 2: the registration form for this brewery.

            GET {base}/api/camera/{mac_id}/keg-registration/form?itemType=keg

        Returns (form, warning). `form` is the cloud's dict (formVersion, fields,
        defaults, ...) or None; `warning` is None on a clean fetch. On failure
        the last cached form for this item type is served, with a warning.
        """
        try:
            response = self.session.get(
                self._keg_reg_url(config.KEG_REG_FORM_PATH.format(mac_id=CAMERA_MAC_ID)),
                params={"itemType": item_type},
                timeout=self.timeout)
            data = self._json_or_none(response)
            if response.status_code == 200 and data and isinstance(data.get("fields"), list):
                self._cache_keg_form(item_type, data)
                return data, None
            if response.status_code in (401, 403):
                reason = _auth_failure_text(response, "keg-form")
            else:
                reason = ((data or {}).get("message")
                          or f"Keg form fetch failed (HTTP {response.status_code})")
        except Exception as e:
            reason = f"Network error: {e}"
        self.logger.warning(f"Keg form fetch failed: {reason}")
        return self._cached_keg_form(item_type), reason

    def _cache_keg_form(self, item_type, form):
        try:
            cache = {}
            if config.KEG_REG_FORM_CACHE_FILE.exists():
                with open(config.KEG_REG_FORM_CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f) or {}
            old = cache.get(item_type) or {}
            # defaults change with every keg but do not change formVersion, so
            # compare the whole form, not just the version.
            if old.get("form") == form:
                return
            cache[item_type] = {"fetched_at": iso_utc(), "form": form}
            cache["macId"] = CAMERA_MAC_ID
            write_private_json(config.KEG_REG_FORM_CACHE_FILE, cache)
        except Exception as e:
            self.logger.warning(f"Could not write keg form cache: {e}")

    def _cached_keg_form(self, item_type):
        try:
            if not config.KEG_REG_FORM_CACHE_FILE.exists():
                return None
            with open(config.KEG_REG_FORM_CACHE_FILE, "r", encoding="utf-8") as f:
                entry = (json.load(f) or {}).get(item_type) or {}
            form = entry.get("form")
            return form if isinstance(form, dict) else None
        except Exception as e:
            self.logger.warning(f"Failed to load cached keg form: {e}")
            return None

    def register_keg(self, serial, values, form_version=None, item_type="keg"):
        """Step 3: create the keg.

            POST {base}/api/camera/keg-registration
            {"macId", "serialNo", "itemType", "formVersion", "values"}

        The signed-in operator (camera_auth) is recorded by the cloud itself.

        Returns {"ok", "status", "error", "message", "fieldErrors", "keg",
        "formChanged"}. A 409 ALREADY_EXISTS comes back with ok=True: per the
        spec the keg is then treated as a normal registered keg.
        """
        body = {"macId": CAMERA_MAC_ID, "serialNo": serial,
                "itemType": item_type, "values": dict(values or {})}
        if form_version:
            body["formVersion"] = form_version
        result = {"ok": False, "status": 0, "error": "", "message": "",
                  "fieldErrors": {}, "keg": None, "formChanged": False}
        try:
            response = self.session.post(
                self._keg_reg_url(config.KEG_REG_PATH), json=body,
                headers={'Content-Type': CONTENT_TYPE_JSON},
                timeout=self.timeout)
        except Exception as e:
            result.update(error="NETWORK", message=f"Could not reach the cloud: {e}")
            return result

        data = self._json_or_none(response) or {}
        sc = response.status_code
        result.update(status=sc, error=data.get("error") or "",
                      message=data.get("message") or "",
                      fieldErrors=data.get("fieldErrors") or {},
                      keg=data.get("keg"),
                      formChanged=bool(data.get("formChanged")))
        if sc in (200, 201) and data.get("success", True):
            result["ok"] = True
        elif sc == 409 and result["error"] == "ALREADY_EXISTS":
            result["ok"] = True
        elif sc in (401, 403) and result["error"] != "KEG_LIMIT_REACHED":
            result["message"] = result["message"] or _auth_failure_text(response, "keg-registration")
        elif not result["message"]:
            result["message"] = f"Registration failed (HTTP {sc})"
        if result["ok"]:
            self.logger.info(f"Keg {serial} registered via camera (HTTP {sc})")
        else:
            self.logger.warning(f"Keg {serial} registration refused: HTTP {sc} "
                                f"{result['error']} {result['message']}")
        return result

    # Operator list
    @property
    def operators_url(self):
        """Resolved at call time so a MAC or env switch needs no restart."""
        return config.operators_url()

    def get_operators(self):
        """Fetch the brewery's operator list for this camera.

            GET {base}/api/camera/{mac_id}/operators
            -> {"macId", "breweryId", "count", "operators": [{id, name, role}]}

        Same shape as the blacklist call: GET, MAC in the path, an envelope
        object.

        Returns (operators, warning): `operators` is a list of
        {"id", "name", "role"} dicts in cloud order, `warning` is None on a
        clean fetch or an operator-readable string. On any failure the last
        cached list is served; the cloud team's rule is to carry on with the
        last list rather than block the line.
        """
        url = self.operators_url
        try:
            self.logger.info(f"Fetching operators from: {url}")
            response = self.session.get(url, timeout=self.timeout)
            # As with the blacklist, some deployments expose this as POST. On
            # 405, retry with the MAC in the body. A 404 is not retried: it means
            # the camera is not registered, and a POST retry would hide that.
            if response.status_code == 405:
                self.logger.info("Operators GET not allowed (405); retrying as POST")
                response = self.session.post(
                    url, json={"macId": CAMERA_MAC_ID},
                    headers={'Content-Type': CONTENT_TYPE_JSON},
                    timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            return self._operators_fallback(f"SSL error: {e}")
        except requests.exceptions.Timeout:
            return self._operators_fallback("Timed out reaching the operator endpoint")
        except requests.exceptions.RequestException as e:
            return self._operators_fallback(f"Network error: {e}")
        except Exception as e:
            return self._operators_fallback(f"Unexpected error: {e}")

        # Same trap as the blacklist and beer types: the Keg Management web app
        # answers any path with HTML and a 200. Checked before the status code,
        # or a web page reads as "no operators" and silently empties the picker.
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype.lower():
            return self._operators_fallback(
                f"Operator URL returned '{ctype or 'no content-type'}' instead of JSON "
                f"(HTTP {response.status_code}) — {url} is not the camera API. "
                f"Check config.PALLET_API_BASE_URL / OPERATORS_PATH.")

        sc = response.status_code
        if sc == 404:
            # Per the cloud team, 404 means the camera is not registered yet.
            # A setup fault; no retry fixes it.
            return self._operators_fallback(
                f"404 — camera MAC {CAMERA_MAC_ID} is not registered in the cloud "
                f"({url}).")
        if sc in (401, 403):
            return self._operators_fallback(_auth_failure_text(response, "operator"))
        if sc != 200:
            return self._operators_fallback(
                f"Unexpected status {sc}: {response.text[:200]}")

        try:
            data = response.json()
        except Exception as e:
            return self._operators_fallback(f"Invalid JSON from operator endpoint: {e}")

        operators = self._process_operators_response(data)
        if not operators:
            # A brewery with no operators registered is possible; don't overwrite
            # a good cache with nothing, and don't report a failure either.
            self.logger.warning("Operator list came back empty")
            return operators, None
        self._cache_operators(operators)
        self.logger.info(f"Successfully fetched {len(operators)} operator(s)")
        return operators, None

    @staticmethod
    def _process_operators_response(data):
        """Reduce the response to an ordered list of {id, name, role} dicts.

        The documented shape is {"operators": [...]}; the other wrapper keys and
        a bare array are tolerated too, so a server-side shape change does not
        blank the picker. Entries with no name are dropped; the picker has
        nothing to show for them.
        """
        if isinstance(data, dict):
            for key in ("operators", "users", "items", "data", "results"):
                if key in data:
                    data = data[key]
                    break
            else:
                data = []
        if not isinstance(data, list):
            return []
        operators, seen = [], set()
        for x in data:
            if isinstance(x, dict):
                name = str(x.get("name", x.get("userName", ""))).strip()
                ident = str(x.get("id", x.get("_id", x.get("userId", "")))).strip()
                role = str(x.get("role", "")).strip()
                # Camera Login API: which sign-in to offer (PIN pad or email).
                has_pin = bool(x.get("hasPin", False))
            else:
                name, ident, role, has_pin = str(x).strip(), "", "", False
            if not name:
                continue
            key = (ident, name.lower())
            if key in seen:
                continue
            seen.add(key)
            operators.append({"id": ident, "name": name, "role": role,
                              "hasPin": has_pin})
        return operators

    def _cache_operators(self, operators):
        """Persist the last known good operator list for offline use."""
        try:
            write_private_json(config.OPERATORS_CACHE_FILE,
                               {"macId": CAMERA_MAC_ID,
                                "fetched_at": iso_utc(),
                                "operators": operators})
        except Exception as e:
            self.logger.warning(f"Could not write operator cache: {e}")

    def _operators_fallback(self, reason):
        """Cloud fetch failed: serve the last cached list.

        Never invents entries. With no cache the caller gets an empty list plus
        the reason, so the operator sees why the picker is blank.
        """
        self.logger.error(f"Operator fetch failed: {reason}")
        try:
            if not config.OPERATORS_CACHE_FILE.exists():
                return [], f"{reason} (no cached operator list yet)"
            with open(config.OPERATORS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            operators = data.get("operators") if isinstance(data, dict) else None
            if not isinstance(operators, list):
                operators = self._process_operators_response(data)
            else:
                operators = self._process_operators_response(operators)
            self.logger.info(f"Loaded {len(operators)} operator(s) from cache")
            return operators, f"{reason} — using the cached list"
        except Exception as e:
            self.logger.warning(f"Failed to load cached operators: {e}")
            return [], f"{reason} (cache unreadable: {e})"

    # Keg-ID prefixes (serial matching, modules/serial_match.py)
    def get_qr_prefixes(self):
        """Fetch the brewery's keg-ID prefixes for this camera.

            GET {base}/api/camera/{mac_id}/qr-prefixes -> {"prefixes": [...]}

        The same endpoint the monoblock camera uses for its serial format
        check. Returns (prefixes, error): `prefixes` is a list of strings (empty
        if the brewery configured none) or None on failure, `error` a readable
        reason. No cache here: serial_match keeps the last good list itself.
        """
        # A camera whose config.py predates qr_prefixes_url() gets the
        # blacklist URL with the last path segment swapped (same base and MAC).
        url = (config.qr_prefixes_url() if hasattr(config, "qr_prefixes_url")
               else config.blacklist_url().rsplit("/", 1)[0] + "/qr-prefixes")
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.exceptions.Timeout:
            return None, "timed out reaching the prefix endpoint"
        except requests.exceptions.RequestException as e:
            return None, f"network error: {e}"
        except Exception as e:
            return None, f"unexpected error: {e}"

        # The Keg Management web app answers unknown paths with HTML and a 200.
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype.lower():
            return None, (f"{url} returned '{ctype or 'no content-type'}' instead of JSON "
                          f"(HTTP {response.status_code})")
        sc = response.status_code
        if sc == 404:
            return None, f"404 from {url}: camera not registered"
        if sc in (401, 403):
            return None, _auth_failure_text(response, "keg-ID prefix")
        if sc != 200:
            return None, f"unexpected status {sc}: {response.text[:200]}"
        try:
            data = response.json()
        except Exception as e:
            return None, f"invalid JSON from the prefix endpoint: {e}"
        if isinstance(data, dict):
            data = data.get("prefixes", [])
        if not isinstance(data, list):
            return None, "prefix endpoint answered with no list"
        return [str(p).strip() for p in data if str(p).strip()], None

    def send_batch(self, batch_id: str, qr_codes: list, payload: dict = None, **kwargs):
        """Send one pallet report to the cloud (POST /api/camera/pallet-report).

        Returns
        -------
        dict
            {"success":   bool,
             "pallet_id": str | None,   # paletteId issued by the cloud
             "error":     str | None,
             "retryable": bool,         # False for a permanent payload/registration fault
             "response":  dict | None}  # parsed cloud body, for reconciliation/logging

        `retryable` decides whether the caller queues the report for a later
        retry. A 400/404/409 means the payload or the cloud-side camera
        registration is wrong, so replaying the same body won't help.
        """
        if not qr_codes:
            self.logger.warning(f"Batch {batch_id}: No keg IDs to send")
            return _failure("No keg IDs to send", retryable=False)

        self.logger.info(f"Sending pallet report {batch_id} with {len(qr_codes)} kegs")

        # Use provided payload or build the spec body from the keg IDs.
        if not payload:
            payload = build_pallet_report(
                keg_ids      = qr_codes,
                batch_number = kwargs.get('batch_number', ''),
                scanned_time = kwargs.get('scanned_time'),
                palette_id   = kwargs.get('palette_id'),
                layer        = kwargs.get('layer'),
                result       = kwargs.get('result'),
            )

        # Spec cap: max 500 codes per report. Refuse locally rather than get a
        # 400. Never truncate silently; that would lose kegs.
        keg_ids = payload.get("kegIds") or []
        if len(keg_ids) > PALLET_MAX_KEG_IDS:
            msg = (f"{len(keg_ids)} keg IDs exceeds the {PALLET_MAX_KEG_IDS}-code "
                   f"limit for one pallet report")
            self.logger.error(f"Batch {batch_id}: {msg}")
            return _failure(msg, retryable=False)

        return self._send_with_retry(batch_id, payload)

    def _send_with_retry(self, batch_id: str, payload: dict, start_attempt: int = 0):
        """Send with retries. Returns the send_batch() result dict."""
        self._print_start_banner(batch_id, start_attempt)

        last = _failure("Send failed", retryable=True)

        # A later layer of a stack carries the id the cloud gave layer 1. It is
        # looked up now, not when the report was built, because layer 1 may
        # have been answered only since (both queued while offline).
        if config.PALLET_SEND_STACKS:
            stack_store.attach_stack_id(batch_id, payload)

        self._add_payload_hash(payload, batch_id)

        print("\nSTEP 3: Preparing HTTP headers...")
        headers = {'Content-Type': CONTENT_TYPE_JSON}

        self._log_request_details(payload, headers)

        fixes = set()       # each stack refusal is put right once, then reported
        for attempt in range(start_attempt + 1, start_attempt + self.max_retries + 1):
            result = self._process_single_attempt(
                batch_id, payload, headers, attempt, start_attempt)
            fix = None if result["success"] else self._fix_stack_refusal(
                batch_id, payload, result, fixes)
            if fix:
                fixes.add(fix)
                self._add_payload_hash(payload, batch_id)
                result = self._process_single_attempt(
                    batch_id, payload, headers, attempt, start_attempt)

            if result["success"]:
                # Covers both the live send and the queue: both come through here.
                from modules import batch_history
                batch_history.record(batch_id, payload, result.get("response"))
                self._note_stack_reply(batch_id, result.get("response"))
                return result

            last = result

            # A permanent fault (bad payload, unregistered camera, strict
            # rejection) fails the same way every time, so stop now.
            if not result["retryable"]:
                self.logger.error(f"Batch {batch_id}: permanent failure, not retrying "
                                  f"— {result['error']}")
                break

            # Exponential backoff before next attempt
            if attempt < start_attempt + self.max_retries:
                self._wait_before_retry(attempt, start_attempt)

        self._log_error_to_db(batch_id, last.get("error"), last.get("retryable", True))
        self._print_failure_banner()
        return last

    def _process_single_attempt(self, batch_id, payload, headers, attempt, start_attempt):
        """One POST attempt. Returns the send_batch() result dict."""
        self._print_attempt_banner(attempt, start_attempt)

        try:
            self.logger.info(f"Sending {batch_id} to cloud (attempt {attempt})")

            import time as _time
            start_time = _time.time()

            response = self.session.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )

            elapsed = _time.time() - start_time
            self._print_response_details(response, elapsed)

            # An HTML body means the request never reached the pallet-report
            # handler: a framework 404 page ("Cannot POST /api/camera/…") when
            # the route is not deployed, or the web app answering with its own
            # page. Check this before the status codes, otherwise:
            #   • a 404 of HTML reads as "camera MAC not registered" and the
            #     operator re-registers a camera that is fine;
            #   • a 200 of HTML counts as success: no pallet, no label, and the
            #     captured frame deleted.
            ctype = response.headers.get("content-type", "")
            if "json" not in ctype.lower():
                error = (f"{self.api_url} did not return JSON (HTTP {response.status_code}, "
                         f"{ctype or 'no content-type'}) — this host does not serve the "
                         f"pallet-report route. Check config.PALLET_API_BASE_URL / "
                         f"API_PREFIX / PALLET_REPORT_PATH.")
                print(f"\nSTEP 8: WRONG ENDPOINT - {response.status_code} {ctype}")
                self.logger.error(f"Batch {batch_id}: {error}")
                self.logger.debug(f"Body was: {response.text[:200]}")
                # A gateway or maintenance page (408, 429, 5xx) is transient: the
                # proxy in front of the API answered, not the API. Keep it queued.
                # Anything else is a config fault, not transient, so don't
                # replay it. The batch is flagged for the operator instead.
                transient = (response.status_code in (408, 429)
                             or response.status_code >= 500)
                return _failure(error, retryable=transient)

            if response.status_code in (200, 201):
                sent_count = len(payload.get("kegIds", []))
                return self._handle_success_response(response, batch_id, sent_count,
                                                     payload.get("batchNumber"))

            # Non-2xx. 408/429/5xx are transient; 400/404/409 are ours to fix.
            # 401 is the camera login (token refused and the one re-login
            # failed). Per the Camera Login API a 401 never means the report
            # was bad, so it stays queued and goes out after the next login.
            retryable = (response.status_code in (401, 408, 429)
                         or response.status_code >= 500)
            error = f"HTTP {response.status_code}: {response.text[:200]}"
            print(f"\nSTEP 8: FAILURE - Status {response.status_code}")
            print(f"  Error: {error}")
            print(f"  Retryable: {retryable}")
            self.logger.warning(f"Batch {batch_id}: {error} (attempt {attempt}, "
                                f"retryable={retryable})")
            if response.status_code in NON_RETRYABLE_STATUS:
                self.logger.error(
                    f"Batch {batch_id}: {self._explain_status(response.status_code)}")
            # The body names a stack refusal and, for a layer already on the
            # stack, its paletteId (see _fix_stack_refusal).
            body = self._json_or_none(response)
            return _failure(error, retryable=retryable,
                            response=body if isinstance(body, dict) else None)

        except requests.exceptions.SSLError as e:
            # No plaintext downgrade: re-sending over http:// would expose keg
            # data on exactly the failure that suggests interception.
            # A TLS failure is retryable; the report is queued, not dropped.
            error = f"SSL verification failed: {str(e)}"
            print("\nSTEP 6: SSL ERROR")
            print(f"  Error: {e}")
            self.logger.error(f"Batch {batch_id}: {error} (attempt {attempt})")
            return _failure(error, retryable=True)

        except requests.exceptions.Timeout:
            error = "Request timeout"
            print("\nSTEP 6: TIMEOUT ERROR")
            print(f"  Request timed out after {self.timeout} seconds")
            # Safe to retry per the spec: the kegs are already on the pallet, so a
            # replay just comes back with changedCount: 0.
            self.logger.warning(f"Batch {batch_id}: {error} (attempt {attempt})")
            return _failure(error, retryable=True)

        except requests.exceptions.RequestException as e:
            error = f"Network error: {str(e)}"
            print("\nSTEP 6: NETWORK ERROR")
            print(f"  Error: {e}")
            self.logger.warning(f"Batch {batch_id}: {error} (attempt {attempt})")
            return _failure(error, retryable=True)

        except Exception as e:
            error = f"Unexpected error: {str(e)}"
            print("\nSTEP 6: UNEXPECTED ERROR")
            print(f"  Error: {e}")
            self.logger.error(f"Batch {batch_id}: {error}")
            return _failure(error, retryable=False)   # stop on unexpected errors

    @staticmethod
    def _explain_status(status_code):
        """Operator-facing reason for a permanent failure, straight from the spec."""
        return {
            400: "Payload problem — missing macId/kegIds, over 500 codes, or a "
                 "paletteId the cloud never issued. Fix the payload.",
            403: "Camera mismatch — the macId in the report is not the camera the "
                 "login token belongs to. Check Settings → Device Identity → Camera MAC "
                 "against the camera registered in Keg Management.",
            404: "Camera not found — this macId is not registered in the cloud. "
                 "Register it under Scanner > Cameras with Line Position = Pallet.",
            409: "Nothing was written — strict mode hit an unmatched code, or NO "
                 "code matched at all (usually the camera is registered to the "
                 "wrong brewery). Re-read or re-register.",
        }.get(status_code, f"HTTP {status_code}")

    # Stacks (cloud doc "Layers, Stacks and Keg Positions")
    _STACK_GONE = ("that stack is closed", "no stack with that id", "invalid stackid")
    _LAYER_LANDED = "that layer is already on the stack"

    def _fix_stack_refusal(self, batch_id, payload, result, done):
        """Put right a report the cloud refused over its stack, so the layer is
        not lost to a stack mix-up. Returns what was changed, or None.

          stack closed / unknown / invalid: send it again without a stackId,
            as the first layer of a new stack (the cloud's own advice);
          layer already on the stack: a send whose answer was lost. Sent again
            with the paletteId the refusal names, which the cloud accepts and
            changes nothing.
        """
        body = result.get("response") or {}
        error = str(body.get("error") or body.get("message") or "").lower()
        if not error:
            return None
        if (any(e in error for e in self._STACK_GONE) and "stackId" in payload
                and "restart" not in done):
            old = payload.pop("stackId")
            payload.pop("layer", None)
            payload.update(stack_store.restart_fields(batch_id) or {"layersPlanned": 1})
            self.logger.warning(f"Batch {batch_id}: the cloud refused stack {old} "
                                f"({body.get('error')}) — sending this layer as the "
                                f"start of a new stack")
            return "restart"
        palette = body.get("paletteId") or body.get("palletId")
        if self._LAYER_LANDED in error and palette and "landed" not in done:
            payload["paletteId"] = palette
            self.logger.info(f"Batch {batch_id}: this layer is already on the stack "
                             f"as pallet {palette} — sending again with that paletteId")
            return "landed"
        return None

    def _note_stack_reply(self, batch_id, reply):
        """Keep the cloud's stack id from a delivered layer, and queue the stack
        label when that layer closed the stack. Never raises."""
        try:
            info = stack_store.note_cloud_reply(batch_id, reply)
            if not info:
                return
            self.logger.info(f"Batch {batch_id}: on stack {info['cloud_id']}"
                             f"{' — stack closed' if info['closed'] else ''}")
            if info["closed"]:
                self._queue_stack_label(info["stack_id"], info["qr"])
        except Exception:
            self.logger.exception(f"Batch {batch_id}: could not read the stack reply")

    def _queue_stack_label(self, stack_id, qr):
        if not (config.PRINT_STACK_LABEL and qr):
            return
        from modules.print_queue import queue_confirmed_label
        queue_confirmed_label(qr, session_id=stack_id)
        self.logger.info(f"Stack label {qr} queued for {stack_id}")

    def close_finished_stacks(self):
        """Close in the cloud every stack finished here before its plan was
        reached (NEW STACK, a lower plan), once all its layers
        are delivered:

            POST {base}/api/camera/stack-close  {macId, stackId, reason}

        Run by the retry monitor after each queue sweep. A stack the cloud does
        not have (404) is marked done so it is not asked again; a network
        failure leaves it for the next sweep. Returns how many were closed."""
        closed = 0
        for local, cloud, status, reason in stack_store.stacks_to_close():
            body = {"macId": CAMERA_MAC_ID, "stackId": cloud,
                    "reason": reason or "operator-finish"}
            try:
                response = self.session.post(config.stack_close_url(), json=body,
                                             headers={'Content-Type': CONTENT_TYPE_JSON},
                                             timeout=self.timeout)
            except requests.exceptions.RequestException as e:
                self.logger.warning(f"Stack {cloud}: stack-close not sent ({e}) — "
                                    f"retried at the next sweep")
                break
            data = self._json_or_none(response) or {}
            if response.status_code == 200:
                stack = data.get("stack") if isinstance(data.get("stack"), dict) else {}
                qr = (stack.get("qr") or {}).get("value") if isinstance(
                    stack.get("qr"), dict) else None
                stack_store.mark_cloud_closed(local, qr or cloud)
                closed += 1
                self.logger.info(f"Stack {cloud} closed in the cloud as "
                                 f"{data.get('status') or status}"
                                 f"{' (already closed)' if data.get('alreadyClosed') else ''}")
                if not data.get("alreadyClosed"):
                    self._queue_stack_label(local, qr or cloud)
            elif response.status_code in (400, 404):
                stack_store.mark_cloud_closed(local)
                self.logger.error(f"Stack {cloud}: stack-close refused "
                                  f"(HTTP {response.status_code}: {response.text[:200]}) — "
                                  f"not asked again")
            else:
                self.logger.warning(f"Stack {cloud}: stack-close got HTTP "
                                    f"{response.status_code} — retried at the next sweep")
                break
        return closed

    def get_open_stacks(self):
        """This camera's stacks still building in the cloud, for the start-up
        check: (list of stack dicts, None) or (None, reason).

            GET {base}/api/camera/{mac_id}/stacks?status=building&mine=true
        """
        url = config.stacks_url()
        try:
            response = self.session.get(url, params={"status": "building", "mine": "true"},
                                        timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            return None, f"network error: {e}"
        data = self._json_or_none(response)
        if response.status_code != 200 or not isinstance(data, dict):
            return None, f"HTTP {response.status_code} from {url}"
        stacks = data.get("stacks")
        return (stacks if isinstance(stacks, list) else []), None

    def check_open_stack(self, stack):
        """Start-up check of the stack this device left open
        (stack_store.open_stack()). Returns the cloud's status for it when the
        cloud has closed it meanwhile ("complete"/"short"), else None."""
        cloud = (stack or {}).get("cloud_stack_id")
        if not cloud:
            return None
        stacks, problem = self.get_open_stacks()
        if stacks is None:
            self.logger.info(f"Open stack {cloud}: not checked with the cloud ({problem})")
            return None
        if any(s.get("stackId") == cloud for s in stacks if isinstance(s, dict)):
            self.logger.info(f"Open stack {cloud} is still building in the cloud — "
                             f"carrying on")
            return None
        self.logger.warning(f"Open stack {cloud} is no longer building in the cloud — "
                            f"closing it here too")
        return stack_store.STATUS_SHORT

    # Whole-stack report (cloud doc "Sending a Whole Stack", 2026-11-04)
    def send_pending_stack_reports(self):
        """Send every whole-stack report waiting in stack_history.db, oldest
        first, and stop at the first one that has to wait (offline, server
        error). Run by the retry monitor, which is woken when a stack is
        complete. Returns how many the cloud stored."""
        done = 0
        for stack_id, report in stack_store.pending_reports():
            if self._stop_event.is_set():
                break
            outcome = self._send_stack_report(stack_id, report)
            if outcome == "sent":
                done += 1
            elif outcome == "deferred":
                break
        return done

    def _send_stack_report(self, stack_id, report, resent=False):
        """One POST /api/camera/stack-report. Returns "sent", "rejected" or
        "deferred", following the cloud's rules for a safe resend:
          a lost answer:        the same body and reportId (duplicate: true);
          layers half written:  the stackId and each applied layer's paletteId;
          already closed:       read the stack back instead of resending."""
        report["timestamp"] = iso_utc()
        url = config.stack_report_url()
        n_layers = len(report.get("layers") or [])
        n_codes = sum(len(l.get("kegIds") or []) for l in report.get("layers") or [])
        self.logger.info(f"Stack {stack_id}: sending the whole stack ({n_layers} layer(s), "
                         f"{n_codes} code(s), report {report.get('reportId')})")
        self.logger.info(f"Stack report body: {json.dumps(report, default=str)[:4000]}")
        try:
            response = self.session.post(url, json=report,
                                         headers={'Content-Type': CONTENT_TYPE_JSON},
                                         timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            return self._defer_stack(stack_id, f"network error: {e}")
        ctype = response.headers.get("content-type", "")
        sc = response.status_code
        if "json" not in ctype.lower():
            # Not the API answering: a proxy page, or a server without the
            # route. Kept and retried; nothing was written.
            why = "server busy" if sc >= 500 else "no stack-report route on this server?"
            return self._defer_stack(stack_id, f"HTTP {sc} without JSON from {url} ({why})")
        data = self._json_or_none(response) or {}
        self.logger.info(f"Stack {stack_id}: HTTP {sc}: {json.dumps(data, default=str)[:2000]}")
        if sc in (200, 201):
            if data.get("applied") is False and data.get("reason") == "result-not-pass":
                stack_store.reject_report(stack_id, "result not pass: nothing written")
                return "rejected"
            self._apply_stack_answer(stack_id, report, data)
            return "sent"
        error = str(data.get("error") or data.get("message") or f"HTTP {sc}")
        if (data.get("stackId") and (data.get("appliedLayers") or data.get("paletteId"))
                and (sc == 409 or sc >= 500)):
            # Some layers landed: resend with their pallet ids (changes nothing
            # for them, writes the rest).
            self._merge_retry(report, data)
            stack_store.update_report(stack_id, report, error)
            self.logger.warning(f"Stack {stack_id}: {error} — resending with stack "
                                f"{data.get('stackId')} and the layers already written")
            if not resent:
                return self._send_stack_report(stack_id, report, resent=True)
            return self._defer_stack(stack_id, error)
        if sc == 409 and data.get("stackId") and (
                data.get("status") in (stack_store.STATUS_COMPLETE, stack_store.STATUS_SHORT)
                or "closed" in error.lower()):
            # Finished by an earlier send whose answer was lost: read it back.
            detail = self.get_stack_detail(data["stackId"])
            if detail:
                self._apply_stack_answer(stack_id, report, detail)
                return "sent"
            return self._defer_stack(stack_id, f"{error} — could not read it back yet")
        if sc in (401, 408, 429) or sc >= 500:
            return self._defer_stack(stack_id, error)
        # 400 / 403 / 404 / 409: the report as a whole was refused and nothing
        # of it was written. It needs a person; it is not resent.
        self.logger.error(f"Stack {stack_id}: REFUSED by the cloud (HTTP {sc}) — {error}. "
                          f"Nothing of it was written.")
        stack_store.reject_report(stack_id, f"HTTP {sc}: {error}")
        self._publish(EV_STACK_REJECTED, stack_id=stack_id, error=error[:200])
        return "rejected"

    def _defer_stack(self, stack_id, error):
        self.logger.warning(f"Stack {stack_id}: not sent yet ({error}) — kept, retried "
                            f"at the next sweep")
        stack_store.update_report_error(stack_id, error)
        self._publish(EV_STACK_DEFERRED, stack_id=stack_id, error=str(error)[:200])
        return "deferred"

    @staticmethod
    def _merge_retry(report, data):
        report["stackId"] = data["stackId"]
        applied = list(data.get("appliedLayers") or [])
        if data.get("layer") and data.get("paletteId"):
            applied.append({"layer": data["layer"], "paletteId": data["paletteId"]})
        for a in applied:
            for entry in report.get("layers") or []:
                if int(entry.get("layer", -1)) == int(a.get("layer", -2)) and a.get("paletteId"):
                    entry["paletteId"] = a["paletteId"]

    def get_stack_detail(self, cloud_id):
        """GET /api/camera/{mac}/stacks/{stackId} as a dict, or None."""
        try:
            response = self.session.get(config.stack_url(cloud_id), timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"Stack {cloud_id}: could not read it back ({e})")
            return None
        data = self._json_or_none(response)
        if response.status_code != 200 or not isinstance(data, dict):
            self.logger.warning(f"Stack {cloud_id}: read back HTTP {response.status_code}")
            return None
        data.setdefault("stackId", cloud_id)
        return data

    def _apply_stack_answer(self, stack_id, report, data):
        """The cloud stored the stack: keep its id and every layer's pallet id,
        record each layer in Batch History, and queue the layer labels and the
        stack label. Never raises."""
        try:
            stack = data.get("stack") if isinstance(data.get("stack"), dict) else {}
            cloud_id = data.get("stackId") or stack.get("stackId")
            pallets, answers = {}, {}
            for source in (stack.get("layers") or [], data.get("layers") or []):
                for l in source:
                    if isinstance(l, dict) and l.get("layer") is not None and l.get("paletteId"):
                        pallets[int(l["layer"])] = l["paletteId"]
                        answers.setdefault(int(l["layer"]), {}).update(l)
            qr = ((stack.get("qr") or {}).get("value")
                  if isinstance(stack.get("qr"), dict) else None) or cloud_id
            stack_store.apply_report_result(stack_id, cloud_id, qr, pallets, closed=True)
            local = stack_store.get_stack(stack_id) or {"layers": []}
            from modules import batch_history
            from modules.print_queue import queue_confirmed_label
            for l in local["layers"]:
                pallet = pallets.get(int(l["layer"]))
                entry = next((e for e in report.get("layers") or []
                              if int(e.get("layer", -1)) == int(l["layer"])), {})
                answer = dict(answers.get(int(l["layer"]), {}))
                answer.setdefault("paletteId", pallet)
                answer.setdefault("layer", l["layer"])
                payload = {k: v for k, v in report.items() if k != "layers"}
                payload.update({"kegIds": entry.get("kegIds") or l["keg_ids"],
                                "layer": l["layer"],
                                "batchNumber": entry.get("batchNumber") or report.get("batchNumber"),
                                "scannedTime": entry.get("scannedTime") or report.get("scannedTime")})
                batch_history.record(l["session_id"], payload, answer)
                if answer.get("unmatched"):
                    self.logger.warning(f"Stack {cloud_id} layer {l['layer']}: "
                                        f"{len(answer['unmatched'])} code(s) matched no keg: "
                                        f"{answer['unmatched']}")
                if pallet:
                    queue_confirmed_label(pallet, session_id=l["session_id"],
                                          batch=payload.get("batchNumber") or "",
                                          keg_count=answer.get("palletisedCount") or len(l["keg_ids"]))
            if config.PRINT_STACK_LABEL and qr:
                queue_confirmed_label(qr, session_id=stack_id)
            self.logger.info(f"Stack {stack_id} stored by the cloud as {cloud_id}"
                             f"{' (resend, nothing written again)' if data.get('duplicate') else ''}: "
                             + ", ".join(f"layer {k} = {v}" for k, v in sorted(pallets.items())))
            self._publish(EV_STACK_SENT, stack_id=stack_id, cloud_id=cloud_id,
                          pallets=[{"layer": k, "pallet_id": v} for k, v in sorted(pallets.items())],
                          duplicate=bool(data.get("duplicate")))
        except Exception:
            self.logger.exception(f"Stack {stack_id}: could not apply the cloud answer")

    def _print_start_banner(self, batch_id, start_attempt):
        print("\n" + "="*60)
        print("STEP 1: Starting API Send Process")
        print("="*60)
        print(f"  Batch ID: {batch_id}")
        print(f"  API Endpoint: {self.api_url}")
        print(f"  Start Attempt: {start_attempt}")

    def _print_attempt_banner(self, attempt, start_attempt):
        print(f"\n{'='*60}")
        print(f"STEP 5: Sending request (Attempt {attempt}/{start_attempt + self.max_retries})")
        print("="*60)
        print(f"  URL: {self.api_url}")
        print("  Method: POST")
        print(f"  Timeout: {self.timeout} seconds")
        print(f"  SSL Verify: {SSL_VERIFY}")

    def _print_response_details(self, response, elapsed):
        print(f"\nSTEP 6: Response received in {elapsed:.4f} seconds")
        print(f"  Status Code: {response.status_code}")
        print(f"  Reason: {response.reason}")
        self.logger.debug(f"Response: {response.status_code} in {elapsed:.4f}s")

    def _wait_before_retry(self, attempt, start_attempt):
        wait_time = 2 ** (attempt - start_attempt - 1)  # 1, 2, 4 seconds
        print(f"\nWaiting {wait_time} seconds before retry...")
        time.sleep(wait_time)

    def _print_failure_banner(self):
        print("\n" + "="*60)
        print("API SEND FAILED")
        print("="*60 + "\n")

    def _add_payload_hash(self, payload, batch_id):
        # Add an integrity hash to the payload if enabled
        print("\nSTEP 2: Checking payload hash configuration...")
        print(f"  ENABLE_PAYLOAD_HASH: {ENABLE_PAYLOAD_HASH}")
        if ENABLE_PAYLOAD_HASH:
            try:
                data_string = json.dumps(payload, sort_keys=True)
                payload['hash'] = hashlib.sha256(data_string.encode()).hexdigest()
                print(f"  Hash added: {payload['hash'][:20]}...")
                self.logger.debug(f"Added hash to payload for batch {batch_id}")
            except Exception as e:
                print(f"  ERROR adding hash: {e}")
                self.logger.warning(f"Failed to add hash to payload: {e}")
        else:
            print("  Payload hash disabled, skipping...")

    def _log_request_details(self, payload, headers):
        print(f"  Headers: {headers}")

        print("\nSTEP 4: Preparing payload...")
        print(f"  Payload keys: {list(payload.keys())}")
        print(f"  Keg count: {len(payload.get('kegIds', []))}")
        # The exact body, so a rejected report can be diffed against the cloud
        # team's contract without re-running the line.
        self.logger.info(f"Pallet report body: {json.dumps(payload, default=str)[:2000]}")
        self.logger.debug(
            f"Payload: {len(payload.get('kegIds', []))} kegs, "
            f"batchNumber={payload.get('batchNumber')}, layer={payload.get('layer')}, "
            f"result={payload.get('result')}, paletteId={payload.get('paletteId')}")

    def _handle_success_response(self, response, batch_id, sent_count=None,
                                 sent_batch=None):
        """Parse a 2xx pallet-report response. Returns the send_batch() result dict.

        Per the spec a 200 means "received and handled", not "applied".
        `applied: false` means the cloud deliberately wrote nothing, so it is
        treated as a permanent failure rather than reporting a pallet that
        does not exist.

        The DB is not written here; process_worker._write_completed_batch() does
        one write at the end.
        """
        print(f"\nSTEP 8: SUCCESS - Status {response.status_code}")
        try:
            resp_data = response.json()
        except Exception as parse_err:
            # 2xx with an unreadable body: the report reached the cloud, but we
            # have no paletteId to print a label with.
            print(f"  Could not parse JSON: {parse_err}")
            self.logger.warning(f"Batch {batch_id} accepted (HTTP {response.status_code}) "
                                f"but the body could not be parsed: {parse_err}")
            return {"success": True, "pallet_id": None, "error": None,
                    "retryable": False, "response": None}

        # Log the full body: it says what the cloud palletised, what it could
        # not match, and which pallet it all landed on.
        self.logger.info(f"Batch {batch_id} cloud response body: "
                         f"{json.dumps(resp_data, default=str)[:1000]}")

        if resp_data.get("ok") is False or resp_data.get("applied") is False:
            error = (resp_data.get("error")
                     or resp_data.get("message")
                     or "cloud acknowledged the report but applied nothing "
                        "(applied=false)")
            print(f"  Report NOT applied: {error}")
            self.logger.error(f"Batch {batch_id}: report not applied — {error}")
            return _failure(f"Report not applied: {error}", retryable=False,
                            response=resp_data)

        pallet_id = (resp_data.get('paletteId')
                     or resp_data.get('palletId')
                     or resp_data.get('id'))
        print("  Parsed JSON successfully")
        print(f"  Pallet ID: {pallet_id}")
        self.logger.info(f"Batch {batch_id} sent successfully. Pallet ID: {pallet_id}")
        self._log_report_outcome(batch_id, resp_data, sent_count, sent_batch)

        print("\n" + "=" * 60)
        print("API SEND COMPLETED SUCCESSFULLY")
        print("=" * 60 + "\n")
        return {"success": True, "pallet_id": pallet_id, "error": None,
                "retryable": False, "response": resp_data}

    def _log_report_outcome(self, batch_id, resp_data, sent_count, sent_batch=None):
        """Reconcile the codes sent against what the cloud actually palletised.

        A short layer is still a success by design: the cloud palletises what it
        matched and lists the rest under `unmatched`. The operator needs to see
        that gap, so it is logged as a warning naming the codes.

        The reply's `batchNumber` is the batch the cloud recorded on the kegs.
        A different value than we sent is logged as a warning. Log only: the
        pallet exists either way.
        """
        if not isinstance(resp_data, dict):
            return

        recorded_batch = resp_data.get("batchNumber")
        if sent_batch and recorded_batch is not None and str(recorded_batch) != str(sent_batch):
            self.logger.warning(
                f"Batch {batch_id}: sent batch {sent_batch!r} but the cloud recorded "
                f"{recorded_batch!r}")
        elif sent_batch:
            self.logger.info(f"Batch {batch_id}: batch {sent_batch!r} recorded on the kegs")

        reported   = resp_data.get("reportedCount")
        palletised = resp_data.get("palletisedCount")
        changed    = resp_data.get("changedCount")
        unmatched  = resp_data.get("unmatched") or []
        duplicates = resp_data.get("duplicates") or []

        if palletised is None:
            self.logger.info(
                f"Batch {batch_id}: sent {sent_count} code(s); cloud did not echo a "
                f"palletisedCount — cannot reconcile (see the response body above).")
        else:
            self.logger.info(
                f"Batch {batch_id}: sent {sent_count} · reported {reported} · "
                f"palletised {palletised} · changed {changed}")

        if unmatched:
            self.logger.warning(
                f"UNMATCHED CODES for {batch_id}: {len(unmatched)} code(s) matched no "
                f"keg and are NOT on the pallet (unreadable label, or a serial that "
                f"isn't registered): {sorted(str(u) for u in unmatched)}")
        if duplicates:
            self.logger.info(
                f"Batch {batch_id}: {len(duplicates)} duplicate code(s) collapsed by "
                f"the cloud: {sorted(str(d) for d in duplicates)}")
        if changed == 0:
            self.logger.info(
                f"Batch {batch_id}: changedCount=0 — this exact report was already "
                f"applied (harmless idempotent retry).")

        self._log_operator_outcome(batch_id, resp_data)

    def _log_operator_outcome(self, batch_id, resp_data):
        """Report what the cloud made of the operator we attributed the work to.

        `operatorMatched` is true (recognised), false (sent but matches nobody at
        this brewery) or null (we sent none). Per the cloud contract, false is
        not an error: the report was accepted and the kegs updated either way.
        So this only logs; it never changes the send result or triggers a retry.
        The usual cause is a stale pick in Settings, so the warning names the
        operator and says where to fix it.
        """
        matched = resp_data.get("operatorMatched")
        if matched is None:
            return                      # nothing sent, nothing to say
        who = (resp_data.get("operator")
               or resp_data.get("operatorId")
               or config.OPERATOR_SELECTED_NAME
               or "unknown")
        if matched:
            self.logger.info(f"Batch {batch_id}: attributed to operator {who}")
        else:
            self.logger.warning(
                f"Batch {batch_id}: operator '{who}' matched nobody at this brewery "
                f"— the pallet was still created and the kegs updated. Re-pick the "
                f"operator in Settings (REFRESH LIST) if this keeps happening.")

    def _log_error_to_db(self, batch_id, last_error, retryable: bool = True):
        """Record why this send attempt failed, without mis-stating the outcome.

        A retryable failure leaves the row api_pending (still queued, error
        recorded) and does not touch require_attention. A queued report will go
        out on its own; flagging it would bury the real rejections in false
        alarms. Only RETRY_ATTENTION_AFTER raises the flag, once the wait is
        abnormal.

        On a first send this updates nothing: process_worker has not written the
        row yet and does so itself, with the same status logic.
        """
        print("\nSTEP FINAL: All retries exhausted, updating error in database...")
        if not last_error:
            return
        print(f"  Last Error: {last_error}  (retryable={retryable})")
        try:
            with db_lock:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cur = conn.cursor()
                if retryable:
                    cur.execute('''
                        UPDATE detection_sessions
                        SET last_error = ?, batch_status = 'api_pending',
                            api_status = 'queued', last_api_attempt = ?
                        WHERE session_id = ?
                    ''', (last_error, datetime.now(), batch_id))
                else:
                    cur.execute('''
                        UPDATE detection_sessions
                        SET last_error = ?, batch_status = 'api_failed',
                            api_status = 'failed', last_api_attempt = ?,
                            require_attention = 1, attention_reason = ?
                        WHERE session_id = ?
                    ''', (last_error, datetime.now(),
                          f"Send failure: {last_error}", batch_id))
                conn.commit()
                conn.close()
            print("  Database updated with error status")
        except Exception as e:
            print(f"  ERROR updating database: {e}")
            self.logger.error(f"Failed to update error for {batch_id}: {e}")

    def _check_network_status(self):
        """Check whether the API server is reachable."""
        try:
            # Probe the base of the configured API endpoint, with the same
            # scheme (http/https) as the real API.
            from urllib.parse import urlparse
            parsed = urlparse(self.api_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            test_url = base_url

            self.logger.debug(f"Checking network status via: {test_url}")
            response = self.session.get(test_url, timeout=5)
            was_online, was_known = self.network_online, self._network_known
            # Any HTTP response, even a 5xx on the root path, proves the host is
            # reachable (DNS + TCP + TLS succeeded). The root may return 503
            # while the API endpoints answer 200, so only a connection/timeout
            # error counts as offline.
            self.network_online = True
            self._network_known = True
            self.last_network_check = datetime.now()

            if not was_online or not was_known:
                self.logger.info("Network: ONLINE - Server reachable")
                self._on_network_restored()
            if response.status_code >= 500:
                self.logger.debug(f"Network online; root health-check returned {response.status_code}")

            return self.network_online

        except requests.exceptions.RequestException as e:
            was_online, was_known = self.network_online, self._network_known
            self.network_online = False
            self._network_known = True
            self.last_network_check = datetime.now()

            if was_online or not was_known:
                self.logger.error(f"Network: OFFLINE - Connection error: {e}")
                # Tell the operator, so pallets don't pile up in the queue with
                # nothing on screen to say so.
                self._publish(EV_NETWORK_DOWN, error=str(e)[:160],
                              pending=self.pending_count())
            return False

    def _on_network_restored(self):
        """The connection is back: deliver the backlog now, in capture order.

        Every queued report is made due immediately (the backoff ladder only
        protects a server that is refusing us). The drain then runs on this
        monitor thread, so reports still go out one at a time.
        """
        try:
            made_due = self.db.requeue_all_now()
        except Exception:
            self.logger.exception("Could not requeue the backlog after reconnect")
            made_due = 0

        pending = self.pending_count()
        self._publish(EV_NETWORK_UP, pending=pending)
        if pending:
            self.logger.info(f"Network restored — {pending} queued report(s) now due "
                             f"({made_due} rescheduled); draining in capture order")

    def start_retry_monitor(self):
        """Start the background thread that watches the retry queue."""
        if self.retry_thread and self.retry_thread.is_alive():
            return

        self._stop_event.clear()
        self.retry_thread = threading.Thread(
            target=self._retry_monitor_loop,
            name="API_Retry_Monitor",
            daemon=True
        )
        self.retry_thread.start()
        self.logger.info("Retry monitor started")

    def wake_retry_monitor(self):
        """Run a queue sweep now instead of at the next tick."""
        self._wake_event.set()

    def is_known_offline(self) -> bool:
        """True only when a probe has actually failed, not when the state is unknown."""
        return self._network_known and not self.network_online

    def stop_retry_monitor(self):
        """Stop the retry monitor thread."""
        self._stop_event.set()
        self._wake_event.set()
        if self.retry_thread:
            self.retry_thread.join(timeout=2)
            self.logger.info("Retry monitor stopped")

    def _retry_monitor_loop(self):
        """Retry monitor main loop."""
        self.logger.info("Retry monitor loop started")

        while not self._stop_event.is_set():
            try:
                # Check network every interval
                current_time = datetime.now()
                if (not self.last_network_check or
                    (current_time - self.last_network_check).seconds >= self.network_check_interval):
                    self._check_network_status()

                # Process retry queue if network is online
                if self.network_online:
                    self._process_retry_queue()
                    self.send_pending_stack_reports()
                    if config.PALLET_SEND_STACKS:
                        self.close_finished_stacks()

                # Sleep before the next check. Interrupted by stop and by a held
                # report (wake_retry_monitor). Capped at the network-check
                # interval so a reconnect is noticed within 30 s, not 60 s.
                self._wake_event.wait(min(self.retry_interval,
                                          self.network_check_interval))
                self._wake_event.clear()

            except Exception as e:
                self.logger.error(f"Retry monitor error: {e}")
                self._stop_event.wait(60)  # longer sleep on error

    def _process_retry_queue(self):
        """Deliver the queued backlog, one report at a time, in capture order.

        The queue is all of this system's offline resilience, so the rules are
        strict:

        One at a time, oldest first. Reports reach the cloud in the order the
        line built them (`_fetch_due_batches` orders by created_at, not
        next_retry, which would shuffle the backlog after staggered failures).

        Never abandoned. There is no max-attempts filter; a report leaves the
        queue only when the cloud accepts it or permanently rejects it.

        Stop as soon as the network looks gone again. A retryable failure ends
        the sweep: the rest would fail the same way, and each wasted attempt
        pushes a good report further up the backoff ladder.
        """
        try:
            batches = self._fetch_due_batches()
            if not batches:
                return

            total = len(batches)
            self.logger.info(f"Delivering {total} queued report(s), oldest first")
            self._publish(EV_DRAIN_START, total=total)

            confirmed = 0
            for index, (session_id, payload_json, attempts, _err) in enumerate(batches, 1):
                if self._stop_event.is_set():
                    self.logger.info("Shutting down — leaving the rest of the queue "
                                     "for the next run")
                    break

                outcome = self._deliver_queued(session_id, payload_json, attempts,
                                               index, total)
                if outcome == "confirmed":
                    confirmed += 1
                elif outcome == "deferred":
                    # Still offline or server still failing: stop the sweep and
                    # keep the remaining order for next time.
                    break

            remaining = self.pending_count()
            self._publish(EV_DRAIN_DONE, confirmed=confirmed, remaining=remaining)
            self.logger.info(f"Queue sweep finished — {confirmed} confirmed, "
                             f"{remaining} still waiting")

        except Exception as e:
            self.logger.exception(f"Error in retry queue processing: {e}")

    def _fetch_due_batches(self):
        """Due reports as (session_id, payload, attempts, error), capture order."""
        # SQLite reads a negative LIMIT as "no limit".
        limit = int(RETRY_DRAIN_LIMIT) if RETRY_DRAIN_LIMIT > 0 else -1
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cur = conn.cursor()
            cur.execute('''
                SELECT session_id, payload, attempts, error_message
                FROM retry_queue
                WHERE next_retry <= ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
            ''', (datetime.now(), limit))
            batches = cur.fetchall()
            conn.close()
        return batches

    def _deliver_queued(self, session_id, payload_json, attempts, index, total):
        """One queued report. Returns "confirmed", "rejected", "deferred" or "error"."""
        try:
            payload = json.loads(payload_json)
        except Exception as e:
            # A corrupt payload can never be sent and would block the queue.
            # Flag it and take it out.
            self.logger.error(f"{session_id}: queued payload is unreadable ({e}) — "
                              f"removing from the queue for operator action")
            self._remove_from_retry_queue(session_id)
            self._mark_for_attention(session_id, f"Unreadable queued payload: {e}")
            self._publish(EV_ITEM_REJECTED, session_id=session_id,
                          error=f"Unreadable queued payload: {e}")
            return "rejected"

        self.logger.info(f"Delivering {session_id} ({index}/{total}, "
                         f"attempt {attempts + 1})")
        self._publish(EV_ITEM_SENDING, session_id=session_id,
                      index=index, total=total)

        # Refresh the send-time stamp; scannedTime stays as read, since
        # that is what the cloud writes onto each keg.
        payload["timestamp"] = iso_utc()

        try:
            result = self._send_with_retry(session_id, payload, attempts)
        except Exception as e:
            self.logger.exception(f"Error delivering {session_id}")
            self._update_retry_attempts(session_id, attempts + 1)
            self._publish(EV_ITEM_DEFERRED, session_id=session_id,
                          error=str(e)[:160], attempts=attempts + 1)
            return "error"

        if result["success"]:
            pallet_id = self._confirm_delivery(session_id, result.get("pallet_id"))
            self._publish(EV_ITEM_CONFIRMED, session_id=session_id,
                          pallet_id=pallet_id, index=index, total=total)
            return "confirmed"

        if not result["retryable"]:
            # Permanent fault: replaying the same body won't help. Take it out
            # of the queue and flag it for the operator. The outbox keeps it,
            # marked rejected, so a restart does not erase it.
            self.logger.error(
                f"{session_id}: permanent send failure, removing from "
                f"retry queue — {result['error']}")
            self._remove_from_retry_queue(session_id, rejected_error=str(result["error"]))
            stack_store.reject_layer(session_id)
            self._mark_for_attention(
                session_id, f"Permanent send failure: {result['error']}")
            self._publish(EV_ITEM_REJECTED, session_id=session_id,
                          error=str(result["error"])[:160])
            return "rejected"

        self._update_retry_attempts(session_id, attempts + 1)
        self._publish(EV_ITEM_DEFERRED, session_id=session_id,
                      error=str(result["error"])[:160], attempts=attempts + 1)
        return "deferred"

    def _confirm_delivery(self, session_id: str, pallet_id):
        """Turn a delivered report into a real pallet on this device.

        The key step in the queue. Without it the session would stay api_pending:
        missing from Pallet Records, invisible to get_used_kegs() (no
        already-palletized warning for its kegs), and with the cloud's paletteId
        lost, so no label could be printed.

        Any move the operator approved for this pallet is applied here too. That
        decision was made when the layer was built, possibly hours earlier;
        skipping it would leave the keg listed on its previous pallet.

        Returns the pallet id to report to the operator.
        """
        self.db.mark_pallet_sent(session_id, pallet_id)
        stack_store.confirm_layer(session_id, pallet_id)

        # The label goes to the print queue now, not only when the screen
        # handles the CONFIRMED event (which may be dropped on exit or in a
        # burst). The screen's own enqueue is then skipped as a duplicate.
        try:
            from modules.print_queue import queue_confirmed_label
            summary = self.db.get_session_summary(session_id)
            queue_confirmed_label(pallet_id, session_id=session_id,
                                  batch=summary.get("batch", ""),
                                  keg_count=summary.get("keg_count", ""))
        except Exception:
            self.logger.exception(f"{session_id}: could not queue the label")

        try:
            codes = self.db.take_pending_moves(session_id)
            if codes:
                moved = self.db.move_kegs_to_pallet(codes, session_id, pallet_id)
                self.logger.info(
                    f"{session_id}: applied the queued MOVE for {len(moved)} of "
                    f"{len(codes)} keg(s) now the pallet is confirmed")
        except Exception:
            # The pallet is confirmed and the label is about to print; a
            # bookkeeping problem must not undo that.
            self.logger.exception(f"{session_id}: queued MOVE could not be applied")

        self.logger.info(f"{session_id}: delivered from the queue — "
                         f"pallet {pallet_id or '(no id returned)'}")
        return pallet_id

    def _update_retry_attempts(self, session_id: str, attempts: int):
        """Reschedule a report that could not be delivered this sweep.

        The wait is capped (see config.RETRY_BACKOFF_MINUTES), so a report that
        becomes deliverable is not left untried for days. A reconnect skips the
        wait anyway.

        The report is never abandoned, but once it has failed
        RETRY_ATTENTION_AFTER times it is also flagged for attention, so
        "stuck since last night" is visible.
        """
        try:
            backoff_minutes = retry_backoff_minutes(attempts)
            next_retry = datetime.now() + timedelta(minutes=backoff_minutes)

            with db_lock:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cur = conn.cursor()
                cur.execute('''
                    UPDATE retry_queue
                    SET attempts = ?, next_retry = ?, last_attempt = ?
                    WHERE session_id = ?
                ''', (attempts, next_retry, datetime.now(), session_id))
                conn.commit()
                conn.close()

            self.logger.info(f"{session_id}: still queued, next attempt in "
                             f"{backoff_minutes} min (attempt {attempts} failed)")

            if RETRY_ATTENTION_AFTER and attempts == RETRY_ATTENTION_AFTER:
                self._mark_for_attention(
                    session_id,
                    f"Still queued after {attempts} delivery attempts — the cloud "
                    f"has not accepted this pallet. It will keep retrying.")

        except Exception as e:
            self.logger.error(f"Failed to update retry attempts: {e}")

    def _remove_from_retry_queue(self, session_id: str, rejected_error: str = None):
        """Remove a batch from the retry queue and the offline outbox.

        With rejected_error (the cloud refused it for good) the outbox entry is
        kept, marked rejected, so the record survives a restart."""
        try:
            with db_lock:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cur = conn.cursor()
                cur.execute('DELETE FROM retry_queue WHERE session_id = ?', (session_id,))
                conn.commit()
                conn.close()
                self.logger.info(f"Removed {session_id} from retry queue")
            from modules import outbox
            if rejected_error is not None:
                outbox.mark_failed(session_id, f"Permanent send failure: {rejected_error}")
            else:
                outbox.remove(session_id)
        except Exception as e:
            self.logger.error(f"Failed to remove {session_id} from retry queue: {e}")

    def _mark_for_attention(self, session_id: str, reason: str):
        """Flag a batch for operator attention in the database."""
        try:
            with db_lock:
                conn = sqlite3.connect(self.db_path, timeout=30)
                cur = conn.cursor()
                cur.execute('''
                    UPDATE detection_sessions
                    SET require_attention = 1, attention_reason = ?
                    WHERE session_id = ?
                ''', (reason, session_id))
                conn.commit()
                conn.close()
                self.logger.info(f"Marked {session_id} for attention: {reason}")
        except Exception as e:
            self.logger.error(f"Failed to mark {session_id} for attention: {e}")

    def get_network_status(self) -> bool:
        """Current network status."""
        return self.network_online

    def close(self):
        """Stop the retry monitor and close the HTTP session."""
        self.stop_retry_monitor()
        self.session.close()
        self.logger.info("API sender closed")
