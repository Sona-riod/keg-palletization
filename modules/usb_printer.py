# modules/usb_printer.py
"""
Find the USB printer that is plugged in and the label language it speaks.

Every USB printer reports an IEEE 1284 Device ID, for example
    MFG:Zebra Technologies;CMD:ZPL;MDL:ZD421;
CMD lists the languages it understands. The label is built in the best of them:

    ZPL  >  TSPL  >  ESC/POS  >  system (PCL, PostScript, URF, PWG, PDF)

so a label printer wins over an office printer when both are connected. Only
when CMD is empty is the language taken from the vendor (Zebra = ZPL, TSC =
TSPL). Anything else (EPL only, no CMD from another vendor) is refused with a
reason: raw data in a language the printer did not report looks like a good
print and nothing comes out.

Where the Device ID comes from:
  • Linux usblp driver: /sys/class/usbmisc/lpN/device/ieee1284_id, and the
    printer is written through /dev/usb/lpN;
  • otherwise PyUSB: the printer-class request GET_DEVICE_ID on every
    interface of class 7.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

SYSFS_USBMISC = "/sys/class/usbmisc"
DEV_USB = "/dev/usb"

VENDOR_ZEBRA = 0x0A5F
VENDOR_TSC = 0x1203

USB_CLASS_PRINTER = 7
GET_DEVICE_ID = 0                 # printer-class request number
GET_DEVICE_ID_TYPE = 0xA1         # device-to-host, class, interface

ZPL = "ZPL"
TSPL = "TSPL"
ESCPOS = "ESC/POS"
SYSTEM = "system"                 # office printer: through CUPS / the OS

# Best first
LANGUAGE_ORDER = (ZPL, TSPL, ESCPOS, SYSTEM)

_CMD_LANGUAGE = {
    "ZPL": ZPL, "ZPLII": ZPL,
    "TSPL": TSPL, "TSPL2": TSPL, "TSC": TSPL,
    "ESC/POS": ESCPOS, "ESCPOS": ESCPOS,
    "POSTSCRIPT": SYSTEM, "PS": SYSTEM, "URF": SYSTEM, "PDF": SYSTEM,
    "PCLM": SYSTEM,
}
_SYSTEM_PREFIXES = ("PCL", "PWG")

_VENDOR_LANGUAGE = {VENDOR_ZEBRA: ZPL, VENDOR_TSC: TSPL}

# Printers the label can go to, shown to the operator: (group, models).
SUPPORTED_GROUPS = (
    ("Zebra label printers (ZPL)",
     "ZD220, ZD230, ZD411, ZD421, ZD621, ZT230, ZT411, ZT610, GK/GX/GC420 with ZPL"),
    ("TSC label printers (TSPL)", "TE200, TE210, TE244, TTP-244 Pro, DA210, TX200"),
    ("Other TSPL label printers", "Xprinter, Munbyn, Gprinter — only if they report TSPL"),
    ("Receipt printers (ESC/POS)", "Epson TM-T20, TM-T88 — prints on receipt paper"),
)
NOT_SUPPORTED_PRINTERS = ("EPL-only Zebra (LP2844, TLP2844), Brother, Datamax, Sato, "
                          "Dymo. Office printers need CUPS on the camera.")

# The same, as one line for a message.
SUPPORTED_PRINTERS = ("Supported: " + "; ".join(f"{g} {m}" for g, m in SUPPORTED_GROUPS)
                      + ". Not supported: " + NOT_SUPPORTED_PRINTERS)

# Why a printer cannot be used (UsbPrinter.problem)
NOT_SUPPORTED = "not_supported"   # language not supported / not reported
BUSY = "busy"                     # held by another program
PERMISSION = "permission"         # the app may not open it
NO_ANSWER = "no_answer"           # did not answer GET_DEVICE_ID


@dataclass
class UsbPrinter:
    manufacturer: str = ""
    model: str = ""
    vendor_id: Optional[int] = None
    commands: List[str] = field(default_factory=list)   # CMD as reported
    language: Optional[str] = None                      # None = refused
    reason: str = ""                                    # why it was refused
    problem: str = ""                                   # NOT_SUPPORTED, BUSY, ...
    device_path: Optional[str] = None                   # /dev/usb/lpN
    usb_device: object = None                           # PyUSB device
    interface: tuple = (0, 0)                           # PyUSB (number, alt setting)
    source: str = ""                                    # "sysfs" | "pyusb"

    @property
    def usable(self):
        return self.language is not None

    @property
    def name(self):
        mfg, mdl = self.manufacturer.strip(), self.model.strip()
        if mfg and mdl.lower().startswith(mfg.split()[0].lower()):
            return mdl
        return " ".join(p for p in (mfg, mdl) if p) or "USB printer"

    @property
    def where(self):
        return self.device_path or "USB (PyUSB)"


# Device ID
def parse_device_id(text):
    """'MFG:x;MDL:y;CMD:a,b;' -> (manufacturer, model, [commands])."""
    keys = {}
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        keys[key.strip().upper()] = value.strip()
    mfg = keys.get("MFG") or keys.get("MANUFACTURER") or ""
    mdl = keys.get("MDL") or keys.get("MODEL") or ""
    cmd = keys.get("CMD") or keys.get("COMMAND SET") or ""
    commands = [c.strip() for c in cmd.split(",") if c.strip()]
    return mfg, mdl, commands


def _language_of(command):
    c = command.strip().upper().replace(" ", "")
    if c in _CMD_LANGUAGE:
        return _CMD_LANGUAGE[c]
    if c.startswith(_SYSTEM_PREFIXES):
        return SYSTEM
    return None


def choose_language(commands, vendor_id=None):
    """-> (language, reason). language is None when the printer is refused."""
    if commands:
        found = {_language_of(c) for c in commands} - {None}
        for lang in LANGUAGE_ORDER:
            if lang in found:
                return lang, ""
        return None, (f"printer language {', '.join(commands)} is not supported "
                      f"(needs ZPL, TSPL, ESC/POS or an office printer)")
    lang = _VENDOR_LANGUAGE.get(vendor_id)
    if lang:
        return lang, ""
    return None, "printer did not report its language (no CMD in its Device ID)"


def _make(device_id, vendor_id, manufacturer="", product="", **where):
    mfg, mdl, commands = parse_device_id(device_id)
    language, reason = choose_language(commands, vendor_id)
    p = UsbPrinter(manufacturer=mfg or manufacturer, model=mdl or product,
                   vendor_id=vendor_id, commands=commands,
                   language=language, reason=reason, **where)
    if reason:
        p.reason = f"{p.name}: {reason}"
        p.problem = NOT_SUPPORTED
    return p


# Linux usblp (sysfs)
def _read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _sysfs_printers(sysfs_root, dev_root):
    try:
        names = sorted((n for n in os.listdir(sysfs_root) if n.startswith("lp")),
                       key=lambda n: int(n[2:]) if n[2:].isdigit() else 999)
    except OSError:
        return []
    printers = []
    for n in names:
        dev = os.path.join(sysfs_root, n, "device")
        usb = os.path.join(dev, "..")          # interface -> its USB device
        try:
            vendor = int(_read(os.path.join(usb, "idVendor")) or "x", 16)
        except ValueError:
            vendor = None
        printers.append(_make(_read(os.path.join(dev, "ieee1284_id")), vendor,
                              _read(os.path.join(usb, "manufacturer")),
                              _read(os.path.join(usb, "product")),
                              device_path=os.path.join(dev_root, n), source="sysfs"))
    return printers


# PyUSB
def _pyusb_printers():
    try:
        import usb.core  # type: ignore[import-not-found]
        import usb.util  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        devices = list(usb.core.find(find_all=True))
    except Exception as e:
        logger.debug(f"PyUSB scan failed: {e}")
        return []

    printers = []
    for dev in devices:
        try:
            answer = _get_device_id(dev)
            if answer is None:
                continue
            device_id, interface = answer
            printers.append(_make(device_id, dev.idVendor,
                                  _usb_string(dev, "iManufacturer"),
                                  _usb_string(dev, "iProduct"),
                                  usb_device=dev, interface=interface, source="pyusb"))
        except Exception as e:
            # A printer that does not answer is shown with the reason, not
            # skipped, so the operator does not see "No printer connected".
            printers.append(_unanswered(dev, e))
        finally:
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
    return printers


_warned = set()      # (device, error) already logged, so a wait does not fill the log


def _unanswered(dev, error):
    p = UsbPrinter(manufacturer=_usb_string(dev, "iManufacturer"),
                   model=_usb_string(dev, "iProduct"), vendor_id=dev.idVendor,
                   usb_device=dev, source="pyusb")
    code = getattr(error, "errno", None)
    text = str(error)
    if code == 16 or "busy" in text.lower():
        p.problem = BUSY
        p.reason = f"{p.name}: in use by another program ({text})"
    elif code == 13 or "access" in text.lower() or "permission" in text.lower():
        p.problem = PERMISSION
        p.reason = f"{p.name}: no permission to open it ({text})"
    else:
        p.problem = NO_ANSWER
        p.reason = f"{p.name}: does not answer ({text})"
    key = (dev.idVendor, getattr(dev, "idProduct", 0), text)
    if key not in _warned:
        _warned.add(key)
        logger.warning(f"USB printer {key[0]:04x}:{key[1]:04x} did not answer "
                       f"GET_DEVICE_ID: {text}")
    return p


def _get_device_id(dev):
    """(Device ID text, (interface, alt setting)) from the first printer
    interface that answers, or None if the device is not a printer. Raises the
    last error when none answers."""
    interfaces = [(ci, intf) for ci, cfg in enumerate(dev) for intf in cfg
                  if intf.bInterfaceClass == USB_CLASS_PRINTER]
    # Classic printer interfaces before IPP-over-USB (protocol 4), which an
    # office printer's ipp-usb service may be holding.
    interfaces.sort(key=lambda ci_intf: ci_intf[1].bInterfaceProtocol == 4)
    error = None
    for cfg_index, intf in interfaces:
        number, alt = intf.bInterfaceNumber, intf.bAlternateSetting
        try:
            data = bytes(dev.ctrl_transfer(GET_DEVICE_ID_TYPE, GET_DEVICE_ID,
                                           cfg_index, (number << 8) | alt, 1024))
        except Exception as e:
            error = e
            continue
        if len(data) < 2:
            return "", (number, alt)
        length = (data[0] << 8) | data[1]               # big-endian, includes itself
        return data[2:length].decode("utf-8", errors="replace"), (number, alt)
    if error is not None:
        raise error
    return None


def _usb_string(dev, attr):
    try:
        import usb.util  # type: ignore[import-not-found]
        index = getattr(dev, attr, 0)
        return (usb.util.get_string(dev, index) or "") if index else ""
    except Exception:
        return ""


# Choice
def find_printers(sysfs_root=None, dev_root=None, use_pyusb=True):
    """Every USB printer connected: the usblp devices, or PyUSB when there are none."""
    printers = _sysfs_printers(sysfs_root or SYSFS_USBMISC, dev_root or DEV_USB)
    if not printers and use_pyusb:
        printers = _pyusb_printers()
    return printers


def find_printer(**kwargs):
    """The printer a label goes to, or None when no USB printer is connected.

    The usable printer with the best language; when none is usable, the first
    refused one, so the caller can show why.
    """
    printers = find_printers(**kwargs)
    usable = [p for p in printers if p.usable]
    if usable:
        return min(usable, key=lambda p: LANGUAGE_ORDER.index(p.language))
    return printers[0] if printers else None


# Label data
def clean_pallet_id(pallet_id):
    """Drop characters that would break out of a ZPL / TSPL field."""
    return "".join(ch for ch in str(pallet_id)
                   if ch not in '"\\^~' and ord(ch) >= 32 and ord(ch) != 127)


def zpl_label(pallet_id):
    # Byte for byte the label ZebraPrinter has always sent.
    return ("^XA\n"
            f"^FO50,50^BQN,2,25^FDQA,{pallet_id}^FS\n"
            f"^FO50,1000^A0N,60,60^FDPallet: {pallet_id}^FS\n"
            "^XZ").encode("utf-8")


# Bytes a QR code holds at error correction Q, versions 1..10.
_QR_Q_BYTES = (11, 20, 32, 46, 60, 74, 86, 108, 130, 151)


def _qr_modules(data_len):
    """Width in squares of the QR for this many bytes (byte mode, level Q).
    Byte mode is the largest case, so the text never lands on the QR."""
    version = next((v for v, cap in enumerate(_QR_Q_BYTES, 1) if data_len <= cap), 10)
    return 17 + 4 * version


def tspl_label(pallet_id):
    # No SIZE / GAP: the printer keeps the media it was calibrated for.
    # Text at y=300, or just under the QR when a long pallet id makes it taller.
    text_y = max(300, 40 + _qr_modules(len(pallet_id.encode("utf-8"))) * 8 + 20)
    lines = ["CLS",
             f'QRCODE 40,40,Q,8,A,0,"{pallet_id}"',
             f'TEXT 40,{text_y},"3",0,1,1,"Pallet: {pallet_id}"',
             "PRINT 1,1"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def escpos_label(pallet_id):
    data = pallet_id.encode("utf-8")
    store = len(data) + 3
    qr = b"\x1d(k"                                       # GS ( k
    return b"".join([
        b"\x1b@",                                        # ESC @  initialise
        b"\x1ba\x01",                                    # ESC a 1  centre
        qr + b"\x04\x00\x31\x41\x32\x00",                # model 2
        qr + b"\x03\x00\x31\x43\x08",                    # module size 8
        qr + b"\x03\x00\x31\x45\x32",                    # error correction Q
        qr + bytes([store & 0xFF, store >> 8]) + b"\x31\x50\x30" + data,  # store
        qr + b"\x03\x00\x31\x51\x30",                    # print
        b"\n",
        b"Pallet: " + data + b"\n",
        b"\n\n\n\n",
        b"\x1dVB\x00",                                   # GS V 66 0  cut
    ])


_BUILDERS = {ZPL: zpl_label, TSPL: tspl_label, ESCPOS: escpos_label}


def label_data(language, pallet_id):
    """Raw label bytes for a ZPL / TSPL / ESC/POS printer."""
    return _BUILDERS[language](pallet_id)
