"""
Build the TensorRT engine for live QR detection as FP16 (half precision).

Run ON THE CAMERA (an engine only works on the GPU and JetPack it was built
on), from palletization_final, with the app stopped:

    python3 tools/build_trt_engine.py            # FP16 (default)
    python3 tools/build_trt_engine.py --fp32     # back to full precision

config.GPU_CONFIG['half_precision'] does not affect an engine: its precision
is fixed here, when it is built. The engine the app used so far is kept as
best.engine.bak. If the new one fails the app's start-up test, the app uses
best.pt by itself (see QRDetector._load_yolo), so nothing stops working.

Check after the next start, in logs/app.log:
  * "TensorRT engine loaded and warmed up"           - the new engine is used
  * the [TIMING] lines: rounds/s up, YOLO ms down     - it is faster
  * the read / unread counts per 30 rounds unchanged  - it reads as well
If the reads get worse: restore best.engine.bak over best.engine.
"""
import argparse
import os
import shutil
import sys

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, APP)

import config


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--fp32", action="store_true", help="build full precision instead")
    args = ap.parse_args()

    from ultralytics import YOLO

    pt = str(config.QR_MODEL_PATH)
    engine = str(config.QR_ENGINE_PATH)
    if not os.path.exists(pt):
        sys.exit(f"Model not found: {pt}")
    if os.path.exists(engine):
        shutil.copy2(engine, engine + ".bak")
        print(f"Kept the current engine as {engine}.bak")

    half = not args.fp32
    print(f"Building {'FP16' if half else 'FP32'} engine, imgsz={config.YOLO_IMGSZ} "
          f"(a few minutes)...")
    built = YOLO(pt).export(format="engine", imgsz=config.YOLO_IMGSZ, half=half,
                            device=config.GPU_CONFIG.get("device", 0))
    if os.path.abspath(str(built)) != os.path.abspath(engine):
        shutil.move(str(built), engine)
    print(f"Done: {engine}. Start the app and compare the [TIMING] lines.")


if __name__ == "__main__":
    main()
