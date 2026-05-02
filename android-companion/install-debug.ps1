param(
    [string]$Variant = "Debug"
)

$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$gradleCmd = $null
if (Test-Path ".\\gradlew.bat") {
    $gradleCmd = ".\\gradlew.bat"
} else {
    $gradle = Get-Command gradle -ErrorAction SilentlyContinue
    if ($gradle) {
        $gradleCmd = "gradle"
    } else {
        Write-Error "No Gradle command found. Install Gradle or generate gradle wrapper (gradlew.bat)."
    }
}

$adb = Get-Command adb -ErrorAction SilentlyContinue
if (-not $adb) {
    Write-Error "adb not found in PATH. Install Android SDK Platform-Tools and add adb to PATH."
}

Write-Host "Building app$Variant APK..."
& $gradleCmd "--no-daemon" "-Dkotlin.compiler.execution.strategy=in-process" ("assemble" + $Variant)

if ($LASTEXITCODE -ne 0) {
    Write-Error "Gradle build failed"
}

$apkPath = Join-Path $PSScriptRoot "app\build\outputs\apk\$($Variant.ToLower())\app-$($Variant.ToLower()).apk"
if (-not (Test-Path $apkPath)) {
    Write-Error "APK not found at $apkPath"
}

Write-Host "Installing on connected device..."
& adb install -r $apkPath

if ($LASTEXITCODE -ne 0) {
    Write-Error "adb install failed"
}

Write-Host "Install complete."
