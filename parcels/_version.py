import os
import subprocess

try:
    version = subprocess.check_output(['git', '-C', os.path.dirname(__file__), 'describe', '--tags'], stderr=subprocess.PIPE).decode('ascii').strip()
except:
    version = 0.1
    # from parcels._version_setup import version as version  # noqa
