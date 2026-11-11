#!/bin/sh
# Build the PYRA native cores:
#   * rust/pyra_core.rs  -> _libpyra_core.so (std-only Rust cdylib)
#   * cpp/pyra_fast.cpp  -> _libpyra_fast.so (C++20 fast paths for v2)
# Both are loaded at runtime via ctypes; absent/unloadable -> pure-Python
# fallback with identical results.
set -eu
cd "$(dirname "$0")/.."

rustc --edition 2021 -O --crate-type cdylib \
      -o _libpyra_core.so rust/pyra_core.rs
echo "built _libpyra_core.so"

if command -v g++ >/dev/null 2>&1; then
    g++ -O3 -std=c++20 -shared -fPIC -o _libpyra_fast.so cpp/pyra_fast.cpp
    echo "built _libpyra_fast.so"
else
    echo "g++ not found - skipping the C++ fast path" >&2
fi
