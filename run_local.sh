#!/bin/sh
set -eu

python3 bench/bench.py prepare

# Default run includes GUI automation via the Swift AX/CoreGraphics probe.
python3 bench/bench.py run --iterations 5 --readiness auto --large-project zed
