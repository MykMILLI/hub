$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

$keystorePath = Join-Path $PSScriptRoot "keystore.properties"
if (-not (Test-Path $keystorePath)) {
    Write-Error "Missing keystore.properties. Copy keystore.properties.example to keystore.properties and set real values first."
}

$gradleCmd = $null
if (Test-Path ".\gradlew.bat") {
    $gradleCmd = ".\gradlew.bat"
} else {
    $gradle = Get-Command gradle -ErrorAction SilentlyContinue
    if ($gradle) {
        $gradleCmd = "gradle"
    } else {
        Write-Error "No Gradle command found. Install Gradle or generate gradle wrapper (gradlew.bat)."
    }
}

Write-Host "Building signed release APK..."
& $gradleCmd "--no-daemon" "-Dkotlin.compiler.execution.strategy=in-process" clean assembleRelease

if ($LASTEXITCODE -ne 0) {
    Write-Error "Release build failed"
}

$apkPath = Join-Path $PSScriptRoot "app\build\outputs\apk\release\app-release.apk"
if (-not (Test-Path $apkPath)) {
    Write-Error "Release APK not found at $apkPath"
}

Write-Host "Signed APK ready: $apkPath"
