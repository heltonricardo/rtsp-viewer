#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec .venv/bin/python rtsp_viewer.py "$@"
