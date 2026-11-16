#!/bin/sh
# Run every verification suite of the project.
#
#   scripts/run_tests.sh            # the Python suites (native + fallback)
#   PYRA_BUILD_TESTS=1 scripts/run_tests.sh
#                                   # ... and the native harnesses
#
# The Python suites are run from the *parent* of the package directory,
# because the package is imported as `pyra`.
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PARENT="$(dirname "$ROOT")"

echo "== Python suites (native core) =="
cd "$PARENT"
python3 -m pyra.tests.test_lossless
python3 -m pyra.tests.test_medical
python3 -m pyra.tests.test_cloud

echo
echo "== Python suites (pure Python fallback) =="
PYRA_DISABLE_NATIVE=1 python3 -m pyra.tests.test_lossless
PYRA_DISABLE_NATIVE=1 python3 -m pyra.tests.test_medical

if [ "${PYRA_BUILD_TESTS:-0}" = "1" ]; then
    echo
    echo "== Rust unit tests =="
    cd "$ROOT"
    rustc --test --edition 2021 -O -o /tmp/pyra_rust_test rust/pyra_core.rs
    /tmp/pyra_rust_test

    echo
    echo "== C++ self test =="
    if command -v g++ >/dev/null 2>&1; then
        g++ -O2 -std=c++20 -DPYRA_CLOUD_SELFTEST -o /tmp/pyra_cloud_selftest \
            cpp/pyra_cloud.cpp
        /tmp/pyra_cloud_selftest
    else
        echo "g++ not found - skipping the C++ self test" >&2
    fi
fi

echo
echo "all suites passed"
