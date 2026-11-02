"""p6flow: Primavera P6 XER files to typed Snowflake-loadable Parquet."""

from importlib.metadata import PackageNotFoundError, version

# pyproject.toml is the single source of truth for the version; read it
# from the installed metadata so this can never drift from a release.
try:
    __version__ = version("p6flow")
except PackageNotFoundError:  # running from a bare checkout, not installed
    __version__ = "0.0.0+unknown"
