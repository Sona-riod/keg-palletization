# modules/__init__.py
# Detector modules (torch) are imported where they are used, not here.

__all__ = [
    'CameraManager',
    'DatabaseManager',
    'APISender',
    'submit_batch',
    'shutdown',
    'get_active_tasks',
]

# These modules are light enough to import here
from .camera import CameraManager
from .database import DatabaseManager
from .api_sender import APISender
from .process_worker import submit_batch, shutdown, get_active_tasks
