$ErrorActionPreference = 'Stop'
$config = Get-Content (Join-Path $PSScriptRoot '../build-config.json') -Raw | ConvertFrom-Json
$hostArch = if ($env:SDK_ARCH -eq 'arm64') { 'arm64' } else { 'x64' }
$suffix = if ($hostArch -eq 'arm64') { 'woa64' } else { 'win64' }
$version = $config.windows_clang_version
$filename = "LLVM-$version-$suffix.msi"
$archive = Join-Path $env:RUNNER_TEMP $filename
$url = "https://github.com/llvm/llvm-project/releases/download/llvmorg-$version/$filename"
& curl.exe --fail --location --show-error --retry 2 --output $archive $url
if ($LASTEXITCODE -ne 0) { throw 'Clang download failed' }
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $config.windows_clang_sha256.$hostArch) {
  throw 'Clang installer SHA-256 mismatch'
}
# Administrative extraction keeps the runner's installed toolchains unchanged.
$destination = Join-Path $env:RUNNER_TEMP "clang-$version-$hostArch"
$log = Join-Path $env:RUNNER_TEMP 'clang-extract.log'
$arguments = "/a `"$archive`" /qn TARGETDIR=`"$destination`" /l*v `"$log`""
$process = Start-Process msiexec.exe -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
  Get-Content -LiteralPath $log -Tail 80
  throw "Clang extraction failed: $($process.ExitCode)"
}
$compilers = @(Get-ChildItem -LiteralPath $destination -Filter clang-cl.exe -Recurse -File)
if ($compilers.Count -ne 1) { throw "Expected one clang-cl executable; found $($compilers.Count)" }
$compiler = $compilers[0].FullName
& $compiler --version
if ($LASTEXITCODE -ne 0) { throw 'clang-cl cannot execute' }
"SDK_CLANG_CL=$compiler" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
