"""Put the repository root on sys.path for the Home Assistant layer tests.

pytest inserts the directory containing a test file and omits the repository
root, so `custom_components.agent_harness_performance_tracker` is not importable without
this. The pure-module tests load by path and do not need it; the config flow and
entity tests import the package properly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
