#!/usr/bin/env pwsh

# Point Blender at this tree instead of at a copy of it.
#
# The addon lives in two places by default: this repo, and a snapshot under
# the Blender user config directory. Two copies can cause Blender to load
# stale code.
#
# Usage:
#
#   .\tools\dev_install.ps1
#       Link into every Blender version found.
#
#   .\tools\dev_install.ps1 5.2
#       Link into Blender 5.2 only.
#
#   .\tools\dev_install.ps1 --copy 5.2
#       Make a real copy instead, useful for testing a release.
#
# An existing real directory is MOVED ASIDE, never deleted.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$mode = "link"
$arguments = @($args)

if ($arguments.Count -gt 0 -and $arguments[0] -eq "--copy") {
    $mode = "copy"

    if ($arguments.Count -gt 1) {
        $arguments = @($arguments[1..($arguments.Count - 1)])
    }
    else {
        $arguments = @()
    }
}

# Resolve:
#
#   tools\dev_install.ps1
#       -> ..\addons\exmateria_map
#
$src = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\addons\exmateria_map")
)

if (-not (Test-Path -LiteralPath (Join-Path $src "__init__.py") -PathType Leaf)) {
    Write-Error "No addon found at $src"
    exit 1
}

# Standard Blender config location on Windows:
#
#   %APPDATA%\Blender Foundation\Blender\<version>
#
$blenderConfigRoot = Join-Path $env:APPDATA "Blender Foundation\Blender"

$roots = @()

if ($arguments.Count -gt 0) {
    foreach ($version in $arguments) {
        $roots += Join-Path $blenderConfigRoot $version
    }
}
else {
    if (Test-Path -LiteralPath $blenderConfigRoot -PathType Container) {
        $roots = @(
            Get-ChildItem `
                -LiteralPath $blenderConfigRoot `
                -Directory `
                -ErrorAction SilentlyContinue |
            Sort-Object Name |
            ForEach-Object { $_.FullName }
        )
    }
}

if ($roots.Count -eq 0) {
    Write-Error "No Blender config directories found under '$blenderConfigRoot'"
    exit 1
}

foreach ($root in $roots) {
    $addonsDir = Join-Path $root "scripts\addons"
    $dest = Join-Path $addonsDir "exmateria_map"

    New-Item `
        -ItemType Directory `
        -Path $addonsDir `
        -Force |
        Out-Null

    # Get the destination itself rather than following it.
    $existing = Get-Item `
        -LiteralPath $dest `
        -Force `
        -ErrorAction SilentlyContinue

    if ($null -ne $existing) {
        $isLink = (
            $existing.Attributes -band
            [System.IO.FileAttributes]::ReparsePoint
        ) -ne 0

        if ($isLink) {
            # Remove only the symlink/junction, not its target.
            Remove-Item `
                -LiteralPath $dest `
                -Force
        }
        else {
            $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $aside = "$dest.aside-$timestamp"

            # Avoid an extremely unlikely timestamp collision.
            $counter = 1
            while (Test-Path -LiteralPath $aside) {
                $aside = "$dest.aside-$timestamp-$counter"
                $counter++
            }

            Move-Item `
                -LiteralPath $dest `
                -Destination $aside

            Write-Host "  moved the existing copy to $aside"
        }
    }

    if ($mode -eq "link") {
        try {
            New-Item `
                -ItemType SymbolicLink `
                -Path $dest `
                -Target $src |
                Out-Null

            Write-Host "linked  $dest -> $src"
        }
        catch {
            Write-Error @"
Could not create the symbolic link:

    $dest
        ->
    $src

Windows may require either:
  1. Developer Mode to be enabled, or
  2. PowerShell to be run as Administrator.

To avoid symlinks, use:
    .\tools\dev_install.ps1 --copy <version>

Original error:
$($_.Exception.Message)
"@
            exit 1
        }
    }
    else {
        Copy-Item `
            -LiteralPath $src `
            -Destination $dest `
            -Recurse `
            -Force

        Write-Host "copied  $src -> $dest"
    }

    # A stale __pycache__ beside newer sources is another way to run old code.
    #
    # Materialize the list first so deleting directories doesn't interfere
    # with recursive enumeration.
    $pycacheDirectories = @(
        Get-ChildItem `
            -LiteralPath $dest `
            -Directory `
            -Recurse `
            -Force `
            -Filter "__pycache__" `
            -ErrorAction SilentlyContinue
    )

    foreach ($pycache in $pycacheDirectories) {
        Remove-Item `
            -LiteralPath $pycache.FullName `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Restart Blender (or disable/re-enable the addon) -- module imports are cached."
Write-Host "On enable it prints where it loaded from; that line is the check."