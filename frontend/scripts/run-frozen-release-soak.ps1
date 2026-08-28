param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
    throw "The frozen NSIS release soak only runs on Windows."
}

$frontendRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = (Resolve-Path -LiteralPath (Join-Path $frontendRoot "..")).Path
$tauriRoot = Join-Path $frontendRoot "src-tauri"
$installerName = "DeskPilot_0.1.0_x64-setup.exe"
$installerPath = Join-Path $tauriRoot "target\release\bundle\nsis\$installerName"

if (-not $SkipBuild) {
    Push-Location $frontendRoot
    try {
        & pnpm desktop:build
        if ($LASTEXITCODE -ne 0) {
            throw "The frozen sidecar/NSIS build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "The exact NSIS installer is missing: $installerPath"
}
$installer = Get-Item -LiteralPath $installerPath
if ($installer.Length -le 0) {
    throw "The exact NSIS installer is empty."
}

$systemTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$soakRoot = [IO.Path]::GetFullPath(
    (Join-Path $systemTemp ("dpf-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)))
)
if (-not $soakRoot.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) {
    throw "The frozen release soak root escaped the system temporary directory."
}
$installRoot = Join-Path $soakRoot "install"
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null

$previousOptIn = $env:DESKPILOT_RUN_FROZEN_RELEASE_SOAK
$previousInstallRoot = $env:DESKPILOT_FROZEN_RELEASE_INSTALL_ROOT
$installed = $false
try {
    $installProcess = Start-Process `
        -FilePath $installer.FullName `
        -ArgumentList @("/S", "/D=$installRoot") `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($installProcess.ExitCode -ne 0) {
        throw "The NSIS installer failed with exit code $($installProcess.ExitCode)."
    }
    $installed = $true

    $desktopPath = Join-Path $installRoot "deskpilot.exe"
    $sidecarPath = Join-Path $installRoot "deskpilot-backend-sidecar.exe"
    $uninstallerPath = Join-Path $installRoot "uninstall.exe"
    $commandRuntimeRoot = Join-Path $installRoot "python-command-runtime"
    foreach ($requiredPath in @($desktopPath, $sidecarPath, $uninstallerPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "The installed NSIS layout is incomplete: $requiredPath"
        }
        if ((Get-Item -LiteralPath $requiredPath).Length -le 0) {
            throw "The installed NSIS artifact is empty: $requiredPath"
        }
    }
    if (-not (Test-Path -LiteralPath $commandRuntimeRoot -PathType Container)) {
        throw "The installed Python Command Profile resource is missing."
    }
    $commandRuntimeEntries = @(
        Get-ChildItem -LiteralPath $commandRuntimeRoot -Force
    )
    if (
        $commandRuntimeEntries.Count -ne 1 -or
        -not $commandRuntimeEntries[0].PSIsContainer -or
        $commandRuntimeEntries[0].Name -notmatch "^[0-9a-f]{64}$"
    ) {
        throw "The installed Python Command Profile resource is not one exact digest directory."
    }
    $commandRuntimeDigest = $commandRuntimeEntries[0].Name

    $installerHash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    $desktopHash = (Get-FileHash -LiteralPath $desktopPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $sidecarHash = (Get-FileHash -LiteralPath $sidecarPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{
        installer = $installer.Name
        installer_sha256 = $installerHash
        desktop_sha256 = $desktopHash
        sidecar_sha256 = $sidecarHash
        python_command_runtime_digest = $commandRuntimeDigest
        install_root = $installRoot
    } | ConvertTo-Json

    $env:DESKPILOT_RUN_FROZEN_RELEASE_SOAK = "1"
    $env:DESKPILOT_FROZEN_RELEASE_INSTALL_ROOT = $installRoot
    Push-Location $tauriRoot
    try {
        $testNames = @(
            "sidecar::tests::frozen_installed_supervisor_survives_two_external_kills_within_resource_caps",
            "sidecar::tests::frozen_installed_command_task_keeps_resultref_and_never_replays_unknown"
        )
        foreach ($testName in $testNames) {
            & cargo test --lib $testName -- --ignored --exact --nocapture
            if ($LASTEXITCODE -ne 0) {
                throw "The installed frozen release test $testName failed with exit code $LASTEXITCODE."
            }
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $previousOptIn) {
        Remove-Item Env:DESKPILOT_RUN_FROZEN_RELEASE_SOAK -ErrorAction SilentlyContinue
    }
    else {
        $env:DESKPILOT_RUN_FROZEN_RELEASE_SOAK = $previousOptIn
    }
    if ($null -eq $previousInstallRoot) {
        Remove-Item Env:DESKPILOT_FROZEN_RELEASE_INSTALL_ROOT -ErrorAction SilentlyContinue
    }
    else {
        $env:DESKPILOT_FROZEN_RELEASE_INSTALL_ROOT = $previousInstallRoot
    }

    $uninstallerPath = Join-Path $installRoot "uninstall.exe"
    if ($installed -and (Test-Path -LiteralPath $uninstallerPath -PathType Leaf)) {
        $uninstallProcess = Start-Process `
            -FilePath $uninstallerPath `
            -ArgumentList "/S" `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
        if ($uninstallProcess.ExitCode -ne 0) {
            Write-Warning "The NSIS uninstaller exited with code $($uninstallProcess.ExitCode)."
        }
    }

    $resolvedSoakRoot = [IO.Path]::GetFullPath($soakRoot)
    if (-not $resolvedSoakRoot.StartsWith($systemTemp, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a frozen release soak root outside the system temporary directory."
    }
    if (Test-Path -LiteralPath $resolvedSoakRoot) {
        Remove-Item -LiteralPath $resolvedSoakRoot -Recurse -Force
    }
}
