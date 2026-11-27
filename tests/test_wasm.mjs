// Cross-check the WebAssembly build against the Python reference.
//
// What this proves, and why it is not a smoke test:
//   * the wasm decoder produces the exact same pixels as `lum decode`
//   * the wasm encoder produces the exact same bytes as `lum encode`
//   * a file written by a browser is readable by the CLI, and vice versa
//
// Run: node tests/test_wasm.mjs   (after scripts/build_wasm.sh)

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { Lumen } from "../web/lum.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const PARENT = path.resolve(ROOT, "..");

let checks = 0;
let failures = 0;

function ok(condition, message) {
  checks += 1;
  if (condition) {
    console.log(`  ok  ${message}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${message}`);
  }
}

function parsePPM(file) {
  const data = fs.readFileSync(file);
  const first = data.indexOf(0x0a);
  const second = data.indexOf(0x0a, first + 1);
  const third = data.indexOf(0x0a, second + 1);
  if (data.subarray(0, 2).toString() !== "P6") throw new Error(`not a P6 PPM: ${file}`);
  return new Uint8Array(data.subarray(third + 1));
}

function decodeWithPython(lumPath, outPath) {
  execFileSync(
    "python3",
    ["-c", "import sys; from lum import api; api.decode_file(sys.argv[1], sys.argv[2])", lumPath, outPath],
    { cwd: PARENT, stdio: "pipe" },
  );
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return false;
  return true;
}

function firstDifference(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i += 1) if (a[i] !== b[i]) return `byte ${i}: ${a[i]} != ${b[i]}`;
  return `length ${a.length} != ${b.length}`;
}

const work = fs.mkdtempSync(path.join(os.tmpdir(), "lum-wasm-"));
console.log(`fixtures: ${work}`);

const fixtureInfo = JSON.parse(
  execFileSync("python3", ["-m", "lum.tests.wasm_fixture", work], { cwd: PARENT }).toString(),
);
const fixtures = JSON.parse(fs.readFileSync(path.join(work, "fixtures.json"), "utf8"));

const wasm = fs.readFileSync(path.join(ROOT, "web", "lum.wasm"));
console.log(`wasm: ${(wasm.length / 1024).toFixed(0)} KiB`);

await Lumen.init(new Uint8Array(wasm));
ok(Lumen.ready, "the module instantiates");

const { width, height } = fixtures;
const src = new Uint8Array(fixtures.src);

for (const kind of ["lossless", "lossy"]) {
  const quality = kind === "lossy" ? 90 : 0;
  const blob = new Uint8Array(fs.readFileSync(path.join(work, `${kind}.lum`)));
  const reference = parsePPM(path.join(work, `${kind}.ppm`));

  const info = Lumen.inspect(blob);
  ok(info.width === width && info.height === height, `${kind}: inspect reports ${width}x${height}`);
  ok(info.channels === 3, `${kind}: inspect reports 3 channels`);
  ok(info.lossy === (kind === "lossy"), `${kind}: inspect reports the right container kind`);
  ok(info.extension === "lum" && info.mime === "image/lum", `${kind}: inspect carries MIME and extension`);

  const t0 = process.hrtime.bigint();
  const image = Lumen.decode(blob);
  const decodeMs = Number(process.hrtime.bigint() - t0) / 1e6;
  ok(image.width === width && image.height === height, `${kind}: decoded ${width}x${height}`);
  ok(image.channels === 3, `${kind}: decoded 3 channels`);
  ok(
    sameBytes(image.data, reference),
    `${kind}: pixels are bit-identical to the Python decoder (${image.data.length} bytes, ${decodeMs.toFixed(1)} ms)`,
  );

  const reencoded = Lumen.encode({ width, height, channels: 3, data: src }, { quality });
  ok(
    sameBytes(reencoded, blob),
    reencoded.length === blob.length
      ? `${kind}: the wasm encoder reproduces the reference bytes exactly (${blob.length} B)`
      : `${kind}: encoder size ${reencoded.length} B vs reference ${blob.length} B`,
  );

  const wasmPath = path.join(work, `${kind}-from-js.lum`);
  const ppmPath = path.join(work, `${kind}-from-js.ppm`);
  fs.writeFileSync(wasmPath, reencoded);
  decodeWithPython(wasmPath, ppmPath);
  ok(
    sameBytes(parsePPM(ppmPath), reference),
    `${kind}: Python decodes the browser-encoded file to the same pixels`,
  );
}

// The browser needs a >8 bit path too: 16-bit lossless must downconvert.
const grey16 = Buffer.alloc(width * height * 1 * 2);
for (let i = 0; i < width * height; i += 1) {
  const v = (i * 977) & 0xffff;
  grey16.writeUInt16BE(v, i * 2);
}
const greyPPM = path.join(work, "grey16.ppm");
fs.writeFileSync(greyPPM, Buffer.concat([Buffer.from(`P5\n${width} ${height}\n65535\n`), grey16]));
const greyLum = path.join(work, "grey16.lum");
execFileSync("python3", ["-c", "import sys; from lum import api; api.encode_file(sys.argv[1], sys.argv[2])", greyPPM, greyLum], { cwd: PARENT });
const grey = Lumen.decode(new Uint8Array(fs.readFileSync(greyLum)));
ok(grey.channels === 1 && grey.width === width, "16-bit greyscale: reports one channel");
let greyOk = true;
for (let i = 0; i < width * height; i += 1) {
  const want = ((i * 977) & 0xffff) >> 8;
  if (grey.data[i] !== want) {
    greyOk = false;
    break;
  }
}
ok(greyOk, "16-bit greyscale: downconverts to 8 bit exactly");

// Regression: the plane loops in lum_lossy.rs go parallel above 65 536 px and
// used to call thread::scope unconditionally, which traps on wasm. A 220x220
// sample is below that threshold, so the browser shipped with a crash that no
// test covered. 256x256 is the smallest size that takes the threaded branch.
{
  const W = 256;
  const H = 256;
  const big = new Uint8Array(W * H * 3);
  for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 3;
      big[i] = (x * 3 + y) & 0xff;
      big[i + 1] = (y * 5) & 0xff;
      big[i + 2] = (x ^ y) & 0xff;
    }
  }
  let bigBlob = null;
  let bigErr = null;
  try {
    bigBlob = Lumen.encode({ width: W, height: H, channels: 3, data: big }, { quality: 90 });
  } catch (err) {
    bigErr = err;
  }
  ok(
    bigBlob && bigBlob.length > 0,
    `256x256 lossy encode does not trap (${bigBlob ? `${bigBlob.length} B` : bigErr})`,
  );
  let bigImage = null;
  try {
    if (bigBlob) bigImage = Lumen.decode(bigBlob);
  } catch (err) {
    bigErr = err;
  }
  ok(
    bigImage && bigImage.width === W && bigImage.height === H,
    "256x256 lossy decode returns 256x256",
  );

  const bigPPM = path.join(work, "big.ppm");
  fs.writeFileSync(bigPPM, Buffer.concat([Buffer.from(`P6\n${W} ${H}\n255\n`), Buffer.from(big)]));
  const bigLum = path.join(work, "big-native.lum");
  execFileSync(
    "python3",
    ["-c", "import sys; from lum import api; api.encode_file(sys.argv[1], sys.argv[2], quality=90)", bigPPM, bigLum],
    { cwd: PARENT },
  );
  const nativeBytes = new Uint8Array(fs.readFileSync(bigLum));
  ok(
    bigBlob && sameBytes(bigBlob, nativeBytes),
    `256x256 wasm lossy bytes match native (${nativeBytes.length} B)`,
  );
}

const junk = new Uint8Array(24);
junk.set(new Uint8Array(fs.readFileSync(path.join(work, "lossless.lum"))).subarray(0, 24));
let threw = null;
try {
  Lumen.decode(junk);
} catch (err) {
  threw = err;
}
ok(threw && threw.code < 0, `a truncated stream is rejected with a codec error (${threw && threw.code})`);

let magic = null;
try {
  Lumen.decode(new Uint8Array(64));
} catch (err) {
  magic = err;
}
ok(magic && magic.code === -100, "a non-LUMEN buffer is rejected on the magic");

ok(fixtureInfo && fixtures.lossless.bytes > 0, "the fixture encodes to a non-empty container");

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) {
  console.log("FAILED");
  process.exit(1);
}
console.log("wasm suite passed");
