"""
System power-off helper for the kiosk.

On the iCam-540 the operator uses the in-app "Power Off Device" action before
cutting mains power. The GUI first does its own cleanup (camera release,
upload/print drain), then calls poweroff() here to halt the machine.

We don't know in advance whether this build has a passwordless sudo rule or
relies on logind/polkit, so several methods are tried in order and the first
one the OS accepts wins. `sudo -n` (non-interactive) makes a missing rule fail
fast instead of blocking on a password prompt.

Deploy note: for the sudo fallbacks to work, add the rule shipped in
deploy/keg-poweroff.sudoers (see deploy/DEPLOY.md). The first (no-sudo) method
works out of the box when the kiosk runs as the active graphical-session user.
"""

import os
import shutil
import logging
import subprocess

log = logging.getLogger(__name__)

# Each entry is an argv list, tried top to bottom until one returns 0.
#   1. logind / polkit: the active console user can normally do this without a
#      password or sudo. Works out of the box on a standard kiosk auto-login.
#   2-3. passwordless sudo, narrowly scoped (see deploy/keg-poweroff.sudoers).
#   4. shutdown(8) fallback for systems without systemctl on PATH.
def _candidate_commands():
    systemctl = shutil.which("systemctl") or "/bin/systemctl"
    shutdown  = shutil.which("shutdown") or "/sbin/shutdown"
    return [
        [systemctl, "poweroff"],
        ["sudo", "-n", systemctl, "poweroff"],
        ["sudo", "-n", "/sbin/poweroff"],
        ["sudo", "-n", shutdown, "-h", "now"],
    ]


def poweroff(dry_run=None):
    """Halt the machine. Returns True if a power-off command was accepted.

    Tries each candidate command until one exits 0. On Windows (the dev box) this
    does nothing and returns False, so the app falls back to a plain exit.

    dry_run: if True, log the chosen command but do not run it. Defaults to the
    env var KEG_POWEROFF_DRYRUN=1 (useful for testing the UI flow on the device
    without shutting it down).
    """
    if dry_run is None:
        dry_run = os.environ.get("KEG_POWEROFF_DRYRUN") == "1"

    if os.name != "posix":
        log.warning("poweroff() called on non-POSIX platform (%s) — skipping", os.name)
        return False

    for cmd in _candidate_commands():
        printable = " ".join(cmd)
        if dry_run:
            log.info("[DRY RUN] would power off with: %s", printable)
            return True
        try:
            log.info("Attempting system power-off: %s", printable)
            # A successful poweroff signals systemd and returns quickly (the halt
            # is asynchronous). The timeout only guards against a command that
            # blocks unexpectedly.
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            if result.returncode == 0:
                log.info("Power-off command accepted: %s", printable)
                return True
            err = (result.stderr or b"").decode("utf-8", "replace").strip()
            log.warning("Power-off command failed (rc=%s): %s — %s",
                        result.returncode, printable, err)
        except FileNotFoundError:
            log.debug("Power-off command not found: %s", printable)
        except subprocess.TimeoutExpired:
            # A slow command most likely means the system is going down. Treat it
            # as success so we don't fall through to the next method.
            log.info("Power-off command timed out (system likely halting): %s", printable)
            return True
        except Exception:
            log.exception("Power-off command errored: %s", printable)

    log.error("All power-off methods failed — check deploy/keg-poweroff.sudoers "
              "and that the kiosk user may halt the machine.")
    return False


def stop_kiosk_service(dry_run=None):
    """Ask systemd to stop the kiosk service so a deliberate Exit stays down.

    Only runs under a systemd user session, so manual/dev runs are not affected.
    The stop is submitted asynchronously, so the GUI can exit right away while
    systemd stops the service.
    """
    if dry_run is None:
        dry_run = os.environ.get("KEG_POWEROFF_DRYRUN") == "1"

    if os.name != "posix":
        log.info("stop_kiosk_service() skipped on non-POSIX platform (%s)", os.name)
        return False

    if os.environ.get("KEG_NO_SERVICE_STOP") == "1":
        log.info("stop_kiosk_service() disabled via KEG_NO_SERVICE_STOP=1")
        return False

    if not os.environ.get("INVOCATION_ID"):
        log.info("stop_kiosk_service() skipped — not launched by systemd")
        return False

    systemctl = shutil.which("systemctl")
    if not systemctl:
        log.warning("stop_kiosk_service() skipped — systemctl not available")
        return False

    unit = os.environ.get("KEG_KIOSK_SERVICE", "keg-palletization.service")
    printable = f"{systemctl} --user stop {unit}"
    if dry_run:
        log.info("[DRY RUN] would stop kiosk service with: %s", printable)
        return True

    # Tell deploy/keg-kiosk-session.sh this stop is a deliberate Exit App, so it
    # opens the desktop. Without this marker it treats the stop as a crash and
    # starts the app again.
    marker = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                          f"keg-exit-requested-{os.getuid()}")
    try:
        with open(marker, "w") as fh:
            fh.write("exit\n")
    except OSError:
        log.exception("Could not write exit marker %s", marker)

    try:
        subprocess.Popen(
            [systemctl, "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        log.exception("Failed to launch service stop command: %s", printable)
        return False

    log.info("Requested systemd stop for kiosk service: %s", printable)
    return True
