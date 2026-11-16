#!/bin/sh
# Build the PYRA native cores:
#   * rust/pyra_core.rs   -> _libpyra_core.so  (std-only Rust cdylib)
#   * cpp/pyra_fast.cpp   -> _libpyra_fast.so  (C++20 fast paths, medical I/O)
#   * cpp/pyra_cloud.cpp  -> _libpyra_cloud.so (C++20 cloud/EO helpers)
# All of them are loaded at runtime via ctypes; an absent or unloadable
# library only disables its own feature, never the codec itself.
set -eu
cd "$(dirname "$0")/.."

rustc --edition 2021 -O --crate-type cdylib \
      -o _libpyra_core.so rust/pyra_core.rs
echo "built _libpyra_core.so"

if command -v g++ >/dev/null 2>&1; then
    g++ -O3 -std=c++20 -shared -fPIC -o _libpyra_fast.so cpp/pyra_fast.cpp
    echo "built _libpyra_fast.so"

    g++ -O3 -std=c++20 -shared -fPIC -o _libpyra_cloud.so cpp/pyra_cloud.cpp
    echo "built _libpyra_cloud.so"
else
    echo "g++ not found - skipping the C++ fast path" >&2
fi

# The self test of the C++ cloud helpers is a separate binary: it is a
# plain executable with its own main(), so it cannot live in the shared
# library.
if command -v g++ >/dev/null 2>&1; then
    g++ -O2 -std=c++20 -DPYRA_CLOUD_SELFTEST -o /tmp/pyra_cloud_selftest \
        cpp/pyra_cloud.cpp
    echo "built the C++ cloud self test (/tmp/pyra_cloud_selftest)"
fi

# The Rust unit tests live in the same source tree as the cdylib and are
# compiled with `rustc --test`.
if [ "${PYRA_BUILD_TESTS:-0}" = "1" ]; then
    rustc --test --edition 2021 -O -o /tmp/pyra_rust_test rust/pyra_core.rs
    echo "built the Rust unit tests (/tmp/pyra_rust_test)"
fi
