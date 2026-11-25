#!/bin/sh
# Build the browser decoder: rust/lum_core.rs -> web/lum.wasm
#
# wasm32-unknown-unknown has no OS scheduler, so the parallel tile loops take
# their serial branch; the bytes they produce are identical to the native
# build (tests/test_wasm.js decodes a Python-encoded file and compares pixels).
#
# The whole codec is compiled in, not just the decoder: the demo page can also
# turn a canvas into a .lum, which is what makes the format usable from a
# normal web app with one <script> tag.
set -eu
cd "$(dirname "$0")/.."

TARGET=wasm32-unknown-unknown
if ! rustup target list --installed 2>/dev/null | grep -qx "$TARGET"; then
    echo "installing the $TARGET target" >&2
    rustup target add "$TARGET"
fi

mkdir -p web
rustc --edition 2021 \
      --target "$TARGET" \
      --crate-type cdylib \
      --crate-name lum_wasm \
      -C opt-level=3 \
      -C lto=fat \
      -C codegen-units=1 \
      -C panic=abort \
      -C strip=symbols \
      -o web/lum.wasm \
      rust/lum_core.rs

ls -l web/lum.wasm | awk '{printf "built web/lum.wasm (%d KiB)\n", $5/1024}'
