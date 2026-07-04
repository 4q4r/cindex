#!/usr/bin/env python
"""Django management entry point for the CIndex project."""

import os
import sys

from django.core.management import execute_from_command_line


def main() -> None:
    """Run Django's management command line."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
