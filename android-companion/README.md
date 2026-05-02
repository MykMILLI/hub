# HomeHub Android Companion App

This companion app wraps your existing HomeHub web app and uses the exact same backend/API calls and auth/session behavior.

## What this app does

- Opens your existing HomeHub deployment URL in a hardened WebView.
- Supports camera + microphone for video calls.
- Supports file upload chooser for message/file workflows.
- Uses adaptive Android UI chrome and WebView sizing for phones and tablets.
- Keeps all desktop web functionality by running the same web app.

## Configure production URL

By default, the app points to:

- `http://192.168.1.225:5001`

To override at build time, add this in `android-companion/gradle.properties` (or pass as Gradle property):

```properties
HOMEHUB_URL=http://192.168.1.225:5001
```

For your public host, set:

```properties
HOMEHUB_URL=https://hub.millitop.net
```

## Open in Android Studio

1. Open Android Studio.
2. Choose **Open** and select `android-companion`.
3. Let Gradle sync.
4. Run on a phone/tablet/emulator.

## Build APK

From the `android-companion` folder:

```bash
./gradlew assembleRelease
```

On Windows with PowerShell:

```powershell
.\gradlew.bat assembleRelease
```

> If wrapper files are not present yet in your local checkout, Android Studio can generate/sync Gradle wrapper automatically.

## Signed release APK (shareable)

1. Copy `keystore.properties.example` to `keystore.properties`.
2. Set your keystore path/password/alias values.
3. Run:

```powershell
.\assemble-release.ps1
```

Output APK:

- `android-companion/app/build/outputs/apk/release/app-release.apk`

## Install on phone quickly

1. Enable Developer Options + USB debugging on your Android phone.
2. Connect device via USB.
3. Run:

```powershell
.\install-debug.ps1
```

This builds and installs the debug APK to the connected device.

The script uses `gradlew.bat` when available, otherwise falls back to a system `gradle` command.

## If Kotlin daemon fails

If you see:

- `Could not connect to Kotlin compile daemon`

the included scripts already force no-daemon + in-process Kotlin compilation.

Try this cleanup once from `android-companion`:

```powershell
if (Test-Path .\.gradle) { Remove-Item .\.gradle -Recurse -Force }
if (Test-Path .\app\build) { Remove-Item .\app\build -Recurse -Force }
if (Test-Path .\build) { Remove-Item .\build -Recurse -Force }
```

Then rebuild with:

```powershell
.\install-debug.ps1
```

or:

```powershell
.\assemble-release.ps1
```

## Security posture in app shell

- `usesCleartextTraffic=false`
- network security config disallows cleartext
- no WebView debug mode in production app code
- external domains open outside app (browser), HomeHub domain stays in-app
- runtime permission checks for camera/microphone before granting WebRTC capture

## LAN test note

The app allows cleartext only for local test hosts (`192.168.1.225`, `10.0.2.2`, `localhost`) so you can test your LAN deployment without forcing HTTPS. All other hosts remain HTTPS-only.
