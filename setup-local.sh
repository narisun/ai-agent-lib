#!/bin/sh
# Resolve paths from this file so the command also works from another folder.
exec python3 "$(dirname "$0")/tools/local_dev.py" setup "$@"
