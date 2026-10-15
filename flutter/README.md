# homehub_mobile

A new Flutter project.

## Getting Started

This project is a starting point for a Flutter application.

A few resources to get you started if this is your first Flutter project:

- [Lab: Write your first Flutter app](https://docs.flutter.dev/get-started/codelab)
- [Cookbook: Useful Flutter samples](https://docs.flutter.dev/cookbook)

For help getting started with Flutter development, view the
[online documentation](https://docs.flutter.dev/), which offers tutorials,
samples, guidance on mobile development, and a full API reference.

## Supabase Runtime Defines

Supabase initialization is optional and only runs when both runtime defines are provided:

- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`

Use project root URL format for `SUPABASE_URL` (for example `https://your-project.supabase.co`) and do not append `/auth/v1` or `/rest/v1`.
You can provide multiple candidates (for example LAN + public) separated by commas.

The mobile app now auto-bootstraps Supabase runtime configuration from the backend endpoint `/api/supabase/client-config` after backend URL setup. In typical HomeHub deployments you do not need dart defines anymore.

Use dart defines only when you want fully standalone app runs without backend bootstrap (development override).

Example:

```bash
flutter run \
	--dart-define=SUPABASE_URL=https://your-project.supabase.co \
	--dart-define=SUPABASE_ANON_KEY=your_anon_key

# LAN + public fallback candidates
flutter run \
	--dart-define=SUPABASE_URL=http://192.168.1.161:8100,https://base.millitop.net \
	--dart-define=SUPABASE_ANON_KEY=your_anon_key
```
