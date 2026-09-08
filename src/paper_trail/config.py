from pathlib import Path

# Project Root: paper_trail/src/paper_trail/config.py -> paper_trail/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DISCOVERED_FILE = PROJECT_ROOT / "discovered_urls.txt"
COMPLETED_FILE = PROJECT_ROOT / "completed_urls.txt"
DOWNLOAD_DIR = PROJECT_ROOT / "downloaded_pdfs"
KBART_CACHE_DIR = PROJECT_ROOT / "kbart_data"
CACHE_INDEX = KBART_CACHE_DIR / ".cache_index.json"
ORGANIZER_LOG = PROJECT_ROOT / "logs" / "organizer.log"
DOWNLOADER_LOG = PROJECT_ROOT / "logs" / "downloader.log"
