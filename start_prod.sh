#!/bin/bash
# =============================================================================
#  PRODUCTION launcher — Keg Palletization HMI (Advantech iCam-540 / Jetson)
# =============================================================================
# This is the UNATTENDED, kiosk version of Start_palletization.sh. It is meant to
# be started automatically by the systemd user service (deploy/keg-palletization
# .service) after the desktop session comes up on power-on.
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1

LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/app.log"

# Rotate the log if it has grown past 10 MB (keep one previous copy).
if [[ -f "$LOG" && $(stat -c%s "$LOG" 2>/dev/null || echo 0) -gt 10485760 ]]; then
    mv -f "$LOG" "$LOG.1"
fi

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

log "=============================================================="
log "Palletization PRODUCTION launch"
log "APP_DIR=$APP_DIR  USER=$(whoami)  DISPLAY=${DISPLAY:-<none>}"

# --- Kiosk: keep the screen awake (needs a display) -------------------------
if [[ -n "${DISPLAY:-}" ]] && command -v xset >/dev/null 2>&1; then
    xset s off     2>/dev/null || true
    xset -dpms     2>/dev/null || true
    xset s noblank 2>/dev/null || true
    log "Screen blanking / DPMS disabled"
fi

# --- No waiting here: the app window opens at once --------------------------
# The wait for the camera's REST daemon (up to 30 s) and the 10 s sensor
# warm-up now run inside the app, behind its splash screen
# (modules/camera_init.py wait_for_daemon, config CAMERA_BOOT_WAIT_S /
# CAMERA_BOOT_WARMUP_S), so the operator sees the app instead of the desktop.

# --- Find the venv Python binary path (matching Start_palletization.sh logic) ---
# We invoke the venv Python binary directly rather than using 'source'.
if [[ -d "venv" ]]; then
    PY_CMD="venv/bin/python"
    log "[INFO] Found virtual environment: venv"
elif [[ -d "../venv" ]]; then
    PY_CMD="../venv/bin/python"
    log "[INFO] Found virtual environment: ../venv"
elif [[ -d ".venv" ]]; then
    PY_CMD=".venv/bin/python"
    log "[INFO] Found virtual environment: .venv"
elif command -v python3 >/dev/null 2>&1; then
    PY_CMD="python3"
    log "WARNING: no venv found — falling back to system python3"
else
    log "ERROR: no python interpreter available"
    exit 1
fi
log "Python: $($PY_CMD --version 2>&1)"

# ============================================================================
# SYSTEMD ENVIRONMENT FIX: Inject Jetson GPU paths and User-Site YOLO packages
# ============================================================================
# systemd services strip environment variables. We must explicitly define the 
# CUDA paths and force Python to load the icam-540 user's local YOLO packages.
export PYTHONUSERBASE=/home/icam-540/.local
export PYTHONNOUSERSITE=0
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}

# Importing torch takes several seconds on the Jetson and the app imports it
# again anyway, so the preflight only runs when asked: KEG_PREFLIGHT=1.
if [[ "${KEG_PREFLIGHT:-0}" == "1" ]]; then
    log "Running Preflight check for Systemd environment..."
    $PY_CMD -c "import torch, ultralytics; print('[PREFLIGHT] torch', torch.__version__, '| CUDA', torch.cuda.is_available(), '| ultralytics', ultralytics.__version__)" >>"$LOG" 2>&1
fi
# ============================================================================

# --- Launch the app in kiosk mode -------------------------------------------
export KEG_KIOSK=1
log "Launching main.py (KEG_KIOSK=1) ..."

$PY_CMD main.py >>"$LOG" 2>&1
CODE=$?

log "main.py exited with code $CODE (systemd will restart it if Restart=on-failure is set)"
exit $CODE
