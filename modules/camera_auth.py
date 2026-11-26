# modules/camera_auth.py
"""
Camera and operator login for the Checkology "Camera Login API".

There are two separate logins:
  * Camera login   POST /api/camera/auth/login  {macId, password} -> Bearer token
                   (valid 7 days). The camera stays signed in around the clock.
  * Operator login POST /api/camera/operator/login  {operatorId, pin} or
                   {email, password}. After that the cloud records everything
                   the camera reports against that operator.

CameraAuth is a requests AuthBase: set it as `session.auth` and every call on
that session to the camera API carries `Authorization: Bearer <token>`. On a
401 with an auth error (TOKEN_EXPIRED, SESSION_ENDED, INVALID_TOKEN,
CAMERA_AUTH_REQUIRED) it logs in once and repeats the call once, as the spec
asks. If the login itself fails on the credentials, it stops (no retry loop)
and keeps the reason in `last_error` for the UI.

The camera password, token, PINs and operator passwords are never logged.
The token is kept in memory only, so after a restart the camera logs in again,
which per the spec also ends the previous operator's shift.
"""

import logging
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.auth import AuthBase

import config

log = logging.getLogger(__name__)

# 401 codes that mean "log in again, then repeat the call".
RELOGIN_CODES = {"TOKEN_EXPIRED", "SESSION_ENDED", "INVALID_TOKEN", "CAMERA_AUTH_REQUIRED"}

# Camera-login failures a retry cannot fix: stop and show them.
CREDENTIAL_CODES = {"NO_PASSWORD", "INVALID_CREDENTIALS", "CAMERA_PASSWORD_NOT_SET"}

# Error code used when the cloud could not be reached at all.
NETWORK = "NETWORK"

# Minimum seconds between automatic re-login attempts (an explicit login() from
# the UI is not throttled). Stops a dead link from turning every call into a login.
AUTO_LOGIN_MIN_GAP = 15.0
# How often to check again while the server has no login route at all.
AUTO_LOGIN_NOT_DEPLOYED_GAP = 600.0

MESSAGES = {
    "NO_PASSWORD": "No camera password is stored on this device yet.",
    "CAMERA_PASSWORD_NOT_SET": "Camera not set up — generate a password in Keg Management "
                               "(Scanner → Cameras → this camera → Camera login).",
    "INVALID_CREDENTIALS": "The camera password was rejected. Enter the current password "
                           "from Keg Management.",
    "CAMERA_LOCKED": "Too many wrong camera passwords — camera login is locked.",
    "CAMERA_MISMATCH": "The saved camera MAC does not match this camera. Check it in "
                       "Settings → Device Identity → Camera MAC.",
    "CAMERA_NOT_FOUND": "This MAC is not registered in Keg Management. Add the camera "
                        "under Scanner → Cameras, or check the MAC for typos.",
    "CAMERA_AUTH_NOT_CONFIGURED": "The cloud has no camera login configured. Contact "
                                  "Checkology support.",
    "INVALID_OPERATOR_CREDENTIALS": "Wrong PIN, or wrong email / password.",
    "PIN_NOT_SET": "This operator has no PIN — sign in with email and password.",
    "OPERATOR_LOCKED": "Too many wrong attempts — operator sign-in is locked.",
    "MISSING_CREDENTIALS": "Please fill in all the fields.",
    NETWORK: "Cannot reach the cloud. Check the network connection.",
    "AUTH_NOT_DEPLOYED": "Camera login is not enabled on this cloud server yet.",
    "WRONG_HOST": "The API host did not answer with JSON — check config.PALLET_API_BASE_URL.",
}


def _json(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _result(ok, error=None, message="", **extra):
    """Common result shape for every call here. Plain dicts, safe to emit over Qt."""
    if not ok and not message:
        message = MESSAGES.get(error, error or "Unknown error")
    return {"ok": ok, "error": error, "message": message, **extra}


class CameraAuth(AuthBase):
    def __init__(self):
        self._lock = threading.RLock()
        self._token = None
        self.expires_at = None
        self.camera = None             # {id, macId, cameraName, position, lineNumber}
        self.operator = None           # {id, name, role, loggedInAt} while on shift
        self.last_error = None         # last camera-login failure (result dict)
        # Set when a camera re-login ended an operator's shift without the UI
        # knowing. The main window polls it and asks the operator to sign in again.
        self.operator_session_lost = False
        self._failed_password = None   # password the cloud rejected; not retried
        self._locked_until = 0.0
        self._last_auto_login = 0.0

        # Plain session for the login call itself (it must not carry this auth).
        self._http = requests.Session()
        self._http.verify = config.SSL_VERIFY
        self._http.headers.update({"User-Agent": "KegDetectionSystem/1.0",
                                   "Accept": "application/json"})
        # Authenticated session for the operator / me / logout calls.
        self._api = requests.Session()
        self._api.verify = config.SSL_VERIFY
        self._api.headers.update({"User-Agent": "KegDetectionSystem/1.0",
                                  "Accept": "application/json"})
        self._api.auth = self

    # State
    @property
    def signed_in(self):
        return bool(self._token)

    @property
    def cloud_login_available(self):
        """False while the server answers the login route with a 404, meaning
        the Camera Login API is not deployed yet (the UI then signs in locally)."""
        return not (self.last_error and self.last_error.get("error") == "AUTH_NOT_DEPLOYED")

    @property
    def camera_label(self):
        cam = self.camera or {}
        return cam.get("cameraName") or config.CAMERA_NUMBER

    # Camera login
    def login(self, password=None):
        """POST /api/camera/auth/login. Returns a result dict.

        If `password` is given, it is a new password typed in the UI and is
        stored only once the cloud accepts it.
        """
        with self._lock:
            typed = password is not None
            pw = (password if typed else config.camera_password()).strip()
            # Without a password the call is still made (macId only): a server
            # with camera login answers 400 MISSING_CREDENTIALS, one without it
            # answers 404. That decides which sign-in page to show.
            body = {"macId": config.CAMERA_MAC_ID}
            if pw:
                body["password"] = pw
            try:
                resp = self._http.post(
                    config.auth_url(config.CAMERA_AUTH_LOGIN_PATH),
                    json=body, timeout=config.API_TIMEOUT)
            except requests.exceptions.RequestException as e:
                log.warning("Camera login: cloud unreachable (%s)", type(e).__name__)
                return self._login_failed(_result(False, NETWORK))

            is_json = "json" in resp.headers.get("content-type", "").lower()
            if resp.status_code == 404 and not is_json:
                # Framework "Cannot POST" page: this server has not deployed
                # the Camera Login API yet. Calls keep working without a token.
                log.warning("Camera login route not deployed on %s",
                            config.PALLET_API_BASE_URL)
                return self._login_failed(_result(False, "AUTH_NOT_DEPLOYED"))
            if not pw:
                return self._login_failed(_result(False, "NO_PASSWORD"))
            if not is_json:
                return self._login_failed(_result(False, "WRONG_HOST",
                                                  status=resp.status_code))
            data = _json(resp)
            if resp.status_code == 200 and data.get("success") and data.get("token"):
                had_operator = self.operator is not None
                self._token = data["token"]
                self.expires_at = data.get("expiresAt")
                self.camera = data.get("camera") or None
                # A fresh camera login ends any operator shift on this camera.
                self.operator = data.get("operator") or None
                if had_operator and self.operator is None:
                    self.operator_session_lost = True
                self.last_error = None
                self._failed_password = None
                self._locked_until = 0.0
                if typed:
                    config.save_camera_password(pw)
                log.info("Camera signed in as %s (token valid until %s)",
                         self.camera_label, self.expires_at)
                return _result(True, camera=self.camera, expires_at=self.expires_at)

            code = data.get("error") or f"HTTP_{resp.status_code}"
            retry_after = int(data.get("retryAfterSeconds") or 0)
            res = _result(False, code, MESSAGES.get(code) or data.get("message") or code,
                          status=resp.status_code, retry_after=retry_after)
            if code in CREDENTIAL_CODES:
                self._failed_password = pw
            if code == "CAMERA_LOCKED":
                self._locked_until = time.monotonic() + retry_after
            log.error("Camera login failed: %s", code)
            return self._login_failed(res)

    def _login_failed(self, res):
        self._token = None
        self.last_error = res
        return res

    def _auto_login_allowed(self):
        """Whether a request may trigger a login by itself. Not with a password
        the cloud already rejected, not while locked, and not in a tight loop."""
        now = time.monotonic()
        if now < self._locked_until:
            return False
        pw = config.camera_password()
        if not pw or pw == self._failed_password:
            return False
        gap = (AUTO_LOGIN_MIN_GAP if self.cloud_login_available
               else AUTO_LOGIN_NOT_DEPLOYED_GAP)
        if now - self._last_auto_login < gap:
            return False
        self._last_auto_login = now
        return True

    def _relogin(self, used_token):
        """Log in again after `used_token` was refused. True when a new token
        is available (possibly fetched by another thread meanwhile)."""
        with self._lock:
            if self._token and self._token != used_token:
                return True
            if not self._auto_login_allowed():
                return False
            return self.login()["ok"]

    # requests AuthBase hook
    @staticmethod
    def _applies(url):
        """True only for the camera API's own scheme, host and port.

        A plain prefix test would also match "https://api.host.evil.net/..."
        (when the base URL has no trailing slash) and hand the token to it."""
        try:
            target = urlsplit(str(url or ""))
            base = urlsplit(config.PALLET_API_BASE_URL)
            return (target.scheme.lower() == base.scheme.lower()
                    and target.hostname is not None
                    and target.hostname == base.hostname
                    and target.port == base.port)
        except ValueError:            # malformed URL / port: never send the token
            return False

    def __call__(self, r):
        if not self._applies(r.url):
            return r
        token = self._token
        if token is None and self._relogin(None):
            token = self._token
        if token:
            r.headers["Authorization"] = f"Bearer {token}"
        r._camera_token = token
        r.register_hook("response", self._on_response)
        return r

    def _on_response(self, resp, **kwargs):
        """On an auth 401: log in once and repeat the call once."""
        if resp.status_code != 401:
            return resp
        req = resp.request
        if getattr(req, "_camera_retried", False):
            return resp
        if _json(resp).get("error") not in RELOGIN_CODES:
            return resp
        if not self._relogin(getattr(req, "_camera_token", None)):
            return resp
        _ = resp.content        # drain so the connection can be reused
        resp.close()
        retry = req.copy()
        retry._camera_retried = True
        retry._camera_token = self._token
        retry.headers["Authorization"] = f"Bearer {self._token}"
        new = resp.connection.send(retry, **kwargs)
        new.history.append(resp)
        new.request = retry
        return new

    # Authenticated calls
    def _call(self, method, path, body=None):
        try:
            resp = self._api.request(method, config.auth_url(path), json=body,
                                     timeout=config.API_TIMEOUT)
        except requests.exceptions.RequestException as e:
            log.warning("%s %s: cloud unreachable (%s)", method, path, type(e).__name__)
            return _result(False, NETWORK)
        data = _json(resp)
        if resp.status_code == 200 and data.get("success", True) is not False:
            return _result(True, data=data)
        code = data.get("error") or f"HTTP_{resp.status_code}"
        return _result(False, code, MESSAGES.get(code) or data.get("message") or code,
                       status=resp.status_code,
                       retry_after=int(data.get("retryAfterSeconds") or 0))

    def me(self):
        """GET /api/camera/auth/me: camera, operator on shift, session."""
        res = self._call("GET", config.CAMERA_AUTH_ME_PATH)
        if res["ok"]:
            data = res["data"]
            self.camera = data.get("camera") or self.camera
            self.operator = data.get("operator") or None
        return res

    def operator_login(self, operator_id=None, pin=None, email=None, password=None):
        """Start of shift. Send one form only: operatorId + pin, or email + password."""
        if pin is not None:
            body, method = {"operatorId": operator_id, "pin": str(pin)}, "pin"
        else:
            body, method = {"email": (email or "").strip(), "password": password or ""}, "password"
        res = self._call("POST", config.OPERATOR_LOGIN_PATH, body)
        if res["ok"]:
            self.operator = res["data"].get("operator") or None
            self.operator_session_lost = False
            log.info("Operator signed in: %s (%s)",
                     (self.operator or {}).get("name"), method)
            res["operator"] = self.operator
        elif res["error"] == "INVALID_OPERATOR_CREDENTIALS":
            res["message"] = ("Wrong PIN." if method == "pin"
                              else "Wrong email or password.")
        return res

    def operator_logout(self):
        """End of shift. The camera stays signed in and keeps reporting."""
        res = self._call("POST", config.OPERATOR_LOGOUT_PATH)
        if res["ok"] or res["error"] != NETWORK:
            self.operator = None
        if res["ok"]:
            log.info("Operator signed out")
        return res

    def logout(self):
        """Camera logout. Only for a decommissioned or replaced device."""
        res = self._call("POST", config.CAMERA_AUTH_LOGOUT_PATH)
        with self._lock:
            self._token = None
            self.operator = None
        return res


def verify_camera(mac_id, password):
    """Check a MAC + camera password with the cloud, for the Camera Setup screen.

    Uses its own plain session and does not touch the shared CameraAuth (no
    token is kept, nothing is saved). Result dict as elsewhere, plus:
      camera          {cameraName, position, lineNumber, ...} when the cloud gives it
      password_checked True when the password itself was verified
    Flow:
      1. POST /camera/auth/login {macId, password}: 200 = MAC registered and
         password right.
      2. Only if this server has no login route (AUTH_NOT_DEPLOYED): fall back
         to GET /camera/{mac}/operators, which checks the MAC only.
    """
    mac = config.parse_mac(mac_id)
    if not mac:
        return _result(False, "INVALID_MAC", "That is not a valid MAC address "
                                             "(e.g. 3C:6D:66:2C:D8:16).")
    pw = str(password or "").strip()
    http = requests.Session()
    http.verify = config.SSL_VERIFY
    http.headers.update({"User-Agent": "KegDetectionSystem/1.0",
                         "Accept": "application/json"})
    try:
        try:
            resp = http.post(config.auth_url(config.CAMERA_AUTH_LOGIN_PATH),
                             json={"macId": mac, "password": pw},
                             timeout=config.API_TIMEOUT)
        except requests.exceptions.RequestException as e:
            log.warning("Camera setup check: cloud unreachable (%s)", type(e).__name__)
            return _result(False, NETWORK)

        is_json = "json" in resp.headers.get("content-type", "").lower()
        if resp.status_code == 404 and not is_json:
            return _verify_mac_only(http, mac)
        if not is_json:
            return _result(False, "WRONG_HOST", status=resp.status_code)
        data = _json(resp)
        if resp.status_code == 200 and data.get("success") and data.get("token"):
            camera = data.get("camera") or {}
            returned = config.parse_mac(camera.get("macId", "")) if camera else ""
            if returned and returned != mac:
                return _result(False, "CAMERA_MISMATCH",
                               f"The cloud answered for camera {returned}, not {mac}.")
            log.info("Camera setup check passed for %s", mac)
            return _result(True, camera=camera, password_checked=True, mac=mac)

        code = data.get("error") or f"HTTP_{resp.status_code}"
        if resp.status_code == 404 or code in ("CAMERA_NOT_FOUND", "NOT_FOUND"):
            return _result(False, "CAMERA_NOT_FOUND", MESSAGES["CAMERA_NOT_FOUND"],
                           status=resp.status_code)
        if not pw and code in ("MISSING_CREDENTIALS", "NO_PASSWORD"):
            code = "NO_PASSWORD"
        log.warning("Camera setup check failed for %s: %s", mac, code)
        return _result(False, code, MESSAGES.get(code) or data.get("message") or code,
                       status=resp.status_code,
                       retry_after=int(data.get("retryAfterSeconds") or 0))
    finally:
        http.close()


def _verify_mac_only(http, mac):
    """Fallback when the server has no camera login: is the MAC registered?"""
    try:
        resp = http.get(config.operators_url(mac), timeout=config.API_TIMEOUT)
    except requests.exceptions.RequestException as e:
        log.warning("Camera setup check: cloud unreachable (%s)", type(e).__name__)
        return _result(False, NETWORK)
    if "json" not in resp.headers.get("content-type", "").lower():
        return _result(False, "WRONG_HOST", status=resp.status_code)
    if resp.status_code == 200:
        data = _json(resp)
        log.info("Camera setup check passed for %s (MAC only, no login route)", mac)
        return _result(True, camera={"breweryId": data.get("breweryId")},
                       password_checked=False, mac=mac)
    if resp.status_code == 404:
        return _result(False, "CAMERA_NOT_FOUND", MESSAGES["CAMERA_NOT_FOUND"], status=404)
    return _result(False, f"HTTP_{resp.status_code}", status=resp.status_code)


_instance = None
_instance_lock = threading.Lock()


def get_camera_auth():
    """Return the shared CameraAuth for this process (one camera, one live login)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = CameraAuth()
        return _instance
