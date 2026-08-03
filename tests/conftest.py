import sys
from pathlib import Path

# Import the Drone Setup app modules by path. app/ is a plain directory of
# modules rather than an installed package, so pytest needs it on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
