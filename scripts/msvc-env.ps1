$ErrorActionPreference = 'Stop'
$vswhere = "${env:ProgramFiles(x86)}/Microsoft Visual Studio/Installer/vswhere.exe"
$vs = & $vswhere -latest -version '[18.0,19.0)' -products '*' -property installationPath
if (-not $vs) { throw 'Visual Studio 2026 with C++ tools is required' }
$arch = $env:SDK_ARCH
if ($arch -notin @('x86', 'x64', 'arm64')) { throw 'Unsupported SDK architecture' }
$hostArch = if ($arch -eq 'arm64') { 'arm64' } else { 'x64' }
$devcmd = Join-Path $vs 'Common7/Tools/VsDevCmd.bat'
$lines = & cmd.exe /d /s /c "`"$devcmd`" -no_logo -arch=$arch -host_arch=$hostArch && set"
if ($LASTEXITCODE -ne 0) { throw 'MSVC environment initialization failed' }
foreach ($line in $lines) {
  if ($line -match '^([^=]+)=(.*)$') {
    $name = $matches[1]
    $value = $matches[2]
    if ($name -match '^(PATH|INCLUDE|LIB|LIBPATH|VCToolsInstallDir|VCToolsVersion|VSINSTALLDIR|WindowsSdkDir|WindowsSDKVersion|VSCMD_ARG_TGT_ARCH|VSCMD_ARG_HOST_ARCH)$') {
      "$name=$value" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    }
  }
}
# Prefer native CMake; an MSYS CMake may lack the resource dependency helper.
$cmake = Join-Path $vs 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin'
if (Test-Path (Join-Path $cmake 'cmake.exe')) {
  "SDK_CMAKE=$(Join-Path $cmake 'cmake.exe')" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
}
