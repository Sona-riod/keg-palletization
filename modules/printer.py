import subprocess
import logging
import os
import sys
import glob
import re
import shutil
import tempfile

from . import usb_printer

logger = logging.getLogger(__name__)

ZPL_ENCODING = 'utf-8'

# CUPS printer names: no spaces, '/', '#' or control characters, and a leading
# '-' would be read by lp as an option.
_CUPS_NAME = re.compile(r"[^\s/#\\'\"-][^\s/#\\'\"]{0,127}")


def _is_printer_device(path):
    """True when `path` resolves inside the USB printer device folder (or /dev).

    The sudo tee fallback writes as root, so it must never be pointed at a
    regular file, even through a symlink."""
    real = os.path.realpath(str(path))
    roots = {os.path.realpath(usb_printer.DEV_USB), os.path.realpath("/dev")}
    return any(real.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def valid_printer_name(name):
    """True for a CUPS queue name that is safe to pass to `lp -d`."""
    return bool(name) and _CUPS_NAME.fullmatch(str(name)) is not None

class ZebraPrinter:
    def __init__(self, device_path='/dev/usb/lp0'):
        self.device_path = device_path
        self.use_pyusb = False
        if not os.path.exists(self.device_path):
            usb_printers = glob.glob('/dev/usb/lp*')
            if usb_printers:
                self.device_path = usb_printers[0]
                logger.info(f"Default printer {device_path} not found. Auto-detected {self.device_path}")
            else:
                logger.warning("No USB printers found in /dev/usb/lp*. Enabling PyUSB fallback.")
                self.use_pyusb = True

    def print_pallet_qr(self, pallet_id):
        if not pallet_id:
            logger.warning("No pallet ID provided for printing.")
            return False, "No pallet ID provided"

        # The id comes from the cloud: a '^' or '~' in it would start a new ZPL
        # command, so it is cleaned exactly as AutoPrinter cleans it.
        pallet_id = usb_printer.clean_pallet_id(pallet_id)
        if not pallet_id:
            return False, "Pallet ID has no printable characters"
        logger.info(f"Generating ZPL for Pallet ID: {pallet_id}")

        # Same bytes as usb_printer.zpl_label: ^BQN,2,25 is a Model 2 QR at
        # magnification 25, the text at ^FO50,1000 sits below it.
        zpl_command = usb_printer.zpl_label(pallet_id).decode(ZPL_ENCODING)

        if self.use_pyusb:
            return self._print_via_pyusb(zpl_command)

        try:
            # Method 1: Direct write (works if user is in 'lp' group)
            with open(self.device_path, 'wb') as printer:
                printer.write(zpl_command.encode(ZPL_ENCODING))
            logger.info("Successfully printed directly to USB.")
            return True, ""

        except PermissionError:
            # Method 2: sudo tee. -n fails at once instead of waiting for a
            # password nobody can type; only a printer device is ever written.
            logger.warning(f"Permission denied for {self.device_path}. Falling back to sudo tee.")
            if not _is_printer_device(self.device_path):
                return False, f"Not a printer device: {self.device_path}"
            try:
                subprocess.run(  # nosec B603 B607
                    ['sudo', '-n', 'tee', self.device_path],
                    input=zpl_command.encode(ZPL_ENCODING),
                    check=True,
                    stdout=subprocess.DEVNULL,
                    timeout=30,
                )
                logger.info("Successfully printed using sudo tee fallback.")
                return True, ""
            except Exception as e:
                logger.exception("Fallback printing failed")
                return False, f"Fallback printing failed: {e}"

        except Exception as e:
            logger.exception("Printer error")
            return False, str(e)

    def _print_via_pyusb(self, zpl_command):
        try:
            import usb.core  # type: ignore[import-not-found]
            import usb.util  # type: ignore[import-not-found]
        except ImportError:
            logger.error("PyUSB not installed. Cannot use USB fallback. Please run: pip install pyusb")
            return False, "PyUSB not installed"

        dev = usb.core.find(idVendor=0x0a5f)
        if dev is None:
            logger.error("No Zebra printer found via PyUSB.")
            return False, "No printer found via PyUSB"

        return self._write_to_usb_device(dev, zpl_command)

    def _safe_reset(self, dev):
        try:
            dev.reset()
        except Exception as e:
            logger.warning(f"Device reset warning (non-fatal): {e}")

    def _detach_kernel_drivers(self, dev):
        detached_ifaces = []
        for iface_num in range(dev.get_active_configuration().bNumInterfaces):
            try:
                if dev.is_kernel_driver_active(iface_num):
                    dev.detach_kernel_driver(iface_num)
                    detached_ifaces.append(iface_num)
                    logger.debug(f"Detached kernel driver from interface {iface_num}")
            except Exception as e:
                logger.warning(f"Could not detach kernel driver from interface {iface_num}: {e}")
        return detached_ifaces

    def _safe_set_configuration(self, dev):
        try:
            dev.set_configuration()
        except Exception as e:
            logger.warning(f"set_configuration warning (non-fatal): {e}")

    def _find_out_endpoint(self, intf):
        import usb.util  # type: ignore[import-not-found]
        return usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            ),
        )

    def _release_usb_resources(self, dev, interface_claimed, detached_ifaces,
                               interface=(0, 0)):
        import usb.util  # type: ignore[import-not-found]
        if interface_claimed:
            try:
                cfg = dev.get_active_configuration()
                intf = cfg[interface]
                usb.util.release_interface(dev, intf.bInterfaceNumber)
                logger.debug("Interface released.")
            except Exception:
                pass

        for iface_num in detached_ifaces:
            try:
                dev.attach_kernel_driver(iface_num)
                logger.debug(f"Re-attached kernel driver to interface {iface_num}")
            except Exception as e:
                logger.warning(f"Could not re-attach kernel driver to interface {iface_num}: {e}")

        usb.util.dispose_resources(dev)
        logger.debug("USB resources disposed — printer closed.")

    def _write_to_usb_device(self, dev, zpl_command, _retry=True, interface=(0, 0)):
        """Write ZPL (text) or raw label bytes to the USB device, retrying once
        with a full reset on [Errno 5]. `interface` is the printer's
        (interface number, alternate setting); Zebra printers use (0, 0)."""
        import usb.util  # type: ignore[import-not-found]

        if isinstance(zpl_command, (bytes, bytearray)):
            payload = bytes(zpl_command)
        else:
            payload = zpl_command.encode(ZPL_ENCODING)
        interface_claimed = False
        detached_ifaces = []

        try:
            # 1. Reset device to clear any stale USB state
            self._safe_reset(dev)

            # 2. Detach kernel driver (usblp)
            detached_ifaces = self._detach_kernel_drivers(dev)
            self._safe_set_configuration(dev)

            cfg = dev.get_active_configuration()
            intf = cfg[interface]

            # 3. Claim the interface
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
            interface_claimed = True
            if intf.bAlternateSetting:
                intf.set_altsetting()

            ep = self._find_out_endpoint(intf)
            if ep is None:
                logger.error("Endpoint not found via PyUSB.")
                return False, "Endpoint not found"

            # 4. Send ZPL
            ep.write(payload, timeout=5000)
            logger.info("Successfully printed using PyUSB.")
            return True, ""

        except Exception as e:
            err_str = str(e)
            logger.exception("PyUSB printing failed")

            if "[Errno 5]" in err_str and _retry:
                # Hard reset and retry once on I/O error
                logger.warning("Got [Errno 5] I/O error — resetting device and retrying once...")
                self._safe_reset(dev)
                return self._write_to_usb_device(dev, zpl_command, _retry=False,
                                                 interface=interface)

            if "Access denied" in err_str or "Insufficient permissions" in err_str:
                logger.error("Try running the app with 'sudo' or add a udev rule for the printer.")
                return False, (f"No permission to use the printer — start the app "
                               f"with sudo ({e})")
            if "[Errno 16]" in err_str or "busy" in err_str.lower():
                return False, f"Printer in use by another program ({e})"

            return False, f"PyUSB failed: {e}"

        finally:
            # 5. Release resources
            self._release_usb_resources(dev, interface_claimed, detached_ifaces,
                                        interface)


# GenericPrinter: works with any installed printer (cross-platform).
#
# Renders the cloud-issued pallet id as a label image (QR code + text) and
# sends it to the OS printing stack:
#    • Windows:   the named or default printer via the GDI print DC (pywin32)
#    • Linux/Mac: the named or default CUPS printer via `lp`
# No special driver is needed.
class GenericPrinter:
    def __init__(self, printer_name=None, copies=1, label_size=(600, 760)):
        # printer_name None/"" means the OS default printer
        self.printer_name = printer_name or None
        self.copies = max(1, int(copies or 1))
        self.label_size = label_size

    # Public API (same as ZebraPrinter.print_pallet_qr)
    def print_pallet_qr(self, pallet_id):
        if not pallet_id:
            logger.warning("No pallet ID provided for printing.")
            return False, "No pallet ID provided"

        try:
            label = self._render_label(str(pallet_id))
        except Exception as e:
            logger.exception("Failed to render pallet label")
            return False, f"Label render failed: {e}"

        try:
            if sys.platform.startswith("win"):
                return self._print_windows(label)
            return self._print_unix(label)
        except Exception as e:
            logger.exception("Generic printing failed")
            return False, str(e)

    # Label rendering
    def _render_label(self, pallet_id):
        """Build a QR + text label image for the pallet id."""
        import qrcode
        from PIL import Image, ImageDraw

        w, h = self.label_size
        label = Image.new("RGB", (w, h), "white")

        # QR code sized to ~80% of the label width
        qr = qrcode.QRCode(border=2, box_size=10,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(pallet_id)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_target = int(w * 0.8)
        qr_img = qr_img.resize((qr_target, qr_target), Image.NEAREST)
        label.paste(qr_img, ((w - qr_target) // 2, int(h * 0.05)))

        # Pallet id caption under the QR
        draw = ImageDraw.Draw(label)
        font = self._load_font(int(h * 0.07))
        text = f"Pallet: {pallet_id}"
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = draw.textlength(text, font=font)
        ty = int(h * 0.05) + qr_target + int(h * 0.04)
        draw.text(((w - tw) // 2, ty), text, fill="black", font=font)
        return label

    @staticmethod
    def _load_font(size):
        from PIL import ImageFont
        for name in ("arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    # Windows backend (GDI print DC)
    def _print_windows(self, label):
        try:
            import win32print
            import win32ui
            from PIL import ImageWin
        except ImportError:
            logger.warning("pywin32 not available — falling back to ShellExecute print.")
            return self._print_windows_shell(label)

        printer_name = self.printer_name or win32print.GetDefaultPrinter()
        if not printer_name:
            return False, "No default printer configured in Windows"

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        try:
            # Printable area in device pixels
            HORZRES, VERTRES = 8, 10
            pw = hdc.GetDeviceCaps(HORZRES)
            ph = hdc.GetDeviceCaps(VERTRES)

            # Scale the label to fit the printable area, preserving aspect ratio
            lw, lh = label.size
            scale = min(pw / lw, ph / lh)
            dw, dh = int(lw * scale), int(lh * scale)
            x = (pw - dw) // 2
            y = max(0, int(ph * 0.02))

            dib = ImageWin.Dib(label)
            for _ in range(self.copies):
                hdc.StartDoc("Pallet Label")
                hdc.StartPage()
                dib.draw(hdc.GetHandleOutput(), (x, y, x + dw, y + dh))
                hdc.EndPage()
                hdc.EndDoc()
            logger.info("Printed pallet label to '%s'", printer_name)
            return True, ""
        finally:
            hdc.DeleteDC()

    def _print_windows_shell(self, label):
        """Last-resort Windows path: print the image via the shell 'print' verb."""
        path = self._save_temp(label)
        try:
            os.startfile(path, "print")  # type: ignore[attr-defined]
            logger.info("Sent pallet label to default printer via ShellExecute.")
            return True, ""
        except Exception as e:
            return False, f"ShellExecute print failed: {e}"

    # Unix backend (CUPS `lp`)
    def _print_unix(self, label):
        if self.printer_name and not valid_printer_name(self.printer_name):
            return False, f"Invalid printer name: {self.printer_name!r}"
        path = self._save_temp(label)
        cmd = ["lp"]
        if self.printer_name:
            cmd += ["-d", self.printer_name]
        if self.copies > 1:
            cmd += ["-n", str(self.copies)]
        cmd.append(path)
        try:
            subprocess.run(cmd, check=True, timeout=60,  # nosec B603 B607
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            logger.info("Printed pallet label via lp (%s).",
                        self.printer_name or "default")
            return True, ""
        except FileNotFoundError:
            return False, "'lp' command not found — install CUPS or a print system"
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode(errors="replace").strip()
            return False, f"lp failed: {err or e}"
        except subprocess.TimeoutExpired:
            return False, "lp did not answer within 60 seconds"
        finally:
            # lp has copied the file into the spool by the time it returns, so
            # delete the temp image (otherwise one PNG piles up per label).
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _save_temp(label):
        fd, path = tempfile.mkstemp(prefix=TEMP_LABEL_PREFIX, suffix=".png")
        os.close(fd)
        label.save(path)
        return path


TEMP_LABEL_PREFIX = "pallet_"


def cleanup_temp_labels(max_age_seconds=3600):
    """Delete leftover label images from the temp folder.

    The Windows shell-print path hands the file to another program and cannot
    delete it itself. Anything older than an hour has long been spooled.
    Never raises. Returns how many were removed.
    """
    import time
    removed = 0
    cutoff = time.time() - max_age_seconds
    pattern = os.path.join(tempfile.gettempdir(), f"{TEMP_LABEL_PREFIX}*.png")
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Removed %d leftover temp label image(s).", removed)
    return removed


# AutoPrinter: any USB printer, no setting per printer.
#
# For every label it looks up the connected USB printer again (so a printer can
# be swapped while the app runs) and builds the label in the language the
# printer reports (modules/usb_printer.py):
#    • ZPL / TSPL / ESC/POS: raw label data to /dev/usb/lpN, or through PyUSB
#    • office printer:       the QR image through the system print service
#    • no USB printer:       the system default printer, if there is one
class AutoPrinter:
    def __init__(self, printer_name=None, copies=1, label_size=(600, 760)):
        self.printer_name = printer_name or None
        self.copies = max(1, int(copies or 1))
        self.label_size = label_size
        # Only ZebraPrinter's PyUSB helpers are used; they keep no state.
        self._usb_writer = object.__new__(ZebraPrinter)

    def print_pallet_qr(self, pallet_id):
        if not pallet_id:
            logger.warning("No pallet ID provided for printing.")
            return False, "No pallet ID provided"
        pallet_id = usb_printer.clean_pallet_id(pallet_id)
        if not pallet_id:
            return False, "Pallet ID has no printable characters"

        found, system = _resolve_auto(self.printer_name)
        if system is not None:
            ok, _, msg = system
            if not ok:
                return False, _auto_not_ready(found, msg)
            logger.info("Printing pallet label through the system print service.")
            return GenericPrinter(self.printer_name, self.copies,
                                  self.label_size).print_pallet_qr(pallet_id)
        if not found.usable:
            return False, found.reason

        data = usb_printer.label_data(found.language, pallet_id) * self.copies
        logger.info(f"Printing {found.language} label for '{pallet_id}' "
                    f"({len(pallet_id)} chars, {len(data)} bytes) on {found.name} "
                    f"({found.where})")
        if found.device_path:
            ok, err = self._write_device(found.device_path, data)
        else:
            ok, err = self._usb_writer._write_to_usb_device(
                found.usb_device, data, interface=found.interface)
        if not ok:
            logger.error(f"Label for '{pallet_id}' not printed: {err}")
        return ok, err

    @staticmethod
    def _write_device(path, data):
        try:
            with open(path, "wb") as f:
                f.write(data)
            logger.info(f"Label sent to {path}.")
            return True, ""
        except PermissionError:
            # -n: fail at once instead of waiting for a password. Only a path
            # inside the printer device folder is ever written as root.
            logger.warning(f"Permission denied for {path}. Trying sudo -n tee.")
            if not _is_printer_device(path):
                return False, f"Not a printer device: {path}"
            try:
                subprocess.run(["sudo", "-n", "tee", path], input=data, check=True,  # nosec B603 B607
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=30)
                logger.info(f"Label sent to {path} with sudo tee.")
                return True, ""
            except Exception as e:
                logger.exception("sudo tee printing failed")
                return False, (f"No permission to use the printer at {path} — add the "
                               f"app user to the 'lp' group or start the app with sudo "
                               f"({e})")
        except Exception as e:
            logger.exception("Printer error")
            return False, str(e)


def _resolve_auto(printer_name=None):
    """What a label printed now would go to. Shared by AutoPrinter and the probe.

    -> (found, system)
       found   the USB printer (usb_printer.UsbPrinter), or None
       system  (ok, name, msg) of the system printer when the label goes there
               (office printer, or no USB printer at all); None for raw printing
    """
    found = usb_printer.find_printer()
    if found is not None and found.language != usb_printer.SYSTEM:
        return found, None
    return found, _probe_generic(printer_name)


def _auto_not_ready(found, system_msg):
    if found is None:
        return "No printer connected"
    return (f"{found.name} needs the system print service, which is not ready "
            f"({system_msg})")


# Factory: picks the printer backend.
def get_printer(config=None):
    """Build a printer from PRINTER_CONFIG (or sensible defaults).

    backend:
        "auto"    → AutoPrinter (any USB printer, language from its Device ID)
        "generic" → GenericPrinter (any installed/default printer)
        "zebra"   → ZebraPrinter (raw ZPL over USB)
    Falls back to GenericPrinter if the requested backend can't init, so a
    printer problem never stops the app at startup.
    """
    if config is None:
        try:
            from config import PRINTER_CONFIG as config
        except Exception:
            config = {}

    backend = (config.get("backend") or "auto").lower()
    name = config.get("printer_name")
    copies = config.get("copies", 1)
    label_size = config.get("label_size", (600, 760))

    def _generic():
        return GenericPrinter(printer_name=name, copies=copies, label_size=label_size)

    try:
        if backend == "generic":
            return _generic()
        if backend == "zebra":
            return ZebraPrinter()
        return AutoPrinter(printer_name=name, copies=copies, label_size=label_size)
    except Exception as e:
        logger.warning("Printer backend '%s' init failed (%s); using GenericPrinter.",
                       backend, e)
        return _generic()


# Printer health probe: tells the operator whether a label can actually print,
# without sending anything to paper. Used by the Settings page and a startup
# warning, so a missing printer is noticed before a pallet is created.
def probe_printer(config=None):
    """Non-printing health check for the pallet-label printer.

    Returns a status dict:
        backend       "zebra" | "generic" | "auto (ZPL)" / "auto (TSPL)" /
                      "auto (ESC/POS)" / "auto (system)" / "auto"
        printer_name  resolved target, or "System default"
        render_ok     bool: qrcode + Pillow importable (label can be rendered)
        render_msg    str
        device_ok     bool: a printer is present / reachable
        device_msg    str
        ready         bool: render_ok and device_ok (label will print)
        summary       one-line human status
        problem       "" when ready, else why (NOT_CONNECTED, usb_printer.BUSY, ...)
        hint          what the operator should do ("" when ready)
        supported     the supported-printer list when the problem calls for it
    """
    if config is None:
        try:
            from config import PRINTER_CONFIG as config
        except Exception:
            config = {}

    backend = (config.get("backend") or "auto").lower()
    name = config.get("printer_name")

    if backend not in ("zebra", "generic"):
        return _probe_auto(name)

    # 1) Can we render a label? Zebra streams raw ZPL (no rendering); generic
    #    needs qrcode + Pillow.
    if backend == "zebra":
        render_ok, render_msg = True, "ZPL (no rendering needed)"
    else:
        render_ok, render_msg = _check_render_deps()

    # 2) Is a printer actually present / reachable?
    if backend == "zebra":
        device_ok, device_name, device_msg = _probe_zebra()
    else:
        device_ok, device_name, device_msg = _probe_generic(name)

    ready = render_ok and device_ok
    if ready:
        summary = "Ready"
        problem = ""
    elif not device_ok:
        summary = "No printer detected"
        problem = NOT_CONNECTED
    else:
        summary = "Printer found, label cannot render"
        problem = RENDER

    return {
        "backend": backend,
        "printer_name": device_name,
        "render_ok": render_ok,
        "render_msg": render_msg,
        "device_ok": device_ok,
        "device_msg": device_msg,
        "ready": ready,
        "summary": summary,
        "problem": problem,
        "hint": _PROBLEMS[problem][1] if problem else "",
        "supported": "",
    }


# Why a label cannot print (probe_printer()["problem"]), with the one-line
# status and what the operator should do. The USB-side kinds come from
# usb_printer (NOT_SUPPORTED, BUSY, PERMISSION, NO_ANSWER).
NOT_CONNECTED = "not_connected"
PRINT_SERVICE = "print_service"
RENDER = "render"

_PROBLEMS = {
    NOT_CONNECTED: ("No printer connected",
                    "Plug the label printer into a USB port of the camera and switch it on."),
    usb_printer.NOT_SUPPORTED: ("Printer not supported",
                                "Connect a supported label printer."),
    usb_printer.BUSY: ("Printer in use by another program",
                       "Close the other program, or unplug the printer and plug it "
                       "back in."),
    usb_printer.PERMISSION: ("No permission to use the printer",
                             "Start the app from its desktop icon (it runs with sudo), "
                             "or add the user to the 'lp' group."),
    usb_printer.NO_ANSWER: ("Printer does not answer",
                            "Switch the printer off and on and check the USB cable."),
    PRINT_SERVICE: ("Office printer needs the print service",
                    "This printer prints through CUPS, which is not set up on this "
                    "camera. Connect a label printer instead."),
    RENDER: ("Printer found, label cannot render",
             "Install qrcode and Pillow in the app's Python."),
}


def _can_write(path):
    """True when the app can write to the printer device, directly or with sudo -n."""
    if os.access(path, os.W_OK):
        return True
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True,
                              timeout=5).returncode == 0
    except Exception:
        return False


def _probe_auto(name):
    # Resolved exactly as AutoPrinter.print_pallet_qr() does, so the print queue
    # waits whenever a label could not print.
    found, system = _resolve_auto(name)
    problem = ""
    if system is not None:
        device_ok, device_name, device_msg = system
        if found is not None:
            device_name = found.name
            device_msg = f"{found.name} on {found.where} through {system[1]}: {system[2]}"
        if not device_ok:
            device_msg = _auto_not_ready(found, system[2])
            problem = NOT_CONNECTED if found is None else PRINT_SERVICE
        backend = "auto (system)" if found is not None or device_ok else "auto"
        render_ok, render_msg = _check_render_deps()
    elif found.usable:
        device_ok, device_name = True, found.name
        device_msg = f"{found.name} on {found.where}, {found.language}"
        backend = f"auto ({found.language})"
        render_ok, render_msg = True, f"{found.language} (no rendering needed)"
        if found.device_path and not _can_write(found.device_path):
            device_ok, problem = False, usb_printer.PERMISSION
            device_msg = f"{found.name}: no permission to write to {found.device_path}"
    else:
        device_ok, device_name, device_msg = False, found.name, found.reason
        backend = "auto"
        render_ok, render_msg = True, "nothing to render"
        problem = found.problem or usb_printer.NOT_SUPPORTED

    ready = render_ok and device_ok
    if not ready and not problem:
        problem = RENDER
    summary, hint = ("Ready", "") if ready else _PROBLEMS[problem]
    supported = (usb_printer.SUPPORTED_PRINTERS
                 if problem in (usb_printer.NOT_SUPPORTED, PRINT_SERVICE, NOT_CONNECTED)
                 else "")

    return {
        "backend": backend,
        "printer_name": device_name,
        "render_ok": render_ok,
        "render_msg": render_msg,
        "device_ok": device_ok,
        "device_msg": device_msg,
        "ready": ready,
        "summary": summary,
        "problem": problem,         # "" when ready, else a key of _PROBLEMS
        "hint": hint,               # what the operator should do
        "supported": supported,     # supported-printer list, when it helps
    }


def _check_render_deps():
    missing = []
    try:
        import qrcode  # noqa: F401
    except Exception:
        missing.append("qrcode")
    try:
        import PIL  # noqa: F401
    except Exception:
        missing.append("Pillow")
    if missing:
        return False, (f"missing: {', '.join(missing)} — run: "
                       f"pip install {' '.join(p.lower() for p in missing)}")
    return True, "qrcode + Pillow OK"


def _probe_zebra():
    devs = glob.glob('/dev/usb/lp*')
    if devs:
        return True, devs[0], f"USB label printer at {devs[0]}"
    try:
        import usb.core  # type: ignore[import-not-found]
        if usb.core.find(idVendor=0x0a5f) is not None:
            return True, "Zebra (USB)", "Zebra printer via PyUSB"
        return False, "Zebra (USB)", "No Zebra USB device found"
    except Exception:
        return False, "Zebra (USB)", "No /dev/usb/lp* device and PyUSB unavailable"


def _probe_generic(name):
    if sys.platform.startswith("win"):
        return _probe_windows(name)
    return _probe_cups(name)


def _probe_windows(name):
    try:
        import win32print  # type: ignore[import-not-found]
    except Exception:
        # Can't enumerate; the ShellExecute fallback may still print to default.
        return True, (name or "System default"), \
            "pywin32 not installed — cannot verify, will try default printer"
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = ""
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        installed = [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:
        installed = []
    target = name or default
    if not target:
        return False, "System default", "No default printer configured in Windows"
    if name and installed and name not in installed:
        return False, name, f"'{name}' not found among installed printers"
    return True, target, ("installed (default)" if not name else "installed")


def _probe_cups(name):
    if shutil.which("lp") is None:
        return False, (name or "System default"), "'lp' / CUPS not installed"

    printers = []
    try:
        out = subprocess.run(["lpstat", "-p"], capture_output=True, text=True, timeout=5)
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "printer":
                printers.append(parts[1])
    except Exception:
        pass

    default = ""
    try:
        d = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=5)
        line = (d.stdout or "").strip()
        if ":" in line:                        # "system default destination: <name>"
            default = line.split(":", 1)[1].strip()
    except Exception:
        pass

    if name:
        if printers and name not in printers:
            return False, name, f"'{name}' not in CUPS ({', '.join(printers) or 'none'})"
        return True, name, "configured in CUPS"
    if default:
        return True, default, "system default (CUPS)"
    if printers:
        return False, "System default", f"no default set; available: {', '.join(printers)}"
    return False, "System default", "no printers configured in CUPS"
