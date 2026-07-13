"""evoctx — local personal context store for AI coding assistants, over MCP. By Evo Bytes."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("evoctx")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0.dev0"
