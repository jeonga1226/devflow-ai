from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data"
ATTACHMENTS_ROOT = DATA_ROOT / "attachments"
MAIL_DATA_ROOT = DATA_ROOT / "raw"
CHROMA_ROOT = DATA_ROOT / "chroma_db"
NOTION_CONFIG_PATH = PROJECT_ROOT / ".notion_config.json"