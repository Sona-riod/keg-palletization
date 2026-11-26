"""
VisionCore colour palette + global Qt stylesheet.

The palette is copied from visioncore_dashboard.py (the demo the client
approved): a light industrial theme with a near-white background, white cards,
deep-navy header/sidebar, blue accent and green/amber/red status colours.

A dark palette with the same keys is used instead when the operator picks the
dark theme (sidebar button). The choice is read once here at start-up, because
widgets bake colours into their styles when they are built; switching theme
restarts the app.

Usage:
    from ui_pyside.theme import C, GLOBAL_QSS
"""

from datetime import datetime
from pathlib import Path

import config

# Bundled UI assets (logos, icons) live next to this package.
ASSETS_DIR = Path(__file__).parent / "assets"


def asset_path(filename: str) -> str:
    """Absolute path to a bundled asset (may not exist yet)."""
    return str(ASSETS_DIR / filename)

# Palette
# Same keys as visioncore_dashboard.py so widget code reads the same.
LIGHT = {
    "navy":        "#1E3A5F",   # dark panels / dialog headers (dashboard heading navy)
    "navy2":       "#274B78",
    "navy3":       "#3A5F8F",
    "slate":       "#64748B",   # muted label
    "slate2":      "#94A3B8",
    "slate3":      "#CBD5E1",
    "bg":          "#F4F7FB",   # app background
    "white":       "#FFFFFF",   # cards
    "border":      "#E1E8F2",
    "blue":        "#2D5A8F",   # primary accent / AUTO (Checkology brand navy)
    "blue_light":  "#3D6FA8",
    "blue_pale":   "#EAF2FB",
    "blue_border": "#C9D9EE",
    "green":       "#22C55E",   # success / online
    "green_dark":  "#16A34A",
    "amber":       "#F59E0B",   # MANUAL / warning
    "amber_dark":  "#D97706",
    "red":         "#EF4444",   # error / offline
    "red_dark":    "#DC2626",
    # Keg the cloud does not know yet: list row and preview box. Fuchsia so it
    # can't be mistaken for any other state (green/amber/red/blue).
    "unreg":       "#C026D3",

    # Off-white "light chrome" surfaces (top bar / sidebar / splash).
    # A cool blue-tinted off-white that pairs with the blue accent.
    "chrome":        "#F5F8FC",   # chrome surface (sidebar)
    "chrome2":       "#E6EEF8",   # hover / active on chrome
    "chrome_border": "#E1E8F2",   # separators / borders on chrome

    # Logo accent (pink), kept for optional use
    "pink":        "#E97DA0",
    "pink_dark":   "#D65B86",
    "pink_pale":   "#FBEEF3",

    # Checkology brand (Keg Management web app): sign-in and splash
    "ck_navy":      "#2D5A8F",   # dashboard tiles / brand badge
    "ck_navy_dark": "#1E3A5F",   # headings, logo wordmark
    "ck_blue_soft": "#7FA6DA",   # hero headline accent
    "ck_sky":       "#EAF2FB",   # light blue surfaces
    "ck_line":      "#DCE6F3",   # borders on brand surfaces
    "ck_pink":      "#EC6A7E",   # primary action (Login button)
    "ck_pink_dark": "#D9546A",
    "ck_pink_soft": "#F395A5",   # logo mark

    # convenience text aliases
    "text1":       "#1E293B",   # primary text
    "text2":       "#475569",   # secondary
    "text3":       "#94A3B8",   # disabled / placeholder

    # Colours used in only one or two styles below
    "title":              "#1E3A5F",   # dialog title text (navy in light)
    "plaque":             "#FFFFFF",   # logo plaque
    "plaque_tint_end":    "#E6EFFB",
    "grad_bg_start":      "#FFFFFF",   # sign-in / splash background gradient
    "danger_border":      "#F5C2C2",
    "login_exit_bg":      "#FDEEF1",
    "login_exit_border":  "#F8C9D1",
    "login_exit_hover":   "#FAD9DF",
    "login_exit_pressed": "#F6C3CC",
    "surface_alt":        "#F8FAFC",   # Exit chooser background
    "orange_text":        "#C2410C",   # printer warning text
}

# Dark theme: same keys. Surfaces go dark slate, text goes light, and the
# brand navy is lifted a little so it reads both as a fill under white text
# and as text on the dark surfaces. Status colours stay the same.
DARK = dict(LIGHT)
DARK.update({
    "slate":       "#94A3B8",
    "slate2":      "#7C8BA1",
    "slate3":      "#475569",
    "bg":          "#0F172A",
    "white":       "#1E293B",   # cards
    "border":      "#334155",
    "blue":        "#4F82C0",
    "blue_light":  "#6A98D0",
    "blue_pale":   "#1E3350",
    "blue_border": "#335079",
    "chrome":        "#162032",
    "chrome2":       "#24324A",
    "chrome_border": "#334155",
    "pink_pale":   "#3B1F2B",
    "unreg":       "#D946EF",
    "ck_navy":      "#4F82C0",
    "ck_navy_dark": "#E2E8F0",  # headings are light on dark
    "ck_sky":       "#1E3350",
    "ck_line":      "#334155",
    "text1":       "#E2E8F0",
    "text2":       "#CBD5E1",
    "text3":       "#64748B",
    "title":              "#E2E8F0",
    "plaque":             "#1E293B",
    "plaque_tint_end":    "#1E3350",
    "grad_bg_start":      "#0F172A",
    "danger_border":      "#7F1D1D",
    "login_exit_bg":      "#3B1F2B",
    "login_exit_border":  "#6B2E40",
    "login_exit_hover":   "#4A2433",
    "login_exit_pressed": "#5A2A3B",
    "surface_alt":        "#1E293B",
    "orange_text":        "#FB923C",
})

THEME = config.ui_theme()          # "light" or "dark", saved on this device
IS_DARK = THEME == "dark"
C = dict(DARK if IS_DARK else LIGHT)


def now_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_date() -> str:
    """Today, e.g. "07 Oct 2026", with the month in the screen language."""
    from PySide6.QtCore import QDate
    from .i18n import qlocale
    return qlocale().toString(QDate.currentDate(), "dd MMM yyyy")


# Reusable button gradients (top to bottom)
GRAD_BLUE        = C["blue"]          # flat brand navy, as on the dashboard
GRAD_BLUE_HOVER  = "#244B78"
GRAD_GREEN       = "#16A34A"
GRAD_GREEN_HOVER = "#15803D"
GRAD_PINK        = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #F07F91, stop:1 {C['ck_pink']})"
GRAD_PINK_HOVER  = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {C['ck_pink']}, stop:1 {C['ck_pink_dark']})"
# Navy to pink: the stripe on top of the Keg Management sign-in card.
GRAD_BRAND       = (f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C['ck_navy']}, "
                    f"stop:0.6 #8C7BB0, stop:1 {C['ck_pink_soft']})")
# White to light blue page background.
GRAD_BRAND_BG    = f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C['grad_bg_start']}, stop:1 {C['ck_sky']})"


# Global stylesheet
# Keyed on object names, so widgets opt in via setObjectName(...).
GLOBAL_QSS = f"""
* {{
    font-family: 'Segoe UI', 'Inter', 'Roboto', sans-serif;
    font-size: 13px;
    color: {C['text1']};
}}
QMainWindow, #appBg {{ background-color: {C['bg']}; }}
QWidget {{ background-color: transparent; }}

/* ── Top bar (white, like the dashboard header) ── */
#topBar {{
    background-color: {C['white']};
    border-bottom: 1px solid {C['chrome_border']};
}}
#brandName {{ color: {C['ck_navy_dark']}; font-size: 15px; font-weight: 800; letter-spacing: 1.5px; }}
#brandPlaque {{ background-color: {C['plaque']}; border-radius: 6px; }}
#brandPlaquePlain {{ background-color: transparent; }}
#brandBadge {{ background-color: {C['ck_navy']}; border-radius: 8px; }}
#brandPlaqueTint {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {C['plaque']}, stop:1 {C['plaque_tint_end']});
    border: 1px solid rgba(255,255,255,0.6); border-radius: 12px;
}}
#topMeta  {{ color: {C['slate']}; font-size: 12px; }}
#clockLbl {{ color: {C['ck_navy_dark']}; font-size: 14px; font-weight: 700; }}
#modeChip {{
    color: {C['slate']}; font-size: 11px; font-weight: 700;
    padding: 3px 10px; border-radius: 4px; background-color: {C['chrome2']};
}}

/* ── Sidebar (dashboard style: light, navy mode bar, plain items) ── */
#sidebar {{
    background-color: {C['chrome']};
    border-right: 1px solid {C['chrome_border']};
}}
#sideOperator {{ color: {C['ck_navy_dark']}; font-size: 13px; font-weight: 700; }}
#sideMode {{
    background-color: {C['ck_navy']}; color: white;
    font-size: 13px; font-weight: 600; padding: 0 12px; border-radius: 3px;
}}

/* ── Section header on cards ── */
#sectionHeader {{ background-color: {C['white']}; border-bottom: 1px solid {C['border']}; }}
#sectionTitle {{
    color: {C['ck_navy_dark']}; font-size: 12px; font-weight: 700;
    letter-spacing: 1.2px; text-transform: uppercase;
}}
#pageTitle {{ font-size: 20px; font-weight: 700; color: {C['ck_navy_dark']}; }}
#headerMeta {{ color: {C['slate']}; font-size: 12px; font-weight: 600; }}
#headerBtn {{
    background-color: {C['white']}; color: {C['ck_navy']};
    border: 1px solid {C['blue_border']}; border-radius: 6px;
    padding: 0 12px; font-size: 12px; font-weight: 600;
}}
#headerBtn:hover {{ background-color: {C['blue_pale']}; color: {C['blue']}; border-color: {C['blue_border']}; }}
#headerBtn:disabled {{ color: {C['text3']}; background-color: {C['bg']}; }}
#fieldLabel {{
    color: {C['text2']}; font-size: 11px; font-weight: 700; letter-spacing: 1px;
}}

/* ── Settings side panel (sections of the Settings page) ── */
#settingsNav {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 8px;
}}
#settingsNavBtn {{
    background-color: transparent; border: none; border-radius: 0;
    border-left: 3px solid transparent; text-align: left;
}}
#settingsNavBtn:hover {{ background-color: {C['chrome2']}; }}
#settingsNavBtn:checked {{
    background-color: {C['blue_pale']}; border-left: 3px solid {C['ck_navy']};
}}
#settingsNavHint {{ color: {C['slate']}; font-size: 11px; background: transparent; }}
#sectionTitleBig {{ color: {C['ck_navy_dark']}; font-size: 17px; font-weight: 700; }}
#sectionLead {{ color: {C['text2']}; font-size: 12px; }}

/* ── Cards & panes ── */
#card {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 8px;
}}
#darkCard {{
    background-color: {C['navy']}; border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px;
}}
#statusCard {{
    background-color: {C['blue_pale']}; border: 1px solid {C['blue_border']};
    border-radius: 8px;
}}

/* ── Camera ── */
#cameraFeed {{ background-color: {C['navy']}; }}
#camFooter {{
    background-color: {C['white']}; border-top: 1px solid {C['border']};
    min-height: 74px; max-height: 74px;
}}
/* Navy stat tiles, like the dashboard's TOTAL KEGS / IN BREWERY cards. */
#statTile {{ background-color: {C['ck_navy']}; border-radius: 8px; }}
#statTile #statLabel {{ color: rgba(255,255,255,0.85); letter-spacing: 1.2px; }}
#statTile #statValue {{ color: white; font-size: 22px; }}
#statLabel {{
    color: {C['slate']}; font-size: 11px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
}}
#statValue {{ font-size: 22px; font-weight: 700; color: {C['text1']}; }}

/* ── QR list rows ── */
#qrItem {{ background-color: {C['white']}; border-bottom: 1px solid {C['border']}; }}
#qrItem:hover {{ background-color: {C['blue_pale']}; }}
#qrCode  {{ color: {C['text1']}; font-size: 14px; font-weight: 600; }}
#qrIndex {{ color: {C['slate']}; font-size: 12px; }}
#emptyLabel {{ color: {C['slate']}; font-size: 13px; font-style: italic; }}

/* ── Buttons ── */
QPushButton {{
    border: none; border-radius: 6px; padding: 8px 16px;
    font-weight: 600; font-size: 13px;
}}
#primaryBtn {{ background: {GRAD_BLUE}; color: white; }}
#primaryBtn:hover {{ background: {GRAD_BLUE_HOVER}; }}
#ghostBtn {{
    background-color: {C['white']}; color: {C['slate']};
    border: 1px solid {C['border']};
}}
#ghostBtn:hover {{ background-color: {C['blue_pale']}; color: {C['blue']}; border-color: {C['blue_border']}; }}
#dangerBtn {{
    background-color: {C['white']}; color: {C['red_dark']};
    border: 1px solid {C['danger_border']};
}}
#dangerBtn:hover {{ background-color: {C['red']}; color: white; border-color: {C['red']}; }}
#exitBtn {{
    background-color: {C['red']}; color: white;
    font-size: 13px; font-weight: 600;
    border-radius: 6px; padding: 0 14px;
}}
#exitBtn:hover {{ background-color: {C['red_dark']}; }}
#refreshBtn {{
    background: {GRAD_BLUE}; color: white;
    font-size: 13px; font-weight: 600;
    border-radius: 6px; padding: 0 14px;
}}
#refreshBtn:hover {{ background: {GRAD_BLUE_HOVER}; }}
#captureBtn {{
    background: {GRAD_BLUE}; color: white; font-size: 14px;
    font-weight: 700; letter-spacing: 0.5px;
    border-radius: 6px; min-height: 44px; max-height: 44px; padding: 0 16px;
}}
#captureBtn:hover {{ background: {GRAD_BLUE_HOVER}; }}
#captureBtn:disabled {{ background: {C['border']}; color: {C['text3']}; }}
#sendBtn {{
    background: {GRAD_GREEN}; color: white; font-size: 14px;
    font-weight: 700; letter-spacing: 0.5px;
    border-radius: 6px; min-height: 44px; max-height: 44px; padding: 0 16px;
}}
#sendBtn:hover {{ background: {GRAD_GREEN_HOVER}; }}
#sendBtn:disabled {{ background: {C['border']}; color: {C['text3']}; }}

/* Segmented two-way toggle (selected = blue gradient): AUTO / MANUAL on the
   operator page, ENABLED / DISABLED for the beer type in Settings. */
#segAuto, #segLeftOn {{ background: {GRAD_BLUE}; color: white; min-height: 40px;
            max-height: 40px; font-size: 13px; font-weight: 700; padding: 0 12px;
            border-top-right-radius: 0; border-bottom-right-radius: 0; }}
#segAutoOff, #segLeftOff {{ background-color: {C['blue_pale']}; color: {C['slate']};
               border: 1px solid {C['border']}; min-height: 40px;
               max-height: 40px; font-size: 13px; font-weight: 700; padding: 0 12px;
               border-top-right-radius: 0; border-bottom-right-radius: 0; }}
#segManual, #segRightOn {{ background: {GRAD_BLUE}; color: white; min-height: 40px;
              max-height: 40px; font-size: 13px; font-weight: 700; padding: 0 12px;
              border-top-left-radius: 0; border-bottom-left-radius: 0; }}
#segManualOff, #segRightOff {{ background-color: {C['blue_pale']}; color: {C['slate']};
                 border: 1px solid {C['border']}; min-height: 40px;
                 max-height: 40px; font-size: 13px; font-weight: 700; padding: 0 12px;
                 border-top-left-radius: 0; border-bottom-left-radius: 0; }}

/* ── Inputs ── */
QLineEdit, QComboBox {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 6px; padding: 8px 10px; color: {C['text1']};
    font-size: 13px; min-height: 20px;   /* 20 + 2×8 padding + 2 border = 38 = CONTROL_H */
}}
QLineEdit:focus, QComboBox:focus {{ border: 2px solid {C['blue']}; padding: 7px 9px; }}
QLineEdit[invalid="true"] {{ border: 2px solid {C['red']}; padding: 7px 9px; }}
QLineEdit:read-only {{ background-color: {C['bg']}; color: {C['text2']}; }}
QSpinBox {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 6px; padding: 0 8px; color: {C['text1']}; font-size: 13px;
}}
QSpinBox:focus {{ border: 2px solid {C['blue']}; padding: 0 7px; }}
/* combobox-popup: 0 opens the list as a plain drop-down under the field. The
   Fusion default centres the list on the current item, over the field, with
   scroll strips above and below it that show as black bars in the frameless
   dialogs. */
QComboBox {{ combobox-popup: 0; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background-color: {C['white']}; color: {C['text1']};
    border: 1px solid {C['border']}; outline: 0; padding: 2px;
    selection-background-color: {C['blue_pale']}; selection-color: {C['blue']};
}}
QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 0 8px; }}
/* ── Target-count field (dropdown w/ painted chevron) ── */
QComboBox#countCombo {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 6px; color: {C['text1']};
    font-size: 13px; font-weight: 600; min-height: 30px; padding: 2px 8px;
}}
QComboBox#countCombo:focus {{ border: 2px solid {C['blue']}; }}
QComboBox#countCombo::drop-down {{
    width: 24px; border-left: 1px solid {C['border']};
    border-top-right-radius: 6px; border-bottom-right-radius: 6px;
    background-color: {C['blue_pale']};
}}
QComboBox#countCombo::drop-down:hover {{
    background-color: {C['blue_border']};
}}
QComboBox#countCombo QAbstractItemView {{
    background-color: {C['white']}; color: {C['text1']};
    border: 1px solid {C['border']}; outline: none;
    selection-background-color: {C['blue_pale']}; selection-color: {C['blue']};
    font-size: 13px; font-weight: 600;
}}

/* ── Confirmation dialog (themed card) ── */
#confirmRoot {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 10px;
}}
#confirmHeader {{
    background-color: {C['navy']};
    border-top-left-radius: 10px; border-top-right-radius: 10px;
    min-height: 48px; max-height: 48px;
}}
#confirmDangerHeader {{
    background-color: {C['red']};
    border-top-left-radius: 10px; border-top-right-radius: 10px;
    min-height: 48px; max-height: 48px;
}}
#confirmTitle {{
    color: #FFFFFF; font-size: 14px; font-weight: 700;
    letter-spacing: 1px; text-transform: uppercase;
}}
#confirmMsg {{ color: {C['text1']}; font-size: 15px; font-weight: 700; }}
#confirmDetail {{ color: {C['slate']}; font-size: 12px; }}
#confirmCancel {{
    background-color: {C['white']}; color: {C['slate']};
    border: 1px solid {C['border']}; border-radius: 6px;
    min-height: 38px; padding: 0 20px; font-size: 12px; font-weight: 700; letter-spacing: 0.5px;
}}
#confirmCancel:hover {{ background-color: {C['bg']}; color: {C['title']}; }}
#confirmOk {{
    background: {GRAD_BLUE}; color: white; border: none; border-radius: 6px;
    min-height: 38px; padding: 0 22px; font-size: 12px; font-weight: 700; letter-spacing: 0.5px;
}}
#confirmOk:hover {{ background: {GRAD_BLUE_HOVER}; }}
#confirmDanger {{
    background-color: {C['red']}; color: white; border: none; border-radius: 6px;
    min-height: 38px; padding: 0 22px; font-size: 12px; font-weight: 700; letter-spacing: 0.5px;
}}
#confirmDanger:hover {{ background-color: {C['red_dark']}; }}

/* ── Operator sign-in page — Checkology brand, after the Keg Management web
   sign-in: light hero with the logo badge left, white card right ── */
#loginHero {{ background-color: {GRAD_BRAND_BG}; }}
#heroTitle {{ color: {C['ck_blue_soft']}; font-size: 44px; font-weight: 800; }}
#heroSub   {{ color: {C['ck_navy']}; font-size: 12px; font-weight: 600; letter-spacing: 3px; }}
#heroRule  {{ background-color: {C['ck_pink']}; }}
#loginExit {{
    background-color: {C['login_exit_bg']}; color: {C['ck_pink_dark']};
    border: 1px solid {C['login_exit_border']}; border-radius: 8px;
    padding: 0 16px 0 12px; font-size: 13px; font-weight: 700;
}}
#loginExit:hover   {{ background-color: {C['login_exit_hover']}; border-color: {C['ck_pink']}; }}
#loginExit:pressed {{ background-color: {C['login_exit_pressed']}; }}
#loginSide {{ background-color: {C['white']}; border-left: 1px solid {C['ck_line']}; }}
#loginCard {{
    background-color: {C['white']}; border: 1px solid {C['ck_line']};
    border-radius: 14px;
}}
#loginStripe {{
    background: {GRAD_BRAND}; border: none;
    border-top-left-radius: 14px; border-top-right-radius: 14px;
}}
#loginTitle {{ color: {C['ck_navy_dark']}; font-size: 22px; font-weight: 700; }}
#loginSub   {{ color: {C['slate']}; font-size: 13px; }}
/* Standard brand-navy action, as on the Keg Management dashboard. */
#loginCard QPushButton#primaryBtn {{
    background: {C['ck_navy']}; color: white; border-radius: 8px;
    font-size: 13px; letter-spacing: 0.5px;
}}
#loginCard QPushButton#primaryBtn:hover {{ background: {GRAD_BLUE_HOVER}; }}
#loginCard QPushButton#primaryBtn:disabled {{ background: {C['blue_border']}; color: white; }}
#batchWho {{
    background-color: {C['ck_sky']}; border: 1px solid {C['ck_line']}; border-radius: 8px;
}}
#batchWhoName {{ color: {C['ck_navy_dark']}; font-size: 14px; font-weight: 700; }}
#loginCard #segLeftOn, #loginCard #segRightOn {{ background: {C['ck_navy']}; color: white; }}
#loginCard #segLeftOff, #loginCard #segRightOff {{
    background-color: {C['ck_sky']}; color: {C['ck_navy']}; border: 1px solid {C['ck_line']};
}}
#loginCard #ghostBtn {{ border: 1px solid {C['ck_line']}; color: {C['ck_navy_dark']}; }}
#loginCard #ghostBtn:hover, #loginCard #headerBtn:hover {{
    background-color: {C['ck_sky']}; color: {C['ck_navy']}; border-color: {C['ck_navy']};
}}
#loginCard #ghostBtn:pressed {{ background-color: {C['ck_navy']}; color: white; }}
#loginCard QLineEdit:focus, #loginCard QComboBox:focus {{ border: 2px solid {C['ck_navy']}; }}
#loginCard #fieldLabel {{ color: {C['ck_navy_dark']}; }}

/* ── Exit / Shut Down chooser (compact message box) ── */
#exitRoot {{
    background-color: {C['surface_alt']}; border: 1px solid {C['slate3']};
    border-radius: 10px;
}}
#exitTitleBar {{
    background-color: {C['chrome2']}; border-bottom: 1px solid {C['chrome_border']};
    border-top-left-radius: 10px; border-top-right-radius: 10px;
}}
#exitTitle {{ color: {C['title']}; font-size: 13px; font-weight: 700; }}
#exitClose {{
    background-color: transparent; color: {C['slate']}; border-radius: 13px;
    padding: 0; font-size: 18px; font-weight: 700;
}}
#exitClose:hover {{ background-color: {C['slate3']}; color: {C['title']}; }}
#exitIcon {{
    background-color: {C['blue']}; color: white; border-radius: 20px;
    font-size: 22px; font-weight: 800;
}}
#exitMsg {{ color: {C['text1']}; font-size: 14px; font-weight: 700; }}
#exitDetail {{ color: {C['text2']}; font-size: 12px; }}
#exitChoiceBtn {{
    background-color: #2B4A6F; color: white; border-radius: 6px;
    padding: 0 10px; font-size: 13px; font-weight: 700;
}}
#exitChoiceBtn:hover {{ background-color: {C['navy2']}; }}
#exitChoiceBtn:pressed {{ background-color: {C['navy']}; }}
#exitCancelBtn {{
    background-color: {C['white']}; color: {C['ck_navy_dark']};
    border: 1px solid {C['blue_border']}; border-radius: 6px;
    padding: 0 10px; font-size: 13px; font-weight: 600;
}}
#exitCancelBtn:hover {{ background-color: {C['blue_pale']}; }}
#exitDangerBtn {{
    background-color: {C['red']}; color: white; border-radius: 6px;
    padding: 0 10px; font-size: 13px; font-weight: 600;
}}
#exitDangerBtn:hover {{ background-color: {C['red_dark']}; }}

/* ── Page header: Back button + title ── */
#backBtn {{
    background-color: {C['white']}; color: {C['ck_navy']};
    border: 1px solid {C['blue_border']}; border-radius: 6px;
    padding: 0 12px 0 8px; font-size: 13px; font-weight: 600;
}}
#backBtn:hover {{ background-color: {C['blue_pale']}; }}

/* ── Application tour card ── */
#tourCard {{
    background-color: {C['white']}; border: 1px solid {C['blue_border']};
    border-radius: 10px;
}}
#tourStep  {{ color: {C['slate']}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
#tourTitle {{ color: {C['ck_navy_dark']}; font-size: 16px; font-weight: 700; }}
#tourText  {{ color: {C['text1']}; font-size: 13px; }}
#tourSkip {{
    background-color: transparent; color: {C['slate']};
    padding: 0 4px; font-size: 13px; font-weight: 600;
}}
#tourSkip:hover {{ color: {C['ck_navy']}; }}

/* ── Language picker ── */
#langTile, #langTileOn {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 8px; padding: 0; text-align: left;
}}
#langTile:hover {{ background-color: {C['blue_pale']}; border-color: {C['blue_border']}; }}
#langTile:disabled {{ background-color: {C['bg']}; }}
#langTileOn {{ background-color: {C['blue_pale']}; border: 2px solid {C['blue']}; }}
#langNative  {{ color: {C['text1']}; font-size: 14px; font-weight: 700; background: transparent; }}
#langEnglish {{ color: {C['slate']}; font-size: 11px; background: transparent; }}
#langNativeOff {{ color: {C['text3']}; font-size: 14px; font-weight: 700; background: transparent; }}

/* ── Feedback pop-up (small card, top-right) ── */
#toastCard {{ background-color: {C['white']}; border-radius: 8px; }}
#toastText {{ color: {C['text1']}; font-size: 13px; font-weight: 600; }}

/* ── Tables ── */
QTableWidget {{
    background-color: {C['white']}; border: 1px solid {C['border']};
    border-radius: 8px; gridline-color: {C['border']};
    selection-background-color: {C['blue_pale']}; selection-color: {C['text1']};
}}
/* Header and cell text start 12px in, on one line per column (the item
   delegate adds ~3px of its own, hence 9px here). See widgets.make_table. */
QTableWidget::item {{ padding: 0 9px; border-bottom: 1px solid {C['border']}; }}
QHeaderView::section {{
    background-color: {C['blue_pale']}; color: {C['ck_navy_dark']};
    padding: 10px 12px; border: none; border-bottom: 1px solid {C['blue_border']};
    font-size: 12px; font-weight: 700; text-transform: uppercase;
}}
QTableWidget {{ font-size: 13px; }}

/* ── Recent activity (Settings) ── */
#activityRow {{ background-color: transparent; border-bottom: 1px solid {C['border']}; }}
#activityTime {{ color: {C['slate']}; font-size: 12px; font-weight: 600; }}
#activityText {{ color: {C['text1']}; font-size: 13px; }}

/* ── Banners ── */
/* Alerts: one colour per severity, a thick bar on the left so the kind reads
   from across the line, and 14 px text for the HMI. Info = blue, warning =
   amber, repeat keg = amber/orange, printer = orange. */
#warnBanner {{ background-color: rgba(245,158,11,0.14); border-left: 6px solid {C['amber']};
               border-bottom: 1px solid rgba(245,158,11,0.45); }}
#warnText   {{ color: {C['text1']}; font-size: 14px; font-weight: 700; }}
#infoBanner {{ background-color: rgba(37,99,235,0.08); border-left: 6px solid {C['blue']};
               border-bottom: 1px solid {C['blue_border']}; }}
#infoText   {{ color: {C['blue']}; font-size: 14px; font-weight: 600; }}
#dupBanner  {{ background-color: rgba(245,158,11,0.16); border-left: 6px solid {C['amber_dark']};
               border-bottom: 1px solid {C['amber']}; }}
#dupText    {{ color: {C['amber_dark']}; font-size: 14px; font-weight: 700; }}
#dupBtn {{
    background-color: {C['white']}; color: {C['amber_dark']};
    border: 1px solid {C['amber']}; border-radius: 8px;
    padding: 0 14px; font-size: 13px; font-weight: 700; min-height: 32px;
}}
#dupBtn:hover {{ background-color: {C['amber']}; color: white; }}
#printerWarnBanner {{
    background-color: rgba(234,88,12,0.10); border-left: 6px solid #EA580C;
    border-bottom: 1px solid rgba(234,88,12,0.50);
}}
#printerWarnText {{ color: {C['orange_text']}; font-size: 14px; font-weight: 700; }}

/* ── Splash — Checkology brand (logo badge, navy → pink progress) ── */
#splash {{ background-color: {GRAD_BRAND_BG}; }}
#splashTitle {{ color: {C['ck_navy_dark']}; font-size: 30px; font-weight: 800; letter-spacing: 1px; }}
#splashSub   {{ color: {C['slate']}; font-size: 13px; }}
#splashTag   {{ color: {C['ck_navy']}; font-size: 11px; font-weight: 600; letter-spacing: 3px; }}
#splashStatus {{ color: {C['ck_navy']}; font-size: 13px; font-weight: 600; }}
#splashPct {{ color: {C['ck_navy_dark']}; font-size: 22px; font-weight: 800; letter-spacing: 1px; }}
QProgressBar {{
    background-color: {C['chrome2']}; border: none; border-radius: 4px;
    height: 6px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background-color: {C['blue']}; border-radius: 4px; }}
#splash QProgressBar {{ background-color: {C['ck_line']}; border-radius: 4px; }}
#splash QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {C['ck_navy']}, stop:1 {C['ck_pink']});
    border-radius: 4px;
}}
#splash #primaryBtn {{ background: {GRAD_PINK}; color: white; border-radius: 8px; }}
#splash #primaryBtn:hover {{ background: {GRAD_PINK_HOVER}; }}

/* ── Scrollbars ── */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 7px; border-radius: 3px; }}
QScrollBar::handle:vertical {{ background: {C['slate3']}; border-radius: 3px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {C['slate']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar:horizontal {{ height: 0px; }}

/* ── Sliders ── */
QSlider::groove:horizontal {{ height: 6px; background: {C['border']}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {C['blue']}; width: 16px; height: 16px; margin: -6px 0;
    border-radius: 8px; border: 2px solid white;
}}
QSlider::sub-page:horizontal {{ background: {C['blue']}; border-radius: 3px; }}

/* ── Tooltips ── */
QToolTip {{
    background: {C['navy2']}; color: #F1F5F9;
    border: 1px solid rgba(255,255,255,0.08); border-radius: 4px;
    padding: 4px 8px; font-size: 11px;
}}

/* ── Dialogs ── */
QDialog {{ background-color: {C['white']}; }}
"""
