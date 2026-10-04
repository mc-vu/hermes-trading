"""Standalone-Aufruf ohne Hermes: ``hermes-python -m plugin --db <pfad> <cmd>``."""

import sys

from .cli import main

sys.exit(main())
