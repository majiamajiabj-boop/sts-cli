param(
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root
$source = Join-Path $root '.tools/sts_lightspeed'
$revision = '46e14e4adc23d2c1c738df8902a6f297109fee50'
if (!(Test-Path -LiteralPath $source)) {
    git clone https://github.com/Attemory/sts_lightspeed.git $source
    if ($LASTEXITCODE) { throw 'clone failed' }
    git -C $source checkout --detach $revision
}
if ((git -C $source rev-parse HEAD) -ne $revision) { throw 'unexpected upstream revision' }
if (git -C $source status --porcelain --untracked-files=no) { throw 'upstream has local changes' }
git -C $source submodule update --init --depth 1
if ($LASTEXITCODE) { throw 'submodule failed' }
$runtime = Join-Path $root '.tools/build-runtime'
if (!(Test-Path -LiteralPath "$runtime/ziglang/zig.exe")) {
    & $Python -m pip install --target $runtime cmake==4.4.3 ninja==1.13.2 ziglang==0.16.0
    if ($LASTEXITCODE) { throw 'toolchain install failed' }
}
$wrapper = Join-Path $root '.tools/zig-cxx.cmd'
Set-Content -LiteralPath $wrapper -Encoding ascii -Value ('@"' + $runtime + '\ziglang\zig.exe" c++ %*')
$cmake = Join-Path $runtime 'cmake/data/bin/cmake.exe'
& $cmake -S $source -B "$source/build" -G Ninja "-DCMAKE_MAKE_PROGRAM=$runtime/bin/ninja.exe" "-DCMAKE_CXX_COMPILER=$wrapper" -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE) { throw 'configure failed' }
& $cmake --build "$source/build" --target battle-sim card-reward-eval -j 4
if ($LASTEXITCODE) { throw 'build failed' }
& "$source/build/card-reward-eval.exe" --self-test
if ($LASTEXITCODE) { throw 'self-test failed' }
