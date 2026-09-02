<#
    Invoke-GamingServicesAction.ps1

    Installs or removes the two packages Forza Horizon (and other Xbox Play
    Anywhere titles) check for at launch:
      - Microsoft.GamingServices        (owns the GamingServices /
                                          GamingServicesNet Windows services)
      - Microsoft.XboxIdentityProvider  (handles the signed-in Xbox Live
                                          token the license check reads)

    Called by GamingServicesToggle.bat, which handles elevation and the
    menu. Can also be run directly from an elevated PowerShell:

        powershell -ExecutionPolicy Bypass -File .\Invoke-GamingServicesAction.ps1 -Action Install
        powershell -ExecutionPolicy Bypass -File .\Invoke-GamingServicesAction.ps1 -Action Remove
        powershell -ExecutionPolicy Bypass -File .\Invoke-GamingServicesAction.ps1 -Action Status
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Remove', 'Status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'

# Package family name -> Microsoft Store product ID
$Targets = [ordered]@{
    'Microsoft.GamingServices'       = '9MWPM2CQNLHN'
    'Microsoft.XboxIdentityProvider' = '9WZDNCRD1HKW'
}

# Windows services that Gaming Services owns. Their registry keys are what
# get left behind - corrupted or orphaned - when a plain Remove-AppxPackage
# doesn't fully clean up, which is the most common reason this error comes
# back on the next launch.
$ServiceNames = @('GamingServices', 'GamingServicesNet')

# Forza Horizon 6's published minimum Gaming Services build (Forza support).
# Worth re-checking on support.forza.net if this ever looks stale.
$MinFH6Version = [version]'37.114.10001.0'

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsElevated)) {
    Write-Host "This needs to run elevated - launch it through GamingServicesToggle.bat instead." -ForegroundColor Red
    exit 1
}

function Get-TargetPackage($PackageName) {
    Get-AppxPackage -Name $PackageName -AllUsers -ErrorAction SilentlyContinue
}

function Show-GamingServicesStatus {
    Write-Host ""
    Write-Host "Packages:" -ForegroundColor Cyan
    foreach ($packageName in $Targets.Keys) {
        $pkg = Get-TargetPackage $packageName
        if ($pkg) {
            Write-Host ("  [x] {0}  v{1}" -f $packageName, $pkg.Version) -ForegroundColor Green
        } else {
            Write-Host ("  [ ] {0}  (not installed)" -f $packageName) -ForegroundColor DarkGray
        }
    }
    Write-Host "Service registry keys:" -ForegroundColor Cyan
    foreach ($serviceName in $ServiceNames) {
        $regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName"
        if (Test-Path $regPath) {
            Write-Host ("  [x] {0}" -f $serviceName) -ForegroundColor Green
        } else {
            Write-Host ("  [ ] {0}  (clean)" -f $serviceName) -ForegroundColor DarkGray
        }
    }
    Write-Host ""
}

function Install-WingetPackage($ProductId) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    & winget install --id $ProductId --source msstore `
        --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Install-TargetPackage($PackageName, $ProductId) {
    if (Get-TargetPackage $PackageName) {
        Write-Host "Already installed: $PackageName" -ForegroundColor DarkGray
        return
    }

    Write-Host "Installing $PackageName ..." -ForegroundColor Yellow
    if (Install-WingetPackage $ProductId) {
        if (Get-TargetPackage $PackageName) {
            Write-Host "  Done (winget)." -ForegroundColor Green
            return
        }
    }

    # Fall back to the Store UI - winget/the msstore source isn't present on
    # every build, and stripped-down installs sometimes lack winget entirely.
    Start-Process ("ms-windows-store://pdp/?productid={0}" -f $ProductId)
    Write-Host "  Store opened - click Install there." -ForegroundColor Yellow
    Write-Host "  Waiting (checks every 5s, gives up after 5 min)..." -NoNewline

    $deadline = (Get-Date).AddMinutes(5)
    while (-not (Get-TargetPackage $PackageName)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host ""
            Write-Host "  Still not seeing it - finish the install manually, then run Status." -ForegroundColor Red
            return
        }
        Start-Sleep -Seconds 5
        Write-Host "." -NoNewline
    }
    Write-Host ""
    Write-Host "  Done." -ForegroundColor Green
}

function Remove-ServiceRegistryRemnant($ServiceName) {
    $serviceObj = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($serviceObj) {
        try {
            if ($serviceObj.Status -ne 'Stopped') {
                Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
            }
            & sc.exe delete $ServiceName | Out-Null
        } catch {
            # Non-fatal - the registry cleanup below still runs regardless.
        }
    }

    $regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
    if (Test-Path $regPath) {
        try {
            Remove-Item -Path $regPath -Recurse -Force
            Write-Host "  Cleared registry key: $ServiceName" -ForegroundColor Green
        } catch {
            Write-Host "  Could not clear $ServiceName registry key: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

switch ($Action) {

    'Install' {
        foreach ($packageName in $Targets.Keys) {
            Install-TargetPackage $packageName $Targets[$packageName]
        }

        $gamingServicesPkg = Get-TargetPackage 'Microsoft.GamingServices'
        if ($gamingServicesPkg -and $gamingServicesPkg.Version -lt $MinFH6Version) {
            Write-Host ""
            Write-Host ("Heads up: Gaming Services is v{0}; Forza Horizon 6 wants v{1}+." -f $gamingServicesPkg.Version, $MinFH6Version) -ForegroundColor Yellow
            Write-Host "Open Microsoft Store > Library/Downloads > Check for updates to grab the newer build." -ForegroundColor Yellow
        }

        Show-GamingServicesStatus
        Write-Host "If FH6 still throws the error on first launch, reboot once and try again." -ForegroundColor DarkGray
    }

    'Remove' {
        foreach ($packageName in $Targets.Keys) {
            $pkg = Get-TargetPackage $packageName
            if ($pkg) {
                $pkg | Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue
                Write-Host "Removed: $packageName" -ForegroundColor Green
            } else {
                Write-Host "Already absent: $packageName" -ForegroundColor DarkGray
            }
        }

        foreach ($serviceName in $ServiceNames) {
            Remove-ServiceRegistryRemnant $serviceName
        }

        Show-GamingServicesStatus
        Write-Host "A reboot fully clears any cached service state, but isn't required." -ForegroundColor DarkGray
    }

    'Status' {
        Show-GamingServicesStatus
    }
}