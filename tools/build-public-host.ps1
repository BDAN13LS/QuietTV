<#
.SYNOPSIS
  Offline, reproducible build of the public QuietTV native host source (launcher/host).

.DESCRIPTION
  Compiles once from source with the pinned .NET SDK 6.0.428, runs the offline
  --self-test, and checks the output carries no absolute build-machine paths.
  No TV, no signing, no deployment, no network: restore uses launcher/host/NuGet.Config
  (no package sources) and every cache lives under the project's ignored obj/ folder,
  which is removed afterwards. Output: <ProjectDir>/bin/<OutName>/QuietTV.dll.
  Every dotnet command runs with ProjectDir as its working directory, so the SDK is
  chosen by <ProjectDir>/global.json (6.0.428, rollForward disable) wherever the
  script is invoked from; the caller's location and environment are restored afterwards.

.PARAMETER Dotnet
  Path to dotnet.exe for SDK 6.0.428. Defaults to $env:QUIETTV_DOTNET, then `dotnet` on PATH.
#>
[CmdletBinding()]
param(
  [string]$Dotnet = $(if ($env:QUIETTV_DOTNET) { $env:QUIETTV_DOTNET } else { 'dotnet' }),
  [string]$ProjectDir,
  [string]$OutName = 'release',
  [switch]$KeepIntermediate
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$sep = [IO.Path]::DirectorySeparatorChar
$ignoreCase = if ($sep -eq '\') { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
function Get-CleanFullPath([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\', '/') }

$repo = Get-CleanFullPath (Split-Path -Parent $PSScriptRoot)
$hostRoot = Get-CleanFullPath (Join-Path $repo 'launcher/host')
$hostBin = $hostRoot + $sep + 'bin'
if (-not $ProjectDir) { $ProjectDir = $hostRoot }
$ProjectDir = Get-CleanFullPath $ProjectDir
# Only the owned host tree (or a copy under its ignored bin/) may be built.
if (-not ([string]::Equals($ProjectDir, $hostRoot, $ignoreCase) -or $ProjectDir.StartsWith($hostBin + $sep, $ignoreCase))) {
  throw "ProjectDir must be launcher/host or a copy under launcher/host/bin: $ProjectDir"
}
if ($OutName -notmatch '^[A-Za-z0-9_-]{1,40}$') { throw "OutName must be a simple folder name" }

# True when the path itself is a junction, symlink or other reparse point.
# File.GetAttributes does not follow the link, so a dangling junction is still seen.
function Test-ReparsePoint([string]$Path) {
  try { return [bool]([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) }
  catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { return $false }
}

# Refuse when any folder from the repository root down to $Path is a reparse point:
# such a link could redirect the build, or a recursive delete, outside the owned tree.
function Assert-NoReparseChain([string]$Path) {
  $p = Get-CleanFullPath $Path
  while ($true) {
    if (Test-ReparsePoint $p) { throw "refusing: reparse point (junction/symlink) at $p" }
    if ([string]::Equals($p, $repo, $ignoreCase)) { return }
    $parent = Split-Path -Parent $p
    if (-not $parent -or [string]::Equals($parent, $p, $ignoreCase)) { throw "refusing: $Path is outside the repository" }
    $p = $parent
  }
}

# Every recursive delete goes through this check, which runs immediately before it.
# The target must resolve to exactly <ProjectDir>\obj or <ProjectDir>\bin\<OutName>.
# Neither it nor any ancestor up to the repo may be a reparse point, and nothing inside it may be one.
function Remove-OwnedTree([string]$Path, [ValidateSet('obj', 'out')][string]$Kind) {
  $full = Get-CleanFullPath $Path
  $expected = if ($Kind -eq 'obj') { $ProjectDir + $sep + 'obj' } else { $ProjectDir + $sep + 'bin' + $sep + $OutName }
  if (-not [string]::Equals($full, $expected, $ignoreCase)) { throw "refusing to delete unexpected path $full (expected $expected)" }
  if (-not ($full.StartsWith($hostRoot + $sep, $ignoreCase))) { throw "refusing to delete outside launcher/host: $full" }
  if ([string]::Equals($full, $hostBin, $ignoreCase)) { throw "refusing to delete launcher/host/bin itself" }
  Assert-NoReparseChain $full
  if (-not (Test-Path -LiteralPath $full)) { return }
  $links = @(Get-ChildItem -LiteralPath $full -Recurse -Force -Attributes ReparsePoint -ErrorAction Stop)
  if ($links.Count -gt 0) { throw "refusing: reparse point inside $full ($($links[0].FullName))" }
  Remove-Item -LiteralPath $full -Recurse -Force
}

Assert-NoReparseChain $ProjectDir
$project = Join-Path $ProjectDir 'QuietTVHost.csproj'
if (-not (Test-Path -LiteralPath $project)) { throw "Project not found: $project" }

$obj = Join-Path $ProjectDir 'obj'
$cache = Join-Path $obj '.cache'
$out = Join-Path $ProjectDir (Join-Path 'bin' $OutName)
Remove-OwnedTree $obj 'obj'
Remove-OwnedTree $out 'out'
New-Item -ItemType Directory -Force -Path $cache | Out-Null

# The SDK pin travels with the source: global.json in ProjectDir, exact version, no roll-forward.
$globalJson = Join-Path $ProjectDir 'global.json'
if (-not (Test-Path -LiteralPath $globalJson)) { throw "global.json missing in $ProjectDir" }
$pin = Get-Content -LiteralPath $globalJson -Raw | ConvertFrom-Json
if (-not ($pin.sdk.version -eq '6.0.428' -and $pin.sdk.rollForward -eq 'disable' -and $pin.sdk.allowPrerelease -eq $false)) {
  throw "global.json must pin sdk 6.0.428 with rollForward disable and allowPrerelease false"
}

# Keep every SDK/NuGet cache inside the ignored obj/ folder; no user-level or global config is written.
$buildEnv = [ordered]@{
  DOTNET_CLI_HOME = $cache
  NUGET_PACKAGES = Join-Path $cache 'packages'
  NUGET_HTTP_CACHE_PATH = Join-Path $cache 'http'
  NUGET_SCRATCH = Join-Path $cache 'scratch'
  DOTNET_MULTILEVEL_LOOKUP = '0'
  DOTNET_NOLOGO = '1'
  DOTNET_CLI_TELEMETRY_OPTOUT = '1'
  DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
  DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
  MSBUILDDISABLENODEREUSE = '1'
}
$savedEnv = @{}
foreach ($name in $buildEnv.Keys) { $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name) }

Push-Location -LiteralPath $ProjectDir
try {
foreach ($name in $buildEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $buildEnv[$name]) }

# Windows PowerShell 5.1 turns redirected stderr into a terminating error under Stop; capture it instead.
$ErrorActionPreference = 'Continue'
$versionOutput = & $Dotnet --version 2>&1
$versionExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($versionExit -ne 0) { throw "dotnet --version failed under $globalJson ($versionExit): $($versionOutput -join ' ')" }
$version = ($versionOutput | Select-Object -Last 1).ToString().Trim()
if ($version -ne '6.0.428') { throw "SDK 6.0.428 required, found $version" }

& $Dotnet restore $project --configfile (Join-Path $ProjectDir 'NuGet.Config') -nodeReuse:false
if ($LASTEXITCODE -ne 0) { throw "restore failed ($LASTEXITCODE)" }
& $Dotnet build $project --no-restore -c Release -o $out -nodeReuse:false -p:UseSharedCompilation=false -p:ContinuousIntegrationBuild=true
if ($LASTEXITCODE -ne 0) { throw "build failed ($LASTEXITCODE)" }

$dll = Join-Path $out 'QuietTV.dll'
$selfTest = & $Dotnet $dll --self-test
$selfTestExit = $LASTEXITCODE
$selfTest | ForEach-Object { Write-Output $_ }
if ($selfTestExit -ne 0 -or -not ($selfTest -contains 'SELF-TEST PASS')) { throw "self-test failed ($selfTestExit)" }
}
finally {
  Pop-Location
  foreach ($name in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name]) }
}

# No absolute build-machine path may survive into the release output (UTF-8 or UTF-16LE).
$needles = @($repo, $ProjectDir, $env:USERPROFILE, 'C:\Users', 'C:/Users') | Where-Object { $_ } |
  ForEach-Object { $_; $_.Replace('\', '/') } | Select-Object -Unique
$leaks = @()
foreach ($file in Get-ChildItem -LiteralPath $out -File -Recurse) {
  $bytes = [IO.File]::ReadAllBytes($file.FullName)
  $latin = [Text.Encoding]::GetEncoding(28591).GetString($bytes).ToLowerInvariant()
  foreach ($n in $needles) {
    $u8 = $n.ToLowerInvariant()
    $u16 = [Text.Encoding]::GetEncoding(28591).GetString([Text.Encoding]::Unicode.GetBytes($n)).ToLowerInvariant()
    if ($latin.Contains($u8) -or $latin.Contains($u16)) { $leaks += "$($file.Name): $n" }
  }
}
if ($leaks.Count -gt 0) { throw "absolute host paths in output: $($leaks -join '; ')" }

if (-not $KeepIntermediate) { Remove-OwnedTree $obj 'obj' }
$hash = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "QUIETTV_HOST_SHA256=$hash"
Write-Output "BUILD-PUBLIC-HOST OK"
