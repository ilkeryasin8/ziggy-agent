"""`python -m ziggy`: how an installed Ziggy starts itself again (panel jobs, the launchd service)."""
import sys

from ziggy.cli import main

sys.exit(main())
