<#
.SYNOPSIS
  Windows launcher for the TV setup/recovery toolkit (tvkit).

.DESCRIPTION
  Finds Python 3.9+ and runs tvkit_cli.py from this folder in isolated mode.
  It downloads nothing and runs nothing fetched from the network. Download
  the release archive yourself from https://github.com/BDAN13LS/QuietTV/releases,
  check its SHA-256 with Get-FileHash, extract it, then run this script from
  the extracted folder. Developer preview: no firmware, no automatic TV installer.

  Every command is read-only or a dry run unless you pass -Apply together with
  your own implemented transport adapter. This toolkit ships no adapter.

.EXAMPLE
  .\Setup.ps1 doctor
  .\Setup.ps1 validate D:\TV-Backups\capsule
  .\Setup.ps1 restore-plan D:\TV-Backups\capsule
  .\Setup.ps1 export-list
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet('doctor', 'profiles', 'validate', 'setup-plan', 'restore-plan', 'fetch', 'export-list', 'release-url', 'help')]
  [string]$Command,

  [Parameter(Position = 1)]
  [string]$Path,

  [string]$Sha256,
  [string]$Out,
  [string]$DeviceFacts,
  [string]$Config,
  [switch]$WithFrontend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRepo = 'https://github.com/BDAN13LS/QuietTV'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$entry = Join-Path $here 'tvkit_cli.py'
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
  throw "tvkit_cli.py not found next to Setup.ps1; extract the whole release archive first."
}

function Find-Python {
  foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = Get-Command $candidate[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $prefix = @()
    if ($candidate.Count -gt 1) { $prefix = $candidate[1..($candidate.Count - 1)] }
    $ok = & $exe.Source @prefix -I -c 'import sys; print(int(sys.version_info >= (3, 9)))' 2>$null
    if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq '1') {
      $found = @($exe.Source) + $prefix
      return , $found
    }
  }
  throw 'Python 3.9 or newer was not found. Install it from python.org, then run this again.'
}

function Require-Arg([string]$Value, [string]$Name) {
  if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Command needs -$Name." }
}

$python = Find-Python
$exeArgs = @()
if ($python.Count -gt 1) { $exeArgs = $python[1..($python.Count - 1)] }
$exeArgs += @('-I', $entry)

switch ($Command) {
  'help'         { $exeArgs += @('--help') }
  'doctor'       {
    $exeArgs += @('doctor')
    if ($Config) { $exeArgs += @('--config', $Config) }
    if ($DeviceFacts) { $exeArgs += @('--device-facts', $DeviceFacts) }
  }
  'profiles'     { $exeArgs += @('profiles') }
  'validate'     {
    Require-Arg $Path 'Path'
    $exeArgs += @('capsule', 'validate', $Path)
    if ($DeviceFacts) { $exeArgs += @('--device-facts', $DeviceFacts) }
  }
  'setup-plan'   { Require-Arg $Path 'Path'; $exeArgs += @('plan', 'setup', '--capsule', $Path) }
  'restore-plan' { Require-Arg $Path 'Path'; $exeArgs += @('plan', 'restore', '--capsule', $Path) }
  'fetch'        {
    Require-Arg $Path 'Path'; Require-Arg $Sha256 'Sha256'; Require-Arg $Out 'Out'
    $exeArgs += @('fetch', $Path, '--sha256', $Sha256, '--out', $Out)
  }
  'export-list'  {
    $exeArgs += @('export', '--list')
    if ($WithFrontend) { $exeArgs += @('--with-frontend') }
  }
  'release-url'  {
    # The project lives at $ProjectRepo. A -Config file may name a fork; anything that is not a
    # plain https://github.com/OWNER/REPO address (including a <CONFIGURE: ...> placeholder) fails closed.
    $url = $ProjectRepo
    if ($Config) {
      $cfg = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
      $url = [string]$cfg.releaseRepoUrl
    }
    if ($url -notmatch '^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
      Write-Error 'releaseRepoUrl is not configured as a plain https://github.com/OWNER/REPO address.'
      exit 1
    }
    Write-Output "$url/releases"
    exit 0
  }
}

& $python[0] @exeArgs
exit $LASTEXITCODE
