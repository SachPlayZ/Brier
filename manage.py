#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def _use_utf8_console():
    """Print UTF-8 whatever the console's own encoding is.

    A Windows console is cp1252 by default, so a command that writes a character
    outside Latin-1 dies with UnicodeEncodeError. `evaluate` did exactly that: it
    wrote its report files, then crashed printing the same report to the screen,
    because the report contains the characters used for "less than or equal".
    Reconfiguring here covers every command at once rather than policing the
    characters each one is allowed to use.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):   # already wrapped, or not a real tty
            pass


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    _use_utf8_console()
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Couldn't import Django. Is it installed and is the virtualenv active?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
