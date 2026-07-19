"""PersonalTrade — personal AI trading research & execution platform (NSE via Upstox)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("personaltrade")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+dev"
