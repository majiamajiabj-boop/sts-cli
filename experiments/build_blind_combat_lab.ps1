param()
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
$source = Join-Path $root '.tools/sts_lightspeed'
$revision = '46e14e4adc23d2c1c738df8902a6f297109fee50'
if (!(Test-Path -LiteralPath $source)) { throw 'Run experiments/build_lightspeed.ps1 first' }
if ((git -C $source rev-parse HEAD) -ne $revision) { throw 'unexpected upstream revision' }
if (git -C $source status --porcelain --untracked-files=no) { throw 'upstream has local changes' }
$runtime = Join-Path $root '.tools/build-runtime'
$cmake = Join-Path $runtime 'cmake/data/bin/cmake.exe'
$wrapper = Join-Path $root '.tools/zig-cxx.cmd'
if (!(Test-Path -LiteralPath $cmake) -or !(Test-Path -LiteralPath $wrapper)) {
    throw 'Run experiments/build_lightspeed.ps1 first to prepare the local toolchain'
}
& $cmake -S "$root/experiments/native" -B "$root/experiments/build" -G Ninja "-DCMAKE_MAKE_PROGRAM=$runtime/bin/ninja.exe" "-DCMAKE_CXX_COMPILER=$wrapper" -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE) { throw 'configure failed' }
& $cmake --build "$root/experiments/build" --target blind-combat-lab -j 4
if ($LASTEXITCODE) { throw 'build failed' }
