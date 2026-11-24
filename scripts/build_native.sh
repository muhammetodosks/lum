#!/bin/sh
# Build the LUMEN native cores:
#   * rust/lum_core.rs   -> _liblum_core.so  (std-only Rust cdylib)
#   * cpp/lum_fast.cpp   -> _liblum_fast.so  (C++20 fast paths, medical I/O)
#   * cpp/lum_cloud.cpp  -> _liblum_cloud.so (C++20 cloud/EO helpers)
# All of them are loaded at runtime via ctypes; an absent or unloadable
# library only disables its own feature, never the codec itself.
set -eu
cd "$(dirname "$0")/.."

rustc --edition 2021 -O --crate-type cdylib \
      -o _liblum_core.so rust/lum_core.rs
echo "built _liblum_core.so"

if command -v g++ >/dev/null 2>&1; then
    g++ -O3 -std=c++20 -shared -fPIC -o _liblum_fast.so cpp/lum_fast.cpp
    echo "built _liblum_fast.so"

    g++ -O3 -std=c++20 -shared -fPIC -o _liblum_cloud.so cpp/lum_cloud.cpp
    echo "built _liblum_cloud.so"
else
    echo "g++ not found - skipping the C++ fast path" >&2
fi

# The self test of the C++ cloud helpers is a separate binary: it is a
# plain executable with its own main(), so it cannot live in the shared
# library.
if command -v g++ >/dev/null 2>&1; then
    g++ -O2 -std=c++20 -DLUMEN_CLOUD_SELFTEST -o /tmp/lum_cloud_selftest \
        cpp/lum_cloud.cpp
    echo "built the C++ cloud self test (/tmp/lum_cloud_selftest)"
fi

# The Rust unit tests live in the same source tree as the cdylib and are
# compiled with `rustc --test`.
if [ "${LUMEN_BUILD_TESTS:-0}" = "1" ]; then
    rustc --test --edition 2021 -O -o /tmp/lum_rust_test rust/lum_core.rs
    echo "built the Rust unit tests (/tmp/lum_rust_test)"
fi
