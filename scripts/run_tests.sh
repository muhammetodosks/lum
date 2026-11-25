#!/bin/sh
# Run every verification suite of the project.
#
#   scripts/run_tests.sh            # the Python suites (native + fallback)
#   LUMEN_BUILD_TESTS=1 scripts/run_tests.sh
#                                   # ... and the native harnesses, plus the
#                                   # Rust unit tests, the C++ self test and
#                                   # the WebAssembly suite (needs node)
#
# The Python suites are run from the *parent* of the package directory,
# because the package is imported as `lum`.
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PARENT="$(dirname "$ROOT")"

echo "== Python suites (native core) =="
cd "$PARENT"
python3 -m lum.tests.test_lossless
python3 -m lum.tests.test_lossy
python3 -m lum.tests.test_medical
python3 -m lum.tests.test_cloud
python3 -m lum.tests.test_format_report

echo
echo "== Python suites (pure Python fallback) =="
LUMEN_DISABLE_NATIVE=1 python3 -m lum.tests.test_lossless
LUMEN_DISABLE_NATIVE=1 python3 -m lum.tests.test_medical

if [ "${LUMEN_BUILD_TESTS:-0}" = "1" ]; then
    echo
    echo "== Rust unit tests =="
    cd "$ROOT"
    rustc --test --edition 2021 -O -o /tmp/lum_rust_test rust/lum_core.rs
    /tmp/lum_rust_test

    echo
    echo "== C++ self test =="
    if command -v g++ >/dev/null 2>&1; then
        g++ -O2 -std=c++20 -DLUMEN_CLOUD_SELFTEST -o /tmp/lum_cloud_selftest \
            cpp/lum_cloud.cpp
        /tmp/lum_cloud_selftest
    else
        echo "g++ not found - skipping the C++ self test" >&2
    fi

    echo
    echo "== Browser / WebAssembly suite =="
    if command -v node >/dev/null 2>&1 && command -v rustc >/dev/null 2>&1; then
        ./scripts/build_wasm.sh
        node tests/test_wasm.mjs
    else
        echo "node or rustc not found - skipping the WebAssembly suite" >&2
    fi
fi

echo
echo "all suites passed"
