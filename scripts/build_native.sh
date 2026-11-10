#!/bin/sh
# Build the PYRA native core (std-only Rust cdylib).
# Output: _libpyra_core.so in the package dir (loaded at runtime via ctypes;
# absent/unloadable -> pure-Python fallback, identical results).
set -eu
cd "$(dirname "$0")/.."
rustc --edition 2021 -O --crate-type cdylib \
      -o _libpyra_core.so rust/pyra_core.rs
echo "built _libpyra_core.so"
