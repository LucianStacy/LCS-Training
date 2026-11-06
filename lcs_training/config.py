"""Paths and settings. Client data lives only in the local database, never in the repository."""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
DATA_DIR = Path(os.environ.get("LCS_DATA_DIR", ROOT_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "lcs_training.db"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("LCS_HOST", "127.0.0.1")
PORT = int(os.environ.get("LCS_PORT", "8765"))

COMPANY = {
    "name": "LCS-Training",
    "website": "lcs-training.com",
    "address": "75 West Nuevo Rd. #708",
    "city": "Perris, CA 92571",
    "phones": ["800-252-2819", "951-514-2551"],
}
FOOTER_LINE = "lcs-training.com  |  75 West Nuevo Rd. #708  |  Perris, CA 92571  |  800-252-2819  |  951-514-2551"

BRAND = {
    "purple": "74439A", "magenta": "E9166E", "blush": "E3A1C5", "sky": "44A6DB", "deep_blue": "3272B8",
    "teal": "77C5C0", "green": "38B54A", "lime": "ACD037", "charcoal": "2D2D2D", "gray": "6E6E6E",
    "light_gray": "F4F4F6",
}
