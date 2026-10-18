"""Package launcher: python3 tools/obsdeploy <command>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from obsdeploy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
