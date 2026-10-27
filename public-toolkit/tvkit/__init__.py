"""Host-side setup and recovery toolkit for one owned TV profile.

Everything in this package runs on the owner's PC. It never opens a device
connection by itself: device work goes through a transport adapter, and the
public toolkit ships no implemented adapter (see ``transport.py``).
"""

__version__ = "0.1.0-draft"
