#!/bin/sh
# Preserve argv, stdin EOF, stdout/stderr bytes and the target's exit code.
# Never evaluate target strings or arguments as shell source.
exec "$@"
