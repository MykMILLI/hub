import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:geolocator/geolocator.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:workmanager/workmanager.dart';

const String _notificationTaskName = 'homehub_notification_poll';
const String _notificationChannelId = 'homehub_alerts';
const String _notificationChannelName = 'HomeHub Alerts';
const String _notificationChannelDesc = 'HomeHub notifications for chat and family activity';
const String _ackActionId = 'acknowledge_notification';
const List<String> _mealTypes = ['breakfast', 'morning_snack', 'lunch', 'afternoon_snack', 'dinner'];
const Map<String, String> _mealReminderTime = {
  'breakfast': '07:55',
  'morning_snack': '09:55',
  'lunch': '11:55',
  'afternoon_snack': '16:25',
  'dinner': '17:55',
};
const String _storyReminderTime = '19:55';
const Duration _scheduledReminderEarlyTolerance = Duration(minutes: 1);
const Duration _scheduledReminderCatchUpWindow = Duration(minutes: 90);
const String _cloudBaseUrl = 'https://hub.millitop.net';
const String _localBaseUrl = 'http://192.168.1.225:5001';
const bool _calendarSupportsOccurrenceScopedUpdates = false;
final http.Client _httpClient = IOClient(HttpClient());

String _normalizeBaseUrl(String raw) => raw.startsWith('http://') || raw.startsWith('https://') ? raw : 'http://$raw';

List<String> _endpointCandidates(String configuredBaseUrl) {
  final values = <String>[configuredBaseUrl.trim(), _cloudBaseUrl, _localBaseUrl]
      .where((value) => value.isNotEmpty)
      .map(_normalizeBaseUrl)
      .toList();
  final seen = <String>{};
  final out = <String>[];
  for (final value in values) {
    if (seen.add(value)) out.add(value);
  }
  return out;
}

Future<bool> _isEndpointReachable(String baseUrl) async {
  try {
    final response = await http.get(_apiUri(baseUrl, '/healthz')).timeout(const Duration(seconds: 3));
    return response.statusCode >= 200 && response.statusCode < 500;
  } on SocketException {
    return false;
  } on TimeoutException {
    return false;
  } on http.ClientException {
    return false;
  } catch (_) {
    return false;
  }
}

Future<String?> _firstReachableEndpoint(String configuredBaseUrl) async {
  for (final baseUrl in _endpointCandidates(configuredBaseUrl)) {
    final ok = await _isEndpointReachable(baseUrl);
    if (ok) return baseUrl;
  }
  return null;
}

Uri _apiUri(String baseUrl, String path, [Map<String, String>? query]) {
  final root = Uri.parse(_normalizeBaseUrl(baseUrl));
  final rootPath = root.path.endsWith('/') ? root.path.substring(0, root.path.length - 1) : root.path;
  return root.replace(path: '$rootPath$path', queryParameters: query);
}

String _todayKey(DateTime dt) => '${dt.year.toString().padLeft(4, '0')}${dt.month.toString().padLeft(2, '0')}${dt.day.toString().padLeft(2, '0')}';

DateTime _easternNow() => _toEasternTime(DateTime.now().toUtc());

DateTime _toEasternTime(DateTime utcNow) {
  final dstStartUtc = _usEasternDstStartUtc(utcNow.year);
  final dstEndUtc = _usEasternDstEndUtc(utcNow.year);
  final isDst = !utcNow.isBefore(dstStartUtc) && utcNow.isBefore(dstEndUtc);
  return utcNow.add(Duration(hours: isDst ? -4 : -5));
}

DateTime _usEasternDstStartUtc(int year) {
  final marchSecondSunday = _nthWeekdayOfMonthUtc(year, 3, DateTime.sunday, 2);
  return DateTime.utc(year, 3, marchSecondSunday.day, 7, 0);
}

DateTime _usEasternDstEndUtc(int year) {
  final novemberFirstSunday = _nthWeekdayOfMonthUtc(year, 11, DateTime.sunday, 1);
  return DateTime.utc(year, 11, novemberFirstSunday.day, 6, 0);
}

DateTime _nthWeekdayOfMonthUtc(int year, int month, int weekday, int nth) {
  final first = DateTime.utc(year, month, 1);
  final delta = (weekday - first.weekday + 7) % 7;
  return first.add(Duration(days: delta + (nth - 1) * 7));
}

String _scheduledShownKey(String baseUrl, String member, String slot, DateTime now) {
  final safeBase = baseUrl.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
  final safeMember = member.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
  return 'homehub_sched_shown_${safeBase}_${safeMember}_${_todayKey(now)}_$slot';
}

String _scheduledDiagKey(String baseUrl, String member, String suffix) {
  final safeBase = baseUrl.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
  final safeMember = member.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
  return 'homehub_sched_diag_${safeBase}_${safeMember}_$suffix';
}

int _scheduledNotificationId(String slot, DateTime now) {
  final seed = '${_todayKey(now)}_$slot';
  return 700000 + seed.codeUnits.fold<int>(0, (sum, ch) => (sum + ch) % 1000000);
}

DateTime? _timeForToday(DateTime now, String hhmm) {
  final parts = hhmm.split(':');
  if (parts.length != 2) return null;
  final h = int.tryParse(parts[0]);
  final m = int.tryParse(parts[1]);
  if (h == null || m == null) return null;
  return DateTime(now.year, now.month, now.day, h, m);
}

bool _isWithinScheduledWindow(DateTime now, DateTime trigger) {
  return !now.isBefore(trigger.subtract(_scheduledReminderEarlyTolerance)) &&
      now.isBefore(trigger.add(_scheduledReminderCatchUpWindow));
}

String _reminderSlot(Map<String, dynamic> row) {
  final id = _toInt(row['id']);
  if (id > 0) return 'reminder_$id';
  final title = '${row['title'] ?? 'reminder'}'.toLowerCase().replaceAll(RegExp(r'[^a-z0-9]+'), '_');
  final timeRaw = '${row['time'] ?? ''}'.trim().replaceAll(':', '');
  return 'reminder_${timeRaw}_$title';
}

String _mealLabel(String mealType) {
  switch (mealType) {
    case 'breakfast':
      return 'Breakfast';
    case 'morning_snack':
      return 'Morning Snack';
    case 'lunch':
      return 'Lunch';
    case 'afternoon_snack':
      return 'Afternoon Snack';
    case 'dinner':
      return 'Dinner';
    default:
      return mealType;
  }
}

class _SseEvent {
  const _SseEvent({required this.event, required this.data});

  final String event;
  final String data;
}

Stream<_SseEvent> _sseConnect(Uri uri) async* {
  final client = http.Client();
  try {
    final req = http.Request('GET', uri)
      ..headers['Accept'] = 'text/event-stream'
      ..headers['Cache-Control'] = 'no-cache';
    final response = await client.send(req).timeout(const Duration(seconds: 20));

    String eventName = 'message';
    final dataBuffer = StringBuffer();
    await for (final line in response.stream.transform(utf8.decoder).transform(const LineSplitter())) {
      if (line.isEmpty) {
        final data = dataBuffer.toString();
        if (data.isNotEmpty) {
          yield _SseEvent(event: eventName, data: data);
        }
        eventName = 'message';
        dataBuffer.clear();
        continue;
      }
      if (line.startsWith('event:')) {
        eventName = line.substring(6).trim();
        continue;
      }
      if (line.startsWith('data:')) {
        if (dataBuffer.isNotEmpty) dataBuffer.write('\n');
        dataBuffer.write(line.substring(5).trim());
      }
    }
  } finally {
    client.close();
  }
}

@pragma('vm:entry-point')
void notificationTapBackground(NotificationResponse response) {}

@pragma('vm:entry-point')
void notificationBackgroundDispatcher() {
  Workmanager().executeTask((taskName, inputData) async {
    if (taskName != _notificationTaskName) return true;

    final prefs = await SharedPreferences.getInstance();
    final baseUrl = (prefs.getString('homehub_base_url') ?? '').trim();
    final member = (prefs.getString('homehub_member') ?? '').trim();
    if (baseUrl.isEmpty || member.isEmpty) return true;

    final normalized = baseUrl.startsWith('http://') || baseUrl.startsWith('https://') ? baseUrl : 'http://$baseUrl';
    final uri = _apiUri(normalized, '/api/notifications', {'member': member, 'unread_only': 'true'});

    final response = await http.get(uri).timeout(const Duration(seconds: 20));
    if (response.statusCode < 200 || response.statusCode >= 300) return true;
    final data = jsonDecode(response.body);
    if (data is! Map<String, dynamic>) return true;
    final items = (data['items'] as List<dynamic>? ?? []).whereType<Map<String, dynamic>>().toList();

    items.sort((a, b) => _toInt(a['id']).compareTo(_toInt(b['id'])));
    final key = 'homehub_last_notification_${baseUrl.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_')}_${member.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_')}';
    int last = prefs.getInt(key) ?? 0;

    final local = FlutterLocalNotificationsPlugin();
    const init = InitializationSettings(android: AndroidInitializationSettings('@mipmap/ic_launcher'));
    await local.initialize(init);

    final now = _easternNow();
    final monthUri = _apiUri(normalized, '/api/meal-planner/entries', {
      'member': member,
      'year': '${now.year}',
      'month': '${now.month}',
    });
    try {
      final mealResponse = await http.get(monthUri).timeout(const Duration(seconds: 20));
      if (mealResponse.statusCode >= 200 && mealResponse.statusCode < 300) {
        final mealObj = jsonDecode(mealResponse.body);
        final rows = (mealObj is Map<String, dynamic> ? mealObj['entries'] as List<dynamic>? : null) ?? const [];
        final todayIso = _fmtDate(now);
        final todayMeals = rows.whereType<Map<String, dynamic>>().where((row) {
          return '${row['date'] ?? ''}' == todayIso && row['is_locked'] != true;
        }).toList();

        for (final mealType in _mealTypes) {
          final trigger = _timeForToday(now, _mealReminderTime[mealType] ?? '');
          if (trigger == null) continue;
          final inWindow = _isWithinScheduledWindow(now, trigger);
          if (!inWindow) continue;
          final slot = todayMeals.where((row) => '${row['meal_type'] ?? ''}' == mealType).toList();

          final shownKey = _scheduledShownKey(baseUrl, member, 'meal_$mealType', now);
          if (prefs.getBool(shownKey) == true) continue;

          final mealLabel = _mealLabel(mealType);
          final planned = slot.map((row) => '${row['title'] ?? ''}').where((value) => value.trim().isNotEmpty).take(3).join(' • ');
          final body = planned.isEmpty ? '$mealLabel - no meal planned.' : '$mealLabel - $planned';

          await local.show(
            _scheduledNotificationId('meal_$mealType', now),
            '$mealLabel reminder',
            body,
            NotificationDetails(
              android: AndroidNotificationDetails(
                _notificationChannelId,
                _notificationChannelName,
                channelDescription: _notificationChannelDesc,
                importance: Importance.max,
                priority: Priority.high,
                actions: const [AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true)],
              ),
            ),
            payload: shownKey,
          );
          await prefs.setBool(shownKey, true);
          await prefs.setString(_scheduledDiagKey(baseUrl, member, 'last_fired_meal_$mealType'), now.toIso8601String());
        }

        final storyTrigger = _timeForToday(now, _storyReminderTime);
        if (storyTrigger != null) {
          final inStoryWindow = _isWithinScheduledWindow(now, storyTrigger);
          if (inStoryWindow) {
            final shownKey = _scheduledShownKey(baseUrl, member, 'story_reader', now);
            if (prefs.getBool(shownKey) != true) {
              String reader = 'Story reader not set';
              try {
                final routinesUri = _apiUri(normalized, '/api/routines');
                final routinesResponse = await http.get(routinesUri).timeout(const Duration(seconds: 20));
                if (routinesResponse.statusCode >= 200 && routinesResponse.statusCode < 300) {
                  final routinesObj = jsonDecode(routinesResponse.body);
                  final bedtime = (routinesObj is Map<String, dynamic> ? (routinesObj['routines'] as Map<String, dynamic>? ?? {})['bedtime'] : null) as Map<String, dynamic>?;
                  final rawReader = '${bedtime?['reader'] ?? ''}'.trim();
                  if (rawReader.isNotEmpty) reader = rawReader;
                }
              } catch (_) {}

              await local.show(
                _scheduledNotificationId('story_reader', now),
                'Bedtime Reader Tonight',
                '$reader is the reader tonight.',
                NotificationDetails(
                  android: AndroidNotificationDetails(
                    _notificationChannelId,
                    _notificationChannelName,
                    channelDescription: _notificationChannelDesc,
                    importance: Importance.max,
                    priority: Priority.high,
                    actions: const [AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true)],
                  ),
                ),
                payload: shownKey,
              );
              await prefs.setBool(shownKey, true);
              await prefs.setString(_scheduledDiagKey(baseUrl, member, 'last_fired_story_reader'), now.toIso8601String());
            }
          }
        }

        try {
          final remindersUri = _apiUri(normalized, '/api/reminders', {
            'scope': 'month',
            'date': _fmtDate(now),
          });
          final remindersResponse = await http.get(remindersUri).timeout(const Duration(seconds: 20));
          if (remindersResponse.statusCode >= 200 && remindersResponse.statusCode < 300) {
            final remindersObj = jsonDecode(remindersResponse.body);
            final reminders = (remindersObj is Map<String, dynamic> ? remindersObj['reminders'] as List<dynamic>? : null) ?? const [];
            final today = _fmtDate(now);

            for (final raw in reminders) {
              if (raw is! Map) continue;
              final row = Map<String, dynamic>.from(raw);
              if ('${row['date'] ?? ''}' != today) continue;
              final rawTime = '${row['time'] ?? ''}'.trim();
              if (rawTime.length < 5) continue;
              final trigger = _timeForToday(now, rawTime.substring(0, 5));
              if (trigger == null || !_isWithinScheduledWindow(now, trigger)) continue;

              final slot = _reminderSlot(row);
              final shownKey = _scheduledShownKey(baseUrl, member, slot, now);
              if (prefs.getBool(shownKey) == true) continue;

              final title = '${row['title'] ?? 'Reminder'}'.trim();
              final description = '${row['description'] ?? ''}'.trim();
              final body = description.isNotEmpty ? description : 'Reminder for $rawTime';
              await local.show(
                _scheduledNotificationId(slot, now),
                title.isEmpty ? 'Reminder' : title,
                body,
                NotificationDetails(
                  android: AndroidNotificationDetails(
                    _notificationChannelId,
                    _notificationChannelName,
                    channelDescription: _notificationChannelDesc,
                    importance: Importance.max,
                    priority: Priority.high,
                    actions: const [AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true)],
                  ),
                ),
                payload: shownKey,
              );
              await prefs.setBool(shownKey, true);
              await prefs.setString(_scheduledDiagKey(baseUrl, member, 'last_fired_$slot'), now.toIso8601String());
            }
          }
        } catch (_) {}
      }
    } catch (_) {}

    await prefs.setString(_scheduledDiagKey(baseUrl, member, 'last_check'), now.toIso8601String());

    for (final item in items) {
      final id = _toInt(item['id']);
      if (id <= last) continue;
      final title = '${item['title'] ?? item['event_type'] ?? 'HomeHub Notification'}';
      final msg = '${item['message'] ?? ''}';
      await local.show(
        id,
        title,
        msg,
        NotificationDetails(
          android: AndroidNotificationDetails(
            _notificationChannelId,
            _notificationChannelName,
            channelDescription: _notificationChannelDesc,
            importance: Importance.max,
            priority: Priority.high,
            actions: const [AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true)],
          ),
        ),
        payload: 'notif_ack_$id',
      );
      last = id;
    }

    await prefs.setInt(key, last);
    return true;
  });
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  Workmanager().initialize(notificationBackgroundDispatcher);
  runApp(const HomeHubMobileApp());
}

class HomeHubMobileApp extends StatefulWidget {
  const HomeHubMobileApp({super.key});

  @override
  State<HomeHubMobileApp> createState() => _HomeHubMobileAppState();
}

class _HomeHubMobileAppState extends State<HomeHubMobileApp> {
  final SettingsStore _settingsStore = SettingsStore();
  AppSettings? _settings;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    final settings = await _settingsStore.load();
    if (!mounted) return;
    setState(() {
      _settings = settings;
      _loading = false;
    });
  }

  Future<void> _saveSettings(AppSettings settings) async {
    await _settingsStore.save(settings);
    if (!mounted) return;
    setState(() => _settings = settings);
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'HomeHub Mobile',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: const ColorScheme.light(
          primary: Color(0xFF4F46E5),
          onPrimary: Colors.white,
          secondary: Color(0xFF6366F1),
          onSecondary: Colors.white,
          surface: Color(0xFFF8FAFC),
          onSurface: Color(0xFF0F172A),
        ),
        scaffoldBackgroundColor: const Color(0xFFF1F5F9),
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.white,
          foregroundColor: Color(0xFF0F172A),
          elevation: 0,
          scrolledUnderElevation: 0,
        ),
        cardTheme: CardThemeData(
          color: Colors.white,
          elevation: 0,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        ),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: Colors.white,
          border: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: const BorderSide(color: Color(0xFFE2E8F0)),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: const BorderSide(color: Color(0xFFE2E8F0)),
          ),
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(14),
            borderSide: const BorderSide(color: Color(0xFF4F46E5), width: 1.5),
          ),
        ),
      ),
      home: _loading
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : (_settings == null || !_settings!.isValid)
              ? SetupScreen(onSave: _saveSettings)
              : (_settings!.member.trim().isEmpty)
                  ? AuthGateScreen(
                      settings: _settings!,
                      onUpdateSettings: _saveSettings,
                      onAuthenticated: (authedSettings) {
                        setState(() => _settings = authedSettings);
                      },
                    )
                  : HomeShell(
                      settings: _settings!,
                      onUpdateSettings: _saveSettings,
                    ),
    );
  }
}

class AppSettings {
  const AppSettings({required this.baseUrl, required this.member});

  final String baseUrl;
  final String member;

  bool get isValid => baseUrl.trim().isNotEmpty;
}

class AppSection {
  const AppSection({
    required this.label,
    required this.icon,
    this.nativeBuilder,
    this.webPath,
  });

  final String label;
  final IconData icon;
  final Widget Function(HomeHubApi api, AppSettings settings)? nativeBuilder;
  final String? webPath;
}

enum LauncherSearchAction {
  addShopping,
  findEvent,
  addEvent,
  addRecipe,
  addExpense,
  addIncome,
}

class SettingsStore {
  static const _baseUrlKey = 'homehub_base_url';
  static const _memberKey = 'homehub_member';

  Future<AppSettings?> load() async {
    final prefs = await SharedPreferences.getInstance();
    final baseUrl = (prefs.getString(_baseUrlKey) ?? '').trim();
    final member = (prefs.getString(_memberKey) ?? '').trim();
    if (baseUrl.isEmpty) return null;
    return AppSettings(baseUrl: baseUrl, member: member);
  }

  Future<void> save(AppSettings settings) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_baseUrlKey, settings.baseUrl.trim());
    await prefs.setString(_memberKey, settings.member.trim());
  }
}

class HomeHubApi {
  HomeHubApi(this.settings)
      : _activeBaseUrl = settings.baseUrl.trim().isEmpty ? _cloudBaseUrl : settings.baseUrl.trim();

  final AppSettings settings;
  String _activeBaseUrl;

  String get member => settings.member;

  Uri eventsStreamUri() {
    return _uri('/api/stream/events', {'member': settings.member});
  }

  Uri _uri(String path, [Map<String, String>? query]) {
    // Always prioritize the configured URL (external DNS) and use runtime fallback only when needed.
    final preferred = settings.baseUrl.trim();
    final base = preferred.isEmpty
        ? (_activeBaseUrl.trim().isEmpty ? _cloudBaseUrl : _activeBaseUrl.trim())
        : preferred;
    return _apiUri(base.isEmpty ? _cloudBaseUrl : base, path, query);
  }

  Uri _remapUriToBase(Uri original, String baseUrl) {
    final root = Uri.parse(_normalizeBaseUrl(baseUrl));
    return root.replace(
      path: original.path,
      query: original.hasQuery ? original.query : null,
    );
  }

  bool _isRetryableNetworkError(Object error) {
    return error is SocketException || error is TimeoutException || error is http.ClientException;
  }

  Future<http.Response> _requestWithFailover({
    required Uri uri,
    required Future<http.Response> Function(Uri uri) sender,
    required int seconds,
  }) async {
    try {
      return await sender(uri).timeout(Duration(seconds: seconds));
    } catch (err) {
      if (!_isRetryableNetworkError(err)) rethrow;
    }

    final tried = <String>{
      '${uri.scheme}://${uri.host}${uri.hasPort ? ':${uri.port}' : ''}',
    };
    for (final candidate in _endpointCandidates(settings.baseUrl)) {
      final mapped = _remapUriToBase(uri, candidate);
      final key = '${mapped.scheme}://${mapped.host}${mapped.hasPort ? ':${mapped.port}' : ''}';
      if (!tried.add(key)) continue;
      try {
        final response = await sender(mapped).timeout(Duration(seconds: seconds));
        // Keep fallback non-sticky when a configured URL exists.
        if (settings.baseUrl.trim().isEmpty) {
          _activeBaseUrl = _normalizeBaseUrl(candidate);
        }
        return response;
      } catch (err) {
        if (!_isRetryableNetworkError(err)) rethrow;
      }
    }

    throw http.ClientException('All configured HomeHub endpoints are unreachable', uri);
  }

  String _weatherLocationStoragePrefix() {
    final raw = '${settings.baseUrl.trim()}|${settings.member.trim()}';
    final safe = raw.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    return 'homehub_weather_location_$safe';
  }

  Future<void> _saveLocalWeatherLocation({
    required double latitude,
    required double longitude,
    required String locationName,
    required bool useGpsLocation,
  }) async {
    final prefs = await SharedPreferences.getInstance();
    final key = _weatherLocationStoragePrefix();
    await prefs.setDouble('${key}_lat', latitude);
    await prefs.setDouble('${key}_lon', longitude);
    await prefs.setString('${key}_name', locationName);
    await prefs.setBool('${key}_gps', useGpsLocation);
  }

  Future<Map<String, dynamic>> _loadLocalWeatherLocation() async {
    final prefs = await SharedPreferences.getInstance();
    final key = _weatherLocationStoragePrefix();
    final lat = prefs.getDouble('${key}_lat');
    final lon = prefs.getDouble('${key}_lon');
    if (lat == null || lon == null) return <String, dynamic>{};
    return {
      'latitude': lat,
      'longitude': lon,
      'location_name': prefs.getString('${key}_name') ?? '',
      'use_gps_location': prefs.getBool('${key}_gps') ?? false,
    };
  }

  Future<http.Response> _get(Uri uri, {int seconds = 15}) {
    return _requestWithFailover(
      uri: uri,
      seconds: seconds,
      sender: (nextUri) => _httpClient.get(nextUri),
    );
  }

  Future<http.Response> _postJson(Uri uri, Map<String, dynamic> body, {int seconds = 15}) {
    return _requestWithFailover(
      uri: uri,
      seconds: seconds,
      sender: (nextUri) => _httpClient.post(
        nextUri,
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ),
    );
  }

  Future<http.Response> _putJson(Uri uri, Map<String, dynamic> body, {int seconds = 15}) {
    return _requestWithFailover(
      uri: uri,
      seconds: seconds,
      sender: (nextUri) => _httpClient.put(
        nextUri,
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ),
    );
  }

  Future<http.Response> _deleteJson(Uri uri, Map<String, dynamic> body, {int seconds = 15}) {
    return _requestWithFailover(
      uri: uri,
      seconds: seconds,
      sender: (nextUri) => _httpClient.delete(
        nextUri,
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode(body),
      ),
    );
  }

  Future<Map<String, dynamic>> fetchExpensesMonth(DateTime date) async {
    final uri = _uri('/api/expenses/month', {
      'year': '${date.year}',
      'month': '${date.month}',
      'user': settings.member,
    });
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchFinanceSummary(DateTime date) async {
    final uri = _uri('/api/expenses/finance/summary', {
      'year': '${date.year}',
      'month': '${date.month}',
      'member': settings.member,
    });
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchCollaboration(DateTime date, {int months = 6}) async {
    final uri = _uri('/api/expenses/collaboration', {
      'year': '${date.year}',
      'month': '${date.month}',
      'months': '$months',
    });
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchDebtWorkflow(DateTime date) async {
    final uri = _uri('/api/expenses/debt-workflow', {
      'year': '${date.year}',
      'month': '${date.month}',
    });
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> previewDebtImpact({
    required DateTime date,
    required String targetType,
    required int targetId,
    required double paymentAmount,
  }) async {
    final uri = _uri('/api/expenses/debt-workflow/impact');
    final response = await _postJson(uri, {
      'year': date.year,
      'month': date.month,
      'target_type': targetType,
      'target_id': targetId,
      'payment_amount': paymentAmount,
    });
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> markExpenseEntryPaid({
    required int entryId,
    required bool paid,
    double? paymentAmount,
  }) async {
    final uri = _uri('/api/expenses/entries/$entryId/paid');
    final payload = <String, dynamic>{'user': settings.member, 'paid': paid};
    if (paymentAmount != null) payload['payment_amount'] = paymentAmount;
    final response = await _postJson(uri, payload);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> deleteExpenseEntry({
    required int entryId,
    String deleteMode = 'instance',
  }) async {
    final uri = _uri('/api/expenses/entries/$entryId');
    final response = await _deleteJson(uri, {'user': settings.member, 'delete_mode': deleteMode});
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchRemindersMonth(DateTime date) async {
    final day = _fmtDate(date);
    final uri = _uri('/api/reminders', {
      'scope': 'month',
      'date': day,
      'member': settings.member,
    });
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> quickAdd({
    required String entryType,
    required String recurrence,
    required String frequency,
    required String title,
    required String category,
    required String date,
    required double amount,
  }) async {
    final uri = _uri('/api/expenses/quick-add');
    final response = await _postJson(uri, {
      'entry_type': entryType,
      'recurrence': recurrence,
      'frequency': frequency,
      'scope': 'family',
      'add_to_timeline': true,
      'user': settings.member,
      'title': title,
      'category': category,
      'date': date,
      'amount': amount,
    });
    return _decodeObject(response);
  }

  Future<List<ThreadSummary>> fetchThreads() async {
    final uri = _uri('/api/messages/threads', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['threads'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(ThreadSummary.fromJson).toList();
  }

  Future<List<ChatMessage>> fetchThreadMessages(String otherMember) async {
    final uri = _uri('/api/messages/thread/$otherMember', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['messages'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(ChatMessage.fromJson).toList();
  }

  Future<void> sendMessage({required String recipient, required String message}) async {
    final uri = _uri('/api/messages/send');
    final req = http.MultipartRequest('POST', uri)
      ..fields['sender'] = settings.member
      ..fields['recipient'] = recipient
      ..fields['message'] = message;
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 15));
    final response = await http.Response.fromStream(streamed);
    _decodeObject(response);
  }

  Future<void> markThreadRead(String otherMember) async {
    final uri = _uri('/api/messages/thread/$otherMember/read');
    final response = await _postJson(uri, {'member': settings.member});
    _decodeObject(response);
  }

  Future<List<AppNotificationItem>> fetchNotifications({bool unreadOnly = false, String eventType = ''}) async {
    final uri = _uri('/api/notifications', {
      'member': settings.member,
      if (unreadOnly) 'unread_only': 'true',
      if (eventType.trim().isNotEmpty) 'event_type': eventType.trim(),
    });
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final items = obj['items'] as List<dynamic>? ?? [];
    return items.whereType<Map<String, dynamic>>().map(AppNotificationItem.fromJson).toList();
  }

  Future<List<TimelinePost>> fetchTimeline({
    int limit = 50,
    int? beforeId,
  }) async {
    final uri = _uri('/api/timeline', {
      'limit': '$limit',
      'member': settings.member,
      if (beforeId != null) 'before_id': '$beforeId',
    });
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['posts'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(TimelinePost.fromJson).toList();
  }

  Future<TimelinePost> createTimelinePost(String summary, {String imageUrl = ''}) async {
    final uri = _uri('/api/timeline');
    final response = await _postJson(uri, {
      'actor': settings.member,
      'summary': summary,
      if (imageUrl.trim().isNotEmpty) 'image_url': imageUrl.trim(),
    });
    final obj = _decodeObject(response);
    final row = obj['post'] as Map<String, dynamic>? ?? {};
    return TimelinePost.fromJson(row);
  }

  Future<TimelinePost> updateTimelinePostImage(int postId, String imageUrl) async {
    final uri = _uri('/api/timeline/$postId/image');
    final response = await _putJson(uri, {
      'user': settings.member,
      'image_url': imageUrl.trim(),
    });
    final obj = _decodeObject(response);
    final row = obj['post'] as Map<String, dynamic>? ?? {};
    return TimelinePost.fromJson(row);
  }

  Future<Map<String, dynamic>> fetchAuthStatus() async {
    final uri = _uri('/api/auth/status');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchCurrentMember() async {
    final uri = _uri('/api/current-member');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> loginMember({required String name, String pin = ''}) async {
    final uri = _uri('/api/member-login');
    final response = await _postJson(uri, {'name': name, 'pin': pin});
    return _decodeObject(response);
  }

  Future<String> uploadTimelineImage(String filePath) async {
    final uri = _uri('/api/timeline/upload-image');
    final req = http.MultipartRequest('POST', uri)
      ..fields['user'] = settings.member
      ..files.add(await http.MultipartFile.fromPath('image', filePath));
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    final body = _decodeObject(response);
    return '${body['url'] ?? ''}'.trim();
  }

  Future<void> deleteTimelinePost(int postId) async {
    final uri = _uri('/api/timeline/$postId');
    final response = await _deleteJson(uri, {'user': settings.member});
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> toggleTimelineLike(int postId) async {
    final uri = _uri('/api/timeline/$postId/like');
    final response = await _postJson(uri, {'user': settings.member});
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> commentTimelinePost(int postId, String text) async {
    final uri = _uri('/api/timeline/$postId/comment');
    final response = await _postJson(uri, {'user': settings.member, 'text': text});
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> shareTimelinePost(int postId) async {
    final uri = _uri('/api/timeline/$postId/share');
    final response = await _postJson(uri, {'user': settings.member, 'channel': 'android'});
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchRoutines() async {
    final uri = _uri('/api/routines', {'member': settings.member});
    final response = await _get(uri);
    if (response.statusCode == 404) {
      return const {'ok': false, 'routines': <String, dynamic>{}};
    }
    final payload = _decodeMapLoose(response);

    Map<String, dynamic> routines = <String, dynamic>{};
    final nested = payload['routines'];
    if (nested is Map) {
      routines = Map<String, dynamic>.from(nested);
    } else if (payload['bedtime'] is Map || payload['bath'] is Map) {
      routines = {
        if (payload['bedtime'] is Map) 'bedtime': Map<String, dynamic>.from(payload['bedtime'] as Map),
        if (payload['bath'] is Map) 'bath': Map<String, dynamic>.from(payload['bath'] as Map),
      };
    } else {
      final data = payload['data'];
      if (data is Map && data['routines'] is Map) {
        routines = Map<String, dynamic>.from(data['routines'] as Map);
      }
    }

    return {
      'ok': payload['ok'] != false,
      'routines': routines,
    };
  }

  Future<Map<String, dynamic>> completeBathRoutine() async {
    final uri = _uri('/api/routines/bath/complete', {'member': settings.member});
    final response = await _postJson(uri, {'completed': true, 'member': settings.member});
    return _decodeObject(response);
  }

  Future<void> markNotificationRead(int notificationId) async {
    final uri = _uri('/api/notifications/$notificationId/read');
    final response = await _postJson(uri, {'member': settings.member});
    _decodeObject(response);
  }

  Future<void> markAllNotificationsRead({String eventType = ''}) async {
    final uri = _uri('/api/notifications/mark-read');
    final response = await _postJson(uri, {
      'member': settings.member,
      if (eventType.trim().isNotEmpty) 'event_type': eventType.trim(),
    });
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchCalendarShareLink() async {
    final uri = _uri('/api/calendar/share-link');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> createCalendarShareLink() async {
    final uri = _uri('/api/calendar/share-link');
    final response = await _postJson(uri, {});
    return _decodeObject(response);
  }

  Future<List<CalendarMember>> fetchCalendarMembers() async {
    final uri = _uri('/api/calendar/members');
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final items = obj['members'] as List<dynamic>? ?? [];
    return items.whereType<Map<String, dynamic>>().map(CalendarMember.fromJson).toList();
  }

  Future<List<CalendarEvent>> fetchCalendarEvents({required DateTime start, required DateTime end, String? memberFilter}) async {
    final uri = _uri('/api/calendar/events', {
      'start': _fmtDate(start),
      'end': _fmtDate(end),
      if (memberFilter != null && memberFilter.isNotEmpty) 'member': memberFilter,
    });
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final items = obj['events'] as List<dynamic>? ?? [];
    return items.whereType<Map<String, dynamic>>().map(CalendarEvent.fromJson).toList();
  }

  Future<CalendarEvent> createCalendarEvent(CalendarEventDraft draft, {String? photoPath}) async {
    final uri = _uri('/api/calendar/events');
    if (photoPath == null || photoPath.trim().isEmpty) {
      final response = await _postJson(uri, draft.toApiJson());
      final obj = _decodeObject(response);
      return CalendarEvent.fromJson((obj['event'] as Map<String, dynamic>? ?? {}));
    }

    final req = http.MultipartRequest('POST', uri);
    _applyCalendarDraftToMultipart(req, draft);
    req.files.add(await http.MultipartFile.fromPath('photo', photoPath.trim()));
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    final obj = _decodeObject(response);
    return CalendarEvent.fromJson((obj['event'] as Map<String, dynamic>? ?? {}));
  }

  Future<CalendarEvent> updateCalendarEvent(
    int eventId,
    CalendarEventDraft draft, {
    String? photoPath,
    String? updateScope,
  }) async {
    final uri = _uri('/api/calendar/events/$eventId');
    if (photoPath == null || photoPath.trim().isEmpty) {
      final payload = draft.toApiJson();
      if (updateScope != null && updateScope.isNotEmpty) {
        payload['update_scope'] = updateScope;
      }
      final response = await _putJson(uri, payload);
      final obj = _decodeObject(response);
      return CalendarEvent.fromJson((obj['event'] as Map<String, dynamic>? ?? {}));
    }

    final req = http.MultipartRequest('PUT', uri);
    _applyCalendarDraftToMultipart(req, draft);
    if (updateScope != null && updateScope.isNotEmpty) {
      req.fields['update_scope'] = updateScope;
    }
    req.files.add(await http.MultipartFile.fromPath('photo', photoPath.trim()));
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    final obj = _decodeObject(response);
    return CalendarEvent.fromJson((obj['event'] as Map<String, dynamic>? ?? {}));
  }

  void _applyCalendarDraftToMultipart(http.MultipartRequest req, CalendarEventDraft draft) {
    final payload = draft.toApiJson();
    payload.forEach((key, value) {
      if (value == null) return;
      if (value is List) {
        req.fields[key] = jsonEncode(value);
        return;
      }
      if (value is bool) {
        req.fields[key] = value ? 'true' : 'false';
        return;
      }
      req.fields[key] = '$value';
    });
  }

  Future<List<MealPlannerEntry>> fetchMealEntries(DateTime date) async {
    final uri = _uri('/api/meal-planner/entries', {
      'member': settings.member,
      'year': '${date.year}',
      'month': '${date.month}',
    });
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['entries'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(MealPlannerEntry.fromJson).toList();
  }

  Future<List<Map<String, dynamic>>> fetchMealLibrary() async {
    final uri = _uri('/api/meal-planner/library', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<Map<String, dynamic>> upsertMealEntry({
    int? id,
    required DateTime date,
    required String mealType,
    required String title,
    required String description,
    required String visibility,
    required bool requirePin,
    required String audience,
    required List<String> allowedMembers,
  }) async {
    final uri = _uri('/api/meal-planner/entries');
    final response = await _postJson(uri, {
      if (id != null) 'id': id,
      'member': settings.member,
      'date': _fmtDate(date),
      'meal_type': mealType,
      'title': title,
      'description': description,
      'visibility': visibility,
      'require_pin': requirePin,
      'audience': audience,
      'allowed_members': allowedMembers,
    });
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> deleteMealEntry(int entryId) async {
    final uri = _uri('/api/meal-planner/entries/$entryId');
    final response = await _deleteJson(uri, {'member': settings.member});
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> unlockMealEntry({required int entryId, required String pin}) async {
    final uri = _uri('/api/meal-planner/entries/$entryId/unlock');
    final response = await _postJson(uri, {'member': settings.member, 'pin': pin});
    return _decodeObject(response);
  }

  Future<void> deleteCalendarEvent(int eventId) async {
    final uri = _uri('/api/calendar/events/$eventId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchPendingPolls() async {
    final uri = _uri('/api/polls/pending', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<List<Map<String, dynamic>>> fetchMyPolls() async {
    final uri = _uri('/api/polls/mine', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> createPoll({required String question, required List<String> options}) async {
    final uri = _uri('/api/polls/create');
    final response = await _postJson(uri, {
      'creator': settings.member,
      'question': question,
      'options': options,
    });
    _decodeObject(response);
  }

  Future<void> votePoll({required int pollId, String? selectedOption, String? responseText}) async {
    final uri = _uri('/api/polls/$pollId/vote');
    final response = await _postJson(uri, {
      'member': settings.member,
      'selected_option': selectedOption,
      'response_text': responseText,
    });
    _decodeObject(response);
  }

  Future<List<SharedCloudFile>> fetchSharedCloudFiles() async {
    final uri = _uri('/api/files');
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(SharedCloudFile.fromJson).toList();
  }

  Future<int> uploadSharedCloudFiles(List<PlatformFile> files) async {
    final uri = _uri('/api/files/upload');
    final req = http.MultipartRequest('POST', uri)..fields['creator'] = settings.member;
    for (final file in files) {
      if (file.bytes != null) {
        req.files.add(http.MultipartFile.fromBytes('files', file.bytes!, filename: file.name));
      } else if (file.path != null && file.path!.isNotEmpty) {
        req.files.add(await http.MultipartFile.fromPath('files', file.path!, filename: file.name));
      }
    }
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    final obj = _decodeObject(response);
    return _toInt(obj['uploaded_count']);
  }

  Future<void> deleteSharedCloudFile(int fileId) async {
    final uri = _uri('/api/files/$fileId');
    final response = await _deleteJson(uri, {'user': settings.member});
    _decodeObject(response);
  }

  Future<List<PhotoAlbumSummary>> fetchPhotoAlbums() async {
    final uri = _uri('/api/photos/albums', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(PhotoAlbumSummary.fromJson).toList();
  }

  Future<List<PhotoAssetItem>> fetchPhotoAssets({int? albumId, String scope = 'all'}) async {
    final uri = _uri('/api/photos/assets', {
      'member': settings.member,
      if (scope.trim().isNotEmpty) 'scope': scope.trim(),
      if (albumId != null) 'album_id': '$albumId',
    });
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(PhotoAssetItem.fromJson).toList();
  }

  Future<void> createPhotoAlbum({required String name, String description = '', String visibility = 'private'}) async {
    final uri = _uri('/api/photos/albums');
    final response = await _postJson(uri, {
      'creator': settings.member,
      'name': name,
      'description': description,
      'visibility': visibility,
    });
    _decodeObject(response);
  }

  Future<int> uploadPhotoAssets(List<PlatformFile> files, {String visibility = 'private', int? albumId}) async {
    final uri = _uri('/api/photos/upload');
    final req = http.MultipartRequest('POST', uri)
      ..fields['creator'] = settings.member
      ..fields['visibility'] = visibility;
    if (albumId != null) req.fields['album_id'] = '$albumId';
    for (final file in files) {
      if (file.bytes != null) {
        req.files.add(http.MultipartFile.fromBytes('files', file.bytes!, filename: file.name));
      } else if (file.path != null && file.path!.isNotEmpty) {
        req.files.add(await http.MultipartFile.fromPath('files', file.path!, filename: file.name));
      }
    }
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 45));
    final response = await http.Response.fromStream(streamed);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.length;
  }

  Future<void> deletePhotoAsset(int assetId) async {
    final uri = _uri('/api/photos/assets/batch');
    final response = await _postJson(uri, {
      'action': 'delete',
      'asset_ids': [assetId],
    });
    _decodeObject(response);
  }

  Future<List<PhotoWatchFolderItem>> fetchPhotoWatchFolders() async {
    final uri = _uri('/api/photos/watch-folders', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().map(PhotoWatchFolderItem.fromJson).toList();
  }

  Future<void> createPhotoWatchFolder({required String folderUri, required String scope, int? targetAlbumId}) async {
    final uri = _uri('/api/photos/watch-folders');
    final response = await _postJson(uri, {
      'member': settings.member,
      'folder_uri': folderUri,
      'scope': scope,
      if (targetAlbumId != null) 'target_album_id': targetAlbumId,
      'platform': Platform.isAndroid ? 'android' : (Platform.isIOS ? 'ios' : 'mobile'),
    });
    _decodeObject(response);
  }

  Future<void> updatePhotoWatchFolder({required int watchId, bool? isActive, String? scope, int? targetAlbumId}) async {
    final uri = _uri('/api/photos/watch-folders/$watchId');
    final payload = <String, dynamic>{};
    if (isActive != null) payload['is_active'] = isActive;
    if (scope != null && scope.trim().isNotEmpty) payload['scope'] = scope.trim();
    if (targetAlbumId != null) payload['target_album_id'] = targetAlbumId;
    final response = await _requestWithFailover(
      uri: uri,
      seconds: 15,
      sender: (nextUri) => _httpClient.patch(nextUri, headers: {'Content-Type': 'application/json'}, body: jsonEncode(payload)),
    );
    _decodeObject(response);
  }

  Future<void> deletePhotoWatchFolder(int watchId) async {
    final uri = _uri('/api/photos/watch-folders/$watchId');
    final response = await _requestWithFailover(
      uri: uri,
      seconds: 15,
      sender: (nextUri) => _httpClient.delete(nextUri),
    );
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchShoppingLists() async {
    final uri = _uri('/api/shopping/lists');
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['lists'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<List<Map<String, dynamic>>> fetchShoppingItems(int listId) async {
    final uri = _uri('/api/shopping/items', {'list_id': '$listId'});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<Map<String, dynamic>> createShoppingList({
    required String name,
    String color = '#3B82F6',
    String listType = 'standard',
  }) async {
    final uri = _uri('/api/shopping/lists');
    final response = await _postJson(uri, {
      'name': name,
      'color': color,
      'list_type': listType,
      'creator': settings.member,
    });
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> updateShoppingList({
    required int listId,
    String? name,
    String? color,
    String? listType,
  }) async {
    final uri = _uri('/api/shopping/lists/$listId');
    final payload = <String, dynamic>{};
    if (name != null) payload['name'] = name;
    if (color != null) payload['color'] = color;
    if (listType != null) payload['list_type'] = listType;
    final response = await _putJson(uri, payload);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> deleteShoppingList({
    required int listId,
    String strategy = 'move',
    int? targetListId,
  }) async {
    final uri = _uri('/api/shopping/lists/$listId');
    final response = await _deleteJson(uri, {
      'strategy': strategy,
      if (targetListId != null) 'target_list_id': targetListId,
    });
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> createShoppingItem({
    required int listId,
    required String item,
    double? price,
    double? quantity,
    String? store,
    String? category,
    List<String>? tags,
    int? repoItemId,
    String? purchasePhotoFilename,
    String? sizeRequirements,
    String? purchaseCategory,
    String? planningNotes,
    double? targetPrice,
    double? budgetCeiling,
  }) async {
    final uri = _uri('/api/shopping/items');
    final cleanTags = (tags ?? const <String>[])
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList();
    final response = await _postJson(uri, {
      'item': item,
      'creator': settings.member,
      'list_id': listId,
      if (price != null) 'price': price,
      if (quantity != null) 'quantity': quantity,
      if (store != null && store.trim().isNotEmpty) 'store': store.trim(),
      if (category != null && category.trim().isNotEmpty) 'category': category.trim(),
      if (cleanTags.isNotEmpty) 'tags': cleanTags,
      if (repoItemId != null) 'repo_item_id': repoItemId,
      if (purchasePhotoFilename != null && purchasePhotoFilename.trim().isNotEmpty) 'purchase_photo_filename': purchasePhotoFilename.trim(),
      if (sizeRequirements != null && sizeRequirements.trim().isNotEmpty) 'size_requirements': sizeRequirements.trim(),
      if (purchaseCategory != null && purchaseCategory.trim().isNotEmpty) 'purchase_category': purchaseCategory.trim(),
      if (planningNotes != null && planningNotes.trim().isNotEmpty) 'planning_notes': planningNotes.trim(),
      if (targetPrice != null) 'target_price': targetPrice,
      if (budgetCeiling != null) 'budget_ceiling': budgetCeiling,
    });
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> updateShoppingItem({
    required int itemId,
    bool? checked,
    String? itemName,
    double? price,
    double? quantity,
    int? listId,
    List<String>? tags,
    String? purchaseCategory,
    String? planningNotes,
    double? targetPrice,
    double? budgetCeiling,
    String? sizeRequirements,
    String? purchasePhotoFilename,
  }) async {
    final uri = _uri('/api/shopping/$itemId');
    final payload = <String, dynamic>{};
    if (checked != null) payload['checked'] = checked;
    if (itemName != null) payload['item'] = itemName;
    if (price != null) payload['price'] = price;
    if (quantity != null) payload['quantity'] = quantity;
    if (listId != null) payload['list_id'] = listId;
    if (tags != null) {
      payload['tags'] = tags.map((e) => e.trim()).where((e) => e.isNotEmpty).toList();
    }
    if (purchaseCategory != null) payload['purchase_category'] = purchaseCategory;
    if (planningNotes != null) payload['planning_notes'] = planningNotes;
    if (targetPrice != null) payload['target_price'] = targetPrice;
    if (budgetCeiling != null) payload['budget_ceiling'] = budgetCeiling;
    if (sizeRequirements != null) payload['size_requirements'] = sizeRequirements;
    if (purchasePhotoFilename != null) payload['purchase_photo_filename'] = purchasePhotoFilename;
    final response = await _putJson(uri, payload);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> uploadShoppingPurchasePhoto(String photoPath) async {
    final uri = _uri('/api/shopping/purchase-photo');
    final req = http.MultipartRequest('POST', uri);
    req.files.add(await http.MultipartFile.fromPath('photo', photoPath));
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    return _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchShoppingRepo({String? query, String? store}) async {
    final uri = _uri('/api/shopping/repo', {
      if (query != null && query.trim().isNotEmpty) 'q': query.trim(),
      if (store != null && store.trim().isNotEmpty) 'store': store.trim(),
    });
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? const [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<Map<String, dynamic>> createShoppingRepoItem({
    required String name,
    String? store,
    String? category,
    double? price,
  }) async {
    final uri = _uri('/api/shopping/repo');
    final response = await _postJson(uri, {
      'name': name,
      if (store != null && store.trim().isNotEmpty) 'store': store.trim(),
      if (category != null && category.trim().isNotEmpty) 'category': category.trim(),
      if (price != null) 'price': price,
      'created_by': settings.member,
    });
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> updateShoppingRepoItem({
    required int repoId,
    String? name,
    String? store,
    String? category,
    double? price,
  }) async {
    final uri = _uri('/api/shopping/repo/$repoId');
    final payload = <String, dynamic>{};
    if (name != null) payload['name'] = name;
    if (store != null) payload['store'] = store;
    if (category != null) payload['category'] = category;
    if (price != null) payload['price'] = price;
    final response = await _putJson(uri, payload);
    return _decodeObject(response);
  }

  Future<void> deleteShoppingRepoItem(int repoId) async {
    final uri = _uri('/api/shopping/repo/$repoId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 20));
    _decodeObject(response);
  }

  Future<void> deleteShoppingItem(int itemId) async {
    final uri = _uri('/api/shopping/items/$itemId', {'user': settings.member});
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchChores() async {
    final uri = _uri('/api/chores');
    final response = await _get(uri);
    final body = jsonDecode(response.body);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Failed to load chores (${response.statusCode})');
    }
    if (body is! List) {
      throw Exception('Unexpected chores response');
    }
    return body.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> createChore(String description) async {
    final uri = _uri('/api/chores');
    final response = await _postJson(uri, {
      'description': description,
      'creator': settings.member,
    });
    _decodeObject(response);
  }

  Future<void> setChoreDone({required int choreId, required bool done}) async {
    final uri = _uri('/api/chores/$choreId/done');
    final response = await _postJson(uri, {'done': done});
    _decodeObject(response);
  }

  Future<void> deleteChore(int choreId) async {
    final uri = _uri('/api/chores/$choreId', {'user': settings.member});
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchWorkspacesOverview() async {
    final uri = _uri('/api/workspaces/overview');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<void> createWorkspaceProject(String name) async {
    final uri = _uri('/api/workspaces/wizard/create');
    final response = await _postJson(uri, {'entity_type': 'project', 'name': name});
    _decodeObject(response);
  }

  Future<void> createWorkspaceSpace(String name) async {
    final uri = _uri('/api/workspaces/wizard/create');
    final response = await _postJson(uri, {'entity_type': 'space', 'name': name});
    _decodeObject(response);
  }

  Future<void> createWorkspaceEntity({
    required String entityType,
    required String name,
    String description = '',
    int? templateId,
    String visibility = 'family',
    List<String> allowedMembers = const <String>[],
  }) async {
    final uri = _uri('/api/workspaces/wizard/create');
    final response = await _postJson(uri, {
      'entity_type': entityType,
      'name': name,
      'description': description,
      'template_id': templateId,
      'visibility': visibility,
      'allowed_members': allowedMembers,
    });
    _decodeObject(response);
  }

  Future<void> createWorkspaceTemplate({required String name, required String templateType, String description = '', Map<String, dynamic> config = const {}}) async {
    final uri = _uri('/api/workspaces/templates');
    final response = await _postJson(uri, {
      'name': name,
      'template_type': templateType,
      'description': description,
      'config': config,
    });
    _decodeObject(response);
  }

  Future<void> deleteWorkspaceTemplate(int templateId) async {
    final uri = _uri('/api/workspaces/templates/$templateId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<void> updateWorkspaceProject({
    required int projectId,
    required String name,
    String description = '',
    String visibility = 'family',
    List<String> allowedMembers = const <String>[],
    bool archived = false,
    int? linkedSpaceId,
  }) async {
    final uri = _uri('/api/workspaces/projects/$projectId');
    final response = await _putJson(uri, {
      'name': name,
      'description': description,
      'visibility': visibility,
      'allowed_members': allowedMembers,
      'archived': archived,
      'linked_space_id': linkedSpaceId,
    });
    _decodeObject(response);
  }

  Future<void> deleteWorkspaceProject(int projectId) async {
    final uri = _uri('/api/workspaces/projects/$projectId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<void> updateWorkspaceSpace({
    required int spaceId,
    required String name,
    String description = '',
    String visibility = 'family',
    List<String> allowedMembers = const <String>[],
    bool archived = false,
    int? linkedProjectId,
  }) async {
    final uri = _uri('/api/workspaces/spaces/$spaceId');
    final response = await _putJson(uri, {
      'name': name,
      'description': description,
      'visibility': visibility,
      'allowed_members': allowedMembers,
      'archived': archived,
      'linked_project_id': linkedProjectId,
    });
    _decodeObject(response);
  }

  Future<void> deleteWorkspaceSpace(int spaceId) async {
    final uri = _uri('/api/workspaces/spaces/$spaceId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchRecipes() async {
    final uri = _uri('/api/recipes');
    final response = await _get(uri);
    final body = jsonDecode(response.body);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Failed to load recipes (${response.statusCode})');
    }
    if (body is! List) {
      throw Exception('Unexpected recipes response');
    }
    return body.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> saveRecipe({
    int? recipeId,
    required String title,
    required String ingredients,
    required String instructions,
    String link = '',
  }) async {
    final uri = _uri('/api/recipes/autosave');
    final response = await _postJson(uri, {
      'recipe_id': recipeId,
      'creator': settings.member,
      'title': title,
      'ingredients': ingredients,
      'instructions': instructions,
      'link': link,
    });
    _decodeObject(response);
  }

  Future<void> deleteRecipe(int recipeId) async {
    final uri = _uri('/api/recipes/$recipeId');
    final response = await _deleteJson(uri, {'user': settings.member});
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> parseRecipeLink(String url) async {
    final uri = _uri('/api/recipes/parse-link');
    final response = await _postJson(uri, {'url': url}, seconds: 20);
    return _decodeObject(response);
  }

  Future<void> addRecipeIngredientsToShopping(int recipeId) async {
    final uri = _uri('/api/recipes/$recipeId/add-ingredients-to-shopping');
    final response = await _postJson(uri, {'creator': settings.member});
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchExpiryItems() async {
    final uri = _uri('/api/expiry/items');
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> saveExpiryItem({required String name, required String expiryDate}) async {
    final uri = _uri('/api/expiry/items');
    final response = await _postJson(uri, {
      'member': settings.member,
      'name': name,
      'expiry_date': expiryDate,
    });
    _decodeObject(response);
  }

  Future<void> deleteExpiryItem(int itemId) async {
    final uri = _uri('/api/expiry/items/$itemId');
    final response = await _deleteJson(uri, {'member': settings.member});
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchExpiryPendingReminders() async {
    final uri = _uri('/api/expiry/reminders/pending', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> ackExpiryReminder(int reminderId) async {
    final uri = _uri('/api/expiry/reminders/$reminderId/ack');
    final response = await _postJson(uri, {'member': settings.member});
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchQrHistory() async {
    final uri = _uri('/api/qr');
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<Map<String, dynamic>> createQr(String text) async {
    final uri = _uri('/api/qr');
    final response = await _postJson(uri, {'text': text, 'creator': settings.member});
    final obj = _decodeObject(response);
    return (obj['item'] as Map<String, dynamic>? ?? {});
  }

  Future<void> deleteQr(int qrId) async {
    final uri = _uri('/api/qr/$qrId', {'user': settings.member});
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchHubSettings() async {
    final uri = _uri('/api/settings');
    final response = await _get(uri);
    final body = jsonDecode(response.body);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Failed to load settings (${response.statusCode})');
    }
    if (body is! Map<String, dynamic>) {
      throw Exception('Unexpected settings response');
    }
    return body;
  }

  Future<void> updateHubName(String hubName) async {
    final uri = _uri('/api/settings');
    final response = await _postJson(uri, {'hub_name': hubName});
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchFamilyMembers() async {
    final uri = _uri('/api/family-members');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchCurrentMemberStatus() async {
    final uri = _uri('/api/member-status/current', {'member': settings.member});
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<void> saveCurrentMemberStatus(String status) async {
    final uri = _uri('/api/member-status');
    final response = await _postJson(uri, {'member': settings.member, 'status': status});
    _decodeObject(response);
  }

  Future<void> clearCurrentMemberStatus() async {
    final uri = _uri('/api/member-status');
    final response = await _deleteJson(uri, {'member': settings.member});
    _decodeObject(response);
  }

  Future<void> sendFamilyNotice({
    required String message,
    required List<String> channels,
    bool requireAck = true,
  }) async {
    final uri = _uri('/api/family-notices/send');
    final response = await _postJson(uri, {
      'sender': settings.member,
      'message': message,
      'channels': channels,
      'require_ack': requireAck,
    });
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> fetchPendingFamilyNotices() async {
    final uri = _uri('/api/family-notices/pending', {'member': settings.member});
    final response = await _get(uri);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> acknowledgeFamilyNotice(int noticeId) async {
    final uri = _uri('/api/family-notices/$noticeId/ack');
    final response = await _postJson(uri, {'member': settings.member});
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchWeatherLocation() async {
    try {
      final uri = _uri('/api/weather/location');
      final response = await _get(uri);
      final remote = _decodeMapLoose(response);
      final lat = (remote['latitude'] as num?)?.toDouble();
      final lon = (remote['longitude'] as num?)?.toDouble();
      if (lat != null && lon != null) {
        await _saveLocalWeatherLocation(
          latitude: lat,
          longitude: lon,
          locationName: '${remote['location_name'] ?? ''}',
          useGpsLocation: remote['use_gps_location'] == true,
        );
        return remote;
      }
    } catch (_) {}
    return _loadLocalWeatherLocation();
  }

  Future<void> saveWeatherLocation({
    required double latitude,
    required double longitude,
    String locationName = '',
    bool useGpsLocation = false,
  }) async {
    await _saveLocalWeatherLocation(
      latitude: latitude,
      longitude: longitude,
      locationName: locationName,
      useGpsLocation: useGpsLocation,
    );
    try {
      final uri = _uri('/api/weather/location');
      final response = await _postJson(uri, {
        'latitude': latitude,
        'longitude': longitude,
        'location_name': locationName,
        'use_gps_location': useGpsLocation,
      });
      _decodeMapLoose(response);
    } catch (_) {}
  }

  Future<Map<String, dynamic>> fetchWeather({
    required double latitude,
    required double longitude,
    String locationName = '',
  }) async {
    final uri = _uri('/api/weather', {
      'latitude': '$latitude',
      'longitude': '$longitude',
      if (locationName.trim().isNotEmpty) 'location_name': locationName.trim(),
    });
    final response = await _get(uri, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<Map<String, dynamic>> fetchLocationName({
    required double latitude,
    required double longitude,
  }) async {
    final uri = _uri('/api/weather/location-name', {
      'latitude': '$latitude',
      'longitude': '$longitude',
    });
    final response = await _get(uri, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<void> addFamilyMember({required String name, String role = 'standard'}) async {
    final uri = _uri('/api/family-members');
    final response = await _postJson(uri, {'name': name, 'role': role});
    _decodeObject(response);
  }

  Future<void> addProjectColumn({required int projectId, required String name}) async {
    final uri = _uri('/api/workspaces/projects/$projectId/columns');
    final response = await _postJson(uri, {'name': name});
    _decodeObject(response);
  }

  Future<void> createProjectCard({
    required int projectId,
    required int columnId,
    required String title,
    String description = '',
    String priority = 'normal',
  }) async {
    final uri = _uri('/api/workspaces/projects/$projectId/cards');
    final response = await _postJson(uri, {
      'column_id': columnId,
      'title': title,
      'description': description,
      'priority': priority,
    });
    _decodeObject(response);
  }

  Future<void> moveProjectCard({required int cardId, required int columnId}) async {
    final uri = _uri('/api/workspaces/cards/$cardId/move');
    final response = await _postJson(uri, {'column_id': columnId});
    _decodeObject(response);
  }

  Future<void> deleteProjectCard(int cardId) async {
    final uri = _uri('/api/workspaces/cards/$cardId/delete');
    final response = await _postJson(uri, {});
    _decodeObject(response);
  }

  Future<void> createSpacePage({required int spaceId, required String title, required String content}) async {
    final uri = _uri('/api/workspaces/spaces/$spaceId/pages');
    final req = http.MultipartRequest('POST', uri)
      ..fields['title'] = title
      ..fields['content'] = content;
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 20));
    final response = await http.Response.fromStream(streamed);
    _decodeObject(response);
  }

  Future<void> deleteSpacePage(int pageId) async {
    final uri = _uri('/api/workspaces/pages/$pageId/delete');
    final response = await _postJson(uri, {});
    _decodeObject(response);
  }

  Future<List<Map<String, dynamic>>> listBudgets() async {
    final uri = _uri('/expenses/budgets');
    final response = await _get(uri, seconds: 20);
    final obj = _decodeMapLoose(response);
    final rows = obj['budgets'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<Map<String, dynamic>> getBudgetDetail(int budgetId) async {
    final uri = _uri('/expenses/budgets/$budgetId');
    final response = await _get(uri, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<void> createBudget({required String name, required int month, required int year, required double totalAmount}) async {
    final uri = _uri('/expenses/budgets');
    final response = await _postJson(uri, {
      'name': name,
      'month': month,
      'year': year,
      'total_amount': totalAmount,
      'created_by': settings.member,
    }, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> addBudgetCategory({required int budgetId, required String categoryName, required double allocatedAmount}) async {
    final uri = _uri('/expenses/budgets/$budgetId/categories');
    final response = await _postJson(uri, {'category_name': categoryName, 'allocated_amount': allocatedAmount}, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<List<Map<String, dynamic>>> listDebts() async {
    final uri = _uri('/expenses/debts');
    final response = await _get(uri, seconds: 20);
    final obj = _decodeMapLoose(response);
    final rows = obj['debts'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> createDebt({
    required String description,
    required String debtor,
    required String creditor,
    required double initialAmount,
    double interestRate = 0,
  }) async {
    final uri = _uri('/expenses/debts');
    final response = await _postJson(uri, {
      'description': description,
      'debtor': debtor,
      'creditor': creditor,
      'initial_amount': initialAmount,
      'interest_rate': interestRate,
    }, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> recordDebtPayment({required int debtId, required double amountPaid, DateTime? paymentDate}) async {
    final uri = _uri('/expenses/debts/$debtId/payments');
    final response = await _postJson(uri, {
      'payment_date': _fmtDate(paymentDate ?? DateTime.now()),
      'amount_paid': amountPaid,
      'principal': amountPaid,
    }, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> deleteDebt(int debtId) async {
    final uri = _uri('/expenses/debts/$debtId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 20));
    _decodeMapLoose(response);
  }

  Future<List<Map<String, dynamic>>> listGoals() async {
    final uri = _uri('/expenses/goals');
    final response = await _get(uri, seconds: 20);
    final obj = _decodeMapLoose(response);
    final rows = obj['goals'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> createGoal({required String name, required double targetAmount, String category = 'savings'}) async {
    final uri = _uri('/expenses/goals');
    final response = await _postJson(uri, {
      'name': name,
      'target_amount': targetAmount,
      'category': category,
      'created_by': settings.member,
    }, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> contributeGoal({required int goalId, required double amount}) async {
    final uri = _uri('/expenses/goals/$goalId/contributions');
    final response = await _postJson(uri, {
      'amount': amount,
      'contributor': settings.member,
    }, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<List<Map<String, dynamic>>> listSharedExpenses({DateTime? monthDate}) async {
    final query = <String, String>{};
    if (monthDate != null) {
      query['month'] = '${monthDate.month}';
      query['year'] = '${monthDate.year}';
    }
    final uri = _uri('/expenses/shared-expenses', query.isEmpty ? null : query);
    final response = await _get(uri, seconds: 20);
    final obj = _decodeMapLoose(response);
    final rows = obj['expenses'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> createSharedExpense({
    required String description,
    required double totalAmount,
    required String paidBy,
    required DateTime date,
    required String category,
    required List<Map<String, dynamic>> participants,
  }) async {
    final uri = _uri('/expenses/shared-expenses');
    final response = await _postJson(uri, {
      'description': description,
      'total_amount': totalAmount,
      'paid_by': paidBy,
      'date': _fmtDate(date),
      'category': category,
      'participants': participants,
      'track_in_budgets': true,
    }, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> setSharedExpenseSettled(int expenseId, bool settled) async {
    final uri = _uri('/expenses/shared-expenses/$expenseId');
    final response = await _putJson(uri, {'user': settings.member, 'settled': settled}, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> deleteSharedExpense(int expenseId) async {
    final uri = _uri('/expenses/shared-expenses/$expenseId');
    final response = await _deleteJson(uri, {'user': settings.member}, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> uploadSharedExpenseReceipt(int expenseId, PlatformFile file) async {
    final uri = _uri('/expenses/shared-expenses/$expenseId/receipt');
    final req = http.MultipartRequest('POST', uri)..fields['user'] = settings.member;
    if (file.bytes != null) {
      req.files.add(http.MultipartFile.fromBytes('receipt', file.bytes!, filename: file.name));
    } else if (file.path != null && file.path!.isNotEmpty) {
      req.files.add(await http.MultipartFile.fromPath('receipt', file.path!, filename: file.name));
    } else {
      throw Exception('No file data');
    }
    final streamed = await _httpClient.send(req).timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    _decodeMapLoose(response);
  }

  Future<Map<String, dynamic>> fetchAnalyticsSummary() async {
    final uri = _uri('/expenses/analytics/summary');
    final response = await _get(uri, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<Map<String, dynamic>> fetchPlaidAccountsSummary() async {
    final uri = _uri('/expenses/accounts/summary');
    final response = await _get(uri, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<List<Map<String, dynamic>>> listPlaidAccounts() async {
    final uri = _uri('/expenses/plaid/accounts');
    final response = await _get(uri, seconds: 20);
    final obj = _decodeMapLoose(response);
    final rows = obj['accounts'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> syncPlaidAccount(int accountId) async {
    final uri = _uri('/expenses/plaid/accounts/$accountId/sync');
    final response = await _postJson(uri, {'user': settings.member}, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> disconnectPlaidAccount(int accountId) async {
    final uri = _uri('/expenses/plaid/accounts/$accountId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 20));
    _decodeMapLoose(response);
  }

  Future<Map<String, dynamic>> createPlaidLinkToken() async {
    final uri = _uri('/expenses/plaid/link-token');
    final response = await _postJson(uri, {'user': settings.member}, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<Map<String, dynamic>> exchangePlaidPublicToken(String publicToken) async {
    final uri = _uri('/expenses/plaid/exchange-token');
    final response = await _postJson(uri, {'public_token': publicToken, 'user': settings.member}, seconds: 20);
    return _decodeMapLoose(response);
  }

  Future<List<Map<String, dynamic>>> fetchPendingIncomeConfirmations() async {
    final uri = _uri('/api/expenses/income/pending-confirmations', {'member': settings.member});
    final response = await _get(uri, seconds: 20);
    final obj = _decodeObject(response);
    final rows = obj['items'] as List<dynamic>? ?? [];
    return rows.whereType<Map<String, dynamic>>().toList();
  }

  Future<void> confirmIncomeEntry({required int incomeId, double? amount}) async {
    final uri = _uri('/expenses/income/confirm/$incomeId');
    final payload = <String, dynamic>{'user': settings.member};
    if (amount != null) payload['amount'] = amount;
    final response = await _postJson(uri, payload, seconds: 20);
    _decodeMapLoose(response);
  }

  Future<void> updateFamilyMember({
    required int memberId,
    required String name,
    required String role,
    String? email,
    String? pin,
  }) async {
    final uri = _uri('/api/family-members/$memberId');
    final payload = <String, dynamic>{
      'name': name,
      'role': role,
      if (email != null) 'email': email,
      if (pin != null) 'pin': pin,
    };
    final response = await _putJson(uri, payload);
    _decodeObject(response);
  }

  Future<void> deleteFamilyMember(int memberId) async {
    final uri = _uri('/api/family-members/$memberId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<void> resetMemberPin(int memberId) async {
    final uri = _uri('/api/member/$memberId/reset-pin');
    final response = await _postJson(uri, {});
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchMenuVisibility() async {
    final uri = _uri('/api/menu-visibility');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<void> setMenuVisibility({required String menuItem, required bool visible}) async {
    final uri = _uri('/api/menu-visibility/$menuItem');
    final response = await _putJson(uri, {'visible': visible});
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> fetchPermissionSchemes() async {
    final uri = _uri('/api/permission-schemes');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<void> updateAppPermissionScheme({required String moduleKey, required String visibility, required List<String> allowedMembers}) async {
    final uri = _uri('/api/permission-schemes/app/$moduleKey');
    final response = await _putJson(uri, {'visibility': visibility, 'allowed_members': allowedMembers});
    _decodeObject(response);
  }

  Future<void> updateProjectPermissionScheme({required int projectId, required String visibility, required List<String> allowedMembers}) async {
    final uri = _uri('/api/permission-schemes/project/$projectId');
    final response = await _putJson(uri, {'visibility': visibility, 'allowed_members': allowedMembers});
    _decodeObject(response);
  }

  Future<void> updateSpacePermissionScheme({required int spaceId, required String visibility, required List<String> allowedMembers}) async {
    final uri = _uri('/api/permission-schemes/space/$spaceId');
    final response = await _putJson(uri, {'visibility': visibility, 'allowed_members': allowedMembers});
    _decodeObject(response);
  }

  Future<void> createPermissionGroup({required String name, String description = ''}) async {
    final uri = _uri('/api/permission-groups');
    final response = await _postJson(uri, {'name': name, 'description': description});
    _decodeObject(response);
  }

  Future<void> setPermissionGroupMembers({required int groupId, required List<String> members}) async {
    final uri = _uri('/api/permission-groups/$groupId/members');
    final response = await _putJson(uri, {'members': members});
    _decodeObject(response);
  }

  Future<void> deletePermissionGroup(int groupId) async {
    final uri = _uri('/api/permission-groups/$groupId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<void> upsertPermissionGrant({
    required String resourceType,
    String? resourceKey,
    int? resourceId,
    required String subjectType,
    required String subjectValue,
    required bool canRead,
    required bool canWrite,
    required bool canAdmin,
  }) async {
    final uri = _uri('/api/permission-grants');
    final response = await _putJson(uri, {
      'resource_type': resourceType,
      'resource_key': resourceKey,
      'resource_id': resourceId,
      'subject_type': subjectType,
      'subject_value': subjectValue,
      'can_read': canRead,
      'can_write': canWrite,
      'can_admin': canAdmin,
    });
    _decodeObject(response);
  }

  Future<void> deletePermissionGrant(int grantId) async {
    final uri = _uri('/api/permission-grants/$grantId');
    final response = await _httpClient.delete(uri).timeout(const Duration(seconds: 15));
    _decodeObject(response);
  }

  Future<Map<String, dynamic>> getMemberProfile(int memberId) async {
    final uri = _uri('/api/member/$memberId/profile');
    final response = await _get(uri);
    return _decodeObject(response);
  }

  Future<Map<String, dynamic>> updateMemberProfile(int memberId, Map<String, dynamic> payload) async {
    final uri = _uri('/api/member/$memberId/profile');
    final response = await _postJson(uri, payload, seconds: 20);
    return _decodeObject(response);
  }

  Future<void> testMemberEmail(int memberId) async {
    final uri = _uri('/api/member/$memberId/profile/test-email');
    final response = await _postJson(uri, {});
    _decodeObject(response);
  }

  Map<String, dynamic> _decodeObject(http.Response response) {
    dynamic body;
    try {
      body = jsonDecode(response.body);
    } catch (_) {
      throw Exception('Invalid server response (${response.statusCode})');
    }
    if (body is! Map<String, dynamic>) {
      throw Exception('Unexpected server response');
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('${body['error'] ?? body['message'] ?? 'Request failed'} (${response.statusCode})');
    }
    if (body['ok'] == false) {
      throw Exception('${body['error'] ?? body['message'] ?? 'Request failed'}');
    }
    return body;
  }

  Map<String, dynamic> _decodeMapLoose(http.Response response) {
    dynamic body;
    try {
      body = jsonDecode(response.body);
    } catch (_) {
      throw Exception('Invalid server response (${response.statusCode})');
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      if (body is Map<String, dynamic>) {
        throw Exception('${body['error'] ?? body['message'] ?? 'Request failed'} (${response.statusCode})');
      }
      throw Exception('Request failed (${response.statusCode})');
    }
    if (body is! Map<String, dynamic>) {
      throw Exception('Unexpected server response');
    }
    return body;
  }
}

class ThreadSummary {
  const ThreadSummary({
    required this.member,
    required this.lastMessage,
    required this.lastAt,
    required this.unread,
  });

  final String member;
  final String lastMessage;
  final DateTime? lastAt;
  final int unread;

  factory ThreadSummary.fromJson(Map<String, dynamic> json) {
    return ThreadSummary(
      member: '${json['member'] ?? ''}',
      lastMessage: '${json['last_message'] ?? ''}',
      lastAt: _parseDateTime(json['last_at']),
      unread: _toInt(json['unread']),
    );
  }
}

class ChatMessage {
  const ChatMessage({
    required this.id,
    required this.sender,
    required this.recipient,
    required this.message,
    required this.createdAt,
  });

  final int id;
  final String sender;
  final String recipient;
  final String message;
  final DateTime? createdAt;

  factory ChatMessage.fromJson(Map<String, dynamic> json) {
    return ChatMessage(
      id: _toInt(json['id']),
      sender: '${json['sender'] ?? ''}',
      recipient: '${json['recipient'] ?? ''}',
      message: '${json['message'] ?? ''}',
      createdAt: _parseDateTime(json['created_at']),
    );
  }
}

class AppNotificationItem {
  const AppNotificationItem({
    required this.id,
    required this.eventType,
    required this.title,
    required this.message,
    required this.isRead,
    required this.createdAt,
  });

  final int id;
  final String eventType;
  final String title;
  final String message;
  final bool isRead;
  final DateTime? createdAt;

  factory AppNotificationItem.fromJson(Map<String, dynamic> json) {
    return AppNotificationItem(
      id: _toInt(json['id']),
      eventType: '${json['event_type'] ?? ''}',
      title: '${json['title'] ?? ''}',
      message: '${json['message'] ?? ''}',
      isRead: json['is_read'] == true,
      createdAt: _parseDateTime(json['created_at']),
    );
  }
}

class TimelinePost {
  const TimelinePost({
    required this.id,
    required this.timestamp,
    required this.actor,
    required this.action,
    required this.summary,
    required this.imageUrl,
    required this.entityType,
    required this.likesCount,
    required this.commentsCount,
    required this.sharesCount,
    required this.likedByMe,
    required this.comments,
    this.entityId,
  });

  final int id;
  final DateTime? timestamp;
  final String actor;
  final String action;
  final String summary;
  final String imageUrl;
  final String entityType;
  final int? entityId;
  final int likesCount;
  final int commentsCount;
  final int sharesCount;
  final bool likedByMe;
  final List<TimelineComment> comments;

  factory TimelinePost.fromJson(Map<String, dynamic> json) {
    final commentRows = (json['comments'] as List<dynamic>? ?? const <dynamic>[])
        .whereType<Map<String, dynamic>>()
        .map(TimelineComment.fromJson)
        .toList();
    return TimelinePost(
      id: _toInt(json['id']),
      timestamp: _parseDateTime(json['timestamp']),
      actor: '${json['actor'] ?? ''}',
      action: '${json['action'] ?? ''}',
      summary: '${json['summary'] ?? ''}',
      imageUrl: '${json['image_url'] ?? ''}',
      entityType: '${json['entity_type'] ?? ''}',
      entityId: json['entity_id'] == null ? null : _toInt(json['entity_id']),
      likesCount: _toInt(json['likes_count']),
      commentsCount: _toInt(json['comments_count']),
      sharesCount: _toInt(json['shares_count']),
      likedByMe: json['liked_by_me'] == true,
      comments: commentRows,
    );
  }
}

class TimelineComment {
  const TimelineComment({
    required this.id,
    required this.author,
    required this.text,
    required this.timestamp,
  });

  final String id;
  final String author;
  final String text;
  final DateTime? timestamp;

  factory TimelineComment.fromJson(Map<String, dynamic> json) {
    return TimelineComment(
      id: '${json['id'] ?? ''}',
      author: '${json['author'] ?? ''}',
      text: '${json['text'] ?? ''}',
      timestamp: _parseDateTime(json['timestamp']),
    );
  }
}

class _TimelineVisualMeta {
  const _TimelineVisualMeta({
    required this.icon,
    required this.label,
    required this.emoji,
    required this.start,
    required this.end,
    required this.soft,
    required this.onSoft,
  });

  final IconData icon;
  final String label;
  final String emoji;
  final Color start;
  final Color end;
  final Color soft;
  final Color onSoft;
}

_TimelineVisualMeta _timelineVisualMeta(String action) {
  final value = action.trim();
  if (value.startsWith('shopping.')) {
    return const _TimelineVisualMeta(
      icon: Icons.shopping_cart,
      label: 'Shopping',
      emoji: '🛒',
      start: Color(0xFF0EA5E9),
      end: Color(0xFF2563EB),
      soft: Color(0xFFE0F2FE),
      onSoft: Color(0xFF075985),
    );
  }
  if (value.startsWith('money.income')) {
    return const _TimelineVisualMeta(
      icon: Icons.trending_up,
      label: 'Income',
      emoji: '💸',
      start: Color(0xFF10B981),
      end: Color(0xFF16A34A),
      soft: Color(0xFFDCFCE7),
      onSoft: Color(0xFF166534),
    );
  }
  if (value.startsWith('money.expense')) {
    return const _TimelineVisualMeta(
      icon: Icons.trending_down,
      label: 'Expense',
      emoji: '🧾',
      start: Color(0xFFF43F5E),
      end: Color(0xFFDC2626),
      soft: Color(0xFFFFE4E6),
      onSoft: Color(0xFF9F1239),
    );
  }
  if (value.startsWith('money.bill')) {
    return const _TimelineVisualMeta(
      icon: Icons.receipt_long,
      label: 'Bills',
      emoji: '🏠',
      start: Color(0xFFF59E0B),
      end: Color(0xFFEA580C),
      soft: Color(0xFFFFF7ED),
      onSoft: Color(0xFF9A3412),
    );
  }
  if (value.startsWith('timeline.manual')) {
    return const _TimelineVisualMeta(
      icon: Icons.edit,
      label: 'Post',
      emoji: '📝',
      start: Color(0xFF8B5CF6),
      end: Color(0xFFD946EF),
      soft: Color(0xFFF3E8FF),
      onSoft: Color(0xFF6B21A8),
    );
  }
  return const _TimelineVisualMeta(
    icon: Icons.campaign,
    label: 'Activity',
    emoji: '📣',
    start: Color(0xFF64748B),
    end: Color(0xFF334155),
    soft: Color(0xFFF1F5F9),
    onSoft: Color(0xFF334155),
  );
}

class _TimelineSocialCard extends StatefulWidget {
  const _TimelineSocialCard({
    required this.post,
    this.onDelete,
    this.onLike,
    this.onComment,
    this.onShare,
    this.onCommentSubmit,
    this.onEditImage,
    this.imageUrl = '',
    this.canEditImage = false,
    this.showInlineCommentComposer = false,
    this.compact = false,
  });

  final TimelinePost post;
  final VoidCallback? onDelete;
  final VoidCallback? onLike;
  final VoidCallback? onComment;
  final VoidCallback? onShare;
  final Future<void> Function(String text)? onCommentSubmit;
  final VoidCallback? onEditImage;
  final String imageUrl;
  final bool canEditImage;
  final bool showInlineCommentComposer;
  final bool compact;

  @override
  State<_TimelineSocialCard> createState() => _TimelineSocialCardState();
}

class _TimelineSocialCardState extends State<_TimelineSocialCard> with SingleTickerProviderStateMixin {
  late final AnimationController _heartController;
  late final Animation<double> _heartScale;
  final TextEditingController _inlineCommentCtrl = TextEditingController();
  final FocusNode _inlineCommentFocus = FocusNode();
  bool _showHeart = false;
  bool _inlineSending = false;
  bool _showInlineComposer = false;

  @override
  void initState() {
    super.initState();
    _showInlineComposer = widget.showInlineCommentComposer;
    _heartController = AnimationController(vsync: this, duration: const Duration(milliseconds: 360));
    _heartScale = CurvedAnimation(parent: _heartController, curve: Curves.easeOutBack);
  }

  @override
  void dispose() {
    _heartController.dispose();
    _inlineCommentCtrl.dispose();
    _inlineCommentFocus.dispose();
    super.dispose();
  }

  void _triggerDoubleTapLike() {
    widget.onLike?.call();
    _heartController.forward(from: 0);
    setState(() => _showHeart = true);
    Future<void>.delayed(const Duration(milliseconds: 700), () {
      if (!mounted) return;
      setState(() => _showHeart = false);
    });
  }

  bool _isFoodPost() {
    final text = '${widget.post.action} ${widget.post.summary}'.toLowerCase();
    return text.contains('recipe') ||
        text.contains('meal') ||
        text.contains('food') ||
        text.contains('dinner') ||
        text.contains('breakfast') ||
        text.contains('lunch') ||
        text.contains('snack');
  }

  bool _isMoneyPost() {
    final text = '${widget.post.action} ${widget.post.summary}'.toLowerCase();
    return text.contains('money') ||
        text.contains('expense') ||
        text.contains('income') ||
        text.contains('bill') ||
        text.contains('debt') ||
        text.contains('payment');
  }

  bool _isEventPost() {
    final text = '${widget.post.action} ${widget.post.summary} ${widget.post.entityType}'.toLowerCase();
    return text.contains('event') || text.contains('calendar');
  }

  Widget _buildMediaFallback() {
    final isFood = _isFoodPost();
    final isMoney = _isMoneyPost();
    final isEvent = _isEventPost();

    final icon = isFood
        ? Icons.restaurant
        : isMoney
            ? Icons.savings
            : isEvent
                ? Icons.calendar_month
                : Icons.image_outlined;
    final label = isFood
        ? 'Fork, knife, and plate'
        : isMoney
            ? 'Money'
            : isEvent
                ? 'Event'
                : 'Timeline';
    final bg = isFood
        ? const LinearGradient(colors: [Color(0xFFFFEDD5), Color(0xFFFED7AA)])
        : isMoney
            ? const LinearGradient(colors: [Color(0xFFDCFCE7), Color(0xFFBBF7D0)])
            : isEvent
                ? const LinearGradient(colors: [Color(0xFFDBEAFE), Color(0xFFBFDBFE)])
                : const LinearGradient(colors: [Color(0xFFE5E7EB), Color(0xFFD1D5DB)]);
    final fg = isFood
        ? const Color(0xFF9A3412)
        : isMoney
            ? const Color(0xFF166534)
            : isEvent
                ? const Color(0xFF1D4ED8)
                : const Color(0xFF374151);

    return Container(
      decoration: BoxDecoration(gradient: bg),
      child: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 46, color: fg),
            const SizedBox(height: 8),
            Text(label, style: TextStyle(color: fg, fontWeight: FontWeight.w700)),
          ],
        ),
      ),
    );
  }

  Future<void> _submitInlineComment() async {
    final submit = widget.onCommentSubmit;
    if (submit == null || _inlineSending) return;
    final value = _inlineCommentCtrl.text.trim();
    if (value.isEmpty) return;
    setState(() => _inlineSending = true);
    try {
      await submit(value);
      if (!mounted) return;
      _inlineCommentCtrl.clear();
      _inlineCommentFocus.unfocus();
    } finally {
      if (mounted) setState(() => _inlineSending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final meta = _timelineVisualMeta(widget.post.action);
    final summary = widget.post.summary.trim().isEmpty ? widget.post.action : widget.post.summary.trim();
    final timestamp = widget.post.timestamp == null ? '' : _relativeTime(widget.post.timestamp!.toIso8601String());
    final actor = widget.post.actor.trim().isEmpty ? 'Family' : widget.post.actor.trim();
    final radius = BorderRadius.circular(widget.compact ? 14 : 18);
    final avatarSize = widget.compact ? 36.0 : 42.0;
    final hasImage = widget.imageUrl.trim().startsWith('http://') || widget.imageUrl.trim().startsWith('https://');

    return Container(
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: radius,
        border: Border.all(color: const Color(0xFFE5E7EB)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: avatarSize,
                  height: avatarSize,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: LinearGradient(colors: [meta.start, meta.end]),
                  ),
                  child: Center(
                    child: Text(
                      actor.substring(0, 1).toUpperCase(),
                      style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700, fontSize: 16),
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        actor,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.titleSmall?.copyWith(fontWeight: FontWeight.w700, color: const Color(0xFF111827)),
                      ),
                      if (timestamp.isNotEmpty)
                        Text(
                          '$timestamp • ${meta.label}',
                          style: theme.textTheme.bodySmall?.copyWith(color: const Color(0xFF6B7280)),
                        ),
                    ],
                  ),
                ),
                if (widget.onDelete != null || (widget.canEditImage && widget.onEditImage != null))
                  PopupMenuButton<String>(
                    tooltip: 'Post actions',
                    icon: const Icon(Icons.more_horiz, color: Color(0xFF4B5563)),
                    onSelected: (value) {
                      if (value == 'delete') widget.onDelete?.call();
                      if (value == 'edit-image') widget.onEditImage?.call();
                    },
                    itemBuilder: (context) => [
                      if (widget.canEditImage && widget.onEditImage != null)
                        const PopupMenuItem<String>(
                          value: 'edit-image',
                          child: Text('Edit image'),
                        ),
                      if (widget.onDelete != null)
                        const PopupMenuItem<String>(
                          value: 'delete',
                          child: Text('Delete post'),
                        ),
                    ],
                  )
                else
                  const SizedBox(width: 28),
              ],
            ),
          ),
          Container(
            width: double.infinity,
            margin: const EdgeInsets.fromLTRB(12, 0, 12, 10),
            padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
            decoration: BoxDecoration(
              color: meta.soft,
              borderRadius: BorderRadius.circular(999),
            ),
            child: Row(
              children: [
                Text(meta.emoji),
                const SizedBox(width: 8),
                Icon(meta.icon, size: 14, color: meta.onSoft),
                const SizedBox(width: 5),
                Text(
                  meta.label,
                  style: TextStyle(color: meta.onSoft, fontWeight: FontWeight.w700, fontSize: 12),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(14),
              child: GestureDetector(
                onDoubleTap: _triggerDoubleTapLike,
                child: AspectRatio(
                  aspectRatio: 1,
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      if (hasImage)
                        Image.network(
                          widget.imageUrl.trim(),
                          fit: BoxFit.cover,
                          errorBuilder: (context, error, stackTrace) => _buildMediaFallback(),
                        )
                      else
                        _buildMediaFallback(),
                      if (_showHeart)
                        Container(
                          color: Colors.black.withValues(alpha: 0.08),
                          child: Center(
                            child: ScaleTransition(
                              scale: _heartScale,
                              child: const Icon(Icons.favorite, color: Colors.white, size: 84),
                            ),
                          ),
                        ),
                      Positioned(
                        right: 8,
                        bottom: 8,
                        child: DecoratedBox(
                          decoration: BoxDecoration(
                            color: Colors.black.withValues(alpha: 0.55),
                            borderRadius: BorderRadius.circular(999),
                          ),
                          child: const Padding(
                            padding: EdgeInsets.symmetric(horizontal: 9, vertical: 5),
                            child: Text(
                              'Double tap to like',
                              style: TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.w600),
                            ),
                          ),
                        ),
                      ),
                      if (widget.canEditImage)
                        Positioned(
                          left: 8,
                          top: 8,
                          child: InkWell(
                            onTap: widget.onEditImage,
                            borderRadius: BorderRadius.circular(999),
                            child: Ink(
                              padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
                              decoration: BoxDecoration(
                                color: Colors.black.withValues(alpha: 0.6),
                                borderRadius: BorderRadius.circular(999),
                              ),
                              child: const Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Icon(Icons.image_outlined, size: 13, color: Colors.white),
                                  SizedBox(width: 4),
                                  Text('Edit image', style: TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.w700)),
                                ],
                              ),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
            child: Text(
              summary,
              style: theme.textTheme.bodyMedium?.copyWith(height: 1.45, fontSize: widget.compact ? 14 : 15, color: const Color(0xFF111827)),
            ),
          ),
          if (!widget.compact)
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 10),
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
                decoration: BoxDecoration(
                  color: const Color(0xFFF9FAFB),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Text(
                  'Shared on your HomeHub timeline',
                  style: theme.textTheme.bodySmall?.copyWith(color: const Color(0xFF6B7280)),
                ),
              ),
            ),
          const Divider(height: 1, color: Color(0xFFE5E7EB)),
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 8, 10, 10),
            child: Column(
              children: [
                Row(
                  children: [
                    const Icon(Icons.favorite, size: 14, color: Color(0xFFF43F5E)),
                    const SizedBox(width: 4),
                    const Icon(Icons.thumb_up, size: 14, color: Color(0xFF3B82F6)),
                    const SizedBox(width: 6),
                    Text(
                      '${widget.post.likesCount} likes',
                      style: theme.textTheme.bodySmall?.copyWith(color: const Color(0xFF6B7280)),
                    ),
                    const Spacer(),
                    Text(
                      '${widget.post.commentsCount} comments • ${widget.post.sharesCount} shares',
                      style: theme.textTheme.bodySmall?.copyWith(color: const Color(0xFF6B7280)),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(
                      child: _TimelineActionPill(
                        icon: widget.post.likedByMe ? Icons.favorite : Icons.favorite_border,
                        label: widget.post.likedByMe ? 'Liked' : 'Like',
                        bg: widget.post.likedByMe ? const Color(0xFFFECDD3) : const Color(0xFFFFE4E6),
                        fg: const Color(0xFFBE123C),
                        onTap: widget.onLike,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _TimelineActionPill(
                        icon: Icons.mode_comment_outlined,
                        label: 'Comment',
                        bg: const Color(0xFFDBEAFE),
                        fg: const Color(0xFF1D4ED8),
                        onTap: () {
                          if (widget.showInlineCommentComposer || widget.onCommentSubmit != null) {
                            setState(() => _showInlineComposer = true);
                            _inlineCommentFocus.requestFocus();
                          } else {
                            widget.onComment?.call();
                          }
                        },
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _TimelineActionPill(
                        icon: Icons.share_outlined,
                        label: 'Share',
                        bg: const Color(0xFFEDE9FE),
                        fg: const Color(0xFF6D28D9),
                        onTap: widget.onShare,
                      ),
                    ),
                  ],
                ),
                if (_showInlineComposer && widget.onCommentSubmit != null) ...[
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _inlineCommentCtrl,
                          focusNode: _inlineCommentFocus,
                          minLines: 1,
                          maxLines: 3,
                          textInputAction: TextInputAction.send,
                          onSubmitted: (_) => _submitInlineComment(),
                          decoration: const InputDecoration(
                            hintText: 'Write a comment...',
                            isDense: true,
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      FilledButton(
                        onPressed: _inlineSending ? null : _submitInlineComment,
                        child: Text(_inlineSending ? '...' : 'Send'),
                      ),
                    ],
                  ),
                ],
                if (widget.post.comments.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  ...widget.post.comments.take(3).map((row) => Container(
                        width: double.infinity,
                        margin: const EdgeInsets.only(bottom: 6),
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                        decoration: BoxDecoration(
                          color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.55),
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: Text.rich(
                          TextSpan(
                            children: [
                              TextSpan(
                                text: '${row.author}: ',
                                style: theme.textTheme.bodySmall?.copyWith(fontWeight: FontWeight.w700, color: const Color(0xFF111827)),
                              ),
                              TextSpan(text: row.text, style: theme.textTheme.bodySmall?.copyWith(color: const Color(0xFF374151))),
                            ],
                          ),
                        ),
                      )),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _TimelineActionPill extends StatelessWidget {
  const _TimelineActionPill({
    required this.icon,
    required this.label,
    required this.bg,
    required this.fg,
    this.onTap,
  });

  final IconData icon;
  final String label;
  final Color bg;
  final Color fg;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: Ink(
      height: 36,
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(10)),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Icon(icon, size: 16, color: fg),
          const SizedBox(width: 5),
          Text(label, style: TextStyle(color: fg, fontWeight: FontWeight.w600, fontSize: 12)),
        ],
      ),
      ),
    );
  }
}

class _TimelineFeedWrapper extends StatelessWidget {
  const _TimelineFeedWrapper({
    required this.title,
    required this.badgeLabel,
    required this.posts,
    this.emptyText = 'No timeline activity yet.',
    this.imageUrlBuilder,
    this.onLikeBuilder,
    this.onCommentBuilder,
    this.onShareBuilder,
    this.onDeleteBuilder,
    this.onEditImageBuilder,
    this.onCommentSubmitBuilder,
    this.canEditImageBuilder,
    this.showInlineCommentComposer = false,
    this.compact = false,
  });

  final String title;
  final String badgeLabel;
  final List<TimelinePost> posts;
  final String emptyText;
  final String Function(TimelinePost post)? imageUrlBuilder;
  final VoidCallback? Function(TimelinePost post)? onLikeBuilder;
  final VoidCallback? Function(TimelinePost post)? onCommentBuilder;
  final VoidCallback? Function(TimelinePost post)? onShareBuilder;
  final VoidCallback? Function(TimelinePost post)? onDeleteBuilder;
  final VoidCallback? Function(TimelinePost post)? onEditImageBuilder;
  final Future<void> Function(TimelinePost post, String text)? onCommentSubmitBuilder;
  final bool Function(TimelinePost post)? canEditImageBuilder;
  final bool showInlineCommentComposer;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final visiblePosts = posts;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text(title, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                const SizedBox(width: 8),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                  decoration: BoxDecoration(
                    color: const Color(0xFFEDE9FE),
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: Text(
                    badgeLabel,
                    style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: Color(0xFF5B21B6)),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            if (visiblePosts.isEmpty)
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.inbox_outlined),
                title: Text(emptyText),
              )
            else
              ...visiblePosts.map(
                (post) => Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: _TimelineSocialCard(
                    post: post,
                    compact: compact,
                    imageUrl: imageUrlBuilder?.call(post) ?? post.imageUrl,
                    onLike: onLikeBuilder?.call(post),
                    onComment: onCommentBuilder?.call(post),
                    onShare: onShareBuilder?.call(post),
                    onDelete: onDeleteBuilder?.call(post),
                    onEditImage: onEditImageBuilder?.call(post),
                    canEditImage: canEditImageBuilder?.call(post) ?? false,
                    showInlineCommentComposer: showInlineCommentComposer,
                    onCommentSubmit: onCommentSubmitBuilder == null ? null : (text) => onCommentSubmitBuilder!(post, text),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class CalendarMember {
  const CalendarMember({required this.id, required this.name, required this.role});

  final int id;
  final String name;
  final String role;

  factory CalendarMember.fromJson(Map<String, dynamic> json) {
    return CalendarMember(
      id: _toInt(json['id']),
      name: '${json['name'] ?? ''}',
      role: '${json['role'] ?? 'standard'}',
    );
  }
}

class CalendarEvent {
  const CalendarEvent({
    required this.id,
    required this.title,
    required this.eventType,
    required this.startDate,
    required this.endDate,
    required this.createdBy,
    required this.color,
    required this.isAllDay,
    required this.visibility,
    required this.isRecurring,
    required this.invitedMembers,
    this.baseStartDate,
    this.baseEndDate,
    this.descriptionHtml,
    this.startTime,
    this.endTime,
    this.recurrenceFrequency,
    this.recurrenceInterval,
    this.recurrenceUntil,
    this.recurrenceCount,
    this.occurrenceStart,
    this.reminderAt,
    this.photoUrl,
  });

  final int id;
  final String title;
  final String eventType;
  final DateTime startDate;
  final DateTime endDate;
  final String createdBy;
  final String color;
  final bool isAllDay;
  final String visibility;
  final bool isRecurring;
  final List<String> invitedMembers;
  final DateTime? baseStartDate;
  final DateTime? baseEndDate;
  final String? descriptionHtml;
  final String? startTime;
  final String? endTime;
  final String? recurrenceFrequency;
  final int? recurrenceInterval;
  final DateTime? recurrenceUntil;
  final int? recurrenceCount;
  final DateTime? occurrenceStart;
  final DateTime? reminderAt;
  final String? photoUrl;

  factory CalendarEvent.fromJson(Map<String, dynamic> json) {
    final start = DateTime.tryParse('${json['start_date'] ?? ''}') ?? DateTime.now();
    final end = DateTime.tryParse('${json['end_date'] ?? ''}') ?? start;
    final baseStart = _parseDateTime(json['base_start_date']);
    final baseEnd = _parseDateTime(json['base_end_date']);
    final invitedRaw = json['invited_members'];
    final invited = (invitedRaw is List ? invitedRaw : const <dynamic>[])
        .map((e) => '$e'.trim())
        .where((e) => e.isNotEmpty)
        .toList();
    return CalendarEvent(
      id: _toInt(json['id']),
      title: '${json['title'] ?? ''}',
      eventType: '${json['event_type'] ?? 'event'}',
      startDate: DateTime(start.year, start.month, start.day),
      endDate: DateTime(end.year, end.month, end.day),
      createdBy: '${json['created_by'] ?? ''}',
      color: '${json['color'] ?? '#3b82f6'}',
      isAllDay: json['is_all_day'] == true,
      visibility: '${json['visibility'] ?? 'family'}',
      isRecurring: json['is_recurring'] == true,
      invitedMembers: invited,
      baseStartDate: baseStart == null ? null : DateTime(baseStart.year, baseStart.month, baseStart.day),
      baseEndDate: baseEnd == null ? null : DateTime(baseEnd.year, baseEnd.month, baseEnd.day),
      descriptionHtml: json['description_html'] == null ? null : '${json['description_html']}',
      startTime: _cleanNullableString(json['start_time']),
      endTime: _cleanNullableString(json['end_time']),
      recurrenceFrequency: _cleanNullableString(json['recurrence_frequency']),
      recurrenceInterval: json['recurrence_interval'] == null ? null : _toInt(json['recurrence_interval']),
      recurrenceUntil: _parseDateTime(json['recurrence_until']),
      recurrenceCount: json['recurrence_count'] == null ? null : _toInt(json['recurrence_count']),
      occurrenceStart: _parseDateTime(json['occurrence_start']),
      reminderAt: _parseDateTime(json['reminder_at']),
      photoUrl: _cleanNullableString(json['photo_url']),
    );
  }
}

class CalendarEventDraft {
  CalendarEventDraft({
    required this.title,
    required this.eventType,
    required this.startDate,
    required this.endDate,
    required this.color,
    required this.visibility,
    required this.isAllDay,
    required this.isRecurring,
    required this.invitedMembers,
    this.descriptionHtml,
    this.startTime,
    this.endTime,
    this.recurrenceFrequency,
    this.recurrenceInterval,
    this.recurrenceUntil,
    this.recurrenceCount,
    this.reminderAt,
  });

  final String title;
  final String eventType;
  final DateTime startDate;
  final DateTime endDate;
  final String color;
  final String visibility;
  final bool isAllDay;
  final bool isRecurring;
  final List<String> invitedMembers;
  final String? descriptionHtml;
  final String? startTime;
  final String? endTime;
  final String? recurrenceFrequency;
  final int? recurrenceInterval;
  final DateTime? recurrenceUntil;
  final int? recurrenceCount;
  final DateTime? reminderAt;

  factory CalendarEventDraft.fromEvent(CalendarEvent event) {
    return CalendarEventDraft(
      title: event.title,
      eventType: event.eventType,
      startDate: event.startDate,
      endDate: event.endDate,
      color: event.color,
      visibility: event.visibility,
      isAllDay: event.isAllDay,
      isRecurring: event.isRecurring,
      invitedMembers: event.invitedMembers,
      descriptionHtml: event.descriptionHtml,
      startTime: event.startTime,
      endTime: event.endTime,
      recurrenceFrequency: event.recurrenceFrequency,
      recurrenceInterval: event.recurrenceInterval,
      recurrenceUntil: event.recurrenceUntil,
      recurrenceCount: event.recurrenceCount,
      reminderAt: event.reminderAt,
    );
  }

  Map<String, dynamic> toApiJson() {
    return {
      'title': title,
      'event_type': eventType,
      'start_date': _fmtDate(startDate),
      'end_date': _fmtDate(endDate),
      'color': color,
      'visibility': visibility,
      'is_all_day': isAllDay,
      'description_html': descriptionHtml ?? '',
      'start_time': isAllDay ? null : startTime,
      'end_time': isAllDay ? null : endTime,
      'is_recurring': isRecurring,
      'recurrence_frequency': isRecurring ? (recurrenceFrequency ?? 'weekly') : null,
      'recurrence_interval': isRecurring ? (recurrenceInterval ?? 1) : null,
      'recurrence_until': isRecurring && recurrenceUntil != null ? _fmtDate(recurrenceUntil!) : null,
      'recurrence_count': isRecurring ? recurrenceCount : null,
      'invited_members': invitedMembers,
      'reminder_at': reminderAt == null
          ? ''
          : '${_fmtDate(reminderAt!)}T${reminderAt!.hour.toString().padLeft(2, '0')}:${reminderAt!.minute.toString().padLeft(2, '0')}',
    };
  }
}

class MealPlannerEntry {
  const MealPlannerEntry({
    required this.id,
    required this.date,
    required this.mealType,
    required this.title,
    required this.description,
    required this.creator,
    required this.visibility,
    required this.allowedMembers,
    required this.requirePin,
    required this.audience,
    required this.isLocked,
    required this.canEdit,
    this.recipeId,
  });

  final int id;
  final DateTime date;
  final String mealType;
  final int? recipeId;
  final String title;
  final String description;
  final String creator;
  final String visibility;
  final List<String> allowedMembers;
  final bool requirePin;
  final String audience;
  final bool isLocked;
  final bool canEdit;

  factory MealPlannerEntry.fromJson(Map<String, dynamic> json) {
    final parsedDate = DateTime.tryParse('${json['date'] ?? ''}') ?? DateTime.now();
    final rawAllowed = json['allowed_members'];
    final allowed = (rawAllowed is List ? rawAllowed : const <dynamic>[])
        .map((value) => '$value'.trim())
        .where((value) => value.isNotEmpty)
        .toList();
    return MealPlannerEntry(
      id: _toInt(json['id']),
      date: DateTime(parsedDate.year, parsedDate.month, parsedDate.day),
      mealType: '${json['meal_type'] ?? 'dinner'}',
      recipeId: json['recipe_id'] == null ? null : _toInt(json['recipe_id']),
      title: '${json['title'] ?? ''}',
      description: '${json['description'] ?? ''}',
      creator: '${json['creator'] ?? ''}',
      visibility: '${json['visibility'] ?? 'family'}',
      allowedMembers: allowed,
      requirePin: json['require_pin'] == true,
      audience: '${json['audience'] ?? 'family'}',
      isLocked: json['is_locked'] == true,
      canEdit: json['can_edit'] == true,
    );
  }
}

class SharedCloudFile {
  const SharedCloudFile({
    required this.id,
    required this.filename,
    required this.creator,
    required this.category,
    required this.uploadTime,
    required this.downloadUrl,
    required this.previewUrl,
  });

  final int id;
  final String filename;
  final String creator;
  final String category;
  final DateTime? uploadTime;
  final String downloadUrl;
  final String previewUrl;

  factory SharedCloudFile.fromJson(Map<String, dynamic> json) {
    return SharedCloudFile(
      id: _toInt(json['id']),
      filename: '${json['filename'] ?? ''}',
      creator: '${json['creator'] ?? ''}',
      category: '${json['category'] ?? ''}',
      uploadTime: _parseDateTime(json['upload_time']),
      downloadUrl: '${json['download_url'] ?? ''}',
      previewUrl: '${json['preview_url'] ?? ''}',
    );
  }
}

class PhotoAlbumSummary {
  const PhotoAlbumSummary({
    required this.id,
    required this.name,
    required this.creator,
    required this.visibility,
    required this.assetCount,
    required this.coverUrl,
  });

  final int id;
  final String name;
  final String creator;
  final String visibility;
  final int assetCount;
  final String coverUrl;

  factory PhotoAlbumSummary.fromJson(Map<String, dynamic> json) {
    return PhotoAlbumSummary(
      id: _toInt(json['id']),
      name: '${json['name'] ?? ''}',
      creator: '${json['creator'] ?? ''}',
      visibility: '${json['visibility'] ?? 'private'}',
      assetCount: _toInt(json['asset_count']),
      coverUrl: '${json['cover_url'] ?? ''}',
    );
  }
}

class PhotoAssetItem {
  const PhotoAssetItem({
    required this.id,
    required this.albumId,
    required this.originalFilename,
    required this.uploader,
    required this.visibility,
    required this.isVideo,
    required this.uploadedAt,
    required this.downloadUrl,
    required this.previewUrl,
  });

  final int id;
  final int? albumId;
  final String originalFilename;
  final String uploader;
  final String visibility;
  final bool isVideo;
  final DateTime? uploadedAt;
  final String downloadUrl;
  final String previewUrl;

  factory PhotoAssetItem.fromJson(Map<String, dynamic> json) {
    return PhotoAssetItem(
      id: _toInt(json['id']),
      albumId: json['album_id'] == null ? null : _toInt(json['album_id']),
      originalFilename: '${json['original_filename'] ?? json['filename'] ?? ''}',
      uploader: '${json['uploader'] ?? ''}',
      visibility: '${json['visibility'] ?? 'private'}',
      isVideo: json['is_video'] == true,
      uploadedAt: _parseDateTime(json['uploaded_at']),
      downloadUrl: '${json['download_url'] ?? ''}',
      previewUrl: '${json['preview_url'] ?? ''}',
    );
  }
}

class PhotoWatchFolderItem {
  const PhotoWatchFolderItem({
    required this.id,
    required this.folderUri,
    required this.scope,
    required this.targetAlbumId,
    required this.isActive,
    required this.lastSyncedAt,
    required this.platform,
  });

  final int id;
  final String folderUri;
  final String scope;
  final int? targetAlbumId;
  final bool isActive;
  final DateTime? lastSyncedAt;
  final String platform;

  factory PhotoWatchFolderItem.fromJson(Map<String, dynamic> json) {
    return PhotoWatchFolderItem(
      id: _toInt(json['id']),
      folderUri: '${json['folder_uri'] ?? ''}',
      scope: '${json['scope'] ?? 'private'}',
      targetAlbumId: json['target_album_id'] == null ? null : _toInt(json['target_album_id']),
      isActive: json['is_active'] == true,
      lastSyncedAt: _parseDateTime(json['last_synced_at']),
      platform: '${json['platform'] ?? ''}',
    );
  }
}

class SetupScreen extends StatefulWidget {
  const SetupScreen({super.key, required this.onSave});

  final Future<void> Function(AppSettings settings) onSave;

  @override
  State<SetupScreen> createState() => _SetupScreenState();
}

class _SetupScreenState extends State<SetupScreen> {
  final _formKey = GlobalKey<FormState>();
  final _urlCtrl = TextEditingController(text: _cloudBaseUrl);
  bool _saving = false;

  @override
  void dispose() {
    _urlCtrl.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _saving = true);
    try {
      await widget.onSave(
        AppSettings(baseUrl: _urlCtrl.text.trim(), member: ''),
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('HomeHub Setup')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(18),
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: const Color(0xFFE0E7FF),
                    borderRadius: BorderRadius.circular(16),
                  ),
                  child: const Text(
                    'Native HomeHub mobile app using the same backend APIs. Enter your backend URL to continue.',
                  ),
                ),
                const SizedBox(height: 14),
                TextFormField(
                  controller: _urlCtrl,
                  decoration: const InputDecoration(labelText: 'Backend URL'),
                  validator: (v) => (v == null || v.trim().isEmpty) ? 'Backend URL is required' : null,
                ),
                const SizedBox(height: 20),
                FilledButton.icon(
                  onPressed: _saving ? null : _submit,
                  icon: const Icon(Icons.check),
                  label: Text(_saving ? 'Saving...' : 'Save & Continue'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class AuthGateScreen extends StatefulWidget {
  const AuthGateScreen({
    super.key,
    required this.settings,
    required this.onUpdateSettings,
    required this.onAuthenticated,
  });

  final AppSettings settings;
  final Future<void> Function(AppSettings settings) onUpdateSettings;
  final ValueChanged<AppSettings> onAuthenticated;

  @override
  State<AuthGateScreen> createState() => _AuthGateScreenState();
}

class _AuthGateScreenState extends State<AuthGateScreen> {
  final _passwordCtrl = TextEditingController();
  final _pinCtrl = TextEditingController();

  bool _loading = true;
  bool _submitting = false;
  String? _error;
  bool _requiresPassword = false;
  bool _authed = false;
  String? _selectedMember;
  String _effectiveBaseUrl = '';
  String _selectedEndpoint = '';
  List<String> _configuredEndpoints = const [];
  List<Map<String, dynamic>> _members = const [];

  @override
  void initState() {
    super.initState();
    _configuredEndpoints = _seedEndpoints(widget.settings.baseUrl);
    final preferred = widget.settings.baseUrl.trim().isEmpty ? _cloudBaseUrl : widget.settings.baseUrl.trim();
    final normalizedPreferred = _normalizeBaseUrl(preferred);
    _selectedEndpoint = _configuredEndpoints.contains(normalizedPreferred) ? normalizedPreferred : _configuredEndpoints.first;
    _effectiveBaseUrl = _selectedEndpoint;
    _bootstrap();
  }

  @override
  void dispose() {
    _passwordCtrl.dispose();
    _pinCtrl.dispose();
    super.dispose();
  }

  List<String> _seedEndpoints(String configuredBaseUrl) {
    final values = <String>[configuredBaseUrl.trim(), _cloudBaseUrl, _localBaseUrl]
        .where((value) => value.isNotEmpty)
        .map(_normalizeBaseUrl)
        .toList();
    final seen = <String>{};
    final out = <String>[];
    for (final value in values) {
      if (seen.add(value)) out.add(value);
    }
    return out;
  }

  String get _preferredBaseUrl => _normalizeBaseUrl(_selectedEndpoint.trim().isEmpty ? _cloudBaseUrl : _selectedEndpoint.trim());

  String _endpointLabel(String endpoint) {
    if (endpoint == _cloudBaseUrl) return 'Cloud (DNS) - $endpoint';
    if (endpoint == _localBaseUrl) return 'Local fallback - $endpoint';
    return 'Custom - $endpoint';
  }

  Future<void> _selectEndpoint(String endpoint) async {
    final normalized = _normalizeBaseUrl(endpoint);
    if (!mounted) return;
    setState(() {
      _selectedEndpoint = normalized;
      _error = null;
    });
    await widget.onUpdateSettings(AppSettings(baseUrl: normalized, member: widget.settings.member));
  }

  Future<void> _showAddServerDialog() async {
    final ctrl = TextEditingController();
    final value = await showDialog<String>(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: const Text('Add Server'),
          content: TextField(
            controller: ctrl,
            autofocus: true,
            decoration: const InputDecoration(labelText: 'Server URL or host'),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(context).pop(ctrl.text.trim()),
              child: const Text('Add'),
            ),
          ],
        );
      },
    );
    ctrl.dispose();

    if (value == null || value.trim().isEmpty) return;
    final normalized = _normalizeBaseUrl(value.trim());
    if (!mounted) return;
    setState(() {
      final updated = List<String>.from(_configuredEndpoints);
      if (!updated.contains(normalized)) {
        updated.insert(0, normalized);
      }
      _configuredEndpoints = updated;
      _selectedEndpoint = normalized;
      _error = null;
    });
    await widget.onUpdateSettings(AppSettings(baseUrl: normalized, member: widget.settings.member));
  }

  Future<void> _bootstrap() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final preferredBaseUrl = _preferredBaseUrl;
      final reachable = await _firstReachableEndpoint(preferredBaseUrl);
      _effectiveBaseUrl = reachable ?? preferredBaseUrl;
      await widget.onUpdateSettings(AppSettings(baseUrl: preferredBaseUrl, member: widget.settings.member));
      await _loadAuthStatus();
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<T> _runWithEndpointFailover<T>(Future<T> Function(String baseUrl) action) async {
    final seed = _preferredBaseUrl;
    final rawCandidates = <String>[
      _preferredBaseUrl,
      _effectiveBaseUrl,
      widget.settings.baseUrl,
      ..._endpointCandidates(seed),
      ..._configuredEndpoints,
    ];
    final seen = <String>{};
    final candidates = <String>[];
    for (final raw in rawCandidates) {
      if (raw.trim().isEmpty) continue;
      final normalized = _normalizeBaseUrl(raw.trim());
      if (seen.add(normalized)) candidates.add(normalized);
    }

    Object? lastNetworkError;
    for (final baseUrl in candidates) {
      try {
        final result = await action(baseUrl);
        if (_effectiveBaseUrl != baseUrl) {
          _effectiveBaseUrl = baseUrl;
        }
        return result;
      } on SocketException catch (err) {
        lastNetworkError = err;
      } on TimeoutException catch (err) {
        lastNetworkError = err;
      } on http.ClientException catch (err) {
        lastNetworkError = err;
      }
    }

    if (lastNetworkError != null) {
      throw lastNetworkError;
    }
    throw Exception('Unable to reach HomeHub backend.');
  }

  Future<void> _loadAuthStatus() async {
    final response = await _runWithEndpointFailover((baseUrl) {
      final uri = _apiUri(baseUrl, '/api/auth/status');
      return _httpClient.get(uri).timeout(const Duration(seconds: 15));
    });
    final body = jsonDecode(response.body);
    if (response.statusCode < 200 || response.statusCode >= 300 || body is! Map<String, dynamic>) {
      throw Exception('Auth status failed (${response.statusCode})');
    }

    final members = (body['members'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
    final currentMember = '${body['current_member'] ?? ''}'.trim();
    final selected = currentMember.isNotEmpty
        ? currentMember
        : (widget.settings.member.trim().isNotEmpty
            ? widget.settings.member.trim()
            : (members.isNotEmpty ? '${members.first['name'] ?? ''}'.trim() : ''));

    if (!mounted) return;
    setState(() {
      _requiresPassword = body['requires_password'] == true;
      _authed = body['authed'] == true;
      _members = members;
      _selectedMember = selected.isEmpty ? null : selected;
      _loading = false;
      _error = null;
    });

    if ((_requiresPassword == false || _authed) && currentMember.isNotEmpty) {
      final configuredBase = _preferredBaseUrl;
      final next = AppSettings(baseUrl: configuredBase, member: currentMember);
      await widget.onUpdateSettings(next);
      if (!mounted) return;
      widget.onAuthenticated(next);
    }
  }

  Future<void> _submitPassword() async {
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final response = await _runWithEndpointFailover((baseUrl) {
        final uri = _apiUri(baseUrl, '/api/auth/login');
        return _httpClient
            .post(
              uri,
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'password': _passwordCtrl.text}),
            )
            .timeout(const Duration(seconds: 15));
      });
      final body = jsonDecode(response.body);
      if (response.statusCode < 200 || response.statusCode >= 300 || body is! Map<String, dynamic> || body['ok'] == false) {
        throw Exception('${body is Map<String, dynamic> ? (body['error'] ?? 'Invalid password') : 'Invalid password'}');
      }
      await _loadAuthStatus();
    } catch (err) {
      if (!mounted) return;
      setState(() => _error = '$err');
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _submitMemberLogin() async {
    final name = (_selectedMember ?? '').trim();
    if (name.isEmpty) {
      setState(() => _error = 'Select a member');
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
    });
    try {
      final response = await _runWithEndpointFailover((baseUrl) {
        final uri = _apiUri(baseUrl, '/api/member-login');
        return _httpClient
            .post(
              uri,
              headers: {'Content-Type': 'application/json'},
              body: jsonEncode({'name': name, 'pin': _pinCtrl.text.trim()}),
            )
            .timeout(const Duration(seconds: 15));
      });
      final body = jsonDecode(response.body);
      if (response.statusCode < 200 || response.statusCode >= 300 || body is! Map<String, dynamic> || body['ok'] == false) {
        throw Exception('${body is Map<String, dynamic> ? (body['error'] ?? 'Login failed') : 'Login failed'}');
      }
      final configuredBase = _preferredBaseUrl;
      final next = AppSettings(baseUrl: configuredBase, member: name);
      await widget.onUpdateSettings(next);
      if (!mounted) return;
      widget.onAuthenticated(next);
    } catch (err) {
      if (!mounted) return;
      setState(() => _error = '$err');
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    final selectedMemberHasPin = _members
        .where((row) => '${row['name'] ?? ''}' == (_selectedMember ?? ''))
        .map((row) => row['has_pin'] == true)
        .cast<bool>()
        .fold<bool>(false, (prev, value) => value);

    return Scaffold(
      appBar: AppBar(title: const Text('HomeHub Sign In')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            DropdownButtonFormField<String>(
              initialValue: _selectedEndpoint,
              decoration: const InputDecoration(labelText: 'Backend URL'),
              items: _configuredEndpoints
                  .map((endpoint) => DropdownMenuItem<String>(value: endpoint, child: Text(_endpointLabel(endpoint))))
                  .toList(),
              onChanged: _submitting
                  ? null
                  : (value) {
                      if (value == null) return;
                      _selectEndpoint(value);
                    },
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                OutlinedButton.icon(
                  onPressed: _submitting ? null : _showAddServerDialog,
                  icon: const Icon(Icons.add),
                  label: const Text('Add Server'),
                ),
                const Spacer(),
                OutlinedButton.icon(
                  onPressed: _submitting ? null : _bootstrap,
                  icon: const Icon(Icons.sync),
                  label: const Text('Recheck Endpoint'),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              'Current endpoint used: $_effectiveBaseUrl',
              style: const TextStyle(fontSize: 12, color: Color(0xFF475569), fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            if (_requiresPassword && !_authed) ...[
              const Text('Hub password required', style: TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              TextField(
                controller: _passwordCtrl,
                obscureText: true,
                decoration: const InputDecoration(labelText: 'Hub Password'),
              ),
              const SizedBox(height: 10),
              FilledButton.icon(
                onPressed: _submitting ? null : _submitPassword,
                icon: const Icon(Icons.lock_open),
                label: Text(_submitting ? 'Signing in...' : 'Sign In'),
              ),
            ] else ...[
              DropdownButtonFormField<String>(
                initialValue: _selectedMember,
                decoration: const InputDecoration(labelText: 'Member'),
                items: _members
                    .map((row) => DropdownMenuItem<String>(value: '${row['name'] ?? ''}', child: Text('${row['name'] ?? ''}')))
                    .toList(),
                onChanged: (value) => setState(() => _selectedMember = value),
              ),
              const SizedBox(height: 8),
              TextField(
                controller: _pinCtrl,
                keyboardType: TextInputType.number,
                obscureText: true,
                decoration: InputDecoration(labelText: selectedMemberHasPin ? 'PIN' : 'PIN (optional for first login)'),
              ),
              const SizedBox(height: 10),
              FilledButton.icon(
                onPressed: _submitting ? null : _submitMemberLogin,
                icon: const Icon(Icons.person),
                label: Text(_submitting ? 'Continuing...' : 'Continue'),
              ),
            ],
            if (_error != null) ...[
              const SizedBox(height: 10),
              Text(_error!, style: const TextStyle(color: Colors.red)),
            ],
          ],
        ),
      ),
    );
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({
    super.key,
    required this.settings,
    required this.onUpdateSettings,
  });

  final AppSettings settings;
  final Future<void> Function(AppSettings settings) onUpdateSettings;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> with WidgetsBindingObserver {
  final FlutterLocalNotificationsPlugin _notifications = FlutterLocalNotificationsPlugin();
  late HomeHubApi _api;
  bool _bootstrapping = true;
  int _index = 0;
  String _hubName = 'HomeHub';
  Timer? _notificationTimer;
  bool _notificationBootstrapDone = false;
  int _lastNotifiedId = 0;
  bool _privacyMaskEnabled = false;
  bool _memberHasPin = false;
  bool _pinLocked = false;
  bool _unlockPromptOpen = false;
  StreamSubscription<_SseEvent>? _eventsSub;
  Timer? _eventsReconnectTimer;

  String get _privacyMaskKey {
    final base = widget.settings.baseUrl.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    final member = widget.settings.member.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    return 'homehub_privacy_mask_${base}_$member';
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _bootstrapApi();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (!_memberHasPin) return;
    if (state == AppLifecycleState.inactive || state == AppLifecycleState.paused || state == AppLifecycleState.detached) {
      _pinLocked = true;
      return;
    }
    if (state == AppLifecycleState.resumed) {
      _ensurePinUnlocked();
    }
  }

  Future<void> _bootstrapApi() async {
    AppSettings effective = widget.settings;
    final reachable = await _firstReachableEndpoint(widget.settings.baseUrl);
    if (reachable != null && _normalizeBaseUrl(widget.settings.baseUrl) != reachable) {
      effective = AppSettings(baseUrl: reachable, member: widget.settings.member);
      await widget.onUpdateSettings(effective);
    }

    _api = HomeHubApi(effective);
    try {
      final settingsObj = await _api.fetchHubSettings();
      final value = '${settingsObj['hub_name'] ?? ''}'.trim();
      if (value.isNotEmpty) {
        _hubName = value;
      }
    } catch (_) {}
    await _refreshPinRequirement();
    await _syncCurrentMemberSession();
    await _loadPrivacyMask();
    await _initNativeNotifications();
    _startLiveEvents();
    await _ensurePinUnlocked();
    if (!mounted) return;
    setState(() {
      _index = 0;
      _bootstrapping = false;
    });
  }

  Future<void> _refreshPinRequirement() async {
    try {
      final status = await _api.fetchAuthStatus();
      final members = (status['members'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
      final row = members.firstWhere(
        (m) => '${m['name'] ?? ''}'.trim().toLowerCase() == widget.settings.member.trim().toLowerCase(),
        orElse: () => const <String, dynamic>{},
      );
      final hasPin = row['has_pin'] == true;
      _memberHasPin = hasPin;
      if (hasPin) {
        _pinLocked = true;
      }
    } catch (_) {}
  }

  Future<void> _syncCurrentMemberSession() async {
    try {
      final current = await _api.fetchCurrentMember();
      final currentMember = '${current['member'] ?? ''}'.trim();
      final expected = widget.settings.member.trim();
      if (currentMember.toLowerCase() == expected.toLowerCase()) return;
      if (!_memberHasPin) {
        await _api.loginMember(name: expected);
      }
    } catch (_) {}
  }

  Future<void> _ensurePinUnlocked() async {
    if (!_memberHasPin || !_pinLocked || _unlockPromptOpen || !mounted) return;
    _unlockPromptOpen = true;
    final pinCtrl = TextEditingController();
    String? error;
    while (mounted && _pinLocked) {
      final result = await showDialog<bool>(
        context: context,
        barrierDismissible: false,
        builder: (context) => StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
            title: const Text('Unlock HomeHub'),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Enter the PIN for ${widget.settings.member} to continue.'),
                  const SizedBox(height: 10),
                  TextField(
                    controller: pinCtrl,
                    keyboardType: TextInputType.number,
                    obscureText: true,
                    maxLength: 4,
                    decoration: const InputDecoration(labelText: 'PIN'),
                  ),
                  if (error != null) ...[
                    const SizedBox(height: 8),
                    Text(error!, style: const TextStyle(color: Colors.red)),
                  ],
                ],
              ),
            ),
            actions: [
              FilledButton(
                onPressed: () async {
                  final pin = pinCtrl.text.trim();
                  if (pin.length != 4) {
                    setDialogState(() => error = 'PIN must be 4 digits');
                    return;
                  }
                  try {
                    await _api.loginMember(name: widget.settings.member, pin: pin);
                    if (!context.mounted) return;
                    Navigator.pop(context, true);
                  } catch (_) {
                    setDialogState(() => error = 'Invalid PIN');
                  }
                },
                child: const Text('Unlock'),
              ),
            ],
          ),
        ),
      );
      if (result == true) {
        _pinLocked = false;
        break;
      }
    }
    pinCtrl.dispose();
    _unlockPromptOpen = false;
  }

  Future<void> _loadPrivacyMask() async {
    final prefs = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() => _privacyMaskEnabled = prefs.getBool(_privacyMaskKey) ?? false);
  }

  Future<void> _togglePrivacyMask() async {
    final next = !_privacyMaskEnabled;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_privacyMaskKey, next);
    if (!mounted) return;
    setState(() => _privacyMaskEnabled = next);
  }

  Future<void> _initNativeNotifications() async {
    const androidSettings = AndroidInitializationSettings('@mipmap/ic_launcher');
    const settings = InitializationSettings(android: androidSettings);
    await _notifications.initialize(
      settings,
      onDidReceiveNotificationResponse: _onNotificationResponse,
      onDidReceiveBackgroundNotificationResponse: notificationTapBackground,
    );

    final android = _notifications.resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>();
    await android?.requestNotificationsPermission();

    final prefs = await SharedPreferences.getInstance();
    _lastNotifiedId = prefs.getInt(_notificationKey) ?? 0;

    await Workmanager().registerPeriodicTask(
      'homehub-notification-poll-${widget.settings.member}',
      _notificationTaskName,
      frequency: const Duration(minutes: 15),
      existingWorkPolicy: ExistingPeriodicWorkPolicy.update,
      constraints: Constraints(networkType: NetworkType.connected),
      initialDelay: const Duration(minutes: 1),
    );

    _notificationTimer = Timer.periodic(
      const Duration(seconds: 10),
      (_) => _pollAndNotify(),
    );
    await _pollAndNotify();
  }

  Future<void> _onNotificationResponse(NotificationResponse response) async {
    if (response.actionId != _ackActionId) return;
    final payload = response.payload ?? '';
    if (payload.isEmpty) return;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(payload, true);
    if (payload.startsWith('notif_ack_')) {
      final idValue = int.tryParse(payload.substring('notif_ack_'.length));
      if (idValue != null) {
        try {
          await _api.markNotificationRead(idValue);
        } catch (_) {}
      }
    }
    if (response.id != null) {
      await _notifications.cancel(response.id!);
    }
  }

  void _startLiveEvents() {
    _eventsReconnectTimer?.cancel();
    _eventsSub?.cancel();
    _eventsSub = _sseConnect(_api.eventsStreamUri()).listen(
      _handleLiveEvent,
      onError: (_) => _scheduleLiveReconnect(),
      onDone: _scheduleLiveReconnect,
      cancelOnError: true,
    );
  }

  void _scheduleLiveReconnect() {
    if (!mounted) return;
    _eventsReconnectTimer?.cancel();
    _eventsReconnectTimer = Timer(const Duration(seconds: 3), _startLiveEvents);
  }

  void _handleLiveEvent(_SseEvent event) {
    if (!mounted) return;
    if (event.event == 'notifications') {
      _pollAndNotify();
      return;
    }
    if (event.event == 'timeline') {
      if (_index == 0) {
        setState(() {});
      }
    }
  }

  String get _notificationKey {
    final base = widget.settings.baseUrl.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    final member = widget.settings.member.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    return 'homehub_last_notification_${base}_$member';
  }

  Future<void> _pollAndNotify() async {
    try {
      await _checkScheduledFamilyNotifications();

      final unread = await _api.fetchNotifications(unreadOnly: true);
      if (unread.isEmpty) return;
      unread.sort((a, b) => a.id.compareTo(b.id));

      if (!_notificationBootstrapDone) {
        _notificationBootstrapDone = true;
        _lastNotifiedId = unread.last.id > _lastNotifiedId ? unread.last.id : _lastNotifiedId;
        final prefs = await SharedPreferences.getInstance();
        await prefs.setInt(_notificationKey, _lastNotifiedId);
        return;
      }

      for (final item in unread) {
        if (item.id <= _lastNotifiedId) continue;
        await _notifications.show(
          item.id,
          item.title.isNotEmpty ? item.title : 'HomeHub Notification',
          item.message,
          NotificationDetails(
            android: AndroidNotificationDetails(
              _notificationChannelId,
              _notificationChannelName,
              channelDescription: _notificationChannelDesc,
              importance: Importance.max,
              priority: Priority.high,
              actions: const [AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true)],
            ),
          ),
          payload: 'notif_ack_${item.id}',
        );
        _lastNotifiedId = item.id;
      }

      final prefs = await SharedPreferences.getInstance();
      await prefs.setInt(_notificationKey, _lastNotifiedId);
    } catch (_) {
      // keep silent; network may be temporarily unavailable
    }
  }

  Future<void> _checkScheduledFamilyNotifications() async {
    final now = _easternNow();
    final prefs = await SharedPreferences.getInstance();
    final minuteKey = 'homehub_schedule_minute_${widget.settings.member}_${_todayKey(now)}_${now.hour}_${now.minute}';
    if (prefs.getBool(minuteKey) == true) return;
    bool completed = false;
    try {
      final today = _fmtDate(now);
      List<MealPlannerEntry> todaysMeals = const <MealPlannerEntry>[];
      try {
        final meals = await _api.fetchMealEntries(now);
        todaysMeals = meals.where((entry) => _fmtDate(entry.date) == today && !entry.isLocked).toList();
      } catch (_) {}

      for (final mealType in _mealTypes) {
        final trigger = _timeForToday(now, _mealReminderTime[mealType] ?? '');
        if (trigger == null || !_isWithinScheduledWindow(now, trigger)) continue;

        final slotItems = todaysMeals.where((entry) => entry.mealType == mealType).toList();

        final shownKey = _scheduledShownKey(widget.settings.baseUrl, widget.settings.member, 'meal_$mealType', now);
        if (prefs.getBool(shownKey) == true) continue;

        final label = _mealLabel(mealType);
        final planned = slotItems.map((entry) => entry.title).where((value) => value.trim().isNotEmpty).take(3).join(' • ');
        final title = '$label reminder';
        final body = planned.isEmpty ? '$label - no meal planned.' : '$label - $planned';

        await _notifications.show(
          _scheduledNotificationId('meal_$mealType', now),
          title,
          body.isEmpty ? 'It is time for ${_mealLabel(mealType).toLowerCase()}.' : body,
          NotificationDetails(
            android: AndroidNotificationDetails(
              _notificationChannelId,
              _notificationChannelName,
              channelDescription: _notificationChannelDesc,
              importance: Importance.max,
              priority: Priority.high,
              actions: const [
                AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true),
              ],
            ),
          ),
          payload: shownKey,
        );
        await prefs.setBool(shownKey, true);
        await prefs.setString(
          _scheduledDiagKey(widget.settings.baseUrl, widget.settings.member, 'last_fired_meal_$mealType'),
          now.toIso8601String(),
        );
      }

      final storyTrigger = _timeForToday(now, _storyReminderTime);
      if (storyTrigger != null && _isWithinScheduledWindow(now, storyTrigger)) {
        final shownKey = _scheduledShownKey(widget.settings.baseUrl, widget.settings.member, 'story_reader', now);
        if (prefs.getBool(shownKey) != true) {
          String reader = 'Story reader not set';
          try {
            final routines = await _api.fetchRoutines();
            final info = routines['routines'] as Map<String, dynamic>? ?? {};
            final bedtime = info['bedtime'] as Map<String, dynamic>? ?? {};
            final value = '${bedtime['reader'] ?? ''}'.trim();
            if (value.isNotEmpty) reader = value;
          } catch (_) {}

          await _notifications.show(
            _scheduledNotificationId('story_reader', now),
            'Bedtime Reader Tonight',
            '$reader is the reader tonight.',
            NotificationDetails(
              android: AndroidNotificationDetails(
                _notificationChannelId,
                _notificationChannelName,
                channelDescription: _notificationChannelDesc,
                importance: Importance.max,
                priority: Priority.high,
                actions: const [
                  AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true),
                ],
              ),
            ),
            payload: shownKey,
          );
          await prefs.setBool(shownKey, true);
          await prefs.setString(
            _scheduledDiagKey(widget.settings.baseUrl, widget.settings.member, 'last_fired_story_reader'),
            now.toIso8601String(),
          );
        }
      }

      try {
        final remindersData = await _api.fetchRemindersMonth(now);
        final reminders = remindersData['reminders'] as List<dynamic>? ?? const [];
        for (final raw in reminders) {
          if (raw is! Map<String, dynamic>) continue;
          if ('${raw['date'] ?? ''}' != today) continue;
          final rawTime = '${raw['time'] ?? ''}'.trim();
          if (rawTime.length < 5) continue;
          final trigger = _timeForToday(now, rawTime.substring(0, 5));
          if (trigger == null || !_isWithinScheduledWindow(now, trigger)) continue;

          final slot = _reminderSlot(raw);
          final shownKey = _scheduledShownKey(widget.settings.baseUrl, widget.settings.member, slot, now);
          if (prefs.getBool(shownKey) == true) continue;

          final title = '${raw['title'] ?? 'Reminder'}'.trim();
          final description = '${raw['description'] ?? ''}'.trim();
          final body = description.isNotEmpty ? description : 'Reminder for $rawTime';

          await _notifications.show(
            _scheduledNotificationId(slot, now),
            title.isEmpty ? 'Reminder' : title,
            body,
            NotificationDetails(
              android: AndroidNotificationDetails(
                _notificationChannelId,
                _notificationChannelName,
                channelDescription: _notificationChannelDesc,
                importance: Importance.max,
                priority: Priority.high,
                actions: const [
                  AndroidNotificationAction(_ackActionId, 'Acknowledge', cancelNotification: true),
                ],
              ),
            ),
            payload: shownKey,
          );
          await prefs.setBool(shownKey, true);
          await prefs.setString(
            _scheduledDiagKey(widget.settings.baseUrl, widget.settings.member, 'last_fired_$slot'),
            now.toIso8601String(),
          );
        }
      } catch (_) {}

      await prefs.setString(
        _scheduledDiagKey(widget.settings.baseUrl, widget.settings.member, 'last_check'),
        now.toIso8601String(),
      );
      completed = true;
    } finally {
      if (completed) {
        await prefs.setBool(minuteKey, true);
      }
    }
  }

  @override
  void dispose() {
    _notificationTimer?.cancel();
    _eventsReconnectTimer?.cancel();
    _eventsSub?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  Future<void> _openQuickAddSheet() async {
    final changed = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      builder: (context) => QuickAddSheet(api: _api),
    );
    if (changed == true && mounted) {
      setState(() {});
    }
  }

  int _sectionIndexByLabel(List<AppSection> sections, String label) {
    return sections.indexWhere((s) => s.label == label);
  }

  Future<void> _openLauncher(List<AppSection> sections) async {
    final result = await showModalBottomSheet<_LauncherResult>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => AppLauncherSheet(
        sections: sections,
        selectedIndex: _index,
      ),
    );
    if (result == null) return;

    if (result.sectionIndex != null) {
      setState(() => _index = result.sectionIndex!);
      return;
    }

    final query = result.query.trim();
    if (query.isEmpty || result.action == null) return;
    await _handleLauncherSearchAction(result.action!, query, sections);
  }

  Future<void> _handleLauncherSearchAction(
    LauncherSearchAction action,
    String query,
    List<AppSection> sections,
  ) async {
    switch (action) {
      case LauncherSearchAction.addShopping:
        await _addShoppingItemFromSearch(query, sections);
        return;
      case LauncherSearchAction.findEvent:
        await _findOrCreateEventFromSearch(query, sections);
        return;
      case LauncherSearchAction.addEvent:
        await _createEventFromSearch(query, sections);
        return;
      case LauncherSearchAction.addRecipe:
        await _addRecipeFromSearch(query, sections);
        return;
      case LauncherSearchAction.addExpense:
        await _quickAddMoneyFromSearch(query, 'expense', sections);
        return;
      case LauncherSearchAction.addIncome:
        await _quickAddMoneyFromSearch(query, 'income', sections);
        return;
    }
  }

  Future<void> _addShoppingItemFromSearch(String query, List<AppSection> sections) async {
    try {
      final lists = await _api.fetchShoppingLists();
      if (lists.isEmpty) throw Exception('No shopping lists found');

      int targetListId = _toInt(lists.first['id']);
      if (lists.length > 1 && mounted) {
        final selected = await showDialog<int>(
          context: context,
          builder: (context) => AlertDialog(
            insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
            title: const Text('Choose Shopping List'),
            content: SizedBox(
              width: 360,
              child: ListView(
                shrinkWrap: true,
                children: lists
                    .map(
                      (row) => ListTile(
                        title: Text('${row['name'] ?? 'List'}'),
                        onTap: () => Navigator.of(context).pop(_toInt(row['id'])),
                      ),
                    )
                    .toList(),
              ),
            ),
          ),
        );
        if (selected == null) return;
        targetListId = selected;
      }

      await _api.createShoppingItem(listId: targetListId, item: query);
      if (!mounted) return;
      final idx = _sectionIndexByLabel(sections, 'Shopping List');
      if (idx >= 0) setState(() => _index = idx);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added "$query" to shopping list')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Could not add shopping item: $err')));
    }
  }

  Future<void> _findOrCreateEventFromSearch(String query, List<AppSection> sections) async {
    try {
      final now = DateTime.now();
      final rows = await _api.fetchCalendarEvents(
        start: now.subtract(const Duration(days: 365)),
        end: now.add(const Duration(days: 365)),
      );
      final q = query.toLowerCase();
      final matches = rows.where((e) => e.title.toLowerCase().contains(q)).take(6).toList();

      if (matches.isNotEmpty) {
        if (!mounted) return;
        final pick = await showDialog<CalendarEvent?>(
          context: context,
          builder: (context) => AlertDialog(
            insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
            title: const Text('Matching Events'),
            content: SizedBox(
              width: 420,
              child: ListView(
                shrinkWrap: true,
                children: [
                  ...matches.map(
                    (event) => ListTile(
                      title: Text(event.title),
                      subtitle: Text('${_fmtDate(event.startDate)} • ${event.eventType}'),
                      onTap: () => Navigator.of(context).pop(event),
                    ),
                  ),
                  const Divider(),
                  ListTile(
                    leading: const Icon(Icons.add_circle_outline),
                    title: const Text('Create new event instead'),
                    onTap: () => Navigator.of(context).pop(null),
                  ),
                ],
              ),
            ),
          ),
        );

        if (!mounted) return;
        final calendarIdx = _sectionIndexByLabel(sections, 'Family Calendar');
        if (calendarIdx >= 0) setState(() => _index = calendarIdx);
        if (pick != null) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Located event: ${pick.title} (${_fmtDate(pick.startDate)})')),
          );
          return;
        }
      }

      await _createEventFromSearch(query, sections);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Event search failed: $err')));
    }
  }

  Future<void> _createEventFromSearch(String query, List<AppSection> sections) async {
    try {
      final now = DateTime.now();
      final isParty = query.toLowerCase().contains('party') || query.toLowerCase().contains('birthday');
      final draft = CalendarEventDraft(
        title: query,
        eventType: isParty ? 'party' : 'event',
        startDate: now,
        endDate: now,
        color: '#3b82f6',
        visibility: 'family',
        isAllDay: true,
        isRecurring: false,
        invitedMembers: const <String>[],
      );
      await _api.createCalendarEvent(draft);
      if (!mounted) return;
      final idx = _sectionIndexByLabel(sections, 'Family Calendar');
      if (idx >= 0) setState(() => _index = idx);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Created event "$query"')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create event failed: $err')));
    }
  }

  Future<void> _addRecipeFromSearch(String query, List<AppSection> sections) async {
    try {
      await _api.saveRecipe(
        title: query,
        ingredients: '',
        instructions: '',
      );
      if (!mounted) return;
      final idx = _sectionIndexByLabel(sections, 'Recipe Book');
      if (idx >= 0) setState(() => _index = idx);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Created recipe draft "$query"')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Could not create recipe: $err')));
    }
  }

  Future<void> _quickAddMoneyFromSearch(String query, String type, List<AppSection> sections) async {
    try {
      final amountMatch = RegExp(r'(\d+(?:\.\d{1,2})?)').firstMatch(query);
      final amount = amountMatch == null ? 1.0 : (double.tryParse(amountMatch.group(1) ?? '') ?? 1.0);
      await _api.quickAdd(
        entryType: type,
        recurrence: 'one-time',
        frequency: 'monthly',
        title: query,
        category: 'General',
        date: _fmtDate(DateTime.now()),
        amount: amount,
      );
      if (!mounted) return;
      final idx = _sectionIndexByLabel(sections, 'Expense & Money');
      if (idx >= 0) setState(() => _index = idx);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Added ${type == 'income' ? 'income' : 'expense'}: "$query"')),
      );
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Quick add failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_bootstrapping) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    final sections = <AppSection>[
      AppSection(
        label: 'Welcome',
        icon: Icons.home_outlined,
        nativeBuilder: (api, settings) => DashboardPage(api: api, member: settings.member),
      ),
      AppSection(label: 'Timeline', icon: Icons.timeline, nativeBuilder: (api, settings) => TimelinePage(api: api)),
      AppSection(label: 'Messages', icon: Icons.chat_bubble_outline, nativeBuilder: (api, settings) => ChatThreadsPage(api: api)),
      AppSection(label: 'Polls', icon: Icons.poll_outlined, nativeBuilder: (api, settings) => PollsPage(api: api)),
      AppSection(label: 'Projects & Spaces', icon: Icons.account_tree_outlined, nativeBuilder: (api, settings) => ProjectsSpacesPage(api: api)),
      AppSection(label: 'Shared Cloud', icon: Icons.cloud_outlined, nativeBuilder: (api, settings) => SharedCloudPage(api: api)),
      AppSection(label: 'Shopping List', icon: Icons.shopping_cart_outlined, nativeBuilder: (api, settings) => ShoppingListPage(api: api)),
      AppSection(label: 'To-Do/Chores', icon: Icons.checklist_outlined, nativeBuilder: (api, settings) => ChoresPage(api: api)),
      AppSection(label: 'Recipe Book', icon: Icons.menu_book_outlined, nativeBuilder: (api, settings) => RecipeBookPage(api: api)),
      AppSection(label: 'Expiry Tracker', icon: Icons.hourglass_top_outlined, nativeBuilder: (api, settings) => ExpiryTrackerPage(api: api)),
      AppSection(label: 'QR Generator', icon: Icons.qr_code_2_outlined, nativeBuilder: (api, settings) => QrGeneratorPage(api: api)),
      AppSection(label: 'Expense & Money', icon: Icons.receipt_long_outlined, nativeBuilder: (api, settings) => ExpensesPage(api: api)),
      AppSection(label: 'Family Calendar', icon: Icons.calendar_month_outlined, nativeBuilder: (api, settings) => CalendarPage(api: api)),
      AppSection(label: 'Meal Planner', icon: Icons.restaurant_menu, nativeBuilder: (api, settings) => MealPlannerPage(api: api)),
      AppSection(label: 'Notifications', icon: Icons.notifications_none, nativeBuilder: (api, settings) => NotificationsPage(api: api)),
      AppSection(label: 'Settings', icon: Icons.settings_outlined, nativeBuilder: (api, settings) => SettingsNativePage(api: api)),
    ];
    if (_index >= sections.length) {
      _index = 0;
    }
    final activeSection = sections[_index];
    final activePage = activeSection.nativeBuilder != null
        ? activeSection.nativeBuilder!.call(_api, widget.settings)
        : ModuleWebPage(
            settings: widget.settings,
            title: activeSection.label,
            path: activeSection.webPath ?? '/',
          );
    final isExpenseSection = activeSection.label == 'Expense & Money';
    final maskedSections = {
      'Welcome',
      'Timeline',
      'Messages',
      'Polls',
      'Projects & Spaces',
      'Shared Cloud',
      'Shopping List',
      'To-Do/Chores',
      'Recipe Book',
      'Expiry Tracker',
      'QR Generator',
      'Expense & Money',
      'Family Calendar',
      'Meal Planner',
      'Notifications',
    };
    final isMaskedSection = _privacyMaskEnabled && maskedSections.contains(activeSection.label);

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Open app launcher',
          icon: const Icon(Icons.apps),
          onPressed: () => _openLauncher(sections),
        ),
        title: Text(activeSection.label == 'Welcome' ? _hubName : '${activeSection.label} • ${widget.settings.member}'),
        actions: [
          IconButton(
            tooltip: _privacyMaskEnabled ? 'Disable privacy mask' : 'Enable privacy mask',
            icon: Icon(_privacyMaskEnabled ? Icons.visibility_off : Icons.visibility),
            onPressed: _togglePrivacyMask,
          ),
          IconButton(
            icon: const Icon(Icons.settings),
            onPressed: () async {
              final next = await Navigator.of(context).push<AppSettings>(
                MaterialPageRoute(builder: (_) => EditSettingsScreen(initial: widget.settings)),
              );
              if (next != null) {
                await widget.onUpdateSettings(next);
                if (mounted) {
                  setState(() {
                    _api = HomeHubApi(next);
                    _notificationBootstrapDone = false;
                  });
                }
              }
            },
          )
        ],
      ),
      body: SafeArea(
        child: Stack(
          children: [
            activePage,
            if (isMaskedSection)
              Positioned.fill(
                child: Container(
                  color: const Color(0xEAF1F5F9),
                  alignment: Alignment.center,
                  padding: const EdgeInsets.all(24),
                  child: Container(
                    constraints: const BoxConstraints(maxWidth: 380),
                    padding: const EdgeInsets.all(18),
                    decoration: BoxDecoration(
                      color: Colors.white,
                      borderRadius: BorderRadius.circular(18),
                      border: Border.all(color: const Color(0xFFE2E8F0)),
                    ),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Icon(Icons.lock_outline, size: 34, color: Color(0xFF4F46E5)),
                        const SizedBox(height: 10),
                        const Text(
                          'Privacy Mask is ON',
                          style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                        ),
                        const SizedBox(height: 8),
                        const Text(
                          'Sensitive family data is hidden so you can safely show the app.',
                          textAlign: TextAlign.center,
                          style: TextStyle(color: Color(0xFF64748B)),
                        ),
                        const SizedBox(height: 12),
                        FilledButton.icon(
                          onPressed: _togglePrivacyMask,
                          icon: const Icon(Icons.visibility),
                          label: const Text('Turn Off Privacy Mask'),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
      floatingActionButton: isExpenseSection
          ? FloatingActionButton.extended(
              onPressed: _openQuickAddSheet,
              icon: const Icon(Icons.add),
              label: const Text('Quick Add'),
            )
          : null,
    );
  }
}

class EditSettingsScreen extends StatefulWidget {
  const EditSettingsScreen({super.key, required this.initial});

  final AppSettings initial;

  @override
  State<EditSettingsScreen> createState() => _EditSettingsScreenState();
}

class _LauncherResult {
  const _LauncherResult({this.sectionIndex, this.action, this.query = ''});

  final int? sectionIndex;
  final LauncherSearchAction? action;
  final String query;
}

class AppLauncherSheet extends StatefulWidget {
  const AppLauncherSheet({
    super.key,
    required this.sections,
    required this.selectedIndex,
  });

  final List<AppSection> sections;
  final int selectedIndex;

  @override
  State<AppLauncherSheet> createState() => _AppLauncherSheetState();
}

class _AppLauncherSheetState extends State<AppLauncherSheet> {
  final TextEditingController _searchCtrl = TextEditingController();

  @override
  void dispose() {
    _searchCtrl.dispose();
    super.dispose();
  }

  void _submitAction(LauncherSearchAction action) {
    Navigator.of(context).pop(
      _LauncherResult(action: action, query: _searchCtrl.text.trim()),
    );
  }

  @override
  Widget build(BuildContext context) {
    final query = _searchCtrl.text.trim();
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 8, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          TextField(
            controller: _searchCtrl,
            autofocus: true,
            onChanged: (_) => setState(() {}),
            onSubmitted: (_) {
              if (query.isEmpty) return;
              final eventLike = query.toLowerCase().contains('event') || query.toLowerCase().contains('party') || query.toLowerCase().contains('birthday');
              _submitAction(eventLike ? LauncherSearchAction.findEvent : LauncherSearchAction.addShopping);
            },
            decoration: InputDecoration(
              hintText: 'Global search',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: query.isEmpty
                  ? null
                  : IconButton(
                      icon: const Icon(Icons.close),
                      onPressed: () {
                        _searchCtrl.clear();
                        setState(() {});
                      },
                    ),
            ),
          ),
          if (query.isNotEmpty) ...[
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                _LauncherActionChip(icon: Icons.shopping_cart_outlined, label: 'Add to Shopping', onTap: () => _submitAction(LauncherSearchAction.addShopping)),
                _LauncherActionChip(icon: Icons.event_outlined, label: 'Find Event', onTap: () => _submitAction(LauncherSearchAction.findEvent)),
                _LauncherActionChip(icon: Icons.add_circle_outline, label: 'Create Event', onTap: () => _submitAction(LauncherSearchAction.addEvent)),
                _LauncherActionChip(icon: Icons.menu_book_outlined, label: 'Add Recipe', onTap: () => _submitAction(LauncherSearchAction.addRecipe)),
                _LauncherActionChip(icon: Icons.receipt_long_outlined, label: 'Add Expense', onTap: () => _submitAction(LauncherSearchAction.addExpense)),
                _LauncherActionChip(icon: Icons.attach_money_outlined, label: 'Add Income', onTap: () => _submitAction(LauncherSearchAction.addIncome)),
              ],
            ),
            const SizedBox(height: 12),
          ] else
            const SizedBox(height: 10),
          Flexible(
            child: GridView.builder(
              shrinkWrap: true,
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 4,
                mainAxisSpacing: 10,
                crossAxisSpacing: 10,
                childAspectRatio: 0.9,
              ),
              itemCount: widget.sections.length,
              itemBuilder: (context, i) {
                final section = widget.sections[i];
                final selected = i == widget.selectedIndex;
                return InkWell(
                  borderRadius: BorderRadius.circular(14),
                  onTap: () => Navigator.of(context).pop(_LauncherResult(sectionIndex: i)),
                  child: Container(
                    decoration: BoxDecoration(
                      color: selected ? const Color(0xFFEDE9FE) : const Color(0xFFF8FAFC),
                      borderRadius: BorderRadius.circular(14),
                      border: Border.all(color: selected ? const Color(0xFF8B5CF6) : const Color(0xFFE2E8F0)),
                    ),
                    padding: const EdgeInsets.all(8),
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(section.icon, color: const Color(0xFF4F46E5), size: 24),
                        const SizedBox(height: 6),
                        Text(
                          section.label,
                          textAlign: TextAlign.center,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w600),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

class _LauncherActionChip extends StatelessWidget {
  const _LauncherActionChip({required this.icon, required this.label, required this.onTap});

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return ActionChip(
      avatar: Icon(icon, size: 18, color: const Color(0xFF4F46E5)),
      label: Text(label),
      onPressed: onTap,
    );
  }
}

class _EditSettingsScreenState extends State<EditSettingsScreen> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _urlCtrl = TextEditingController(text: widget.initial.baseUrl);
  late final TextEditingController _memberCtrl = TextEditingController(text: widget.initial.member);

  @override
  void dispose() {
    _urlCtrl.dispose();
    _memberCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Form(
          key: _formKey,
          child: Column(
            children: [
              TextFormField(
                controller: _urlCtrl,
                decoration: const InputDecoration(labelText: 'Backend URL'),
                validator: (v) => (v == null || v.trim().isEmpty) ? 'Required' : null,
              ),
              const SizedBox(height: 12),
              TextFormField(
                controller: _memberCtrl,
                decoration: const InputDecoration(labelText: 'Member name'),
                validator: (v) => (v == null || v.trim().isEmpty) ? 'Required' : null,
              ),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: () {
                  if (!_formKey.currentState!.validate()) return;
                  Navigator.of(context).pop(
                    AppSettings(baseUrl: _urlCtrl.text.trim(), member: _memberCtrl.text.trim()),
                  );
                },
                child: const Text('Save'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class ModuleWebPage extends StatefulWidget {
  const ModuleWebPage({
    super.key,
    required this.settings,
    required this.title,
    required this.path,
  });

  final AppSettings settings;
  final String title;
  final String path;

  @override
  State<ModuleWebPage> createState() => _ModuleWebPageState();
}

class _ModuleWebPageState extends State<ModuleWebPage> {
  late final WebViewController _controller;
  int _loadingProgress = 0;

  @override
  void initState() {
    super.initState();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFFF1F5F9))
      ..setNavigationDelegate(
        NavigationDelegate(
          onProgress: (progress) {
            if (!mounted) return;
            setState(() => _loadingProgress = progress);
          },
        ),
      )
      ..loadRequest(Uri.parse(_moduleUrl(widget.settings.baseUrl, widget.path)));
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        if (_loadingProgress < 100)
          LinearProgressIndicator(value: _loadingProgress == 0 ? null : _loadingProgress / 100),
        Expanded(
          child: RefreshIndicator(
            onRefresh: () => _controller.reload(),
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              children: [
                SizedBox(
                  height: MediaQuery.of(context).size.height,
                  child: WebViewWidget(controller: _controller),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key, required this.api, required this.member});

  final HomeHubApi api;
  final String member;

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  late Future<_DashboardData> _future;
  final TextEditingController _statusCtrl = TextEditingController();
  bool _statusBusy = false;
  bool _weatherGpsBusy = false;
  bool _bathCompleteBusy = false;
  bool _gpsPromptShown = false;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _statusCtrl.dispose();
    super.dispose();
  }

  Future<_DashboardData> _load() async {
    final now = DateTime.now();
    final remindersF = (() async {
      try {
        return await widget.api.fetchRemindersMonth(now);
      } catch (_) {
        return <String, dynamic>{'reminders': <dynamic>[]};
      }
    })();
    final notificationsF = (() async {
      try {
        return await widget.api.fetchNotifications(unreadOnly: true);
      } catch (_) {
        return <AppNotificationItem>[];
      }
    })();
    final timelineF = (() async {
      try {
        return await widget.api.fetchTimeline(limit: 5);
      } catch (_) {
        return <TimelinePost>[];
      }
    })();
    final membersF = (() async {
      try {
        return await widget.api.fetchFamilyMembers();
      } catch (_) {
        return <String, dynamic>{'members': <dynamic>[]};
      }
    })();

    Map<String, dynamic> routines = const {'ok': false, 'routines': <String, dynamic>{}};
    try {
      routines = await widget.api.fetchRoutines();
    } catch (_) {
      routines = const {'ok': false, 'routines': <String, dynamic>{}};
    }

    String hubName = 'HomeHub';
    try {
      final settings = await widget.api.fetchHubSettings();
      final value = '${settings['hub_name'] ?? ''}'.trim();
      if (value.isNotEmpty) hubName = value;
    } catch (_) {}

    String myStatus = '';
    try {
      final statusObj = await widget.api.fetchCurrentMemberStatus();
      myStatus = '${statusObj['status'] ?? ''}'.trim();
    } catch (_) {}

    final weatherLocation = await (() async {
      try {
        return await widget.api.fetchWeatherLocation();
      } catch (_) {
        return <String, dynamic>{};
      }
    })();

    Map<String, dynamic>? weather;
    final lat = (weatherLocation['latitude'] as num?)?.toDouble();
    final lon = (weatherLocation['longitude'] as num?)?.toDouble();
    if (lat != null && lon != null) {
      try {
        weather = await widget.api.fetchWeather(
          latitude: lat,
          longitude: lon,
          locationName: '${weatherLocation['location_name'] ?? ''}',
        );
      } catch (_) {
        weather = null;
      }
    }

    final pendingNotices = await (() async {
      try {
        return await widget.api.fetchPendingFamilyNotices();
      } catch (_) {
        return <Map<String, dynamic>>[];
      }
    })();

    final reminders = await remindersF;
    final notifications = await notificationsF;
    final timeline = await timelineF;
    final membersPayload = await membersF;
    final members = (membersPayload['members'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();

    return _DashboardData(
      hubName: hubName,
      myStatus: myStatus,
      reminders: reminders,
      routines: routines,
      timeline: timeline,
      members: members,
      weather: weather,
      weatherLocation: weatherLocation,
      pendingNotices: pendingNotices,
      unreadNotifications: notifications.length,
    );
  }

  Future<void> _refresh() async {
    setState(() {
      _future = _load();
    });
    await _future;
  }

  Future<void> _saveStatus() async {
    final text = _statusCtrl.text.trim();
    if (text.isEmpty) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Status cannot be empty')));
      return;
    }
    setState(() => _statusBusy = true);
    try {
      await widget.api.saveCurrentMemberStatus(text);
      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Status saved')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Save failed: $err')));
    } finally {
      if (mounted) setState(() => _statusBusy = false);
    }
  }

  Future<void> _clearStatus() async {
    setState(() => _statusBusy = true);
    try {
      await widget.api.clearCurrentMemberStatus();
      _statusCtrl.clear();
      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Status cleared')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Clear failed: $err')));
    } finally {
      if (mounted) setState(() => _statusBusy = false);
    }
  }

  Future<void> _sendFamilyNotice() async {
    final messageCtrl = TextEditingController();
    bool email = true;
    bool mobile = true;
    bool requireAck = true;
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Send Family Notice'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: messageCtrl,
                  minLines: 3,
                  maxLines: 5,
                  decoration: const InputDecoration(labelText: 'Message'),
                ),
                const SizedBox(height: 10),
                CheckboxListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Email'),
                  value: email,
                  onChanged: (v) => setDialogState(() => email = v == true),
                ),
                CheckboxListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Mobile'),
                  value: mobile,
                  onChanged: (v) => setDialogState(() => mobile = v == true),
                ),
                CheckboxListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  title: const Text('Require acknowledgement'),
                  value: requireAck,
                  onChanged: (v) => setDialogState(() => requireAck = v == true),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Send')),
          ],
        ),
      ),
    );
    if (should != true) return;

    final message = messageCtrl.text.trim();
    if (message.isEmpty) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Notice message is required')));
      return;
    }

    final channels = <String>['in_app'];
    if (email) channels.add('email');
    if (mobile) channels.add('mobile');

    try {
      await widget.api.sendFamilyNotice(message: message, channels: channels, requireAck: requireAck);
      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Family notice sent')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Send failed: $err')));
    }
  }

  Future<void> _acknowledgeNotice(int noticeId) async {
    try {
      await widget.api.acknowledgeFamilyNotice(noticeId);
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Acknowledge failed: $err')));
    }
  }

  String _bathCompletedMetaLabel(Map<String, dynamic> bath) {
    final by = '${bath['completed_by'] ?? ''}'.trim();
    final atRaw = '${bath['completed_at'] ?? ''}'.trim();
    if (by.isEmpty || atRaw.isEmpty) return 'Not completed yet today.';
    final parsed = DateTime.tryParse(atRaw);
    final at = parsed == null ? atRaw : _fmtDateTime(parsed.toLocal());
    return 'Last completed by $by at $at';
  }

  Future<void> _completeBathRoutine() async {
    if (_bathCompleteBusy) return;
    setState(() => _bathCompleteBusy = true);
    try {
      await widget.api.completeBathRoutine();
      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Bath assignments switched')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Failed to switch bath assignments: $err')));
    } finally {
      if (mounted) setState(() => _bathCompleteBusy = false);
    }
  }

  Future<void> _timelineLike(TimelinePost post) async {
    try {
      await widget.api.toggleTimelineLike(post.id);
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Like failed: $err')));
    }
  }

  Future<void> _timelineComment(TimelinePost post) async {
    final ctrl = TextEditingController();
    final text = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Add comment'),
        content: TextField(
          controller: ctrl,
          minLines: 2,
          maxLines: 4,
          decoration: const InputDecoration(hintText: 'Write a comment...'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, ctrl.text.trim()), child: const Text('Post')),
        ],
      ),
    );
    if (!mounted) return;
    final value = (text ?? '').trim();
    if (value.isEmpty) return;
    try {
      await widget.api.commentTimelinePost(post.id, value);
      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Comment added')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Comment failed: $err')));
    }
  }

  Future<void> _timelineShare(TimelinePost post) async {
    try {
      await widget.api.shareTimelinePost(post.id);
      final link = '${widget.api.settings.baseUrl.replaceAll(RegExp(r'/$'), '')}/timeline';
      await Clipboard.setData(ClipboardData(text: link));
      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Timeline link copied and share recorded')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Share failed: $err')));
    }
  }

  bool _hasWeatherCoords(Map<String, dynamic> weatherLocation) {
    final lat = (weatherLocation['latitude'] as num?)?.toDouble();
    final lon = (weatherLocation['longitude'] as num?)?.toDouble();
    return lat != null && lon != null;
  }

  bool _isGpsWeatherEnabled(Map<String, dynamic> weatherLocation) {
    return weatherLocation['use_gps_location'] == true;
  }

  Future<void> _maybePromptForGpsWeather(Map<String, dynamic> weatherLocation) async {
    if (_gpsPromptShown || !mounted) return;
    if (_hasWeatherCoords(weatherLocation) && _isGpsWeatherEnabled(weatherLocation)) return;
    _gpsPromptShown = true;

    final shouldUseGps = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Use your location for weather?'),
        content: const SingleChildScrollView(
          child: Text('Allow HomeHub to use Android location so Weather shows your current local forecast automatically.'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Not now')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Use My Location')),
        ],
      ),
    );

    if (shouldUseGps == true) {
      await _useCurrentLocationForWeather();
    }
  }

  Future<void> _useCurrentLocationForWeather() async {
    if (_weatherGpsBusy) return;
    if (mounted) {
      setState(() {
        _weatherGpsBusy = true;
      });
    }

    try {
      final serviceEnabled = await Geolocator.isLocationServiceEnabled();
      if (!serviceEnabled) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Turn on location services to use GPS weather.')),
        );
        return;
      }

      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }

      if (permission == LocationPermission.denied) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Location permission is required for GPS weather.')),
        );
        return;
      }

      if (permission == LocationPermission.deniedForever) {
        if (!mounted) return;
        final shouldOpen = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
            title: const Text('Location Permission Blocked'),
            content: const SingleChildScrollView(
              child: Text('Android is blocking location permission for HomeHub. Open app settings and enable Location to use weather from your current position.'),
            ),
            actions: [
              TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
              FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Open Settings')),
            ],
          ),
        );
        if (shouldOpen == true) {
          await Geolocator.openAppSettings();
        }
        return;
      }

      final LocationSettings highSettings = Platform.isAndroid
          ? AndroidSettings(
              accuracy: LocationAccuracy.high,
              timeLimit: Duration(seconds: 12),
              distanceFilter: 0,
            )
          : const LocationSettings(
              accuracy: LocationAccuracy.high,
              timeLimit: Duration(seconds: 12),
            );
      final LocationSettings mediumSettings = Platform.isAndroid
          ? AndroidSettings(
              accuracy: LocationAccuracy.medium,
              timeLimit: Duration(seconds: 10),
              distanceFilter: 0,
            )
          : const LocationSettings(
              accuracy: LocationAccuracy.medium,
              timeLimit: Duration(seconds: 10),
            );

      Position? position;
      try {
        position = await Geolocator.getCurrentPosition(locationSettings: highSettings);
      } catch (_) {
        position = await Geolocator.getLastKnownPosition();
      }

      if (position == null) {
        position = await Geolocator.getCurrentPosition(locationSettings: mediumSettings);
      }

      final lat = position.latitude;
      final lon = position.longitude;
      var locationName = 'My Location';
      try {
        final reverse = await widget.api.fetchLocationName(latitude: lat, longitude: lon);
        final resolved = '${reverse['location_name'] ?? ''}'.trim();
        if (resolved.isNotEmpty) locationName = resolved;
      } catch (_) {}

      await widget.api.saveWeatherLocation(
        latitude: lat,
        longitude: lon,
        locationName: locationName,
        useGpsLocation: true,
      );

      try {
        await widget.api.fetchWeather(latitude: lat, longitude: lon, locationName: locationName);
      } catch (_) {}

      await _refresh();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Weather location updated: $locationName')),
      );
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('GPS weather setup failed: $err')),
      );
    } finally {
      if (mounted) {
        setState(() {
          _weatherGpsBusy = false;
        });
      }
    }
  }

  Future<void> _configureWeatherLocation() async {
    final latCtrl = TextEditingController();
    final lonCtrl = TextEditingController();
    final nameCtrl = TextEditingController();
    final action = await showDialog<int>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Weather Location'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(controller: latCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true), decoration: const InputDecoration(labelText: 'Latitude')),
              const SizedBox(height: 8),
              TextField(controller: lonCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true, signed: true), decoration: const InputDecoration(labelText: 'Longitude')),
              const SizedBox(height: 8),
              TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Location name (optional)')),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, 0), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(context, 2), child: const Text('Use My Location')),
          FilledButton(onPressed: () => Navigator.pop(context, 1), child: const Text('Save Manual')),
        ],
      ),
    );

    if (action == 2) {
      await _useCurrentLocationForWeather();
      return;
    }
    if (action != 1) return;

    final lat = double.tryParse(latCtrl.text.trim());
    final lon = double.tryParse(lonCtrl.text.trim());
    if (lat == null || lon == null) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Latitude and longitude are required')));
      return;
    }
    try {
      await widget.api.saveWeatherLocation(latitude: lat, longitude: lon, locationName: nameCtrl.text.trim());
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Save location failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<_DashboardData>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(
              error: snapshot.error.toString(),
              onRetry: () {
                setState(() {
                  _future = _load();
                });
              },
            );
          }

          final data = snapshot.data!;
          final reminders = (data.reminders['reminders'] as List<dynamic>? ?? []);
          final routinesRaw = data.routines['routines'];
          final routines = routinesRaw is Map ? Map<String, dynamic>.from(routinesRaw) : <String, dynamic>{};
          final bedtime = routines['bedtime'] as Map<String, dynamic>? ?? {};
          final bath = routines['bath'] as Map<String, dynamic>? ?? {};
          final members = data.members;
          final timeline = data.timeline;
          final pendingNotices = data.pendingNotices;
          final weather = data.weather;
          final locationName = weather == null
              ? '${data.weatherLocation['location_name'] ?? ''}'
              : '${weather['location_name'] ?? data.weatherLocation['location_name'] ?? ''}';

          if (!_gpsPromptShown && (!_hasWeatherCoords(data.weatherLocation) || !_isGpsWeatherEnabled(data.weatherLocation))) {
            WidgetsBinding.instance.addPostFrameCallback((_) {
              _maybePromptForGpsWeather(data.weatherLocation);
            });
          }

          if (_statusCtrl.text.isEmpty && data.myStatus.isNotEmpty) {
            _statusCtrl.text = data.myStatus;
          }

          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: LayoutBuilder(
                    builder: (context, constraints) {
                      final compact = constraints.maxWidth < 430;
                      final headline = Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(data.hubName, style: const TextStyle(fontSize: 24, fontWeight: FontWeight.w800)),
                          const SizedBox(height: 4),
                          Text('Welcome back, ${widget.member}', style: const TextStyle(color: Color(0xFF1E293B), fontWeight: FontWeight.w600)),
                          const SizedBox(height: 6),
                          Text(
                            data.myStatus.trim().isEmpty ? 'My status: No status set yet.' : 'My status: ${data.myStatus}',
                            style: const TextStyle(color: Color(0xFF64748B)),
                          ),
                        ],
                      );
                      final noticeButton = FilledButton.icon(
                        onPressed: _sendFamilyNotice,
                        icon: const Icon(Icons.campaign_outlined),
                        label: const Text('Send Family Notice'),
                      );

                      if (compact) {
                        return Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            headline,
                            const SizedBox(height: 14),
                            noticeButton,
                          ],
                        );
                      }

                      return Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Expanded(child: headline),
                          const SizedBox(width: 12),
                          noticeButton,
                        ],
                      );
                    },
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      TextField(
                        controller: _statusCtrl,
                        minLines: 2,
                        maxLines: 4,
                        decoration: const InputDecoration(hintText: 'Update my status...'),
                      ),
                      const SizedBox(height: 10),
                      LayoutBuilder(
                        builder: (context, constraints) {
                          final compact = constraints.maxWidth < 360;
                          if (compact) {
                            return Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                FilledButton(
                                  onPressed: _statusBusy ? null : _saveStatus,
                                  child: Text(_statusBusy ? 'Saving...' : 'Save'),
                                ),
                                const SizedBox(height: 8),
                                OutlinedButton(
                                  onPressed: _statusBusy ? null : _clearStatus,
                                  child: const Text('Clear'),
                                ),
                              ],
                            );
                          }
                          return Row(
                            children: [
                              Expanded(
                                child: FilledButton(
                                  onPressed: _statusBusy ? null : _saveStatus,
                                  child: Text(_statusBusy ? 'Saving...' : 'Save'),
                                ),
                              ),
                              const SizedBox(width: 10),
                              Expanded(
                                child: OutlinedButton(
                                  onPressed: _statusBusy ? null : _clearStatus,
                                  child: const Text('Clear'),
                                ),
                              ),
                            ],
                          );
                        },
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              LayoutBuilder(
                builder: (context, constraints) {
                  final compact = constraints.maxWidth < 420;
                  if (compact) {
                    return Column(
                      children: [
                        _MetricCard(
                          title: 'Members',
                          value: '${members.length}',
                          subtitle: 'Family members',
                          icon: Icons.people_alt_outlined,
                        ),
                        const SizedBox(height: 10),
                        _MetricCard(
                          title: 'Reminders',
                          value: '${reminders.length}',
                          subtitle: 'This month',
                          icon: Icons.event_note,
                        ),
                      ],
                    );
                  }

                  return Row(
                    children: [
                      Expanded(
                        child: _MetricCard(
                          title: 'Members',
                          value: '${members.length}',
                          subtitle: 'Family members',
                          icon: Icons.people_alt_outlined,
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: _MetricCard(
                          title: 'Reminders',
                          value: '${reminders.length}',
                          subtitle: 'This month',
                          icon: Icons.event_note,
                        ),
                      ),
                    ],
                  );
                },
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Icon(Icons.wb_cloudy_outlined, color: Color(0xFF8B5CF6)),
                          const SizedBox(width: 8),
                          const Expanded(child: Text('Weather', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 22))),
                          TextButton(onPressed: _configureWeatherLocation, child: const Text('Settings')),
                        ],
                      ),
                      const SizedBox(height: 8),
                      if (weather == null)
                        Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(locationName.trim().isEmpty ? 'Location not configured.' : 'Unable to load weather for $locationName.'),
                            const SizedBox(height: 10),
                            Wrap(
                              spacing: 8,
                              runSpacing: 8,
                              children: [
                                FilledButton(
                                  onPressed: _weatherGpsBusy ? null : _useCurrentLocationForWeather,
                                  child: Text(_weatherGpsBusy ? 'Locating...' : 'Use My Location'),
                                ),
                                OutlinedButton(onPressed: _configureWeatherLocation, child: const Text('Set Manually')),
                              ],
                            ),
                          ],
                        )
                      else
                        Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(locationName.trim().isEmpty ? 'Current location' : locationName, style: const TextStyle(color: Color(0xFF64748B))),
                            const SizedBox(height: 8),
                            Row(
                              children: [
                                Text(_weatherEmoji(_toInt(weather['weather_code'])), style: const TextStyle(fontSize: 38)),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: Column(
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      Text('${_toDouble(weather['temperature']).toStringAsFixed(0)}°F', style: const TextStyle(fontSize: 40, fontWeight: FontWeight.w800)),
                                      Text('${weather['description'] ?? ''}'),
                                      Text('Feels like ${_toDouble(weather['apparent_temperature']).toStringAsFixed(0)}°F', style: const TextStyle(color: Color(0xFF64748B))),
                                    ],
                                  ),
                                ),
                              ],
                            ),
                            const SizedBox(height: 8),
                            Text('Wind ${_toDouble(weather['wind_speed']).toStringAsFixed(1)} mph • Humidity ${_toDouble(weather['humidity']).toStringAsFixed(0)}%'),
                            const SizedBox(height: 8),
                            Text('Last updated: ${_relativeTime('${weather['timestamp'] ?? ''}')}', style: const TextStyle(color: Color(0xFF64748B))),
                            const SizedBox(height: 8),
                            const Text('7-Day Forecast', style: TextStyle(fontWeight: FontWeight.w700)),
                            const SizedBox(height: 6),
                            SizedBox(
                              height: 110,
                              child: ListView(
                                scrollDirection: Axis.horizontal,
                                children: (weather['daily_forecast'] as List<dynamic>? ?? const <dynamic>[])
                                    .whereType<Map>()
                                    .map((raw) {
                                  final day = Map<String, dynamic>.from(raw);
                                  final max = _toDouble(day['max_temp']).toStringAsFixed(0);
                                  final min = _toDouble(day['min_temp']).toStringAsFixed(0);
                                  final date = '${day['date'] ?? ''}';
                                  final label = date.length >= 10 ? _weekdayShort(date) : date;
                                  return Container(
                                    width: 82,
                                    margin: const EdgeInsets.only(right: 8),
                                    padding: const EdgeInsets.all(8),
                                    decoration: BoxDecoration(
                                      border: Border.all(color: const Color(0xFFE2E8F0)),
                                      borderRadius: BorderRadius.circular(12),
                                    ),
                                    child: Column(
                                      mainAxisAlignment: MainAxisAlignment.center,
                                      children: [
                                        Text(label, style: const TextStyle(fontWeight: FontWeight.w700)),
                                        Text(_weatherEmoji(_toInt(day['weather_code'])), style: const TextStyle(fontSize: 24)),
                                        Text('$max°  $min°', style: const TextStyle(fontSize: 12)),
                                      ],
                                    ),
                                  );
                                }).toList(),
                              ),
                            ),
                          ],
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 10),
              _MetricCard(
                title: 'Unread Alerts',
                value: '${data.unreadNotifications}',
                subtitle: 'Notifications',
                icon: Icons.notifications_active,
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Family Members', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      if (members.isEmpty)
                        const Text('No family members yet.')
                      else
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: members.map((row) {
                            final name = '${row['name'] ?? ''}';
                            final avatar = '${row['avatar'] ?? ''}';
                            return Chip(
                              avatar: avatar.isNotEmpty
                                  ? CircleAvatar(backgroundImage: NetworkImage(avatar))
                                  : CircleAvatar(child: Text(name.isNotEmpty ? name[0].toUpperCase() : '?')),
                              label: Text(name),
                            );
                          }).toList(),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Bath Time & Bedtime', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        leading: const Icon(Icons.nightlight_round),
                        title: const Text('Bedtime Reader (Tonight)'),
                        subtitle: Text('${bedtime['reader'] ?? 'Rachel'}'),
                      ),
                      ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        leading: const Icon(Icons.bathtub_outlined),
                        title: const Text('Bath Time Pairings'),
                        subtitle: Text('Avery: ${bath['avery'] ?? 'Avery & Mommy'}\nHarley: ${bath['harley'] ?? 'Harley & Daddy'}'),
                        isThreeLine: true,
                      ),
                      Text(
                        _bathCompletedMetaLabel(bath),
                        style: const TextStyle(fontSize: 12, color: Color(0xFF64748B)),
                      ),
                      const SizedBox(height: 8),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: FilledButton(
                          onPressed: _bathCompleteBusy ? null : _completeBathRoutine,
                          child: Text(_bathCompleteBusy ? 'Updating...' : 'Completed'),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              _TimelineFeedWrapper(
                title: 'Timeline',
                badgeLabel: 'Preview',
                posts: timeline,
                compact: true,
                imageUrlBuilder: (post) => post.imageUrl,
                onLikeBuilder: (post) => () => _timelineLike(post),
                onCommentBuilder: (post) => () => _timelineComment(post),
                onShareBuilder: (post) => () => _timelineShare(post),
              ),
              const SizedBox(height: 12),
              Text('Family Notice Acknowledgements', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              if (pendingNotices.isEmpty)
                const Card(child: ListTile(title: Text('No pending acknowledgements.')))
              else
                ...pendingNotices.map((notice) => Card(
                      child: ListTile(
                        title: Text('${notice['title'] ?? 'Family Notice'}'),
                        subtitle: Text('${notice['message'] ?? ''}\nFrom ${notice['sender'] ?? ''}'),
                        isThreeLine: true,
                        trailing: FilledButton(
                          onPressed: () => _acknowledgeNotice(_toInt(notice['id'])),
                          child: const Text('Acknowledge'),
                        ),
                      ),
                    )),
              const SizedBox(height: 12),
              Text('Upcoming reminders', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              ...reminders.take(5).map((raw) {
                final item = raw as Map<String, dynamic>;
                return Card(
                  child: ListTile(
                    title: Text('${item['title'] ?? 'Reminder'}'),
                    subtitle: Text('${item['date'] ?? ''}${item['time'] != null ? ' • ${item['time']}' : ''}'),
                    trailing: Text('${item['creator'] ?? ''}'),
                  ),
                );
              }),
            ],
          );
        },
      ),
    );
  }
}

class _DashboardData {
  _DashboardData({
    required this.hubName,
    required this.myStatus,
    required this.reminders,
    required this.routines,
    required this.timeline,
    required this.members,
    required this.weather,
    required this.weatherLocation,
    required this.pendingNotices,
    required this.unreadNotifications,
  });

  final String hubName;
  final String myStatus;
  final Map<String, dynamic> reminders;
  final Map<String, dynamic> routines;
  final List<TimelinePost> timeline;
  final List<Map<String, dynamic>> members;
  final Map<String, dynamic>? weather;
  final Map<String, dynamic> weatherLocation;
  final List<Map<String, dynamic>> pendingNotices;
  final int unreadNotifications;
}

class RemindersPage extends StatefulWidget {
  const RemindersPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<RemindersPage> createState() => _RemindersPageState();
}

class _RemindersPageState extends State<RemindersPage> {
  late Future<List<dynamic>> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<List<dynamic>> _load() async {
    final data = await widget.api.fetchRemindersMonth(DateTime.now());
    return data['reminders'] as List<dynamic>? ?? [];
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async {
        setState(() => _future = _load());
        await _future;
      },
      child: FutureBuilder<List<dynamic>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: snapshot.error.toString(), onRetry: () => setState(() => _future = _load()));
          }

          final rows = snapshot.data ?? [];
          if (rows.isEmpty) {
            return const Center(child: Text('No reminders found for this month.'));
          }

          return ListView.builder(
            padding: const EdgeInsets.all(12),
            itemCount: rows.length,
            itemBuilder: (context, index) {
              final item = rows[index] as Map<String, dynamic>;
              return Card(
                child: ListTile(
                  title: Text('${item['title'] ?? 'Reminder'}'),
                  subtitle: Text('${item['date'] ?? ''}${item['time'] != null ? ' • ${item['time']}' : ''}'),
                  trailing: Text('${item['creator'] ?? ''}'),
                ),
              );
            },
          );
        },
      ),
    );
  }
}

class TimelinePage extends StatefulWidget {
  const TimelinePage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<TimelinePage> createState() => _TimelinePageState();
}

class _TimelinePageState extends State<TimelinePage> {
  late Future<List<TimelinePost>> _future;
  final TextEditingController _composerCtrl = TextEditingController();
  final TextEditingController _composerImageCtrl = TextEditingController();
  bool _posting = false;
  List<Map<String, dynamic>> _recipes = const <Map<String, dynamic>>[];
  Timer? _liveTimer;
  StreamSubscription<_SseEvent>? _sseSub;
  Timer? _sseReconnectTimer;
  final Map<int, bool> _optimisticLiked = <int, bool>{};
  final Map<int, int> _optimisticLikesCount = <int, int>{};
  final Map<int, List<TimelineComment>> _optimisticComments = <int, List<TimelineComment>>{};

  static const Map<String, String> _imagePresets = {
    'Food': 'https://dummyimage.com/1080x1080/fed7aa/9a3412.png&text=Food',
    'Money': 'https://dummyimage.com/1080x1080/bbf7d0/166534.png&text=Money',
    'Event': 'https://dummyimage.com/1080x1080/bfdbfe/1d4ed8.png&text=Event',
    'Generic': 'https://dummyimage.com/1080x1080/e5e7eb/374151.png&text=Timeline',
  };

  @override
  void initState() {
    super.initState();
    _future = widget.api.fetchTimeline(limit: 60);
    _loadRecipes();
    _liveTimer = Timer.periodic(const Duration(seconds: 8), (_) => _refreshLive());
    _startSse();
  }

  @override
  void dispose() {
    _liveTimer?.cancel();
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _composerCtrl.dispose();
    _composerImageCtrl.dispose();
    super.dispose();
  }

  void _startSse() {
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _sseSub = _sseConnect(widget.api.eventsStreamUri()).listen(
      (event) {
        if (!mounted) return;
        if (event.event == 'timeline' || event.event == 'notifications') {
          _refreshLive();
        }
      },
      onError: (_) => _scheduleSseReconnect(),
      onDone: _scheduleSseReconnect,
      cancelOnError: true,
    );
  }

  void _scheduleSseReconnect() {
    if (!mounted) return;
    _sseReconnectTimer?.cancel();
    _sseReconnectTimer = Timer(const Duration(seconds: 3), _startSse);
  }

  String _absoluteUrl(String value) {
    final raw = value.trim();
    if (raw.isEmpty) return '';
    if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
    if (!raw.startsWith('/')) return raw;
    final base = widget.api.settings.baseUrl.replaceAll(RegExp(r'/$'), '');
    return '$base$raw';
  }

  Future<void> _loadRecipes() async {
    try {
      final rows = await widget.api.fetchRecipes();
      if (!mounted) return;
      setState(() => _recipes = rows);
    } catch (_) {}
  }

  String _normLookup(String value) {
    return value.toLowerCase().replaceAll(RegExp(r'[^a-z0-9]+'), ' ').trim();
  }

  bool _isFoodPost(TimelinePost post) {
    final text = _normLookup('${post.action} ${post.summary}');
    return text.contains('recipe') ||
        text.contains('meal') ||
        text.contains('food') ||
        text.contains('dinner') ||
        text.contains('breakfast') ||
        text.contains('lunch') ||
        text.contains('snack');
  }

  String _recipeImageForPost(TimelinePost post) {
    if (!_isFoodPost(post) || _recipes.isEmpty) return '';
    final summary = _normLookup(post.summary);
    if (summary.isEmpty) return '';
    for (final row in _recipes) {
      final title = _normLookup('${row['title'] ?? ''}');
      final photo = '${row['photo_url'] ?? ''}'.trim();
      if (title.isEmpty || photo.isEmpty) continue;
      if (summary.contains(title) || title.contains(summary)) {
        return photo;
      }
    }
    return '';
  }

  String _resolvedPostImage(TimelinePost post) {
    final custom = post.imageUrl.trim();
    if (custom.startsWith('http://') || custom.startsWith('https://')) {
      return custom;
    }
    if (custom.startsWith('/')) {
      return _absoluteUrl(custom);
    }
    final recipe = _recipeImageForPost(post);
    return _absoluteUrl(recipe);
  }

  bool _canEditPostImage(TimelinePost post) {
    final actor = post.actor.trim().toLowerCase();
    final me = widget.api.member.trim().toLowerCase();
    return actor.isNotEmpty && actor == me;
  }

  Future<void> _refresh() async {
    setState(() => _future = widget.api.fetchTimeline(limit: 60));
    await Future.wait([_future, _loadRecipes()]);
  }

  Future<void> _refreshLive() async {
    try {
      final rows = await widget.api.fetchTimeline(limit: 60);
      if (!mounted) return;
      setState(() => _future = Future<List<TimelinePost>>.value(rows));
    } catch (_) {}
  }

  TimelinePost _withOptimistic(TimelinePost post) {
    final liked = _optimisticLiked[post.id] ?? post.likedByMe;
    final likesCount = _optimisticLikesCount[post.id] ?? post.likesCount;
    final comments = _optimisticComments[post.id] ?? post.comments;
    return TimelinePost(
      id: post.id,
      timestamp: post.timestamp,
      actor: post.actor,
      action: post.action,
      summary: post.summary,
      imageUrl: post.imageUrl,
      entityType: post.entityType,
      entityId: post.entityId,
      likesCount: likesCount < 0 ? 0 : likesCount,
      commentsCount: comments.length,
      sharesCount: post.sharesCount,
      likedByMe: liked,
      comments: comments,
    );
  }

  Future<void> _post() async {
    final summary = _composerCtrl.text.trim();
    final imageUrl = _composerImageCtrl.text.trim();
    if (summary.isEmpty) return;
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _posting = true);
    try {
      await widget.api.createTimelinePost(summary, imageUrl: imageUrl);
      _composerCtrl.clear();
      _composerImageCtrl.clear();
      if (!mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Posted to timeline')));
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Post failed: $err')));
    } finally {
      if (mounted) setState(() => _posting = false);
    }
  }

  Future<void> _deletePost(TimelinePost post) async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      await widget.api.deleteTimelinePost(post.id);
      if (!mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Timeline post deleted')));
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    }
  }

  Future<void> _toggleLike(TimelinePost post) async {
    final messenger = ScaffoldMessenger.of(context);
    final current = _withOptimistic(post);
    final nextLiked = !current.likedByMe;
    final nextLikes = current.likesCount + (nextLiked ? 1 : -1);
    setState(() {
      _optimisticLiked[post.id] = nextLiked;
      _optimisticLikesCount[post.id] = nextLikes < 0 ? 0 : nextLikes;
    });
    try {
      final payload = await widget.api.toggleTimelineLike(post.id);
      final interactions = payload['interactions'] as Map<String, dynamic>? ?? const <String, dynamic>{};
      if (!mounted) return;
      setState(() {
        _optimisticLiked[post.id] = interactions['liked_by_me'] == true;
        _optimisticLikesCount[post.id] = _toInt(interactions['likes_count']);
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _optimisticLiked[post.id] = current.likedByMe;
        _optimisticLikesCount[post.id] = current.likesCount;
      });
      messenger.showSnackBar(SnackBar(content: Text('Like failed: $err')));
    }
  }

  Future<void> _commentPost(TimelinePost post, String value) async {
    if (value.isEmpty) return;
    final messenger = ScaffoldMessenger.of(context);
    final current = _withOptimistic(post);
    final optimistic = TimelineComment(
      id: 'tmp_${DateTime.now().millisecondsSinceEpoch}',
      author: widget.api.member,
      text: value,
      timestamp: DateTime.now(),
    );
    setState(() {
      _optimisticComments[post.id] = [...current.comments, optimistic];
    });
    try {
      final payload = await widget.api.commentTimelinePost(post.id, value);
      final interactions = payload['interactions'] as Map<String, dynamic>? ?? const <String, dynamic>{};
      final rows = (interactions['comments'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
      if (mounted && rows.isNotEmpty) {
        setState(() {
          _optimisticComments[post.id] = rows.map(TimelineComment.fromJson).toList();
        });
      }
      if (!mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Comment added')));
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _optimisticComments[post.id] = current.comments;
      });
      messenger.showSnackBar(SnackBar(content: Text('Comment failed: $err')));
    }
  }

  Future<String?> _openTimelineImagePickerSheet({String initialUrl = ''}) async {
    final customCtrl = TextEditingController(text: initialUrl);
    bool uploading = false;
    String? picked = await showModalBottomSheet<String>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      builder: (context) => StatefulBuilder(
        builder: (context, setSheetState) {
          Future<void> pickFromDevice() async {
            if (uploading) return;
            setSheetState(() => uploading = true);
            try {
              final result = await FilePicker.platform.pickFiles(type: FileType.image, allowMultiple: false);
              final path = result?.files.first.path ?? '';
              if (path.isEmpty) {
                setSheetState(() => uploading = false);
                return;
              }
              final url = await widget.api.uploadTimelineImage(path);
              if (!context.mounted) return;
              Navigator.pop(context, _absoluteUrl(url));
            } catch (_) {
              if (!context.mounted) return;
              ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Image upload failed')));
              setSheetState(() => uploading = false);
            }
          }

          return Padding(
            padding: EdgeInsets.only(left: 16, right: 16, top: 12, bottom: MediaQuery.of(context).viewInsets.bottom + 16),
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Choose image', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 18)),
                  const SizedBox(height: 10),
                  Wrap(
                    spacing: 10,
                    runSpacing: 10,
                    children: _imagePresets.entries.map((entry) {
                      final color = entry.key == 'Food'
                          ? const Color(0xFFFED7AA)
                          : entry.key == 'Money'
                              ? const Color(0xFFBBF7D0)
                              : entry.key == 'Event'
                                  ? const Color(0xFFBFDBFE)
                                  : const Color(0xFFE5E7EB);
                      return InkWell(
                        onTap: () => Navigator.pop(context, entry.value),
                        borderRadius: BorderRadius.circular(12),
                        child: Ink(
                          width: 80,
                          height: 80,
                          decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(12), border: Border.all(color: const Color(0xFFD1D5DB))),
                          child: Center(child: Text(entry.key, textAlign: TextAlign.center, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 12))),
                        ),
                      );
                    }).toList(),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    controller: customCtrl,
                    minLines: 1,
                    maxLines: 2,
                    decoration: const InputDecoration(labelText: 'Custom image URL', hintText: 'https://example.com/image.jpg'),
                  ),
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      Expanded(
                        child: OutlinedButton.icon(
                          onPressed: uploading ? null : pickFromDevice,
                          icon: const Icon(Icons.photo_library_outlined),
                          label: Text(uploading ? 'Uploading...' : 'From Device'),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: FilledButton(
                          onPressed: () => Navigator.pop(context, customCtrl.text.trim()),
                          child: const Text('Use URL'),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  TextButton(onPressed: () => Navigator.pop(context, ''), child: const Text('Use auto/fallback image')),
                ],
              ),
            ),
          );
        },
      ),
    );
    customCtrl.dispose();
    return picked;
  }

  Future<void> _editPostImage(TimelinePost post) async {
    final picked = await _openTimelineImagePickerSheet(initialUrl: post.imageUrl);
    if (picked == null) return;

    final messenger = ScaffoldMessenger.of(context);
    final nextImageUrl = picked.trim();
    try {
      await widget.api.updateTimelinePostImage(post.id, nextImageUrl);
      await _refresh();
      if (!mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Post image updated')));
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Image update failed: $err')));
    }
  }

  Future<void> _sharePost(TimelinePost post) async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      await widget.api.shareTimelinePost(post.id);
      final link = '${widget.api.settings.baseUrl.replaceAll(RegExp(r'/$'), '')}/timeline';
      await Clipboard.setData(ClipboardData(text: link));
      if (!mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Timeline link copied and share recorded')));
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Share failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<List<TimelinePost>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: snapshot.error.toString(), onRetry: _refresh);
          }

          final posts = snapshot.data ?? [];
            final feedPosts = posts.map(_withOptimistic).toList();
          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Text('Create post', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                          const Spacer(),
                          IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh_rounded)),
                        ],
                      ),
                      if (_composerImageCtrl.text.trim().isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: SizedBox(
                            height: 70,
                            child: Row(
                              children: [
                                ClipRRect(
                                  borderRadius: BorderRadius.circular(10),
                                  child: Image.network(
                                    _absoluteUrl(_composerImageCtrl.text.trim()),
                                    width: 70,
                                    height: 70,
                                    fit: BoxFit.cover,
                                    errorBuilder: (context, error, stackTrace) => Container(
                                      width: 70,
                                      height: 70,
                                      color: const Color(0xFFE5E7EB),
                                      child: const Icon(Icons.image_not_supported_outlined),
                                    ),
                                  ),
                                ),
                                const SizedBox(width: 8),
                                Expanded(child: Text(_composerImageCtrl.text.trim(), maxLines: 2, overflow: TextOverflow.ellipsis)),
                              ],
                            ),
                          ),
                        ),
                      TextField(
                        controller: _composerCtrl,
                        minLines: 2,
                        maxLines: 5,
                        decoration: const InputDecoration(
                          hintText: 'Share an update with your family...',
                          filled: true,
                          fillColor: Color(0xFFF9FAFB),
                          border: OutlineInputBorder(),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          OutlinedButton.icon(
                            onPressed: () async {
                              final picked = await _openTimelineImagePickerSheet(initialUrl: _composerImageCtrl.text.trim());
                              if (picked == null || !mounted) return;
                              setState(() => _composerImageCtrl.text = picked.trim());
                            },
                            icon: const Icon(Icons.image_outlined),
                            label: const Text('Pick image'),
                          ),
                          FilledButton.icon(
                            onPressed: _posting ? null : _post,
                            icon: const Icon(Icons.send),
                            label: Text(_posting ? 'Posting...' : 'Share'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              _TimelineFeedWrapper(
                title: 'Timeline',
                badgeLabel: 'Live',
                posts: feedPosts,
                emptyText: 'No timeline posts yet.',
                imageUrlBuilder: _resolvedPostImage,
                onLikeBuilder: (post) => () => _toggleLike(post),
                onCommentSubmitBuilder: (post, text) => _commentPost(post, text),
                onShareBuilder: (post) => () => _sharePost(post),
                canEditImageBuilder: _canEditPostImage,
                onEditImageBuilder: (post) => _canEditPostImage(post) ? () => _editPostImage(post) : null,
                onDeleteBuilder: (post) => post.actor == widget.api.member ? () => _deletePost(post) : null,
                showInlineCommentComposer: true,
              ),
            ],
          );
        },
      ),
    );
  }
}

class CalendarPage extends StatefulWidget {
  const CalendarPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<CalendarPage> createState() => _CalendarPageState();
}

class _CalendarPageState extends State<CalendarPage> {
  DateTime _anchor = DateTime(DateTime.now().year, DateTime.now().month, 1);
  DateTime? _lastInteractionDay;
  List<CalendarMember> _members = const [];
  List<CalendarEvent> _events = const [];
  List<MealPlannerEntry> _mealEntries = const [];
  bool _loading = true;
  bool _showFamilyMeals = false;
  String _viewMode = 'month';
  String _memberFilter = '';
  String? _error;

  Future<void> _openShareDialog() async {
    Map<String, dynamic> shareData;
    try {
      shareData = await widget.api.fetchCalendarShareLink();
      final hasLink = (shareData['url'] ?? '').toString().trim().isNotEmpty;
      if (!hasLink) {
        shareData = await widget.api.createCalendarShareLink();
      }
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Share link failed: $err')));
      return;
    }
    if (!mounted) return;

    final links = <MapEntry<String, String>>[
      MapEntry('Share URL', '${shareData['url'] ?? ''}'),
      MapEntry('Google', '${shareData['google_url'] ?? ''}'),
      MapEntry('Outlook', '${shareData['outlook_url'] ?? ''}'),
      MapEntry('ICS', '${shareData['ics_url'] ?? ''}'),
      MapEntry('Apple (webcal)', '${shareData['webcal_url'] ?? ''}'),
    ].where((entry) => entry.value.trim().isNotEmpty).toList();

    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Calendar Share & Sync'),
        content: SizedBox(
          width: 460,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: links.map((entry) {
                return Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(entry.key, style: const TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 4),
                      SelectableText(entry.value, style: const TextStyle(fontSize: 12)),
                      const SizedBox(height: 6),
                      OutlinedButton.icon(
                        onPressed: () async {
                          await Clipboard.setData(ClipboardData(text: entry.value));
                          if (context.mounted) {
                            ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${entry.key} copied')));
                          }
                        },
                        icon: const Icon(Icons.copy, size: 16),
                        label: const Text('Copy'),
                      ),
                    ],
                  ),
                );
              }).toList(),
            ),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Close')),
        ],
      ),
    );
  }

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final range = _rangeForView(_anchor, _viewMode);
      final results = await Future.wait([
        widget.api.fetchCalendarMembers(),
        widget.api.fetchCalendarEvents(
          start: range.start,
          end: range.end,
          memberFilter: _memberFilter.isEmpty ? null : _memberFilter,
        ),
      ]);
      final meals = _showFamilyMeals
          ? await _fetchMealEntriesForRange(range.start, range.end)
          : const <MealPlannerEntry>[];
      if (!mounted) return;
      setState(() {
        _members = results[0] as List<CalendarMember>;
        _events = results[1] as List<CalendarEvent>;
        _mealEntries = meals;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  DateTimeRange _rangeForView(DateTime anchor, String viewMode) {
    if (viewMode == 'week') {
      final start = _startOfWeek(anchor);
      return DateTimeRange(start: start, end: start.add(const Duration(days: 6)));
    }
    if (viewMode == 'two-week') {
      final start = _startOfWeek(anchor);
      return DateTimeRange(start: start, end: start.add(const Duration(days: 13)));
    }
    if (viewMode == 'three-month') {
      final start = DateTime(anchor.year, anchor.month, 1);
      final end = DateTime(anchor.year, anchor.month + 3, 0);
      return DateTimeRange(start: start, end: end);
    }
    final first = DateTime(anchor.year, anchor.month, 1);
    final last = DateTime(anchor.year, anchor.month + 1, 0);
    return DateTimeRange(start: _startOfWeek(first), end: _endOfWeek(last));
  }

  Future<List<MealPlannerEntry>> _fetchMealEntriesForRange(DateTime start, DateTime end) async {
    final monthStarts = <DateTime>[];
    var cursor = DateTime(start.year, start.month, 1);
    final last = DateTime(end.year, end.month, 1);
    while (!cursor.isAfter(last)) {
      monthStarts.add(cursor);
      cursor = DateTime(cursor.year, cursor.month + 1, 1);
    }

    final fetched = await Future.wait(monthStarts.map(widget.api.fetchMealEntries));
    final merged = fetched.expand((rows) => rows).where((row) {
      final day = DateTime(row.date.year, row.date.month, row.date.day);
      final isFamily = row.visibility.toLowerCase() == 'family';
      return isFamily && !day.isBefore(start) && !day.isAfter(end);
    }).toList();

    final byId = <int, MealPlannerEntry>{};
    for (final row in merged) {
      byId[row.id] = row;
    }
    final items = byId.values.toList();
    items.sort((a, b) {
      final byDate = a.date.compareTo(b.date);
      if (byDate != 0) return byDate;
      final byType = a.mealType.compareTo(b.mealType);
      if (byType != 0) return byType;
      return a.title.compareTo(b.title);
    });
    return items;
  }

  List<CalendarEvent> _eventsForDay(DateTime day) {
    final date = DateTime(day.year, day.month, day.day);
    return _events.where((ev) {
      final start = DateTime(ev.startDate.year, ev.startDate.month, ev.startDate.day);
      final end = DateTime(ev.endDate.year, ev.endDate.month, ev.endDate.day);
      return !date.isBefore(start) && !date.isAfter(end);
    }).toList()
      ..sort((a, b) {
        final aa = a.startTime ?? '';
        final bb = b.startTime ?? '';
        return aa.compareTo(bb);
      });
  }

    List<MealPlannerEntry> _mealsForDay(DateTime day) {
      final date = DateTime(day.year, day.month, day.day);
      return _mealEntries.where((entry) {
        final d = DateTime(entry.date.year, entry.date.month, entry.date.day);
        return d == date;
      }).toList()
        ..sort((a, b) => a.mealType.compareTo(b.mealType));
    }

    String _mealTypeLabel(String mealType) {
      return mealType.replaceAll('_', ' ');
    }

    String _eventTimeLabel(CalendarEvent ev) {
      final start = (ev.startTime ?? '').trim();
      final end = (ev.endTime ?? '').trim();
      if (ev.isAllDay) return 'All day';
      if (start.isNotEmpty && end.isNotEmpty) return '$start-$end';
      if (start.isNotEmpty) return start;
      if (end.isNotEmpty) return end;
      return 'All day';
    }

  Future<void> _openDay(DateTime day) async {
    _lastInteractionDay = DateTime(day.year, day.month, day.day);
    final changed = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      builder: (_) => CalendarDaySheet(
        day: day,
        events: _eventsForDay(day),
        meals: _mealsForDay(day),
        members: _members,
        api: widget.api,
      ),
    );
    if (changed == true && mounted) {
      await _load();
    }
  }

  Future<void> _openPresetEditor(CalendarEventDraft draft, DateTime initialDay) async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => CalendarEventEditorPage(
          api: widget.api,
          members: _members,
          initialDay: initialDay,
          initialDraft: draft,
        ),
      ),
    );
    if (changed == true && mounted) {
      await _load();
    }
  }

  Future<void> _openLakeHouseShortcut() async {
    final start = _lastInteractionDay ?? DateTime.now();
    final startDay = DateTime(start.year, start.month, start.day);
    final endDay = startDay.add(const Duration(days: 2));
    final draft = CalendarEventDraft(
      title: 'Marblehead Lake House',
      eventType: 'event',
      startDate: startDay,
      endDate: endDay,
      color: '#0ea5e9',
      visibility: 'family',
      isAllDay: true,
      isRecurring: false,
      invitedMembers: const [],
      descriptionHtml: '<p>Lake House booking</p>',
    );
    await _openPresetEditor(draft, startDay);
  }

  Future<void> _toggleWeekMonthQuick() async {
    setState(() {
      _viewMode = _viewMode == 'month' ? 'week' : 'month';
    });
    await _load();
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ErrorPanel(error: _error!, onRetry: _load);
    }

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      IconButton(
                        onPressed: () {
                          setState(() {
                            if (_viewMode == 'week') {
                              _anchor = _anchor.subtract(const Duration(days: 7));
                            } else if (_viewMode == 'two-week') {
                              _anchor = _anchor.subtract(const Duration(days: 14));
                            } else if (_viewMode == 'three-month') {
                              _anchor = DateTime(_anchor.year, _anchor.month - 3, 1);
                            } else {
                              _anchor = DateTime(_anchor.year, _anchor.month - 1, 1);
                            }
                          });
                          _load();
                        },
                        icon: const Icon(Icons.chevron_left),
                      ),
                      Expanded(
                        child: Center(
                          child: Text(
                            _viewMode == 'month' ? _monthLabel(_anchor) : _dateRangeLabel(_rangeForView(_anchor, _viewMode)),
                            style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 16),
                            textAlign: TextAlign.center,
                          ),
                        ),
                      ),
                      IconButton(
                        onPressed: () {
                          setState(() {
                            if (_viewMode == 'week') {
                              _anchor = _anchor.add(const Duration(days: 7));
                            } else if (_viewMode == 'two-week') {
                              _anchor = _anchor.add(const Duration(days: 14));
                            } else if (_viewMode == 'three-month') {
                              _anchor = DateTime(_anchor.year, _anchor.month + 3, 1);
                            } else {
                              _anchor = DateTime(_anchor.year, _anchor.month + 1, 1);
                            }
                          });
                          _load();
                        },
                        icon: const Icon(Icons.chevron_right),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      OutlinedButton(
                        onPressed: () {
                          setState(() => _anchor = DateTime.now());
                          _load();
                        },
                        child: const Text('Today'),
                      ),
                      OutlinedButton.icon(
                        onPressed: _openLakeHouseShortcut,
                        icon: const Icon(Icons.house, size: 16),
                        label: const Text('Lake House'),
                      ),
                      OutlinedButton.icon(
                        onPressed: _toggleWeekMonthQuick,
                        icon: const Icon(Icons.table_view_outlined, size: 16),
                        label: Text(_viewMode == 'month' ? 'Week View' : 'Month View'),
                      ),
                      OutlinedButton.icon(
                        onPressed: _openShareDialog,
                        icon: const Icon(Icons.link, size: 16),
                        label: const Text('Share'),
                      ),
                      DropdownButton<String>(
                        value: _viewMode,
                        items: const [
                          DropdownMenuItem(value: 'week', child: Text('Week')),
                          DropdownMenuItem(value: 'two-week', child: Text('2 Weeks')),
                          DropdownMenuItem(value: 'month', child: Text('Month')),
                          DropdownMenuItem(value: 'three-month', child: Text('3 Months')),
                        ],
                        onChanged: (value) {
                          if (value == null) return;
                          setState(() => _viewMode = value);
                          _load();
                        },
                      ),
                      DropdownButton<String>(
                        value: _memberFilter.isEmpty ? '__all__' : _memberFilter,
                        items: [
                          const DropdownMenuItem(value: '__all__', child: Text('All members')),
                          ..._members.map((m) => DropdownMenuItem(value: m.name, child: Text(m.name))),
                        ],
                        onChanged: (value) {
                          setState(() => _memberFilter = (value == null || value == '__all__') ? '' : value);
                          _load();
                        },
                      ),
                    ],
                  ),
                  SwitchListTile.adaptive(
                    contentPadding: EdgeInsets.zero,
                    dense: true,
                    title: const Text('Show family meals'),
                    value: _showFamilyMeals,
                    onChanged: (value) {
                      setState(() => _showFamilyMeals = value);
                      _load();
                    },
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 8),
          if (_viewMode == 'month') _buildMonthView(context),
          if (_viewMode == 'week') _buildSpanView(context, days: 7),
          if (_viewMode == 'two-week') _buildSpanView(context, days: 14),
          if (_viewMode == 'three-month') _buildThreeMonthView(context),
        ],
      ),
    );
  }

  String _dateRangeLabel(DateTimeRange range) {
    final sameMonth = range.start.year == range.end.year && range.start.month == range.end.month;
    if (sameMonth) {
      return '${range.start.month}/${range.start.day} - ${range.end.day}/${range.end.year}';
    }
    return '${range.start.month}/${range.start.day}/${range.start.year} - ${range.end.month}/${range.end.day}/${range.end.year}';
  }

  Widget _buildMonthView(BuildContext context) {
    final first = DateTime(_anchor.year, _anchor.month, 1);
    final last = DateTime(_anchor.year, _anchor.month + 1, 0);
    final start = _startOfWeek(first);
    final end = _endOfWeek(last);
    final days = <DateTime>[];
    for (var d = start; !d.isAfter(end); d = d.add(const Duration(days: 1))) {
      days.add(d);
    }

    final today = DateTime.now();
    final todayDate = DateTime(today.year, today.month, today.day);
    final compact = MediaQuery.of(context).size.width < 430;
    final visiblePerCell = compact ? 1 : 2;

    return Column(
      children: [
        Row(
          children: const ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
              .map((w) => Expanded(
                    child: Center(
                      child: Padding(
                        padding: EdgeInsets.symmetric(vertical: 6),
                        child: Text(
                          w,
                          style: TextStyle(fontSize: 12, color: Color(0xFF64748B), fontWeight: FontWeight.w600),
                        ),
                      ),
                    ),
                  ))
              .toList(),
        ),
        GridView.builder(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          itemCount: days.length,
          gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
            crossAxisCount: 7,
            mainAxisSpacing: 4,
            crossAxisSpacing: 4,
            childAspectRatio: compact ? 0.65 : 0.82,
          ),
          itemBuilder: (context, index) {
            final day = days[index];
            return _buildDayCell(
              day,
              inMonth: day.month == _anchor.month,
              isToday: day == todayDate,
              maxItems: visiblePerCell,
            );
          },
        ),
      ],
    );
  }

  Widget _buildSpanView(BuildContext context, {required int days}) {
    final start = _startOfWeek(_anchor);
    final list = List<DateTime>.generate(days, (i) => start.add(Duration(days: i)));
    final width = MediaQuery.of(context).size.width;
    final crossAxis = width >= 1000 ? 7 : (width >= 700 ? 4 : 2);
    final today = DateTime.now();
    final todayDate = DateTime(today.year, today.month, today.day);

    return GridView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: list.length,
      gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: crossAxis,
        mainAxisSpacing: 6,
        crossAxisSpacing: 6,
        childAspectRatio: width < 430 ? 0.8 : 0.92,
      ),
      itemBuilder: (context, index) {
        final day = list[index];
        return _buildDayCell(
          day,
          inMonth: true,
          isToday: day == todayDate,
          maxItems: 3,
        );
      },
    );
  }

  Widget _buildThreeMonthView(BuildContext context) {
    final start = DateTime(_anchor.year, _anchor.month, 1);
    final width = MediaQuery.of(context).size.width;
    final columns = width >= 1200 ? 3 : 1;

    return GridView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      itemCount: 3,
      gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: columns,
        mainAxisSpacing: 8,
        crossAxisSpacing: 8,
        childAspectRatio: columns == 1 ? 1.25 : 0.95,
      ),
      itemBuilder: (context, idx) {
        final month = DateTime(start.year, start.month + idx, 1);
        final monthStart = _startOfWeek(month);
        final monthEnd = _endOfWeek(DateTime(month.year, month.month + 1, 0));
        final days = <DateTime>[];
        for (var d = monthStart; !d.isAfter(monthEnd); d = d.add(const Duration(days: 1))) {
          days.add(d);
        }
        return Card(
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(_monthLabel(month), style: const TextStyle(fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Expanded(
                  child: GridView.builder(
                    physics: const NeverScrollableScrollPhysics(),
                    itemCount: days.length,
                    gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                      crossAxisCount: 7,
                      mainAxisSpacing: 2,
                      crossAxisSpacing: 2,
                    ),
                    itemBuilder: (context, i) {
                      final day = days[i];
                      final eventCount = _eventsForDay(day).length;
                      final mealCount = _showFamilyMeals ? _mealsForDay(day).length : 0;
                      final count = eventCount + mealCount;
                      return InkWell(
                        onTap: () => _openDay(day),
                        borderRadius: BorderRadius.circular(6),
                        child: Container(
                          padding: const EdgeInsets.all(3),
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(6),
                            border: Border.all(color: const Color(0xFFE2E8F0)),
                            color: day.month == month.month ? Colors.white : const Color(0xFFF8FAFC),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text('${day.day}', style: const TextStyle(fontSize: 10, fontWeight: FontWeight.w600)),
                              if (count > 0)
                                Text(
                                  '$count',
                                  style: const TextStyle(fontSize: 9, color: Color(0xFF4F46E5), fontWeight: FontWeight.w700),
                                ),
                            ],
                          ),
                        ),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildDayCell(
    DateTime day, {
    required bool inMonth,
    required bool isToday,
    required int maxItems,
  }) {
    final events = _eventsForDay(day);
    final meals = _showFamilyMeals ? _mealsForDay(day) : const <MealPlannerEntry>[];
    final items = <Widget>[];

    for (final meal in meals.take(2)) {
      items.add(
        Container(
          margin: const EdgeInsets.only(bottom: 2),
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
          decoration: BoxDecoration(
            color: const Color(0xFFEEF2FF),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Text(
            '${_mealTypeLabel(meal.mealType)}: ${meal.title}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 10, color: Color(0xFF4338CA), fontWeight: FontWeight.w600),
          ),
        ),
      );
    }

    for (final ev in events.take(maxItems - items.length.clamp(0, maxItems))) {
      final time = _eventTimeLabel(ev);
      final showTime = !ev.isAllDay && time.isNotEmpty;
      items.add(
        Container(
          margin: const EdgeInsets.only(bottom: 2),
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
          decoration: BoxDecoration(
            color: _colorFromHex(ev.color).withValues(alpha: 0.15),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Text(
            '${showTime ? '$time ' : ''}${ev.title}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(fontSize: 10, color: _colorFromHex(ev.color), fontWeight: FontWeight.w600),
          ),
        ),
      );
    }

    final hidden = (events.length + meals.length) - items.length;

    return InkWell(
      onTap: () => _openDay(day),
      borderRadius: BorderRadius.circular(12),
      child: Container(
        padding: const EdgeInsets.all(6),
        decoration: BoxDecoration(
          color: isToday ? const Color(0xFFE0E7FF) : Colors.white,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: const Color(0xFFE2E8F0)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${day.day}',
              style: TextStyle(
                color: inMonth ? const Color(0xFF0F172A) : const Color(0xFF94A3B8),
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 4),
            ...items,
            if (hidden > 0)
              Text(
                '+$hidden more',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 10, color: Color(0xFF6366F1), fontWeight: FontWeight.w700),
              ),
          ],
        ),
      ),
    );
  }
}

class CalendarDaySheet extends StatelessWidget {
  const CalendarDaySheet({
    super.key,
    required this.day,
    required this.events,
    required this.meals,
    required this.members,
    required this.api,
  });

  final DateTime day;
  final List<CalendarEvent> events;
  final List<MealPlannerEntry> meals;
  final List<CalendarMember> members;
  final HomeHubApi api;

  Future<String?> _pickRecurringUpdateScope(BuildContext context) async {
    if (!_calendarSupportsOccurrenceScopedUpdates) return null;
    return showDialog<String>(
      context: context,
      builder: (ctx) {
        return AlertDialog(
          title: const Text('Update recurring event'),
          content: const Text('Apply this change to just this occurrence or the whole series?'),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('Cancel'),
            ),
            TextButton(
              onPressed: () => Navigator.of(ctx).pop('occurrence'),
              child: const Text('This occurrence'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(ctx).pop('series'),
              child: const Text('Whole series'),
            ),
          ],
        );
      },
    );
  }

  String _calendarAbsoluteUrl(String value) {
    final raw = value.trim();
    if (raw.isEmpty) return raw;
    if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
    final configured = api.settings.baseUrl.trim();
    final base = configured.isEmpty ? _cloudBaseUrl : configured;
    final root = Uri.parse(_normalizeBaseUrl(base));
    final rootPath = root.path.endsWith('/') ? root.path.substring(0, root.path.length - 1) : root.path;
    final assetPath = raw.startsWith('/') ? raw : '/$raw';
    return root.replace(path: '$rootPath$assetPath').toString();
  }

  DateTime _dateOnly(DateTime d) => DateTime(d.year, d.month, d.day);

  CalendarEventDraft _draftWithNewDates(
    CalendarEvent ev, {
    required DateTime newStart,
    required DateTime newEnd,
  }) {
    return CalendarEventDraft(
      title: ev.title,
      eventType: ev.eventType,
      startDate: _dateOnly(newStart),
      endDate: _dateOnly(newEnd),
      color: ev.color,
      visibility: ev.visibility,
      isAllDay: ev.isAllDay,
      isRecurring: ev.isRecurring,
      invitedMembers: ev.invitedMembers,
      descriptionHtml: ev.descriptionHtml,
      startTime: ev.startTime,
      endTime: ev.endTime,
      recurrenceFrequency: ev.recurrenceFrequency,
      recurrenceInterval: ev.recurrenceInterval,
      recurrenceUntil: ev.recurrenceUntil,
      recurrenceCount: ev.recurrenceCount,
      reminderAt: ev.reminderAt,
    );
  }

  Future<void> _updateEventDates(
    BuildContext context,
    CalendarEvent ev, {
    DateTime? targetStart,
    DateTime? targetEnd,
  }) async {
    final messenger = ScaffoldMessenger.of(context);
    String? updateScope;

    if (ev.isRecurring) {
      final pickedScope = await _pickRecurringUpdateScope(context);
      if (!context.mounted) return;
      if (_calendarSupportsOccurrenceScopedUpdates && pickedScope == null) return;
      updateScope = pickedScope;
    }

    final baseStart = _dateOnly(ev.baseStartDate ?? ev.startDate);
    final baseEnd = _dateOnly(ev.baseEndDate ?? ev.endDate);
    final originalSpanDays = baseEnd.difference(baseStart).inDays;

    DateTime nextStart = baseStart;
    DateTime nextEnd = baseEnd;

    if (targetStart != null) {
      final desiredOccurrenceStart = _dateOnly(targetStart);
      if (ev.isRecurring) {
        final occurrenceAnchor = _dateOnly(ev.occurrenceStart ?? ev.startDate);
        final shiftDays = desiredOccurrenceStart.difference(occurrenceAnchor).inDays;
        nextStart = baseStart.add(Duration(days: shiftDays));
      } else {
        nextStart = desiredOccurrenceStart;
      }
      nextEnd = nextStart.add(Duration(days: originalSpanDays < 0 ? 0 : originalSpanDays));
    }

    if (targetEnd != null) {
      final desiredEnd = _dateOnly(targetEnd);
      nextEnd = desiredEnd.isBefore(nextStart) ? nextStart : desiredEnd;
    }

    try {
      final draft = _draftWithNewDates(ev, newStart: nextStart, newEnd: nextEnd);
      await api.updateCalendarEvent(ev.id, draft, updateScope: updateScope);
      if (!context.mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Event updated')));
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!context.mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Update failed: $err')));
    }
  }

  Future<void> _shiftEventByDays(BuildContext context, CalendarEvent ev, int days) async {
    final start = _dateOnly(ev.startDate).add(Duration(days: days));
    await _updateEventDates(context, ev, targetStart: start);
  }

  Future<void> _pickAndShiftEvent(BuildContext context, CalendarEvent ev) async {
    final picked = await showDatePicker(
      context: context,
      initialDate: _dateOnly(ev.startDate),
      firstDate: DateTime(2020),
      lastDate: DateTime(2100),
    );
    if (picked == null) return;
    if (!context.mounted) return;
    await _updateEventDates(context, ev, targetStart: picked);
  }

  Future<void> _pickAndResizeEventEnd(BuildContext context, CalendarEvent ev) async {
    final picked = await showDatePicker(
      context: context,
      initialDate: _dateOnly(ev.endDate),
      firstDate: _dateOnly(ev.startDate),
      lastDate: DateTime(2100),
    );
    if (picked == null) return;
    if (!context.mounted) return;
    await _updateEventDates(context, ev, targetEnd: picked);
  }

  Future<void> _resizeEventEndByDays(BuildContext context, CalendarEvent ev, int days) async {
    final currentEnd = _dateOnly(ev.baseEndDate ?? ev.endDate);
    final currentStart = _dateOnly(ev.baseStartDate ?? ev.startDate);
    final nextEnd = currentEnd.add(Duration(days: days));
    await _updateEventDates(context, ev, targetEnd: nextEnd.isBefore(currentStart) ? currentStart : nextEnd);
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      'Events • ${_fmtDate(day)}',
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
                    ),
                  ),
                  IconButton(
                    onPressed: () => Navigator.of(context).pop(false),
                    icon: const Icon(Icons.close),
                  )
                ],
              ),
              Flexible(
                child: SingleChildScrollView(
                  child: Column(
                    children: [
                      if (events.isEmpty && meals.isEmpty)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 18),
                          child: Text('No events on this day.'),
                        ),
                      ...meals.map((meal) {
                        return Card(
                          child: ListTile(
                            leading: const Icon(Icons.restaurant_menu, color: Color(0xFF7C3AED)),
                            title: Text(meal.title),
                            subtitle: Text('${meal.mealType.replaceAll('_', ' ')} • ${meal.creator}'),
                          ),
                        );
                      }),
                      ...events.map((ev) {
                        final timeText = ev.isAllDay
                          ? 'All day'
                          : [ev.startTime, ev.endTime].whereType<String>().where((e) => e.isNotEmpty).join(' - ');
                        final desc = _stripHtml(ev.descriptionHtml ?? '').trim();
                        final reminder = ev.reminderAt == null ? '' : _fmtDateTime(ev.reminderAt!.toLocal());
                        final recurrence = ev.isRecurring
                            ? '${ev.recurrenceFrequency ?? 'weekly'} every ${ev.recurrenceInterval ?? 1}'
                            : 'No recurrence';
                        final photo = (ev.photoUrl ?? '').trim();
                        return Card(
                          child: InkWell(
                            borderRadius: BorderRadius.circular(12),
                            onTap: () async {
                              final changed = await Navigator.of(context).push<bool>(
                                MaterialPageRoute(
                                  builder: (_) => CalendarEventEditorPage(
                                    api: api,
                                    members: members,
                                    initialDay: day,
                                    sourceEvent: ev,
                                  ),
                                ),
                              );
                              if (context.mounted) Navigator.of(context).pop(changed == true);
                            },
                            child: Padding(
                              padding: const EdgeInsets.all(12),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Row(
                                    children: [
                                      Expanded(
                                        child: Text(
                                          ev.title,
                                          style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15),
                                        ),
                                      ),
                                      Container(
                                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                                        decoration: BoxDecoration(
                                          borderRadius: BorderRadius.circular(999),
                                          color: _colorFromHex(ev.color).withValues(alpha: 0.15),
                                        ),
                                        child: Text(
                                          ev.eventType.toUpperCase(),
                                          style: TextStyle(
                                            color: _colorFromHex(ev.color),
                                            fontSize: 10,
                                            fontWeight: FontWeight.w700,
                                          ),
                                        ),
                                      ),
                                    ],
                                  ),
                                  const SizedBox(height: 6),
                                  Text('$timeText • ${ev.createdBy} • ${ev.visibility}', style: const TextStyle(color: Color(0xFF475569))),
                                  if (reminder.isNotEmpty)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 4),
                                      child: Text('Reminder: $reminder', style: const TextStyle(color: Color(0xFF0F766E), fontSize: 12)),
                                    ),
                                  Padding(
                                    padding: const EdgeInsets.only(top: 4),
                                    child: Text('Recurrence: $recurrence', style: const TextStyle(color: Color(0xFF64748B), fontSize: 12)),
                                  ),
                                  const SizedBox(height: 8),
                                  Wrap(
                                    spacing: 6,
                                    runSpacing: 6,
                                    children: [
                                      ActionChip(
                                        label: const Text('-1d', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _shiftEventByDays(context, ev, -1),
                                      ),
                                      ActionChip(
                                        label: const Text('+1d', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _shiftEventByDays(context, ev, 1),
                                      ),
                                      ActionChip(
                                        label: const Text('+1w', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _shiftEventByDays(context, ev, 7),
                                      ),
                                      ActionChip(
                                        label: const Text('Move...', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _pickAndShiftEvent(context, ev),
                                      ),
                                      ActionChip(
                                        label: const Text('End -1d', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _resizeEventEndByDays(context, ev, -1),
                                      ),
                                      ActionChip(
                                        label: const Text('End +1d', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _resizeEventEndByDays(context, ev, 1),
                                      ),
                                      ActionChip(
                                        label: const Text('Resize...', style: TextStyle(fontSize: 11)),
                                        onPressed: () => _pickAndResizeEventEnd(context, ev),
                                      ),
                                    ],
                                  ),
                                  if (desc.isNotEmpty)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 8),
                                      child: Text(desc, style: const TextStyle(fontSize: 13), maxLines: 4, overflow: TextOverflow.ellipsis),
                                    ),
                                  if (photo.isNotEmpty)
                                    Padding(
                                      padding: const EdgeInsets.only(top: 8),
                                      child: ClipRRect(
                                        borderRadius: BorderRadius.circular(10),
                                        child: Image.network(
                                          _calendarAbsoluteUrl(photo),
                                          height: 140,
                                          width: double.infinity,
                                          fit: BoxFit.cover,
                                          errorBuilder: (_, __, ___) => Container(
                                            height: 110,
                                            color: const Color(0xFFF1F5F9),
                                            alignment: Alignment.center,
                                            child: const Text('Photo unavailable'),
                                          ),
                                        ),
                                      ),
                                    ),
                                ],
                              ),
                            ),
                          ),
                        );
                      }),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 10),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton.icon(
                      onPressed: () async {
                        final startDay = DateTime(day.year, day.month, day.day);
                        final draft = CalendarEventDraft(
                          title: 'Marblehead Lake House',
                          eventType: 'event',
                          startDate: startDay,
                          endDate: startDay.add(const Duration(days: 2)),
                          color: '#0ea5e9',
                          visibility: 'family',
                          isAllDay: true,
                          isRecurring: false,
                          invitedMembers: const [],
                          descriptionHtml: '<p>Lake House booking</p>',
                        );
                        final changed = await Navigator.of(context).push<bool>(
                          MaterialPageRoute(
                            builder: (_) => CalendarEventEditorPage(
                              api: api,
                              members: members,
                              initialDay: day,
                              initialDraft: draft,
                            ),
                          ),
                        );
                        if (context.mounted) Navigator.of(context).pop(changed == true);
                      },
                      icon: const Icon(Icons.house_outlined),
                      label: const Text('Lake House'),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: FilledButton.icon(
                      onPressed: () async {
                        final changed = await Navigator.of(context).push<bool>(
                          MaterialPageRoute(
                            builder: (_) => CalendarEventEditorPage(
                              api: api,
                              members: members,
                              initialDay: day,
                            ),
                          ),
                        );
                        if (context.mounted) Navigator.of(context).pop(changed == true);
                      },
                      icon: const Icon(Icons.add),
                      label: const Text('Add Event'),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class CalendarEventEditorPage extends StatefulWidget {
  const CalendarEventEditorPage({
    super.key,
    required this.api,
    required this.members,
    required this.initialDay,
    this.sourceEvent,
    this.initialDraft,
  });

  final HomeHubApi api;
  final List<CalendarMember> members;
  final DateTime initialDay;
  final CalendarEvent? sourceEvent;
  final CalendarEventDraft? initialDraft;

  @override
  State<CalendarEventEditorPage> createState() => _CalendarEventEditorPageState();
}

class _CalendarEventEditorPageState extends State<CalendarEventEditorPage> {
  final _formKey = GlobalKey<FormState>();
  final _titleCtrl = TextEditingController();
  final _descriptionCtrl = TextEditingController();
  final _startTimeCtrl = TextEditingController();
  final _endTimeCtrl = TextEditingController();
  final _recurrenceIntervalCtrl = TextEditingController(text: '1');
  final _recurrenceCountCtrl = TextEditingController();
  final _reminderAtCtrl = TextEditingController();
  String _photoPath = '';
  String _existingPhotoUrl = '';

  late DateTime _startDate;
  late DateTime _endDate;
  DateTime? _recurrenceUntil;
  String _eventType = 'event';
  String _color = '#6366f1';
  String _visibility = 'family';
  bool _isAllDay = true;
  bool _isRecurring = false;
  String _recurrenceFrequency = 'weekly';
  bool _saving = false;
  final Set<String> _invited = <String>{};

  bool get _editing => widget.sourceEvent != null;

  @override
  void initState() {
    super.initState();
    final src = widget.sourceEvent;
    final preset = widget.initialDraft;
    _startDate = src?.startDate ?? preset?.startDate ?? widget.initialDay;
    _endDate = src?.endDate ?? preset?.endDate ?? widget.initialDay;
    _titleCtrl.text = src?.title ?? preset?.title ?? '';
    _descriptionCtrl.text = _stripHtml(src?.descriptionHtml ?? preset?.descriptionHtml ?? '');
    _eventType = src?.eventType ?? preset?.eventType ?? 'event';
    _color = src?.color ?? preset?.color ?? '#6366f1';
    _visibility = src?.visibility ?? preset?.visibility ?? 'family';
    _isAllDay = src?.isAllDay ?? preset?.isAllDay ?? true;
    _isRecurring = src?.isRecurring ?? preset?.isRecurring ?? false;
    _startTimeCtrl.text = src?.startTime ?? preset?.startTime ?? '';
    _endTimeCtrl.text = src?.endTime ?? preset?.endTime ?? '';
    _recurrenceFrequency = src?.recurrenceFrequency ?? preset?.recurrenceFrequency ?? 'weekly';
    _recurrenceIntervalCtrl.text = '${src?.recurrenceInterval ?? preset?.recurrenceInterval ?? 1}';
    _recurrenceUntil = src?.recurrenceUntil ?? preset?.recurrenceUntil;
    _recurrenceCountCtrl.text = (src?.recurrenceCount ?? preset?.recurrenceCount)?.toString() ?? '';
    final reminder = src?.reminderAt ?? preset?.reminderAt;
    _reminderAtCtrl.text = reminder == null ? '' : _fmtDateTime(reminder);
    _existingPhotoUrl = (src?.photoUrl ?? '').trim();
    _invited.addAll(src?.invitedMembers ?? preset?.invitedMembers ?? const []);
  }

  @override
  void dispose() {
    _titleCtrl.dispose();
    _descriptionCtrl.dispose();
    _startTimeCtrl.dispose();
    _endTimeCtrl.dispose();
    _recurrenceIntervalCtrl.dispose();
    _recurrenceCountCtrl.dispose();
    _reminderAtCtrl.dispose();
    super.dispose();
  }

  DateTime? _parseReminderInput(String raw) {
    final text = raw.trim();
    if (text.isEmpty) return null;
    final normalized = text.replaceFirst('T', ' ');
    return DateTime.tryParse(normalized);
  }

  String _calendarAbsoluteUrl(String value) {
    final raw = value.trim();
    if (raw.isEmpty) return raw;
    if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
    final configured = widget.api.settings.baseUrl.trim();
    final base = configured.isEmpty ? _cloudBaseUrl : configured;
    final root = Uri.parse(_normalizeBaseUrl(base));
    final rootPath = root.path.endsWith('/') ? root.path.substring(0, root.path.length - 1) : root.path;
    final assetPath = raw.startsWith('/') ? raw : '/$raw';
    return root.replace(path: '$rootPath$assetPath').toString();
  }

  Future<void> _pickPhoto() async {
    final picked = await FilePicker.platform.pickFiles(type: FileType.image, allowMultiple: false);
    if (picked == null || picked.files.isEmpty) return;
    final file = picked.files.first;
    if ((file.path ?? '').trim().isEmpty) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Unable to access selected image file path.')));
      return;
    }
    setState(() => _photoPath = file.path!.trim());
  }

  Future<void> _pickDate({required bool start}) async {
    final picked = await showDatePicker(
      context: context,
      initialDate: start ? _startDate : _endDate,
      firstDate: DateTime(2020),
      lastDate: DateTime(2100),
    );
    if (picked == null) return;
    setState(() {
      if (start) {
        _startDate = picked;
        if (_endDate.isBefore(_startDate)) _endDate = _startDate;
      } else {
        _endDate = picked.isBefore(_startDate) ? _startDate : picked;
      }
    });
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    final messenger = ScaffoldMessenger.of(context);
    setState(() => _saving = true);
    try {
      final draft = CalendarEventDraft(
        title: _titleCtrl.text.trim(),
        eventType: _eventType,
        startDate: _startDate,
        endDate: _endDate,
        color: _color,
        visibility: _visibility,
        isAllDay: _isAllDay,
        isRecurring: _isRecurring,
        invitedMembers: _invited.toList(),
        descriptionHtml: _descriptionCtrl.text.trim(),
        startTime: _isAllDay ? null : _normalizeTimeInput(_startTimeCtrl.text),
        endTime: _isAllDay ? null : _normalizeTimeInput(_endTimeCtrl.text),
        recurrenceFrequency: _isRecurring ? _recurrenceFrequency : null,
        recurrenceInterval: _isRecurring ? (_toInt(_recurrenceIntervalCtrl.text) <= 0 ? 1 : _toInt(_recurrenceIntervalCtrl.text)) : null,
        recurrenceUntil: _isRecurring ? _recurrenceUntil : null,
        recurrenceCount: _isRecurring ? (_toInt(_recurrenceCountCtrl.text) <= 0 ? null : _toInt(_recurrenceCountCtrl.text)) : null,
        reminderAt: _parseReminderInput(_reminderAtCtrl.text),
      );

      if (_editing) {
        await widget.api.updateCalendarEvent(widget.sourceEvent!.id, draft, photoPath: _photoPath);
      } else {
        await widget.api.createCalendarEvent(draft, photoPath: _photoPath);
      }

      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text(_editing ? 'Event updated' : 'Event created')));
      Navigator.pop(context, true);
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Save failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _delete() async {
    if (!_editing) return;
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete event?'),
        content: const Text('This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (confirm != true) return;
    setState(() => _saving = true);
    try {
      await widget.api.deleteCalendarEvent(widget.sourceEvent!.id);
      if (!mounted) return;
      messenger.showSnackBar(const SnackBar(content: Text('Event deleted')));
      navigator.pop(true);
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_editing ? 'Edit Event' : 'New Event'),
        actions: [
          if (_editing)
            IconButton(
              onPressed: _saving ? null : _delete,
              icon: const Icon(Icons.delete_outline),
            ),
        ],
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextFormField(
                  controller: _titleCtrl,
                  decoration: const InputDecoration(labelText: 'Title'),
                  validator: (v) => (v == null || v.trim().isEmpty) ? 'Title is required' : null,
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _eventType,
                  decoration: const InputDecoration(labelText: 'Event type'),
                  items: const [
                    DropdownMenuItem(value: 'event', child: Text('Event')),
                    DropdownMenuItem(value: 'reminder', child: Text('Reminder')),
                    DropdownMenuItem(value: 'birthday', child: Text('Birthday')),
                  ],
                  onChanged: (v) => setState(() => _eventType = v ?? 'event'),
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _color,
                  decoration: const InputDecoration(labelText: 'Color'),
                  items: const [
                    DropdownMenuItem(value: '#6366f1', child: Text('Indigo')),
                    DropdownMenuItem(value: '#3b82f6', child: Text('Blue')),
                    DropdownMenuItem(value: '#10b981', child: Text('Green')),
                    DropdownMenuItem(value: '#f59e0b', child: Text('Amber')),
                    DropdownMenuItem(value: '#ef4444', child: Text('Red')),
                  ],
                  onChanged: (v) => setState(() => _color = v ?? '#6366f1'),
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: () => _pickDate(start: true),
                        icon: const Icon(Icons.event),
                        label: Text('Start ${_fmtDate(_startDate)}'),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: () => _pickDate(start: false),
                        icon: const Icon(Icons.event_available),
                        label: Text('End ${_fmtDate(_endDate)}'),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                SwitchListTile.adaptive(
                  value: _isAllDay,
                  onChanged: (v) => setState(() => _isAllDay = v),
                  title: const Text('All day'),
                  contentPadding: EdgeInsets.zero,
                ),
                if (!_isAllDay) ...[
                  Row(
                    children: [
                      Expanded(
                        child: TextFormField(
                          controller: _startTimeCtrl,
                          decoration: const InputDecoration(labelText: 'Start time (HH:MM)'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: TextFormField(
                          controller: _endTimeCtrl,
                          decoration: const InputDecoration(labelText: 'End time (HH:MM)'),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                ],
                TextFormField(
                  controller: _descriptionCtrl,
                  decoration: const InputDecoration(labelText: 'Description'),
                  maxLines: 3,
                ),
                const SizedBox(height: 8),
                Align(
                  alignment: Alignment.centerLeft,
                  child: Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      OutlinedButton.icon(
                        onPressed: _pickPhoto,
                        icon: const Icon(Icons.photo_library_outlined),
                        label: Text(_photoPath.isEmpty ? 'Attach photo' : 'Change photo'),
                      ),
                      if (_photoPath.isNotEmpty)
                        TextButton(
                          onPressed: () => setState(() => _photoPath = ''),
                          child: const Text('Clear selection'),
                        ),
                    ],
                  ),
                ),
                if (_photoPath.isNotEmpty || _existingPhotoUrl.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  ClipRRect(
                    borderRadius: BorderRadius.circular(10),
                    child: _photoPath.isNotEmpty
                        ? Image.file(
                            File(_photoPath),
                            height: 140,
                            width: double.infinity,
                            fit: BoxFit.cover,
                            errorBuilder: (_, __, ___) => Container(
                              height: 140,
                              color: const Color(0xFFF1F5F9),
                              alignment: Alignment.center,
                              child: const Text('Preview unavailable'),
                            ),
                          )
                        : Image.network(
                            _calendarAbsoluteUrl(_existingPhotoUrl),
                            height: 140,
                            width: double.infinity,
                            fit: BoxFit.cover,
                            errorBuilder: (_, __, ___) => Container(
                              height: 140,
                              color: const Color(0xFFF1F5F9),
                              alignment: Alignment.center,
                              child: const Text('Photo unavailable'),
                            ),
                          ),
                  ),
                ],
                const SizedBox(height: 8),
                TextFormField(
                  controller: _reminderAtCtrl,
                  decoration: const InputDecoration(labelText: 'Reminder (YYYY-MM-DD HH:MM)'),
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _visibility,
                  decoration: const InputDecoration(labelText: 'Visibility'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'restricted', child: Text('Restricted (invited only)')),
                    DropdownMenuItem(value: 'private', child: Text('Private')),
                  ],
                  onChanged: (v) => setState(() => _visibility = v ?? 'family'),
                ),
                if (_visibility == 'restricted') ...[
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 6,
                    children: widget.members.map((m) {
                      final selected = _invited.contains(m.name);
                      return FilterChip(
                        selected: selected,
                        label: Text(m.name),
                        onSelected: (v) {
                          setState(() {
                            if (v) {
                              _invited.add(m.name);
                            } else {
                              _invited.remove(m.name);
                            }
                          });
                        },
                      );
                    }).toList(),
                  ),
                ],
                const SizedBox(height: 8),
                SwitchListTile.adaptive(
                  value: _isRecurring,
                  onChanged: (v) => setState(() => _isRecurring = v),
                  title: const Text('Recurring event'),
                  contentPadding: EdgeInsets.zero,
                ),
                if (_isRecurring) ...[
                  DropdownButtonFormField<String>(
                    initialValue: _recurrenceFrequency,
                    decoration: const InputDecoration(labelText: 'Frequency'),
                    items: const [
                      DropdownMenuItem(value: 'daily', child: Text('Daily')),
                      DropdownMenuItem(value: 'weekly', child: Text('Weekly')),
                      DropdownMenuItem(value: 'monthly', child: Text('Monthly')),
                      DropdownMenuItem(value: 'yearly', child: Text('Yearly')),
                    ],
                    onChanged: (v) => setState(() => _recurrenceFrequency = v ?? 'weekly'),
                  ),
                  const SizedBox(height: 8),
                  TextFormField(
                    controller: _recurrenceIntervalCtrl,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(labelText: 'Repeat every (interval)'),
                  ),
                  const SizedBox(height: 8),
                  TextFormField(
                    controller: _recurrenceCountCtrl,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(labelText: 'Occurrence count (optional)'),
                  ),
                  const SizedBox(height: 8),
                  OutlinedButton.icon(
                    onPressed: () async {
                      final picked = await showDatePicker(
                        context: context,
                        initialDate: _recurrenceUntil ?? _endDate,
                        firstDate: _endDate,
                        lastDate: DateTime(2100),
                      );
                      if (picked != null) {
                        setState(() => _recurrenceUntil = picked);
                      }
                    },
                    icon: const Icon(Icons.repeat),
                    label: Text('Repeat until ${_recurrenceUntil == null ? 'not set' : _fmtDate(_recurrenceUntil!)}'),
                  ),
                ],
                const SizedBox(height: 16),
                FilledButton.icon(
                  onPressed: _saving ? null : _save,
                  icon: const Icon(Icons.save),
                  label: Text(_saving ? 'Saving...' : (_editing ? 'Update Event' : 'Create Event')),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class ExpensesPage extends StatefulWidget {
  const ExpensesPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<ExpensesPage> createState() => _ExpensesPageState();
}

class _ExpensesPageState extends State<ExpensesPage> with TickerProviderStateMixin {
  late TabController _tabController;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
          child: Container(
            decoration: BoxDecoration(
              color: Colors.white,
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: const Color(0xFFE2E8F0)),
            ),
            child: TabBar(
              controller: _tabController,
              tabs: const [
                Tab(text: 'Expenses'),
                Tab(text: 'Money'),
              ],
            ),
          ),
        ),
        Expanded(
          child: TabBarView(
            controller: _tabController,
            children: [
              _ExpensesLedgerTab(api: widget.api),
              _MoneyInsightsTab(api: widget.api),
            ],
          ),
        ),
      ],
    );
  }
}

class _ExpensesLedgerTab extends StatefulWidget {
  const _ExpensesLedgerTab({required this.api});

  final HomeHubApi api;

  @override
  State<_ExpensesLedgerTab> createState() => _ExpensesLedgerTabState();
}

class _ExpensesLedgerTabState extends State<_ExpensesLedgerTab> {
  DateTime _month = DateTime(DateTime.now().year, DateTime.now().month, 1);
  late Future<Map<String, dynamic>> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.api.fetchExpensesMonth(_month);
  }

  Future<void> _refresh() async {
    setState(() => _future = widget.api.fetchExpensesMonth(_month));
    await _future;
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: snapshot.error.toString(), onRetry: _refresh);
          }

          final summary = snapshot.data!['summary'] as Map<String, dynamic>? ?? {};
          final byDate = snapshot.data!['by_date'] as Map<String, dynamic>? ?? {};
          final days = byDate.keys.toList()..sort((a, b) => b.compareTo(a));

          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Row(
                    children: [
                      IconButton(
                        onPressed: () {
                          setState(() {
                            _month = DateTime(_month.year, _month.month - 1, 1);
                            _future = widget.api.fetchExpensesMonth(_month);
                          });
                        },
                        icon: const Icon(Icons.chevron_left),
                      ),
                      Expanded(
                        child: Center(
                          child: Text(
                            _monthLabel(_month),
                            style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 17),
                          ),
                        ),
                      ),
                      IconButton(
                        onPressed: () {
                          setState(() {
                            _month = DateTime(_month.year, _month.month + 1, 1);
                            _future = widget.api.fetchExpensesMonth(_month);
                          });
                        },
                        icon: const Icon(Icons.chevron_right),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              _MetricCard(
                title: 'Month total',
                value: _currency(_toDouble(summary['total_this_month'])),
                subtitle: '${_toInt(summary['days_with_entries'])} days with entries',
                icon: Icons.receipt_long,
              ),
              const SizedBox(height: 8),
              if (days.isEmpty)
                const Card(child: Padding(padding: EdgeInsets.all(16), child: Text('No expenses found for this month.'))),
              ...days.map((day) {
                final dayData = byDate[day] as Map<String, dynamic>? ?? {};
                final entries = dayData['entries'] as List<dynamic>? ?? [];
                final total = _toDouble(dayData['total']);
                return Card(
                  child: ExpansionTile(
                    title: Text(day),
                    subtitle: Text('Total ${_currency(total)}'),
                    children: entries.map((raw) {
                      final item = raw as Map<String, dynamic>;
                      final recurring = item['is_recurring'] == true;
                      final isPaid = item['is_paid'] == true;
                      return ListTile(
                        dense: true,
                        title: Text('${item['title'] ?? 'Entry'}'),
                        subtitle: Text('${item['category'] ?? 'General'}${item['payer'] != null ? ' • ${item['payer']}' : ''}${recurring ? ' • recurring' : ''}'),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            if (recurring)
                              IconButton(
                                icon: Icon(isPaid ? Icons.check_circle : Icons.radio_button_unchecked, color: isPaid ? Colors.green : null),
                                tooltip: isPaid ? 'Mark unpaid' : 'Mark paid',
                                onPressed: () async {
                                  final messenger = ScaffoldMessenger.of(context);
                                  try {
                                    await widget.api.markExpenseEntryPaid(entryId: _toInt(item['id']), paid: !isPaid);
                                    if (!mounted) return;
                                    messenger.showSnackBar(SnackBar(content: Text(isPaid ? 'Marked unpaid' : 'Marked paid')));
                                    await _refresh();
                                  } catch (err) {
                                    if (!mounted) return;
                                    messenger.showSnackBar(SnackBar(content: Text('Update failed: $err')));
                                  }
                                },
                              ),
                            PopupMenuButton<String>(
                              onSelected: (value) async {
                                final messenger = ScaffoldMessenger.of(context);
                                try {
                                  if (value == 'delete') {
                                    await widget.api.deleteExpenseEntry(entryId: _toInt(item['id']), deleteMode: 'instance');
                                  } else if (value == 'delete_all') {
                                    await widget.api.deleteExpenseEntry(entryId: _toInt(item['id']), deleteMode: 'all');
                                  }
                                  if (!mounted) return;
                                  messenger.showSnackBar(const SnackBar(content: Text('Entry removed')));
                                  await _refresh();
                                } catch (err) {
                                  if (!mounted) return;
                                  messenger.showSnackBar(SnackBar(content: Text('Delete failed: $err')));
                                }
                              },
                              itemBuilder: (context) => [
                                const PopupMenuItem(value: 'delete', child: Text('Delete entry')),
                                if (recurring) const PopupMenuItem(value: 'delete_all', child: Text('Delete recurring series')),
                              ],
                            ),
                            Text(_currency(_toDouble(item['amount']))),
                          ],
                        ),
                      );
                    }).toList(),
                  ),
                );
              }),
            ],
          );
        },
      ),
    );
  }
}

class _MoneyInsightsTab extends StatefulWidget {
  const _MoneyInsightsTab({required this.api});

  final HomeHubApi api;

  @override
  State<_MoneyInsightsTab> createState() => _MoneyInsightsTabState();
}

class _MoneyInsightsTabState extends State<_MoneyInsightsTab> {
  DateTime _month = DateTime(DateTime.now().year, DateTime.now().month, 1);
  late Future<_MoneyBundle> _future;
  final TextEditingController _impactAmountCtrl = TextEditingController();
  String? _impactTargetType;
  int? _impactTargetId;
  Map<String, dynamic>? _impactResult;
  bool _impactLoading = false;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _impactAmountCtrl.dispose();
    super.dispose();
  }

  Future<_MoneyBundle> _load() async {
    final results = await Future.wait([
      widget.api.fetchFinanceSummary(_month),
      widget.api.fetchCollaboration(_month),
      widget.api.fetchDebtWorkflow(_month),
      widget.api.listBudgets(),
      widget.api.listDebts(),
      widget.api.listGoals(),
      widget.api.fetchAnalyticsSummary(),
      widget.api.listSharedExpenses(monthDate: _month),
      widget.api.listPlaidAccounts(),
      widget.api.fetchPendingIncomeConfirmations(),
    ]);
    return _MoneyBundle(
      finance: results[0] as Map<String, dynamic>,
      collaboration: results[1] as Map<String, dynamic>,
      debtWorkflow: results[2] as Map<String, dynamic>,
      budgets: results[3] as List<Map<String, dynamic>>,
      debtAccounts: results[4] as List<Map<String, dynamic>>,
      goals: results[5] as List<Map<String, dynamic>>,
      analytics: results[6] as Map<String, dynamic>,
      sharedExpenses: results[7] as List<Map<String, dynamic>>,
      plaidAccounts: results[8] as List<Map<String, dynamic>>,
      pendingIncomeConfirmations: results[9] as List<Map<String, dynamic>>,
    );
  }

  Future<void> _refresh() async {
    setState(() => _future = _load());
    await _future;
  }

  Future<void> _previewImpact() async {
    final amount = double.tryParse(_impactAmountCtrl.text.trim());
    if (_impactTargetType == null || _impactTargetId == null || amount == null || amount <= 0) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Pick target and enter valid payment amount')));
      return;
    }
    setState(() => _impactLoading = true);
    try {
      final result = await widget.api.previewDebtImpact(
        date: _month,
        targetType: _impactTargetType!,
        targetId: _impactTargetId!,
        paymentAmount: amount,
      );
      if (!mounted) return;
      setState(() => _impactResult = result);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Impact preview failed: $err')));
    } finally {
      if (mounted) setState(() => _impactLoading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<_MoneyBundle>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: snapshot.error.toString(), onRetry: _refresh);
          }

          final data = snapshot.data!;
          final family = data.finance['family'] as Map<String, dynamic>? ?? {};
          final budget = family['budget'] as Map<String, dynamic>? ?? {};
          final member = data.finance['member'] as Map<String, dynamic>? ?? {};
          final collaborationSummary = data.collaboration['summary'] as Map<String, dynamic>? ?? {};
          final categories = data.collaboration['categories'] as List<dynamic>? ?? [];
          final members = data.collaboration['members'] as List<dynamic>? ?? [];
          final debtSnapshot = data.debtWorkflow['snapshot'] as Map<String, dynamic>? ?? {};
          final debts = data.debtWorkflow['debts'] as List<dynamic>? ?? [];
          final dueItems = data.debtWorkflow['due_items'] as List<dynamic>? ?? [];
          final budgets = data.budgets;
          final debtAccounts = data.debtAccounts;
          final goals = data.goals;
          final analytics = data.analytics;
          final sharedExpenses = data.sharedExpenses;
          final plaidAccounts = data.plaidAccounts;
          final pendingIncomeConfirmations = data.pendingIncomeConfirmations;

          final impactTargets = <DropdownMenuItem<String>>[
            ...debts.map((raw) {
              final item = raw as Map<String, dynamic>;
              return DropdownMenuItem(
                value: 'debt:${_toInt(item['id'])}',
                child: Text('Debt • ${item['description'] ?? 'Debt'}'),
              );
            }),
            ...dueItems.map((raw) {
              final item = raw as Map<String, dynamic>;
              return DropdownMenuItem(
                value: 'recurring_expense:${_toInt(item['entry_id'])}',
                child: Text('Bill • ${item['title'] ?? 'Recurring expense'}'),
              );
            }),
          ];

          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Row(
                    children: [
                      IconButton(
                        onPressed: () {
                          setState(() {
                            _month = DateTime(_month.year, _month.month - 1, 1);
                            _future = _load();
                          });
                        },
                        icon: const Icon(Icons.chevron_left),
                      ),
                      Expanded(
                        child: Center(
                          child: Text(
                            _monthLabel(_month),
                            style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 17),
                          ),
                        ),
                      ),
                      IconButton(
                        onPressed: () {
                          setState(() {
                            _month = DateTime(_month.year, _month.month + 1, 1);
                            _future = _load();
                          });
                        },
                        icon: const Icon(Icons.chevron_right),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              _MetricCard(
                title: 'Family net',
                value: _currency(_toDouble(family['net_total'])),
                subtitle: 'Income ${_currency(_toDouble(family['income_total']))} • Expenses ${_currency(_toDouble(family['expenses_total']))}',
                icon: Icons.savings,
              ),
              const SizedBox(height: 8),
              Row(
                children: [
                  Expanded(
                    child: _MetricCard(
                      title: 'Budget remaining',
                      value: _currency(_toDouble(budget['remaining'])),
                      subtitle: budget['has_budget'] == true ? 'Total ${_currency(_toDouble(budget['total']))}' : 'No budget set',
                      icon: Icons.account_balance,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: _MetricCard(
                      title: 'My net',
                      value: _currency(_toDouble(member['net_total'])),
                      subtitle: '${member['name'] ?? ''}',
                      icon: Icons.person,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              _MetricCard(
                title: 'Debt due in 30 days',
                value: _currency(_toDouble(debtSnapshot['due_in_30_days'])),
                subtitle: 'Outstanding ${_currency(_toDouble(debtSnapshot['total_outstanding_debt']))}',
                icon: Icons.warning_amber,
              ),
              const SizedBox(height: 10),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Collaboration', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      Text('Shared income ${_currency(_toDouble(collaborationSummary['shared_income_total']))}'),
                      Text('Shared expenses ${_currency(_toDouble(collaborationSummary['shared_expenses_total']))}'),
                      Text('Savings ${_currency(_toDouble(collaborationSummary['savings_total']))}'),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Top categories', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      if (categories.isEmpty) const Text('No category data.'),
                      ...categories.take(8).map((raw) {
                        final row = raw as Map<String, dynamic>;
                        return ListTile(
                          dense: true,
                          contentPadding: EdgeInsets.zero,
                          title: Text('${row['name'] ?? 'Uncategorized'}'),
                          trailing: Text(_currency(_toDouble(row['amount']))),
                        );
                      }),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Member balances', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      if (members.isEmpty) const Text('No member data.'),
                      ...members.map((raw) {
                        final row = raw as Map<String, dynamic>;
                        return ListTile(
                          dense: true,
                          contentPadding: EdgeInsets.zero,
                          title: Text('${row['name'] ?? ''}'),
                          subtitle: Text('Income ${_currency(_toDouble(row['income_actual']))} • Expenses ${_currency(_toDouble(row['expenses']))}'),
                          trailing: Text(_currency(_toDouble(row['net_projected']))),
                        );
                      }),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Debt priorities', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      if (debts.isEmpty) const Text('No active debts.'),
                      ...debts.map((raw) {
                        final debt = raw as Map<String, dynamic>;
                        return ListTile(
                          dense: true,
                          contentPadding: EdgeInsets.zero,
                          title: Text('${debt['description'] ?? 'Debt'}'),
                          subtitle: Text('APR ${_toDouble(debt['interest_rate']).toStringAsFixed(2)}% • Priority ${debt['priority_band'] ?? 'low'}'),
                          trailing: Text(_currency(_toDouble(debt['current_balance']))),
                        );
                      }),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Payment impact preview', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      DropdownButtonFormField<String>(
                        initialValue: (_impactTargetType != null && _impactTargetId != null) ? '$_impactTargetType:$_impactTargetId' : null,
                        decoration: const InputDecoration(labelText: 'Target'),
                        items: impactTargets,
                        onChanged: (value) {
                          if (value == null) return;
                          final parts = value.split(':');
                          if (parts.length != 2) return;
                          setState(() {
                            _impactTargetType = parts[0];
                            _impactTargetId = int.tryParse(parts[1]);
                          });
                        },
                      ),
                      const SizedBox(height: 8),
                      TextField(
                        controller: _impactAmountCtrl,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true),
                        decoration: const InputDecoration(labelText: 'Payment amount'),
                      ),
                      const SizedBox(height: 10),
                      FilledButton.icon(
                        onPressed: _impactLoading ? null : _previewImpact,
                        icon: const Icon(Icons.auto_graph),
                        label: Text(_impactLoading ? 'Calculating...' : 'Preview impact'),
                      ),
                      if (_impactResult != null) ...[
                        const SizedBox(height: 10),
                        Container(
                          width: double.infinity,
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                            color: const Color(0xFFF8FAFC),
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(color: const Color(0xFFE2E8F0)),
                          ),
                          child: Text(
                            'Applied ${_currency(_toDouble(_impactResult!['applied_amount']))}\n'
                            'Before ${_currency(_toDouble((_impactResult!['before'] as Map<String, dynamic>? ?? {})['balance'] ?? (_impactResult!['before'] as Map<String, dynamic>? ?? {})['remaining_balance']))}\n'
                            'After ${_currency(_toDouble((_impactResult!['after'] as Map<String, dynamic>? ?? {})['balance'] ?? (_impactResult!['after'] as Map<String, dynamic>? ?? {})['remaining_balance']))}\n'
                            'Free cash after ${_currency(_toDouble((_impactResult!['cashflow'] as Map<String, dynamic>? ?? {})['month_free_cash_after']))}',
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Expanded(child: Text('Family Budgets', style: TextStyle(fontWeight: FontWeight.w700))),
                          TextButton(
                            onPressed: () async {
                              final nameCtrl = TextEditingController();
                              final amountCtrl = TextEditingController();
                              final monthCtrl = TextEditingController(text: '${_month.month}');
                              final yearCtrl = TextEditingController(text: '${_month.year}');
                              final should = await showDialog<bool>(
                                context: context,
                                builder: (context) => AlertDialog(
                                  insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                  title: const Text('Create Budget'),
                                  content: SingleChildScrollView(
                                    child: Column(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Name')),
                                        const SizedBox(height: 8),
                                        TextField(controller: amountCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Total amount')),
                                        const SizedBox(height: 8),
                                        TextField(controller: monthCtrl, keyboardType: TextInputType.number, decoration: const InputDecoration(labelText: 'Month')),
                                        const SizedBox(height: 8),
                                        TextField(controller: yearCtrl, keyboardType: TextInputType.number, decoration: const InputDecoration(labelText: 'Year')),
                                      ],
                                    ),
                                  ),
                                  actions: [
                                    TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                    FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
                                  ],
                                ),
                              );
                              if (should != true) return;
                              try {
                                await widget.api.createBudget(
                                  name: nameCtrl.text.trim(),
                                  month: int.tryParse(monthCtrl.text.trim()) ?? _month.month,
                                  year: int.tryParse(yearCtrl.text.trim()) ?? _month.year,
                                  totalAmount: double.tryParse(amountCtrl.text.trim()) ?? 0,
                                );
                                await _refresh();
                              } catch (err) {
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create budget failed: $err')));
                              }
                            },
                            child: const Text('Add'),
                          ),
                        ],
                      ),
                      if (budgets.isEmpty)
                        const Text('No budgets yet.')
                      else
                        ...budgets.take(6).map(
                          (budgetRow) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${budgetRow['name'] ?? ''} (${budgetRow['month']}/${budgetRow['year']})'),
                            subtitle: Text('Spent ${_currency(_toDouble(budgetRow['spent']))} / ${_currency(_toDouble(budgetRow['total']))}'),
                            trailing: TextButton(
                              onPressed: () async {
                                final amountCtrl = TextEditingController();
                                final categoryCtrl = TextEditingController();
                                final should = await showDialog<bool>(
                                  context: context,
                                  builder: (context) => AlertDialog(
                                    insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                    title: const Text('Add Budget Category'),
                                    content: SingleChildScrollView(
                                      child: Column(
                                        mainAxisSize: MainAxisSize.min,
                                        children: [
                                          TextField(controller: categoryCtrl, decoration: const InputDecoration(labelText: 'Category')),
                                          const SizedBox(height: 8),
                                          TextField(controller: amountCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Allocated amount')),
                                        ],
                                      ),
                                    ),
                                    actions: [
                                      TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                      FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
                                    ],
                                  ),
                                );
                                if (should != true) return;
                                try {
                                  await widget.api.addBudgetCategory(
                                    budgetId: _toInt(budgetRow['id']),
                                    categoryName: categoryCtrl.text.trim(),
                                    allocatedAmount: double.tryParse(amountCtrl.text.trim()) ?? 0,
                                  );
                                  await _refresh();
                                } catch (err) {
                                  if (!mounted) return;
                                  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add category failed: $err')));
                                }
                              },
                              child: const Text('Category'),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Expanded(child: Text('Debt Accounts', style: TextStyle(fontWeight: FontWeight.w700))),
                          TextButton(
                            onPressed: () async {
                              final descriptionCtrl = TextEditingController();
                              final creditorCtrl = TextEditingController();
                              final amountCtrl = TextEditingController();
                              final rateCtrl = TextEditingController(text: '0');
                              final should = await showDialog<bool>(
                                context: context,
                                builder: (context) => AlertDialog(
                                  insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                  title: const Text('Create Debt'),
                                  content: SingleChildScrollView(
                                    child: Column(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        TextField(controller: descriptionCtrl, decoration: const InputDecoration(labelText: 'Description')),
                                        const SizedBox(height: 8),
                                        TextField(controller: creditorCtrl, decoration: const InputDecoration(labelText: 'Creditor')),
                                        const SizedBox(height: 8),
                                        TextField(controller: amountCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Initial amount')),
                                        const SizedBox(height: 8),
                                        TextField(controller: rateCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'APR %')),
                                      ],
                                    ),
                                  ),
                                  actions: [
                                    TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                    FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
                                  ],
                                ),
                              );
                              if (should != true) return;
                              try {
                                await widget.api.createDebt(
                                  description: descriptionCtrl.text.trim(),
                                  debtor: widget.api.member,
                                  creditor: creditorCtrl.text.trim(),
                                  initialAmount: double.tryParse(amountCtrl.text.trim()) ?? 0,
                                  interestRate: double.tryParse(rateCtrl.text.trim()) ?? 0,
                                );
                                await _refresh();
                              } catch (err) {
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create debt failed: $err')));
                              }
                            },
                            child: const Text('Add'),
                          ),
                        ],
                      ),
                      if (debtAccounts.isEmpty)
                        const Text('No debt accounts yet.')
                      else
                        ...debtAccounts.take(8).map(
                          (debtRow) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${debtRow['description'] ?? ''}'),
                            subtitle: Text('Balance ${_currency(_toDouble(debtRow['current_balance']))} • APR ${_toDouble(debtRow['interest_rate']).toStringAsFixed(2)}%'),
                            trailing: Wrap(
                              spacing: 2,
                              children: [
                                TextButton(
                                  onPressed: () async {
                                    final amountCtrl = TextEditingController();
                                    final should = await showDialog<bool>(
                                      context: context,
                                      builder: (context) => AlertDialog(
                                        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                        title: const Text('Record Payment'),
                                        content: TextField(
                                          controller: amountCtrl,
                                          keyboardType: const TextInputType.numberWithOptions(decimal: true),
                                          decoration: const InputDecoration(labelText: 'Amount paid'),
                                        ),
                                        actions: [
                                          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
                                        ],
                                      ),
                                    );
                                    if (should != true) return;
                                    try {
                                      await widget.api.recordDebtPayment(
                                        debtId: _toInt(debtRow['id']),
                                        amountPaid: double.tryParse(amountCtrl.text.trim()) ?? 0,
                                      );
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Payment failed: $err')));
                                    }
                                  },
                                  child: const Text('Pay'),
                                ),
                                IconButton(
                                  onPressed: () async {
                                    try {
                                      await widget.api.deleteDebt(_toInt(debtRow['id']));
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete debt failed: $err')));
                                    }
                                  },
                                  icon: const Icon(Icons.delete_outline),
                                ),
                              ],
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Expanded(child: Text('Financial Goals', style: TextStyle(fontWeight: FontWeight.w700))),
                          TextButton(
                            onPressed: () async {
                              final nameCtrl = TextEditingController();
                              final amountCtrl = TextEditingController();
                              final should = await showDialog<bool>(
                                context: context,
                                builder: (context) => AlertDialog(
                                  insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                  title: const Text('Create Goal'),
                                  content: SingleChildScrollView(
                                    child: Column(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Goal name')),
                                        const SizedBox(height: 8),
                                        TextField(controller: amountCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Target amount')),
                                      ],
                                    ),
                                  ),
                                  actions: [
                                    TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                    FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
                                  ],
                                ),
                              );
                              if (should != true) return;
                              try {
                                await widget.api.createGoal(name: nameCtrl.text.trim(), targetAmount: double.tryParse(amountCtrl.text.trim()) ?? 0);
                                await _refresh();
                              } catch (err) {
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create goal failed: $err')));
                              }
                            },
                            child: const Text('Add'),
                          ),
                        ],
                      ),
                      if (goals.isEmpty)
                        const Text('No goals yet.')
                      else
                        ...goals.take(8).map(
                          (goalRow) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${goalRow['name'] ?? ''}'),
                            subtitle: Text('Progress ${_currency(_toDouble(goalRow['current_amount']))} / ${_currency(_toDouble(goalRow['target_amount']))}'),
                            trailing: TextButton(
                              onPressed: () async {
                                final amountCtrl = TextEditingController();
                                final should = await showDialog<bool>(
                                  context: context,
                                  builder: (context) => AlertDialog(
                                    insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                    title: const Text('Contribute Goal'),
                                    content: TextField(
                                      controller: amountCtrl,
                                      keyboardType: const TextInputType.numberWithOptions(decimal: true),
                                      decoration: const InputDecoration(labelText: 'Amount'),
                                    ),
                                    actions: [
                                      TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                      FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
                                    ],
                                  ),
                                );
                                if (should != true) return;
                                try {
                                  await widget.api.contributeGoal(goalId: _toInt(goalRow['id']), amount: double.tryParse(amountCtrl.text.trim()) ?? 0);
                                  await _refresh();
                                } catch (err) {
                                  if (!mounted) return;
                                  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Contribution failed: $err')));
                                }
                              },
                              child: const Text('Add'),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Expanded(child: Text('Shared Expenses', style: TextStyle(fontWeight: FontWeight.w700))),
                          TextButton(
                            onPressed: () async {
                              final descCtrl = TextEditingController();
                              final catCtrl = TextEditingController(text: 'Household');
                              final totalCtrl = TextEditingController();
                              final should = await showDialog<bool>(
                                context: context,
                                builder: (context) => AlertDialog(
                                  insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                  title: const Text('Create Shared Expense'),
                                  content: SingleChildScrollView(
                                    child: Column(
                                      mainAxisSize: MainAxisSize.min,
                                      children: [
                                        TextField(controller: descCtrl, decoration: const InputDecoration(labelText: 'Description')),
                                        const SizedBox(height: 8),
                                        TextField(controller: catCtrl, decoration: const InputDecoration(labelText: 'Category')),
                                        const SizedBox(height: 8),
                                        TextField(controller: totalCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Total amount')),
                                      ],
                                    ),
                                  ),
                                  actions: [
                                    TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
                                    FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
                                  ],
                                ),
                              );
                              if (should != true) return;
                              final total = double.tryParse(totalCtrl.text.trim()) ?? 0;
                              if (total <= 0) return;
                              try {
                                await widget.api.createSharedExpense(
                                  description: descCtrl.text.trim(),
                                  totalAmount: total,
                                  paidBy: widget.api.member,
                                  date: _month,
                                  category: catCtrl.text.trim(),
                                  participants: [
                                    {'name': widget.api.member, 'amount': total},
                                  ],
                                );
                                await _refresh();
                              } catch (err) {
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create shared expense failed: $err')));
                              }
                            },
                            child: const Text('Add'),
                          ),
                        ],
                      ),
                      if (sharedExpenses.isEmpty)
                        const Text('No shared expenses this period.')
                      else
                        ...sharedExpenses.take(8).map(
                          (row) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${row['description'] ?? ''}'),
                            subtitle: Text('${row['category'] ?? ''} • ${row['date'] ?? ''}'),
                            trailing: Wrap(
                              spacing: 2,
                              children: [
                                Text(_currency(_toDouble(row['total_amount']))),
                                IconButton(
                                  onPressed: () async {
                                    final picked = await FilePicker.platform.pickFiles(withData: true, allowMultiple: false);
                                    if (picked == null || picked.files.isEmpty) return;
                                    try {
                                      await widget.api.uploadSharedExpenseReceipt(_toInt(row['id']), picked.files.first);
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Upload receipt failed: $err')));
                                    }
                                  },
                                  icon: const Icon(Icons.receipt_long),
                                  tooltip: 'Attach receipt',
                                ),
                                IconButton(
                                  onPressed: () async {
                                    try {
                                      await widget.api.setSharedExpenseSettled(_toInt(row['id']), !(row['settled'] == true));
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Settle failed: $err')));
                                    }
                                  },
                                  icon: Icon(row['settled'] == true ? Icons.check_circle : Icons.pending_actions),
                                  tooltip: 'Toggle settled',
                                ),
                                IconButton(
                                  onPressed: () async {
                                    try {
                                      await widget.api.deleteSharedExpense(_toInt(row['id']));
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete shared expense failed: $err')));
                                    }
                                  },
                                  icon: const Icon(Icons.delete_outline),
                                  tooltip: 'Delete',
                                ),
                              ],
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const Expanded(child: Text('Plaid & Accounts', style: TextStyle(fontWeight: FontWeight.w700))),
                          TextButton(
                            onPressed: () async {
                              try {
                                final link = await widget.api.createPlaidLinkToken();
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Link token ready: ${(link['link_token'] ?? '').toString().substring(0, 12)}...')));
                              } catch (err) {
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Plaid link token failed: $err')));
                              }
                            },
                            child: const Text('Create Link Token'),
                          ),
                        ],
                      ),
                      if (plaidAccounts.isEmpty)
                        const Text('No connected Plaid accounts.')
                      else
                        ...plaidAccounts.take(8).map(
                          (accountRow) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${accountRow['account_name'] ?? ''}'),
                            subtitle: Text('${accountRow['account_type'] ?? ''} • ${accountRow['user'] ?? ''}'),
                            trailing: Wrap(
                              spacing: 2,
                              children: [
                                Text(_currency(_toDouble(accountRow['balance']))),
                                IconButton(
                                  onPressed: () async {
                                    try {
                                      await widget.api.syncPlaidAccount(_toInt(accountRow['id']));
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Sync failed: $err')));
                                    }
                                  },
                                  icon: const Icon(Icons.sync),
                                  tooltip: 'Sync',
                                ),
                                IconButton(
                                  onPressed: () async {
                                    try {
                                      await widget.api.disconnectPlaidAccount(_toInt(accountRow['id']));
                                      await _refresh();
                                    } catch (err) {
                                      if (!mounted) return;
                                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Disconnect failed: $err')));
                                    }
                                  },
                                  icon: const Icon(Icons.link_off),
                                  tooltip: 'Disconnect',
                                ),
                              ],
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Analytics & Income Confirmations', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      Text('Period: ${analytics['period'] ?? ''}'),
                      Text('Debts: ${_currency(_toDouble((analytics['debts'] as Map<String, dynamic>? ?? {})['total_balance']))}'),
                      Text('Goals progress: ${_toDouble((analytics['goals'] as Map<String, dynamic>? ?? {})['progress']).toStringAsFixed(1)}%'),
                      const SizedBox(height: 8),
                      if (pendingIncomeConfirmations.isEmpty)
                        const Text('No pending income confirmations.')
                      else
                        ...pendingIncomeConfirmations.map(
                          (incomeRow) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${incomeRow['title'] ?? ''} • ${_currency(_toDouble(incomeRow['amount']))}'),
                            subtitle: Text('${incomeRow['date'] ?? ''} • ${incomeRow['recipient'] ?? ''}'),
                            trailing: TextButton(
                              onPressed: () async {
                                try {
                                  await widget.api.confirmIncomeEntry(incomeId: _toInt(incomeRow['id']));
                                  await _refresh();
                                } catch (err) {
                                  if (!mounted) return;
                                  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Confirm failed: $err')));
                                }
                              },
                              child: const Text('Confirm'),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _MoneyBundle {
  const _MoneyBundle({
    required this.finance,
    required this.collaboration,
    required this.debtWorkflow,
    required this.budgets,
    required this.debtAccounts,
    required this.goals,
    required this.analytics,
    required this.sharedExpenses,
    required this.plaidAccounts,
    required this.pendingIncomeConfirmations,
  });

  final Map<String, dynamic> finance;
  final Map<String, dynamic> collaboration;
  final Map<String, dynamic> debtWorkflow;
  final List<Map<String, dynamic>> budgets;
  final List<Map<String, dynamic>> debtAccounts;
  final List<Map<String, dynamic>> goals;
  final Map<String, dynamic> analytics;
  final List<Map<String, dynamic>> sharedExpenses;
  final List<Map<String, dynamic>> plaidAccounts;
  final List<Map<String, dynamic>> pendingIncomeConfirmations;
}

class MealPlannerPage extends StatefulWidget {
  const MealPlannerPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<MealPlannerPage> createState() => _MealPlannerPageState();
}

class _MealPlannerPageState extends State<MealPlannerPage> {
  DateTime _month = DateTime(DateTime.now().year, DateTime.now().month, 1);
  DateTime _weekStart = DateTime.now().subtract(Duration(days: DateTime.now().weekday - 1));
  bool _weeklyView = true;
  final TextEditingController _searchCtrl = TextEditingController();
  String _searchQuery = '';
  late Future<_MealData> _future;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _searchCtrl.dispose();
    super.dispose();
  }

  DateTime _dateOnly(DateTime d) => DateTime(d.year, d.month, d.day);

  List<DateTime> _monthStartsInRange(DateTime start, DateTime end) {
    final out = <DateTime>[];
    var cursor = DateTime(start.year, start.month, 1);
    final last = DateTime(end.year, end.month, 1);
    while (!cursor.isAfter(last)) {
      out.add(cursor);
      cursor = DateTime(cursor.year, cursor.month + 1, 1);
    }
    return out;
  }

  DateTime _rangeStart() {
    return _weeklyView ? _dateOnly(_weekStart) : DateTime(_month.year, _month.month, 1);
  }

  DateTime _rangeEnd() {
    if (_weeklyView) return _dateOnly(_weekStart).add(const Duration(days: 6));
    return DateTime(_month.year, _month.month + 1, 0);
  }

  String _weeklyRangeLabel(DateTime start) {
    final end = start.add(const Duration(days: 6));
    if (start.month == end.month) {
      return '${_monthLabel(start)} ${start.day}-${end.day}';
    }
    return '${start.month}/${start.day} - ${end.month}/${end.day}';
  }

  Future<_MealData> _load() async {
    final start = _rangeStart();
    final end = _rangeEnd();
    final monthStarts = _monthStartsInRange(start, end);

    final entryResponses = await Future.wait(monthStarts.map(widget.api.fetchMealEntries));
    final byId = <int, MealPlannerEntry>{};
    for (final rows in entryResponses) {
      for (final entry in rows) {
        if (entry.date.isBefore(start) || entry.date.isAfter(end)) continue;
        byId[entry.id] = entry;
      }
    }

    final entries = byId.values.toList()
      ..sort((a, b) {
        final byDate = a.date.compareTo(b.date);
        if (byDate != 0) return byDate;
        return a.mealType.compareTo(b.mealType);
      });

    final library = await widget.api.fetchMealLibrary();
    return _MealData(
      entries: entries,
      library: library,
    );
  }

  Future<void> _refresh() async {
    setState(() => _future = _load());
    await _future;
  }

  Future<void> _moveMealEntry({
    required MealPlannerEntry entry,
    required DateTime targetDay,
    required String targetMealType,
  }) async {
    if (entry.isLocked) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Unlock this meal before moving it.')));
      return;
    }
    if (!entry.canEdit) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Only editable meals can be moved.')));
      return;
    }
    final sameDay = _fmtDate(entry.date) == _fmtDate(targetDay);
    if (sameDay && entry.mealType == targetMealType) return;
    try {
      await widget.api.upsertMealEntry(
        id: entry.id,
        date: targetDay,
        mealType: targetMealType,
        title: entry.title,
        description: entry.description,
        visibility: entry.visibility,
        requirePin: entry.requirePin,
        audience: entry.audience,
        allowedMembers: entry.allowedMembers,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Moved to ${_mealTypeLabel(targetMealType)} on ${_fmtDate(targetDay)}')),
      );
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Move failed: $err')));
    }
  }

  Future<void> _duplicateMealEntry({
    required MealPlannerEntry entry,
    required DateTime targetDay,
    required String targetMealType,
  }) async {
    try {
      await widget.api.upsertMealEntry(
        date: targetDay,
        mealType: targetMealType,
        title: entry.title,
        description: entry.description,
        visibility: entry.visibility,
        requirePin: entry.requirePin,
        audience: entry.audience,
        allowedMembers: entry.allowedMembers,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Duplicated to ${_mealTypeLabel(targetMealType)} on ${_fmtDate(targetDay)}')),
      );
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Duplicate failed: $err')));
    }
  }

  Future<void> _openDuplicateSheet(MealPlannerEntry entry) async {
    final picked = await showModalBottomSheet<_MealTarget>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      builder: (_) => _MealTargetSheet(
        initialDate: entry.date,
        initialMealType: entry.mealType,
        title: 'Duplicate meal to...',
        actionLabel: 'Duplicate here',
      ),
    );
    if (picked == null) return;
    await _duplicateMealEntry(entry: entry, targetDay: picked.day, targetMealType: picked.mealType);
  }

  String _libraryMealType(Map<String, dynamic> row) {
    final value = '${row['meal_type'] ?? ''}'.trim();
    if (_mealTypes.contains(value)) return value;
    return 'dinner';
  }

  Future<void> _saveLibraryMealToTarget({
    required Map<String, dynamic> row,
    required DateTime day,
    required String mealType,
  }) async {
    final title = '${row['title'] ?? ''}'.trim();
    if (title.isEmpty) return;
    final description = '${row['description'] ?? ''}'.trim();
    await widget.api.upsertMealEntry(
      date: day,
      mealType: mealType,
      title: title,
      description: description,
      visibility: 'family',
      requirePin: false,
      audience: 'family',
      allowedMembers: const <String>[],
    );
  }

  Future<void> _addFromMealLibraryToToday(Map<String, dynamic> row) async {
    final title = '${row['title'] ?? ''}'.trim();
    if (title.isEmpty) return;
    final mealType = _libraryMealType(row);
    final today = DateTime.now();
    try {
      await _saveLibraryMealToTarget(
        row: row,
        day: DateTime(today.year, today.month, today.day),
        mealType: mealType,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Added "$title" to today (${_mealTypeLabel(mealType)})')),
      );
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Quick add failed: $err')));
    }
  }

  Future<void> _addFromMealLibrary(Map<String, dynamic> row) async {
    final title = '${row['title'] ?? ''}'.trim();
    if (title.isEmpty) return;
    final mealType = _libraryMealType(row);
    final picked = await showModalBottomSheet<_MealTarget>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      builder: (_) => _MealTargetSheet(
        initialDate: DateTime.now(),
        initialMealType: mealType,
        title: 'Add from meal library',
        actionLabel: 'Add meal plan',
      ),
    );
    if (picked == null) return;

    try {
      await _saveLibraryMealToTarget(
        row: row,
        day: picked.day,
        mealType: picked.mealType,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Added "$title" to ${_mealTypeLabel(picked.mealType)} on ${_fmtDate(picked.day)}')),
      );
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add from library failed: $err')));
    }
  }

  Future<void> _openAddToSlot(DateTime day, String mealType) async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => MealEntryEditorPage(
          api: widget.api,
          initialDate: day,
          initialMealType: mealType,
        ),
      ),
    );
    if (changed == true && mounted) {
      await _refresh();
    }
  }

  Future<void> _openEditor({MealPlannerEntry? source, DateTime? day}) async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => MealEntryEditorPage(
          api: widget.api,
          source: source,
          initialDate: day ?? _month,
        ),
      ),
    );
    if (changed == true && mounted) {
      await _refresh();
    }
  }

  Future<void> _unlock(MealPlannerEntry entry) async {
    final pinCtrl = TextEditingController();
    final pin = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Unlock meal entry'),
        content: TextField(
          controller: pinCtrl,
          keyboardType: TextInputType.number,
          obscureText: true,
          decoration: const InputDecoration(labelText: 'PIN'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, pinCtrl.text.trim()), child: const Text('Unlock')),
        ],
      ),
    );
    pinCtrl.dispose();
    if (pin == null || pin.isEmpty) return;
    try {
      await widget.api.unlockMealEntry(entryId: entry.id, pin: pin);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Entry unlocked')));
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Unlock failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<_MealData>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: snapshot.error.toString(), onRetry: _refresh);
          }

          final data = snapshot.data!;
          final q = _searchQuery.trim().toLowerCase();
          final filteredEntries = q.isEmpty
              ? data.entries
              : data.entries.where((entry) {
                  final hay = [
                    entry.title,
                    entry.description,
                    entry.creator,
                    entry.mealType,
                    entry.audience,
                    entry.visibility,
                    _fmtDate(entry.date),
                  ].join(' ').toLowerCase();
                  return hay.contains(q);
                }).toList();

          final grouped = <String, List<MealPlannerEntry>>{};
          for (final entry in filteredEntries) {
            final key = _fmtDate(entry.date);
            grouped.putIfAbsent(key, () => []).add(entry);
          }
          final days = _weeklyView
              ? List<DateTime>.generate(7, (index) => _dateOnly(_weekStart).add(Duration(days: index)))
              : _daysInMonth(_month);

          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Row(
                    children: [
                      IconButton(
                        onPressed: () {
                          setState(() {
                            if (_weeklyView) {
                              _weekStart = _dateOnly(_weekStart).subtract(const Duration(days: 7));
                              _month = DateTime(_weekStart.year, _weekStart.month, 1);
                            } else {
                              _month = DateTime(_month.year, _month.month - 1, 1);
                            }
                            _future = _load();
                          });
                        },
                        icon: const Icon(Icons.chevron_left),
                      ),
                      Expanded(
                        child: Center(
                          child: Text(
                            _weeklyView ? _weeklyRangeLabel(_weekStart) : _monthLabel(_month),
                            style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 17),
                          ),
                        ),
                      ),
                      IconButton(
                        onPressed: () {
                          setState(() {
                            if (_weeklyView) {
                              _weekStart = _dateOnly(_weekStart).add(const Duration(days: 7));
                              _month = DateTime(_weekStart.year, _weekStart.month, 1);
                            } else {
                              _month = DateTime(_month.year, _month.month + 1, 1);
                            }
                            _future = _load();
                          });
                        },
                        icon: const Icon(Icons.chevron_right),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          ChoiceChip(
                            label: const Text('Weekly'),
                            selected: _weeklyView,
                            onSelected: (selected) {
                              if (!selected) return;
                              setState(() {
                                _weeklyView = true;
                                _future = _load();
                              });
                            },
                          ),
                          ChoiceChip(
                            label: const Text('Monthly'),
                            selected: !_weeklyView,
                            onSelected: (selected) {
                              if (!selected) return;
                              setState(() {
                                _weeklyView = false;
                                _month = DateTime(_month.year, _month.month, 1);
                                _future = _load();
                              });
                            },
                          ),
                        ],
                      ),
                      const SizedBox(height: 10),
                      TextField(
                        controller: _searchCtrl,
                        decoration: InputDecoration(
                          labelText: 'Search meals',
                          hintText: 'Title, notes, audience, creator...',
                          suffixIcon: _searchQuery.isEmpty
                              ? const Icon(Icons.search)
                              : IconButton(
                                  tooltip: 'Clear search',
                                  onPressed: () {
                                    setState(() {
                                      _searchCtrl.clear();
                                      _searchQuery = '';
                                    });
                                  },
                                  icon: const Icon(Icons.close),
                                ),
                        ),
                        onChanged: (value) => setState(() => _searchQuery = value),
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              _MetricCard(
                title: 'Planned meals',
                value: '${filteredEntries.length}',
                subtitle: 'Library items ${data.library.length}',
                icon: Icons.restaurant_menu,
              ),
              const SizedBox(height: 10),
              FilledButton.icon(
                onPressed: () => _openEditor(day: DateTime.now()),
                icon: const Icon(Icons.add),
                label: const Text('Add meal plan'),
              ),
              const SizedBox(height: 8),
              if (_weeklyView)
                _MealWeeklyLaneTable(
                  days: days,
                  grouped: grouped,
                  onAdd: (day, mealType) => _openAddToSlot(day, mealType),
                  onAcceptMove: (entry, day, mealType) => _moveMealEntry(entry: entry, targetDay: day, targetMealType: mealType),
                  onUnlock: _unlock,
                  onEdit: (entry) => _openEditor(source: entry),
                  onDuplicate: _openDuplicateSheet,
                )
              else
                ...days.map((day) {
                  final key = _fmtDate(day);
                  final entries = (grouped[key] ?? <MealPlannerEntry>[])..sort((a, b) => a.mealType.compareTo(b.mealType));
                  final slotMap = <String, List<MealPlannerEntry>>{
                    for (final type in _mealTypes) type: entries.where((entry) => entry.mealType == type).toList(),
                  };
                  return Card(
                    margin: const EdgeInsets.only(bottom: 10),
                    child: ExpansionTile(
                      initiallyExpanded: _fmtDate(day) == _fmtDate(DateTime.now()),
                      title: Text('${_weekdayLabel(day)} • ${_fmtDate(day)}'),
                      subtitle: Text('${entries.length} meal${entries.length == 1 ? '' : 's'} planned'),
                      childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                      children: _mealTypes.map((mealType) {
                        final slotEntries = slotMap[mealType] ?? const <MealPlannerEntry>[];
                        return Padding(
                          padding: const EdgeInsets.only(top: 8),
                          child: _MealSlotCard(
                            day: day,
                            mealType: mealType,
                            entries: slotEntries,
                            onAdd: () => _openAddToSlot(day, mealType),
                            onAcceptMove: (entry) => _moveMealEntry(entry: entry, targetDay: day, targetMealType: mealType),
                            onUnlock: _unlock,
                            onEdit: (entry) => _openEditor(source: entry),
                            onDuplicate: _openDuplicateSheet,
                          ),
                        );
                      }).toList(),
                    ),
                  );
                }),
              const SizedBox(height: 8),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text('Meal library', style: TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 8),
                      if (data.library.isEmpty) const Text('No library items yet.'),
                      ...data.library.map((row) => ListTile(
                            dense: true,
                            contentPadding: EdgeInsets.zero,
                            title: Text('${row['title'] ?? ''}'),
                            subtitle: Text('${row['meal_type'] ?? ''}${row['last_used_date'] != null ? ' • ${row['last_used_date']}' : ''}'),
                            trailing: Wrap(
                              spacing: 4,
                              children: [
                                IconButton(
                                  tooltip: 'Add to today',
                                  onPressed: () => _addFromMealLibraryToToday(row),
                                  icon: const Icon(Icons.today_outlined),
                                ),
                                const Icon(Icons.add_circle_outline),
                              ],
                            ),
                            onTap: () => _addFromMealLibrary(row),
                          )),
                    ],
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _MealData {
  const _MealData({required this.entries, required this.library});

  final List<MealPlannerEntry> entries;
  final List<Map<String, dynamic>> library;
}

class _MealTarget {
  const _MealTarget({required this.day, required this.mealType});

  final DateTime day;
  final String mealType;
}

class _MealTargetSheet extends StatefulWidget {
  const _MealTargetSheet({
    required this.initialDate,
    required this.initialMealType,
    this.title = 'Choose meal slot',
    this.actionLabel = 'Continue',
  });

  final DateTime initialDate;
  final String initialMealType;
  final String title;
  final String actionLabel;

  @override
  State<_MealTargetSheet> createState() => _MealTargetSheetState();
}

class _MealTargetSheetState extends State<_MealTargetSheet> {
  late DateTime _day = widget.initialDate;
  late String _mealType = widget.initialMealType;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(widget.title, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: () async {
                  final picked = await showDatePicker(
                    context: context,
                    initialDate: _day,
                    firstDate: DateTime(2020),
                    lastDate: DateTime(2100),
                  );
                  if (picked != null) setState(() => _day = picked);
                },
                icon: const Icon(Icons.calendar_today),
                label: Text('Date ${_fmtDate(_day)}'),
              ),
              const SizedBox(height: 10),
              DropdownButtonFormField<String>(
                initialValue: _mealType,
                decoration: const InputDecoration(labelText: 'Meal slot'),
                items: _mealTypes
                    .map((type) => DropdownMenuItem(value: type, child: Text(_mealTypeLabel(type))))
                    .toList(),
                onChanged: (value) => setState(() => _mealType = value ?? _mealType),
              ),
              const SizedBox(height: 14),
              FilledButton.icon(
                onPressed: () => Navigator.of(context).pop(_MealTarget(day: _day, mealType: _mealType)),
                icon: const Icon(Icons.copy_all_outlined),
                label: Text(widget.actionLabel),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _MealWeeklyLaneTable extends StatelessWidget {
  const _MealWeeklyLaneTable({
    required this.days,
    required this.grouped,
    required this.onAdd,
    required this.onAcceptMove,
    required this.onUnlock,
    required this.onEdit,
    required this.onDuplicate,
  });

  final List<DateTime> days;
  final Map<String, List<MealPlannerEntry>> grouped;
  final Future<void> Function(DateTime day, String mealType) onAdd;
  final Future<void> Function(MealPlannerEntry entry, DateTime day, String mealType) onAcceptMove;
  final Future<void> Function(MealPlannerEntry entry) onUnlock;
  final Future<void> Function(MealPlannerEntry entry) onEdit;
  final Future<void> Function(MealPlannerEntry entry) onDuplicate;

  @override
  Widget build(BuildContext context) {
    const double cellWidth = 240;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Table(
            defaultVerticalAlignment: TableCellVerticalAlignment.top,
            columnWidths: {
              0: const FixedColumnWidth(130),
              for (var i = 0; i < days.length; i++) i + 1: const FixedColumnWidth(cellWidth),
            },
            border: TableBorder.all(color: const Color(0xFFE2E8F0)),
            children: [
              TableRow(
                decoration: const BoxDecoration(color: Color(0xFFF8FAFC)),
                children: [
                  const Padding(
                    padding: EdgeInsets.all(10),
                    child: Text('Meal Slot', style: TextStyle(fontWeight: FontWeight.w700)),
                  ),
                  ...days.map((day) => Padding(
                        padding: const EdgeInsets.all(10),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(_weekdayLabel(day), style: const TextStyle(fontWeight: FontWeight.w700)),
                            Text(_fmtDate(day), style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                          ],
                        ),
                      )),
                ],
              ),
              ..._mealTypes.map((mealType) {
                return TableRow(
                  children: [
                    Padding(
                      padding: const EdgeInsets.all(10),
                      child: Text(_mealTypeLabel(mealType), style: const TextStyle(fontWeight: FontWeight.w700)),
                    ),
                    ...days.map((day) {
                      final dayEntries = grouped[_fmtDate(day)] ?? const <MealPlannerEntry>[];
                      final slotEntries = dayEntries.where((entry) => entry.mealType == mealType).toList();
                      return _MealLaneCell(
                        day: day,
                        mealType: mealType,
                        entries: slotEntries,
                        onAdd: () => onAdd(day, mealType),
                        onAcceptMove: (entry) => onAcceptMove(entry, day, mealType),
                        onUnlock: onUnlock,
                        onEdit: onEdit,
                        onDuplicate: onDuplicate,
                      );
                    }),
                  ],
                );
              }),
            ],
          ),
        ),
      ),
    );
  }
}

class _MealLaneCell extends StatelessWidget {
  const _MealLaneCell({
    required this.day,
    required this.mealType,
    required this.entries,
    required this.onAdd,
    required this.onAcceptMove,
    required this.onUnlock,
    required this.onEdit,
    required this.onDuplicate,
  });

  final DateTime day;
  final String mealType;
  final List<MealPlannerEntry> entries;
  final VoidCallback onAdd;
  final Future<void> Function(MealPlannerEntry entry) onAcceptMove;
  final Future<void> Function(MealPlannerEntry entry) onUnlock;
  final Future<void> Function(MealPlannerEntry entry) onEdit;
  final Future<void> Function(MealPlannerEntry entry) onDuplicate;

  @override
  Widget build(BuildContext context) {
    return DragTarget<MealPlannerEntry>(
      onWillAcceptWithDetails: (details) {
        final entry = details.data;
        return entry.canEdit && !entry.isLocked;
      },
      onAcceptWithDetails: (details) => onAcceptMove(details.data),
      builder: (context, candidateData, rejectedData) {
        final isActive = candidateData.isNotEmpty;
        return AnimatedContainer(
          duration: const Duration(milliseconds: 150),
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(
            color: isActive ? const Color(0xFFE0E7FF) : Colors.transparent,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Align(
                alignment: Alignment.centerRight,
                child: TextButton.icon(
                  onPressed: onAdd,
                  icon: const Icon(Icons.add, size: 16),
                  label: const Text('Add'),
                ),
              ),
              if (entries.isEmpty)
                const Padding(
                  padding: EdgeInsets.only(bottom: 6),
                  child: Text('Drop here', style: TextStyle(fontSize: 12, color: Color(0xFF94A3B8))),
                ),
              ...entries.map(
                (entry) => Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: _MealEntryDragCard(
                    entry: entry,
                    onUnlock: () => onUnlock(entry),
                    onEdit: entry.canEdit ? () => onEdit(entry) : null,
                    onDuplicate: () => onDuplicate(entry),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _MealSlotCard extends StatelessWidget {
  const _MealSlotCard({
    required this.day,
    required this.mealType,
    required this.entries,
    required this.onAdd,
    required this.onAcceptMove,
    required this.onUnlock,
    required this.onEdit,
    required this.onDuplicate,
  });

  final DateTime day;
  final String mealType;
  final List<MealPlannerEntry> entries;
  final VoidCallback onAdd;
  final Future<void> Function(MealPlannerEntry entry) onAcceptMove;
  final Future<void> Function(MealPlannerEntry entry) onUnlock;
  final Future<void> Function(MealPlannerEntry entry) onEdit;
  final Future<void> Function(MealPlannerEntry entry) onDuplicate;

  @override
  Widget build(BuildContext context) {
    return DragTarget<MealPlannerEntry>(
      onWillAcceptWithDetails: (details) {
        final entry = details.data;
        return entry.canEdit && !entry.isLocked;
      },
      onAcceptWithDetails: (details) => onAcceptMove(details.data),
      builder: (context, candidateData, rejectedData) {
        final isActive = candidateData.isNotEmpty;
        return AnimatedContainer(
          duration: const Duration(milliseconds: 150),
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: isActive ? const Color(0xFFE0E7FF) : const Color(0xFFF8FAFC),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: isActive ? const Color(0xFF6366F1) : const Color(0xFFE2E8F0), width: isActive ? 1.5 : 1),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      _mealTypeLabel(mealType),
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                  ),
                  TextButton.icon(onPressed: onAdd, icon: const Icon(Icons.add, size: 16), label: const Text('Add')),
                ],
              ),
              if (entries.isEmpty)
                const Padding(
                  padding: EdgeInsets.only(top: 2),
                  child: Text('Drop a meal here or add a new one.', style: TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                ),
              ...entries.map(
                (entry) => Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: _MealEntryDragCard(
                    entry: entry,
                    onUnlock: () => onUnlock(entry),
                    onEdit: entry.canEdit ? () => onEdit(entry) : null,
                    onDuplicate: () => onDuplicate(entry),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _MealEntryDragCard extends StatelessWidget {
  const _MealEntryDragCard({
    required this.entry,
    required this.onUnlock,
    this.onEdit,
    required this.onDuplicate,
  });

  final MealPlannerEntry entry;
  final VoidCallback onUnlock;
  final VoidCallback? onEdit;
  final VoidCallback onDuplicate;

  @override
  Widget build(BuildContext context) {
    final card = Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: entry.isLocked
            ? onUnlock
            : () {
                showModalBottomSheet<void>(
                  context: context,
                  isScrollControlled: true,
                  useSafeArea: true,
                  backgroundColor: Colors.transparent,
                  builder: (_) => _MealDetailsSheet(
                    entry: entry,
                    onEdit: onEdit,
                    onDuplicate: onDuplicate,
                  ),
                );
              },
        borderRadius: BorderRadius.circular(12),
        child: Ink(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: entry.isLocked ? const Color(0xFFF8FAFC) : Colors.white,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: const Color(0xFFE2E8F0)),
          ),
          child: Row(
            children: [
              const Icon(Icons.drag_indicator, size: 18, color: Color(0xFF94A3B8)),
              const SizedBox(width: 8),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(entry.title, style: const TextStyle(fontWeight: FontWeight.w700)),
                    const SizedBox(height: 2),
                    Text(
                      '${entry.audience} • ${entry.visibility}${entry.requirePin ? ' • PIN' : ''}',
                      style: const TextStyle(fontSize: 12, color: Color(0xFF64748B)),
                    ),
                  ],
                ),
              ),
              if (entry.isLocked)
                TextButton(onPressed: onUnlock, child: const Text('Unlock'))
              else ...[
                IconButton(onPressed: onDuplicate, icon: const Icon(Icons.copy_outlined), tooltip: 'Duplicate'),
                if (onEdit != null) IconButton(onPressed: onEdit, icon: const Icon(Icons.edit_outlined), tooltip: 'Edit'),
              ],
            ],
          ),
        ),
      ),
    );

    if (entry.isLocked || !entry.canEdit) {
      return card;
    }

    return LongPressDraggable<MealPlannerEntry>(
      data: entry,
      feedback: SizedBox(width: 240, child: Opacity(opacity: 0.92, child: card)),
      childWhenDragging: Opacity(opacity: 0.45, child: card),
      child: card,
    );
  }
}

class _MealDetailsSheet extends StatelessWidget {
  const _MealDetailsSheet({required this.entry, this.onEdit, this.onDuplicate});

  final MealPlannerEntry entry;
  final VoidCallback? onEdit;
  final VoidCallback? onDuplicate;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(entry.title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 18))),
                  IconButton(onPressed: () => Navigator.of(context).pop(), icon: const Icon(Icons.close)),
                ],
              ),
              Text('${entry.mealType.replaceAll('_', ' ')} • ${_fmtDate(entry.date)}'),
              const SizedBox(height: 6),
              Text('Audience: ${entry.audience} • Visibility: ${entry.visibility}'),
              if (entry.allowedMembers.isNotEmpty) Text('Allowed: ${entry.allowedMembers.join(', ')}'),
              const SizedBox(height: 8),
              Text(entry.description.isEmpty ? 'No description.' : entry.description),
              const SizedBox(height: 12),
              FilledButton.icon(
                onPressed: () {
                  Navigator.of(context).pop();
                  showModalBottomSheet<void>(
                    context: context,
                    isScrollControlled: true,
                    useSafeArea: true,
                    backgroundColor: Colors.transparent,
                    builder: (_) => _MealCookModeSheet(entry: entry),
                  );
                },
                icon: const Icon(Icons.soup_kitchen_outlined),
                label: const Text('Cook Mode'),
              ),
              if (onEdit != null) ...[
                const SizedBox(height: 12),
                Row(
                  children: [
                    if (onDuplicate != null)
                      Expanded(
                        child: OutlinedButton.icon(
                          onPressed: () {
                            Navigator.of(context).pop();
                            onDuplicate?.call();
                          },
                          icon: const Icon(Icons.copy_outlined),
                          label: const Text('Duplicate'),
                        ),
                      ),
                    if (onDuplicate != null && onEdit != null) const SizedBox(width: 10),
                    if (onEdit != null)
                      Expanded(
                        child: FilledButton.icon(
                          onPressed: () {
                            Navigator.of(context).pop();
                            onEdit?.call();
                          },
                          icon: const Icon(Icons.edit),
                          label: const Text('Edit meal'),
                        ),
                      ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _MealCookModeSheet extends StatefulWidget {
  const _MealCookModeSheet({required this.entry});

  final MealPlannerEntry entry;

  @override
  State<_MealCookModeSheet> createState() => _MealCookModeSheetState();
}

class _MealCookModeSheetState extends State<_MealCookModeSheet> {
  final TextEditingController _minutesCtrl = TextEditingController(text: '15');
  Timer? _timer;
  int _remainingSeconds = 0;
  bool _running = false;

  @override
  void dispose() {
    _timer?.cancel();
    _minutesCtrl.dispose();
    super.dispose();
  }

  String _fmtTimer() {
    final mins = (_remainingSeconds ~/ 60).toString().padLeft(2, '0');
    final secs = (_remainingSeconds % 60).toString().padLeft(2, '0');
    return '$mins:$secs';
  }

  void _startTimer() {
    if (_running) return;
    if (_remainingSeconds <= 0) {
      final mins = int.tryParse(_minutesCtrl.text.trim()) ?? 0;
      _remainingSeconds = mins > 0 ? mins * 60 : 0;
      if (_remainingSeconds <= 0) return;
    }
    setState(() => _running = true);
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      if (_remainingSeconds <= 1) {
        timer.cancel();
        setState(() {
          _remainingSeconds = 0;
          _running = false;
        });
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Cooking timer finished')));
        return;
      }
      setState(() => _remainingSeconds -= 1);
    });
  }

  void _pauseTimer() {
    _timer?.cancel();
    setState(() => _running = false);
  }

  void _resetTimer() {
    _timer?.cancel();
    final mins = int.tryParse(_minutesCtrl.text.trim()) ?? 0;
    setState(() {
      _running = false;
      _remainingSeconds = mins > 0 ? mins * 60 : 0;
    });
  }

  @override
  Widget build(BuildContext context) {
    final notes = widget.entry.description
        .split(RegExp(r'\r?\n'))
        .map((line) => line.trim())
        .where((line) => line.isNotEmpty)
        .toList();

    return Container(
      decoration: const BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(widget.entry.title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 20)),
                          Text('${widget.entry.mealType.replaceAll('_', ' ')} • ${_fmtDate(widget.entry.date)}', style: const TextStyle(color: Color(0xFF64748B))),
                        ],
                      ),
                    ),
                    IconButton(onPressed: () => Navigator.of(context).pop(), icon: const Icon(Icons.close)),
                  ],
                ),
                const SizedBox(height: 10),
                const Text('Prep Notes', style: TextStyle(fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: notes.isEmpty
                        ? const Text('No prep notes yet. Add details in meal editor.')
                        : Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: notes.map((line) => Padding(
                              padding: const EdgeInsets.only(bottom: 4),
                              child: Text('• $line'),
                            )).toList(),
                          ),
                  ),
                ),
                const SizedBox(height: 12),
                const Text('Cooking Timer', style: TextStyle(fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(_fmtTimer(), style: const TextStyle(fontSize: 36, fontWeight: FontWeight.w800, letterSpacing: 1.2)),
                        const SizedBox(height: 8),
                        Row(
                          children: [
                            SizedBox(
                              width: 110,
                              child: TextField(
                                controller: _minutesCtrl,
                                keyboardType: TextInputType.number,
                                decoration: const InputDecoration(labelText: 'Minutes'),
                                onSubmitted: (_) => _resetTimer(),
                              ),
                            ),
                            const SizedBox(width: 12),
                            OutlinedButton(onPressed: _startTimer, child: const Text('Start')),
                            const SizedBox(width: 8),
                            OutlinedButton(onPressed: _pauseTimer, child: const Text('Pause')),
                            const SizedBox(width: 8),
                            OutlinedButton(onPressed: _resetTimer, child: const Text('Reset')),
                          ],
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                const Text('Common Conversions', style: TextStyle(fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Column(
                      children: const [
                        _ConversionRow(left: '1 cup', right: '16 tbsp'),
                        _ConversionRow(left: '1 tbsp', right: '3 tsp'),
                        _ConversionRow(left: '1 cup', right: '240 ml'),
                        _ConversionRow(left: '1 oz', right: '28 g'),
                        _ConversionRow(left: '1 lb', right: '454 g'),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _ConversionRow extends StatelessWidget {
  const _ConversionRow({required this.left, required this.right});

  final String left;
  final String right;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          Expanded(child: Text(left, style: const TextStyle(fontWeight: FontWeight.w600))),
          const SizedBox(width: 10),
          Expanded(child: Text(right)),
        ],
      ),
    );
  }
}

class MealEntryEditorPage extends StatefulWidget {
  const MealEntryEditorPage({
    super.key,
    required this.api,
    required this.initialDate,
    this.initialMealType,
    this.source,
  });

  final HomeHubApi api;
  final DateTime initialDate;
  final String? initialMealType;
  final MealPlannerEntry? source;

  @override
  State<MealEntryEditorPage> createState() => _MealEntryEditorPageState();
}

class _MealEntryEditorPageState extends State<MealEntryEditorPage> {
  final _formKey = GlobalKey<FormState>();
  final _titleCtrl = TextEditingController();
  final _descCtrl = TextEditingController();
  DateTime _date = DateTime.now();
  String _mealType = 'dinner';
  String _visibility = 'family';
  String _audience = 'family';
  bool _requirePin = false;
  bool _saving = false;
  bool _loadingMembers = false;
  List<String> _memberChoices = const <String>[];
  final Set<String> _allowedMembers = <String>{};

  bool get _editing => widget.source != null;

  @override
  void initState() {
    super.initState();
    final source = widget.source;
    _date = source?.date ?? widget.initialDate;
    _mealType = source?.mealType ?? widget.initialMealType ?? 'dinner';
    _visibility = source?.visibility ?? 'family';
    _audience = source?.audience ?? 'family';
    _requirePin = source?.requirePin ?? false;
    _titleCtrl.text = source?.title ?? '';
    _descCtrl.text = source?.description ?? '';
    _allowedMembers
      ..clear()
      ..addAll(source?.allowedMembers ?? const <String>[]);
    _loadMembers();
  }

  @override
  void dispose() {
    _titleCtrl.dispose();
    _descCtrl.dispose();
    super.dispose();
  }

  Future<void> _loadMembers() async {
    setState(() => _loadingMembers = true);
    try {
      final members = await widget.api.fetchCalendarMembers();
      if (!mounted) return;
      final names = members
          .map((member) => member.name.trim())
          .where((name) => name.isNotEmpty)
          .toSet()
          .toList()
        ..sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
      setState(() {
        _memberChoices = names;
        _loadingMembers = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() => _loadingMembers = false);
    }
  }

  List<String> _parseAllowedMembers() {
    return _allowedMembers.toList()..sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    if (_visibility == 'restricted' && _allowedMembers.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Select at least one allowed member for restricted visibility.')));
      return;
    }
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() => _saving = true);
    try {
      final result = await widget.api.upsertMealEntry(
        id: widget.source?.id,
        date: _date,
        mealType: _mealType,
        title: _titleCtrl.text.trim(),
        description: _descCtrl.text.trim(),
        visibility: _visibility,
        requirePin: _requirePin,
        audience: _audience,
        allowedMembers: _parseAllowedMembers(),
      );
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('${result['message'] ?? 'Meal saved'}')));
      navigator.pop(true);
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Save failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _delete() async {
    if (!_editing) return;
    final confirm = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete meal plan?'),
        content: const Text('This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (!mounted) return;
    if (confirm != true) return;
    final messenger = ScaffoldMessenger.of(context);
    final navigator = Navigator.of(context);
    setState(() => _saving = true);
    try {
      final result = await widget.api.deleteMealEntry(widget.source!.id);
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('${result['message'] ?? 'Meal removed'}')));
      navigator.pop(true);
    } catch (err) {
      if (!mounted) return;
      messenger.showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_editing ? 'Edit Meal Plan' : 'New Meal Plan'),
        actions: [
          if (_editing)
            IconButton(
              onPressed: _saving ? null : _delete,
              icon: const Icon(Icons.delete_outline),
            ),
        ],
      ),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                OutlinedButton.icon(
                  onPressed: _saving
                      ? null
                      : () async {
                          final picked = await showDatePicker(
                            context: context,
                            initialDate: _date,
                            firstDate: DateTime(2020),
                            lastDate: DateTime(2100),
                          );
                          if (picked != null) setState(() => _date = picked);
                        },
                  icon: const Icon(Icons.calendar_today),
                  label: Text('Date ${_fmtDate(_date)}'),
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _mealType,
                  decoration: const InputDecoration(labelText: 'Meal type'),
                  items: const [
                    DropdownMenuItem(value: 'breakfast', child: Text('Breakfast')),
                    DropdownMenuItem(value: 'morning_snack', child: Text('Morning Snack')),
                    DropdownMenuItem(value: 'lunch', child: Text('Lunch')),
                    DropdownMenuItem(value: 'afternoon_snack', child: Text('Afternoon Snack')),
                    DropdownMenuItem(value: 'dinner', child: Text('Dinner')),
                  ],
                  onChanged: (value) => setState(() => _mealType = value ?? 'dinner'),
                ),
                const SizedBox(height: 10),
                TextFormField(
                  controller: _titleCtrl,
                  decoration: const InputDecoration(labelText: 'Title'),
                  validator: (value) => (value == null || value.trim().isEmpty) ? 'Title is required' : null,
                ),
                const SizedBox(height: 10),
                TextFormField(
                  controller: _descCtrl,
                  minLines: 2,
                  maxLines: 4,
                  decoration: const InputDecoration(labelText: 'Description'),
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _visibility,
                  decoration: const InputDecoration(labelText: 'Visibility'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'restricted', child: Text('Restricted')),
                    DropdownMenuItem(value: 'private', child: Text('Private')),
                  ],
                  onChanged: (value) {
                    setState(() {
                      _visibility = value ?? 'family';
                      if (_visibility != 'restricted') {
                        _allowedMembers.clear();
                      }
                    });
                  },
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _audience,
                  decoration: const InputDecoration(labelText: 'Audience'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'adults', child: Text('Adults')),
                    DropdownMenuItem(value: 'kids', child: Text('Kids')),
                    DropdownMenuItem(value: 'custom', child: Text('Custom')),
                  ],
                  onChanged: (value) => setState(() => _audience = value ?? 'family'),
                ),
                if (_visibility == 'restricted') ...[
                  const SizedBox(height: 10),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      'Allowed members',
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(fontWeight: FontWeight.w700, color: const Color(0xFF475569)),
                    ),
                  ),
                  const SizedBox(height: 6),
                  if (_loadingMembers)
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 4),
                      child: LinearProgressIndicator(minHeight: 3),
                    )
                  else if (_memberChoices.isEmpty)
                    const Text('No member list available right now.', style: TextStyle(color: Color(0xFF64748B), fontSize: 12))
                  else
                    Wrap(
                      spacing: 8,
                      runSpacing: 8,
                      children: _memberChoices.map((name) {
                        final selected = _allowedMembers.contains(name);
                        return FilterChip(
                          label: Text(name),
                          selected: selected,
                          onSelected: (value) {
                            setState(() {
                              if (value) {
                                _allowedMembers.add(name);
                              } else {
                                _allowedMembers.remove(name);
                              }
                            });
                          },
                        );
                      }).toList(),
                    ),
                  if (_allowedMembers.isEmpty)
                    const Padding(
                      padding: EdgeInsets.only(top: 6),
                      child: Text('Select one or more members for restricted visibility.', style: TextStyle(color: Color(0xFF64748B), fontSize: 12)),
                    ),
                ],
                SwitchListTile.adaptive(
                  contentPadding: EdgeInsets.zero,
                  value: _requirePin,
                  title: const Text('Require PIN to open'),
                  onChanged: (value) => setState(() => _requirePin = value),
                ),
                const SizedBox(height: 12),
                FilledButton.icon(
                  onPressed: _saving ? null : _save,
                  icon: const Icon(Icons.save),
                  label: Text(_saving ? 'Saving...' : (_editing ? 'Update meal plan' : 'Create meal plan')),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class ChatThreadsPage extends StatefulWidget {
  const ChatThreadsPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<ChatThreadsPage> createState() => _ChatThreadsPageState();
}

class _ChatThreadsPageState extends State<ChatThreadsPage> {
  List<ThreadSummary> _threads = const <ThreadSummary>[];
  bool _loading = true;
  bool _refreshing = false;
  String? _error;
  Timer? _liveTimer;
  StreamSubscription<_SseEvent>? _sseSub;
  Timer? _sseReconnectTimer;

  @override
  void initState() {
    super.initState();
    _load();
    _liveTimer = Timer.periodic(const Duration(seconds: 6), (_) => _refreshLive());
    _startSse();
  }

  @override
  void dispose() {
    _liveTimer?.cancel();
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    super.dispose();
  }

  Future<void> _load({bool showSpinner = true}) async {
    if (_refreshing) return;
    _refreshing = true;
    if (showSpinner && mounted) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final rows = await widget.api.fetchThreads();
      if (!mounted) return;
      setState(() {
        _threads = rows;
        _error = null;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _refreshLive() async {
    await _load(showSpinner: false);
  }

  void _startSse() {
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _sseSub = _sseConnect(widget.api.eventsStreamUri()).listen(
      (event) {
        if (!mounted) return;
        if (event.event == 'notifications' || event.event == 'sync' || event.event == 'messages') {
          _refreshLive();
        }
      },
      onError: (_) => _scheduleSseReconnect(),
      onDone: _scheduleSseReconnect,
      cancelOnError: true,
    );
  }

  void _scheduleSseReconnect() {
    if (!mounted) return;
    _sseReconnectTimer?.cancel();
    _sseReconnectTimer = Timer(const Duration(seconds: 3), _startSse);
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async {
        await _load(showSpinner: false);
      },
      child: _loading
          ? const Center(child: CircularProgressIndicator())
          : (_error != null)
              ? ErrorPanel(error: _error!, onRetry: _load)
              : (_threads.isEmpty)
                  ? const Center(child: Text('No family members available for chat.'))
                  : ListView.builder(
                      padding: const EdgeInsets.fromLTRB(12, 12, 12, 16),
                      itemCount: _threads.length,
                      itemBuilder: (context, index) {
                        final thread = _threads[index];
                        final lastAt = thread.lastAt == null ? '' : _fmtDateTime(thread.lastAt!);
                        return Card(
                          margin: const EdgeInsets.only(bottom: 8),
                          child: InkWell(
                            borderRadius: BorderRadius.circular(16),
                            onTap: () async {
                              await Navigator.of(context).push(
                                MaterialPageRoute(
                                  builder: (_) => ChatConversationPage(api: widget.api, otherMember: thread.member),
                                ),
                              );
                              if (!mounted) return;
                              await _load(showSpinner: false);
                            },
                            child: Padding(
                              padding: const EdgeInsets.all(12),
                              child: Row(
                                children: [
                                  CircleAvatar(
                                    radius: 22,
                                    backgroundColor: const Color(0xFFE0E7FF),
                                    foregroundColor: const Color(0xFF4338CA),
                                    child: Text(thread.member.isNotEmpty ? thread.member[0].toUpperCase() : '?', style: const TextStyle(fontWeight: FontWeight.w700)),
                                  ),
                                  const SizedBox(width: 12),
                                  Expanded(
                                    child: Column(
                                      crossAxisAlignment: CrossAxisAlignment.start,
                                      children: [
                                        Text(thread.member, style: const TextStyle(fontWeight: FontWeight.w700)),
                                        const SizedBox(height: 2),
                                        Text(
                                          thread.lastMessage.isEmpty ? 'No messages yet' : thread.lastMessage,
                                          maxLines: 1,
                                          overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(color: Color(0xFF64748B)),
                                        ),
                                      ],
                                    ),
                                  ),
                                  const SizedBox(width: 8),
                                  Column(
                                    crossAxisAlignment: CrossAxisAlignment.end,
                                    children: [
                                      if (lastAt.isNotEmpty)
                                        Text(lastAt, style: const TextStyle(fontSize: 11, color: Color(0xFF94A3B8))),
                                      const SizedBox(height: 4),
                                      if (thread.unread > 0)
                                        Container(
                                          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                                          decoration: BoxDecoration(
                                            color: const Color(0xFF4F46E5),
                                            borderRadius: BorderRadius.circular(999),
                                          ),
                                          child: Text('${thread.unread}', style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700, fontSize: 11)),
                                        )
                                      else
                                        const Icon(Icons.chevron_right, color: Color(0xFF94A3B8)),
                                    ],
                                  )
                                ],
                              ),
                            ),
                          ),
                        );
                      },
                    ),
    );
  }
}

class ChatConversationPage extends StatefulWidget {
  const ChatConversationPage({super.key, required this.api, required this.otherMember});

  final HomeHubApi api;
  final String otherMember;

  @override
  State<ChatConversationPage> createState() => _ChatConversationPageState();
}

class _ChatConversationPageState extends State<ChatConversationPage> {
  List<ChatMessage> _messages = const <ChatMessage>[];
  bool _loading = true;
  bool _refreshing = false;
  String? _error;
  final TextEditingController _messageCtrl = TextEditingController();
  bool _sending = false;
  Timer? _liveTimer;
  StreamSubscription<_SseEvent>? _sseSub;
  Timer? _sseReconnectTimer;
  Color _myBubbleColor = const Color(0xFF4F46E5);
  Color _theirBubbleColor = Colors.white;

  static const List<Color> _bubblePalette = [
    Color(0xFF4F46E5),
    Color(0xFF0EA5E9),
    Color(0xFF10B981),
    Color(0xFFF97316),
    Color(0xFFEF4444),
    Color(0xFF7C3AED),
    Color(0xFF1F2937),
    Color(0xFFE2E8F0),
  ];

  @override
  void initState() {
    super.initState();
    _loadBubblePrefs();
    _load(showSpinner: true);
    _liveTimer = Timer.periodic(const Duration(seconds: 4), (_) => _refreshLive());
    _startSse();
  }

  String _chatColorKey(String side) {
    final base = widget.api.settings.baseUrl.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    final member = widget.api.member.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    final peer = widget.otherMember.replaceAll(RegExp(r'[^a-zA-Z0-9]'), '_');
    return 'homehub_chat_color_${base}_${member}_${peer}_$side';
  }

  Future<void> _loadBubblePrefs() async {
    final prefs = await SharedPreferences.getInstance();
    final mine = prefs.getInt(_chatColorKey('mine'));
    final theirs = prefs.getInt(_chatColorKey('theirs'));
    if (!mounted) return;
    setState(() {
      if (mine != null) _myBubbleColor = Color(mine);
      if (theirs != null) _theirBubbleColor = Color(theirs);
    });
  }

  Future<void> _saveBubblePrefs() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt(_chatColorKey('mine'), _myBubbleColor.toARGB32());
    await prefs.setInt(_chatColorKey('theirs'), _theirBubbleColor.toARGB32());
  }

  Future<void> _openBubbleColorSheet() async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (context) {
        return StatefulBuilder(
          builder: (context, setSheetState) {
            Widget colorGrid({required String title, required Color current, required ValueChanged<Color> onSelect}) {
              return Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: _bubblePalette.map((color) {
                      final selected = color.toARGB32() == current.toARGB32();
                      return InkWell(
                        borderRadius: BorderRadius.circular(999),
                        onTap: () => onSelect(color),
                        child: Container(
                          width: 34,
                          height: 34,
                          decoration: BoxDecoration(
                            color: color,
                            shape: BoxShape.circle,
                            border: Border.all(color: selected ? const Color(0xFF111827) : Colors.transparent, width: 2),
                          ),
                          child: selected
                              ? const Icon(Icons.check, size: 18, color: Colors.white)
                              : null,
                        ),
                      );
                    }).toList(),
                  ),
                ],
              );
            }

            return Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    children: [
                      const Text('Chat Bubble Colors', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
                      const Spacer(),
                      IconButton(onPressed: () => Navigator.of(context).pop(), icon: const Icon(Icons.close)),
                    ],
                  ),
                  colorGrid(
                    title: 'Your bubbles',
                    current: _myBubbleColor,
                    onSelect: (value) {
                      setSheetState(() => _myBubbleColor = value);
                      setState(() => _myBubbleColor = value);
                    },
                  ),
                  const SizedBox(height: 12),
                  colorGrid(
                    title: 'Their bubbles',
                    current: _theirBubbleColor,
                    onSelect: (value) {
                      setSheetState(() => _theirBubbleColor = value);
                      setState(() => _theirBubbleColor = value);
                    },
                  ),
                  const SizedBox(height: 14),
                  FilledButton.icon(
                    onPressed: () async {
                      await _saveBubblePrefs();
                      if (!context.mounted) return;
                      Navigator.of(context).pop();
                    },
                    icon: const Icon(Icons.save),
                    label: const Text('Save Colors'),
                  ),
                ],
              ),
            );
          },
        );
      },
    );
  }

  Future<void> _load({bool showSpinner = false}) async {
    if (_refreshing) return;
    _refreshing = true;
    if (showSpinner && mounted) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final rows = await widget.api.fetchThreadMessages(widget.otherMember);
      await widget.api.markThreadRead(widget.otherMember);
      if (!mounted) return;
      setState(() {
        _messages = rows;
        _error = null;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _refreshLive() async {
    await _load(showSpinner: false);
  }

  void _startSse() {
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _sseSub = _sseConnect(widget.api.eventsStreamUri()).listen(
      (event) {
        if (!mounted) return;
        if (event.event == 'notifications' || event.event == 'sync' || event.event == 'messages') {
          _refreshLive();
        }
      },
      onError: (_) => _scheduleSseReconnect(),
      onDone: _scheduleSseReconnect,
      cancelOnError: true,
    );
  }

  void _scheduleSseReconnect() {
    if (!mounted) return;
    _sseReconnectTimer?.cancel();
    _sseReconnectTimer = Timer(const Duration(seconds: 3), _startSse);
  }

  @override
  void dispose() {
    _liveTimer?.cancel();
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _messageCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        titleSpacing: 8,
        title: Row(
          children: [
            CircleAvatar(
              radius: 16,
              backgroundColor: const Color(0xFFE0E7FF),
              foregroundColor: const Color(0xFF4338CA),
              child: Text(widget.otherMember.isNotEmpty ? widget.otherMember[0].toUpperCase() : '?', style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 12)),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(widget.otherMember, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
                  const Text('End-to-end encrypted', style: TextStyle(fontSize: 11, color: Color(0xFF16A34A))),
                ],
              ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Chat colors',
            icon: const Icon(Icons.palette_outlined),
            onPressed: _openBubbleColorSheet,
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: RefreshIndicator(
              onRefresh: () async {
                await _load(showSpinner: false);
              },
              child: _loading
                  ? const Center(child: CircularProgressIndicator())
                  : (_error != null)
                      ? ErrorPanel(error: _error!, onRetry: () => _load(showSpinner: true))
                      : (_messages.isEmpty)
                          ? const Center(child: Text('No messages yet. Start the conversation.'))
                          : ListView.builder(
                              reverse: true,
                              padding: const EdgeInsets.fromLTRB(12, 12, 12, 6),
                              itemCount: _messages.length,
                              itemBuilder: (context, index) {
                                final msg = _messages[_messages.length - 1 - index];
                                final mine = msg.sender == widget.api.member;
                                final timeText = msg.createdAt == null ? '' : _fmtDateTime(msg.createdAt!);
                                final bubbleColor = mine ? _myBubbleColor : _theirBubbleColor;
                                final bubbleTextColor = ThemeData.estimateBrightnessForColor(bubbleColor) == Brightness.dark ? Colors.white : const Color(0xFF0F172A);
                                return Align(
                                  alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
                                  child: Padding(
                                    padding: const EdgeInsets.symmetric(vertical: 3),
                                    child: Column(
                                      crossAxisAlignment: mine ? CrossAxisAlignment.end : CrossAxisAlignment.start,
                                      children: [
                                        Container(
                                          margin: EdgeInsets.only(left: mine ? 44 : 0, right: mine ? 0 : 44),
                                          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
                                          decoration: BoxDecoration(
                                            color: bubbleColor,
                                            borderRadius: BorderRadius.only(
                                              topLeft: const Radius.circular(18),
                                              topRight: const Radius.circular(18),
                                              bottomLeft: Radius.circular(mine ? 18 : 6),
                                              bottomRight: Radius.circular(mine ? 6 : 18),
                                            ),
                                            boxShadow: const [
                                              BoxShadow(color: Color(0x14000000), blurRadius: 6, offset: Offset(0, 2)),
                                            ],
                                          ),
                                          child: Text(
                                            msg.message,
                                            style: TextStyle(color: bubbleTextColor, height: 1.35),
                                          ),
                                        ),
                                        if (timeText.isNotEmpty)
                                          Padding(
                                            padding: EdgeInsets.only(top: 2, left: mine ? 0 : 6, right: mine ? 6 : 0),
                                            child: Text(timeText, style: const TextStyle(fontSize: 10, color: Color(0xFF94A3B8))),
                                          ),
                                      ],
                                    ),
                                  ),
                                );
                              },
                            ),
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(10, 8, 10, 10),
              child: Row(
                children: [
                  Expanded(
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(24),
                        border: Border.all(color: const Color(0xFFE2E8F0)),
                      ),
                      child: TextField(
                        controller: _messageCtrl,
                        minLines: 1,
                        maxLines: 5,
                        decoration: const InputDecoration(
                          hintText: 'iMessage-style encrypted chat...',
                          border: InputBorder.none,
                          enabledBorder: InputBorder.none,
                          focusedBorder: InputBorder.none,
                          filled: false,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Container(
                    width: 46,
                    height: 46,
                    decoration: const BoxDecoration(
                      shape: BoxShape.circle,
                      color: Color(0xFF4F46E5),
                    ),
                    child: IconButton(
                      color: Colors.white,
                      onPressed: _sending
                        ? null
                        : () async {
                            final text = _messageCtrl.text.trim();
                            if (text.isEmpty) return;
                            final messenger = ScaffoldMessenger.of(context);
                            setState(() => _sending = true);
                            try {
                              await widget.api.sendMessage(recipient: widget.otherMember, message: text);
                              _messageCtrl.clear();
                              if (!mounted) return;
                              await _load(showSpinner: false);
                            } catch (err) {
                              if (!mounted) return;
                              messenger.showSnackBar(SnackBar(content: Text('Send failed: $err')));
                            } finally {
                              if (mounted) setState(() => _sending = false);
                            }
                          },
                      icon: const Icon(Icons.send_rounded),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class PollsPage extends StatefulWidget {
  const PollsPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<PollsPage> createState() => _PollsPageState();
}

class _PollsPageState extends State<PollsPage> {
  final _questionCtrl = TextEditingController();
  final _optionsCtrl = TextEditingController();
  bool _loading = true;
  bool _creating = false;
  final Set<int> _votingIds = <int>{};
  String? _error;
  List<Map<String, dynamic>> _pending = const [];
  List<Map<String, dynamic>> _mine = const [];
  bool _refreshing = false;
  Timer? _liveTimer;
  StreamSubscription<_SseEvent>? _sseSub;
  Timer? _sseReconnectTimer;

  @override
  void initState() {
    super.initState();
    _optionsCtrl.text = 'Yes, No';
    _load();
    _liveTimer = Timer.periodic(const Duration(seconds: 8), (_) => _refreshLive());
    _startSse();
  }

  @override
  void dispose() {
    _liveTimer?.cancel();
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _questionCtrl.dispose();
    _optionsCtrl.dispose();
    super.dispose();
  }

  Future<void> _load({bool showSpinner = true}) async {
    if (_refreshing) return;
    _refreshing = true;
    if (showSpinner && mounted) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final pending = await widget.api.fetchPendingPolls();
      final mine = await widget.api.fetchMyPolls();
      if (!mounted) return;
      setState(() {
        _pending = pending;
        _mine = mine;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _refreshLive() async {
    await _load(showSpinner: false);
  }

  void _startSse() {
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _sseSub = _sseConnect(widget.api.eventsStreamUri()).listen(
      (event) {
        if (!mounted) return;
        if (event.event == 'notifications' || event.event == 'sync' || event.event == 'polls') {
          _refreshLive();
        }
      },
      onError: (_) => _scheduleSseReconnect(),
      onDone: _scheduleSseReconnect,
      cancelOnError: true,
    );
  }

  void _scheduleSseReconnect() {
    if (!mounted) return;
    _sseReconnectTimer?.cancel();
    _sseReconnectTimer = Timer(const Duration(seconds: 3), _startSse);
  }

  Future<void> _createPoll() async {
    final question = _questionCtrl.text.trim();
    final options = _optionsCtrl.text
        .split(',')
        .map((value) => value.trim())
        .where((value) => value.isNotEmpty)
        .toList();
    if (question.isEmpty || options.length < 2) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Enter a question and at least 2 comma-separated options.')));
      return;
    }
    setState(() => _creating = true);
    try {
      await widget.api.createPoll(question: question, options: options);
      if (!mounted) return;
      _questionCtrl.clear();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Poll created.')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create failed: $err')));
    } finally {
      if (mounted) setState(() => _creating = false);
    }
  }

  Future<void> _vote(int pollId, String option) async {
    setState(() => _votingIds.add(pollId));
    try {
      await widget.api.votePoll(pollId: pollId, selectedOption: option);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Vote submitted.')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Vote failed: $err')));
    } finally {
      if (mounted) {
        setState(() => _votingIds.remove(pollId));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ErrorPanel(error: _error!, onRetry: _load);
    }

    return RefreshIndicator(
      onRefresh: () => _load(showSpinner: false),
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Create Poll', style: TextStyle(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 10),
                  TextField(controller: _questionCtrl, decoration: const InputDecoration(labelText: 'Question')),
                  const SizedBox(height: 10),
                  TextField(controller: _optionsCtrl, decoration: const InputDecoration(labelText: 'Options (comma-separated)')),
                  const SizedBox(height: 12),
                  Align(
                    alignment: Alignment.centerRight,
                    child: FilledButton.icon(
                      onPressed: _creating ? null : _createPoll,
                      icon: const Icon(Icons.add_chart),
                      label: Text(_creating ? 'Creating...' : 'Create Poll'),
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          Text('Pending Votes (${_pending.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          if (_pending.isEmpty)
            const Card(child: ListTile(title: Text('No pending polls.')))
          else
            ..._pending.map((item) {
              final pollId = _toInt(item['id']);
              final options = (item['options'] as List<dynamic>? ?? const <dynamic>[]).map((e) => '$e').where((e) => e.trim().isNotEmpty).toList();
              final voting = _votingIds.contains(pollId);
              return Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('${item['question'] ?? ''}', style: const TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 4),
                      Text('From ${item['creator'] ?? ''}', style: const TextStyle(color: Color(0xFF64748B))),
                      const SizedBox(height: 10),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: options
                            .map(
                              (option) => OutlinedButton(
                                onPressed: voting ? null : () => _vote(pollId, option),
                                child: Text(option),
                              ),
                            )
                            .toList(),
                      ),
                    ],
                  ),
                ),
              );
            }),
          const SizedBox(height: 12),
          Text('My Polls (${_mine.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          if (_mine.isEmpty)
            const Card(child: ListTile(title: Text('No polls created yet.')))
          else
            ..._mine.map((item) {
              final responseCount = _toInt(item['response_count']);
              final recipientCount = _toInt(item['recipient_count']);
              final responses = item['responses'] as List<dynamic>? ?? const <dynamic>[];
              return Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('${item['question'] ?? ''}', style: const TextStyle(fontWeight: FontWeight.w700)),
                      const SizedBox(height: 6),
                      Text('Responses: $responseCount / $recipientCount'),
                      const SizedBox(height: 8),
                      LinearProgressIndicator(value: recipientCount > 0 ? responseCount / recipientCount : 0),
                      if (responses.isNotEmpty) const SizedBox(height: 10),
                      ...responses.take(4).whereType<Map<String, dynamic>>().map(
                        (row) => Padding(
                          padding: const EdgeInsets.only(bottom: 4),
                          child: Text('• ${row['member_name'] ?? ''}: ${row['selected_option'] ?? row['response_text'] ?? ''}'),
                        ),
                      ),
                    ],
                  ),
                ),
              );
            }),
        ],
      ),
    );
  }
}

class SharedCloudPage extends StatefulWidget {
  const SharedCloudPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<SharedCloudPage> createState() => _SharedCloudPageState();
}

class _SharedCloudPageState extends State<SharedCloudPage> {
  bool _loading = true;
  bool _refreshing = false;
  bool _uploading = false;
  bool _uploadingPhotos = false;
  bool _creatingAlbum = false;
  bool _savingWatchFolder = false;
  final Set<int> _deletingIds = <int>{};
  final Set<int> _deletingPhotoIds = <int>{};
  final Set<int> _updatingWatchIds = <int>{};
  final Set<int> _deletingWatchIds = <int>{};
  String? _error;
  List<SharedCloudFile> _files = const [];
  List<PhotoAlbumSummary> _albums = const [];
  List<PhotoAssetItem> _photoAssets = const [];
  List<PhotoWatchFolderItem> _watchFolders = const [];
  int? _selectedPhotoAlbumFilter;
  int? _selectedPhotoUploadAlbum;
  int? _selectedWatchAlbum;
  String _photoVisibility = 'private';
  String _watchScope = 'private';
  final TextEditingController _albumNameCtrl = TextEditingController();
  final TextEditingController _watchFolderCtrl = TextEditingController();
  Timer? _liveTimer;
  StreamSubscription<_SseEvent>? _sseSub;
  Timer? _sseReconnectTimer;

  @override
  void initState() {
    super.initState();
    _load();
    _liveTimer = Timer.periodic(const Duration(seconds: 10), (_) => _load(showSpinner: false));
    _startSse();
  }

  @override
  void dispose() {
    _liveTimer?.cancel();
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _albumNameCtrl.dispose();
    _watchFolderCtrl.dispose();
    super.dispose();
  }

  Future<void> _load({bool showSpinner = true}) async {
    if (_refreshing) return;
    _refreshing = true;
    if (showSpinner) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final files = await widget.api.fetchSharedCloudFiles();
      final albums = await widget.api.fetchPhotoAlbums();
      final photoAssets = await widget.api.fetchPhotoAssets();
      final watchFolders = await widget.api.fetchPhotoWatchFolders();
      if (!mounted) return;
      setState(() {
        _files = files;
        _albums = albums;
        _photoAssets = photoAssets;
        _watchFolders = watchFolders;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    } finally {
      _refreshing = false;
    }
  }

  void _startSse() {
    _sseReconnectTimer?.cancel();
    _sseSub?.cancel();
    _sseSub = _sseConnect(widget.api.eventsStreamUri()).listen(
      (event) {
        if (!mounted) return;
        if (event.event == 'notifications' || event.event == 'sync' || event.event == 'timeline' || event.event == 'photos') {
          _load(showSpinner: false);
        }
      },
      onError: (_) => _scheduleSseReconnect(),
      onDone: _scheduleSseReconnect,
      cancelOnError: true,
    );
  }

  void _scheduleSseReconnect() {
    if (!mounted) return;
    _sseReconnectTimer?.cancel();
    _sseReconnectTimer = Timer(const Duration(seconds: 3), _startSse);
  }

  String _absoluteUrl(String path) {
    final root = Uri.parse(_normalizeBaseUrl(widget.api.settings.baseUrl));
    return root.resolve(path).toString();
  }

  Future<void> _uploadFiles() async {
    final picked = await FilePicker.platform.pickFiles(allowMultiple: true, withData: true);
    if (picked == null || picked.files.isEmpty) return;
    setState(() => _uploading = true);
    try {
      final count = await widget.api.uploadSharedCloudFiles(picked.files);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Uploaded $count file(s).')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Upload failed: $err')));
    } finally {
      if (mounted) setState(() => _uploading = false);
    }
  }

  Future<void> _deleteFile(SharedCloudFile file) async {
    setState(() => _deletingIds.add(file.id));
    try {
      await widget.api.deleteSharedCloudFile(file.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('File deleted.')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    } finally {
      if (mounted) setState(() => _deletingIds.remove(file.id));
    }
  }

  Future<void> _createAlbum() async {
    final name = _albumNameCtrl.text.trim();
    if (name.isEmpty) return;
    setState(() => _creatingAlbum = true);
    try {
      await widget.api.createPhotoAlbum(name: name, visibility: _photoVisibility);
      if (!mounted) return;
      _albumNameCtrl.clear();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Album created.')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create album failed: $err')));
    } finally {
      if (mounted) setState(() => _creatingAlbum = false);
    }
  }

  Future<void> _uploadPhotos() async {
    final picked = await FilePicker.platform.pickFiles(
      allowMultiple: true,
      withData: true,
      type: FileType.custom,
      allowedExtensions: const ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif', 'mp4', 'mov', 'm4v', 'avi', 'mkv', 'webm'],
    );
    if (picked == null || picked.files.isEmpty) return;
    setState(() => _uploadingPhotos = true);
    try {
      final count = await widget.api.uploadPhotoAssets(
        picked.files,
        visibility: _photoVisibility,
        albumId: _selectedPhotoUploadAlbum,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Uploaded $count photo/video file(s).')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Photo upload failed: $err')));
    } finally {
      if (mounted) setState(() => _uploadingPhotos = false);
    }
  }

  Future<void> _deletePhoto(PhotoAssetItem item) async {
    setState(() => _deletingPhotoIds.add(item.id));
    try {
      await widget.api.deletePhotoAsset(item.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Photo deleted.')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete photo failed: $err')));
    } finally {
      if (mounted) setState(() => _deletingPhotoIds.remove(item.id));
    }
  }

  Future<void> _pickWatchFolderPath() async {
    final path = await FilePicker.platform.getDirectoryPath(dialogTitle: 'Select watched photo folder');
    if (path == null || path.trim().isEmpty) return;
    if (!mounted) return;
    setState(() => _watchFolderCtrl.text = path.trim());
  }

  Future<void> _addWatchFolder() async {
    final folderUri = _watchFolderCtrl.text.trim();
    if (folderUri.isEmpty) return;
    setState(() => _savingWatchFolder = true);
    try {
      await widget.api.createPhotoWatchFolder(
        folderUri: folderUri,
        scope: _watchScope,
        targetAlbumId: _selectedWatchAlbum,
      );
      if (!mounted) return;
      _watchFolderCtrl.clear();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Watched folder saved.')));
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Save watch folder failed: $err')));
    } finally {
      if (mounted) setState(() => _savingWatchFolder = false);
    }
  }

  Future<void> _toggleWatchFolder(PhotoWatchFolderItem item) async {
    setState(() => _updatingWatchIds.add(item.id));
    try {
      await widget.api.updatePhotoWatchFolder(watchId: item.id, isActive: !item.isActive);
      if (!mounted) return;
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update watch folder failed: $err')));
    } finally {
      if (mounted) setState(() => _updatingWatchIds.remove(item.id));
    }
  }

  Future<void> _deleteWatchFolder(PhotoWatchFolderItem item) async {
    setState(() => _deletingWatchIds.add(item.id));
    try {
      await widget.api.deletePhotoWatchFolder(item.id);
      if (!mounted) return;
      await _load(showSpinner: false);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete watch folder failed: $err')));
    } finally {
      if (mounted) setState(() => _deletingWatchIds.remove(item.id));
    }
  }

  List<PhotoAssetItem> _filteredPhotoAssets() {
    if (_selectedPhotoAlbumFilter == null) return _photoAssets;
    return _photoAssets.where((a) => a.albumId == _selectedPhotoAlbumFilter).toList();
  }

  Widget _buildFilesTab(bool isAdmin, String member) {
    return RefreshIndicator(
      onRefresh: () => _load(showSpinner: false),
      child: ListView.builder(
        padding: const EdgeInsets.all(12),
        itemCount: _files.length + 1,
        itemBuilder: (context, index) {
          if (index == 0) {
            return Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Align(
                alignment: Alignment.centerRight,
                child: FilledButton.icon(
                  onPressed: _uploading ? null : _uploadFiles,
                  icon: const Icon(Icons.upload_file),
                  label: Text(_uploading ? 'Uploading...' : 'Upload Files'),
                ),
              ),
            );
          }

          final file = _files[index - 1];
          final canDelete = isAdmin || file.creator.toLowerCase() == member;
          final deleting = _deletingIds.contains(file.id);
          return Card(
            child: ListTile(
              leading: const Icon(Icons.insert_drive_file_outlined),
              title: Text(file.filename),
              subtitle: Text('${file.creator} • ${file.category}${file.uploadTime != null ? ' • ${_fmtDateTime(file.uploadTime!)}' : ''}'),
              trailing: Wrap(
                spacing: 4,
                children: [
                  IconButton(
                    tooltip: 'Show link',
                    onPressed: () {
                      showModalBottomSheet<void>(
                        context: context,
                        useSafeArea: true,
                        isScrollControlled: true,
                        builder: (context) => Padding(
                          padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
                          child: Column(
                            mainAxisSize: MainAxisSize.min,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              const Text('Download URL', style: TextStyle(fontWeight: FontWeight.w700)),
                              const SizedBox(height: 8),
                              SelectableText(_absoluteUrl(file.downloadUrl)),
                            ],
                          ),
                        ),
                      );
                    },
                    icon: const Icon(Icons.link),
                  ),
                  if (canDelete)
                    IconButton(
                      tooltip: 'Delete file',
                      onPressed: deleting ? null : () => _deleteFile(file),
                      icon: deleting ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.delete_outline),
                    ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  Widget _buildPhotosTab(bool isAdmin, String member) {
    final filtered = _filteredPhotoAssets();
    return RefreshIndicator(
      onRefresh: () => _load(showSpinner: false),
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Family Photos', style: TextStyle(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 10),
                  TextField(controller: _albumNameCtrl, decoration: const InputDecoration(labelText: 'New album name')),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: DropdownButtonFormField<String>(
                          initialValue: _photoVisibility,
                          decoration: const InputDecoration(labelText: 'Visibility'),
                          items: const [
                            DropdownMenuItem(value: 'private', child: Text('Private')),
                            DropdownMenuItem(value: 'family', child: Text('Family')),
                          ],
                          onChanged: (value) => setState(() => _photoVisibility = value ?? 'private'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      FilledButton.icon(
                        onPressed: _creatingAlbum ? null : _createAlbum,
                        icon: const Icon(Icons.create_new_folder_outlined),
                        label: Text(_creatingAlbum ? 'Creating...' : 'Create Album'),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: DropdownButtonFormField<int?>(
                          initialValue: _selectedPhotoUploadAlbum,
                          decoration: const InputDecoration(labelText: 'Upload into album (optional)'),
                          items: [
                            const DropdownMenuItem<int?>(value: null, child: Text('No album')),
                            ..._albums.map((a) => DropdownMenuItem<int?>(value: a.id, child: Text(a.name))),
                          ],
                          onChanged: (value) => setState(() => _selectedPhotoUploadAlbum = value),
                        ),
                      ),
                      const SizedBox(width: 8),
                      FilledButton.icon(
                        onPressed: _uploadingPhotos ? null : _uploadPhotos,
                        icon: const Icon(Icons.add_a_photo_outlined),
                        label: Text(_uploadingPhotos ? 'Uploading...' : 'Upload'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              ChoiceChip(
                label: const Text('All Albums'),
                selected: _selectedPhotoAlbumFilter == null,
                onSelected: (_) => setState(() => _selectedPhotoAlbumFilter = null),
              ),
              ..._albums.map(
                (a) => ChoiceChip(
                  label: Text('${a.name} (${a.assetCount})'),
                  selected: _selectedPhotoAlbumFilter == a.id,
                  onSelected: (_) => setState(() => _selectedPhotoAlbumFilter = a.id),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          if (filtered.isEmpty)
            const Card(child: ListTile(title: Text('No photos/videos available for your access rights.')))
          else
            ...filtered.take(100).map((asset) {
              final canDelete = isAdmin || asset.uploader.toLowerCase() == member;
              final deleting = _deletingPhotoIds.contains(asset.id);
              return Card(
                child: Padding(
                  padding: const EdgeInsets.all(10),
                  child: Row(
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(10),
                        child: SizedBox(
                          width: 72,
                          height: 72,
                          child: Stack(
                            fit: StackFit.expand,
                            children: [
                              Image.network(
                                _absoluteUrl(asset.previewUrl),
                                fit: BoxFit.cover,
                                errorBuilder: (_, __, ___) => const ColoredBox(color: Color(0xFFE2E8F0), child: Icon(Icons.broken_image_outlined)),
                              ),
                              if (asset.isVideo)
                                const Align(
                                  alignment: Alignment.center,
                                  child: Icon(Icons.play_circle_outline, color: Colors.white, size: 28),
                                ),
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(asset.originalFilename, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w700)),
                            const SizedBox(height: 2),
                            Text('${asset.uploader} • ${asset.visibility}${asset.uploadedAt != null ? ' • ${_fmtDateTime(asset.uploadedAt!)}' : ''}', style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                          ],
                        ),
                      ),
                      IconButton(
                        tooltip: 'Download URL',
                        onPressed: () {
                          showModalBottomSheet<void>(
                            context: context,
                            useSafeArea: true,
                            isScrollControlled: true,
                            builder: (context) => Padding(
                              padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  const Text('Photo URL', style: TextStyle(fontWeight: FontWeight.w700)),
                                  const SizedBox(height: 8),
                                  SelectableText(_absoluteUrl(asset.downloadUrl)),
                                ],
                              ),
                            ),
                          );
                        },
                        icon: const Icon(Icons.link),
                      ),
                      if (canDelete)
                        IconButton(
                          tooltip: 'Delete photo',
                          onPressed: deleting ? null : () => _deletePhoto(asset),
                          icon: deleting ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.delete_outline),
                        ),
                    ],
                  ),
                ),
              );
            }),
        ],
      ),
    );
  }

  Widget _buildWatchFoldersTab() {
    return RefreshIndicator(
      onRefresh: () => _load(showSpinner: false),
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Watched Folders (Device Opt-In)', style: TextStyle(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 6),
                  const Text(
                    'Add a local folder path/URI for this device. Devices opted into the same family scope/album sync photos across members who have album rights.',
                    style: TextStyle(fontSize: 12, color: Color(0xFF64748B)),
                  ),
                  const SizedBox(height: 10),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _watchFolderCtrl,
                          decoration: const InputDecoration(labelText: 'Folder path or URI'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      IconButton(
                        tooltip: 'Pick folder',
                        onPressed: _pickWatchFolderPath,
                        icon: const Icon(Icons.folder_open),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      Expanded(
                        child: DropdownButtonFormField<String>(
                          initialValue: _watchScope,
                          decoration: const InputDecoration(labelText: 'Scope'),
                          items: const [
                            DropdownMenuItem(value: 'private', child: Text('Private')),
                            DropdownMenuItem(value: 'family', child: Text('Family')),
                          ],
                          onChanged: (value) => setState(() => _watchScope = value ?? 'private'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: DropdownButtonFormField<int?>(
                          initialValue: _selectedWatchAlbum,
                          decoration: const InputDecoration(labelText: 'Target Album'),
                          items: [
                            const DropdownMenuItem<int?>(value: null, child: Text('No album')),
                            ..._albums.map((a) => DropdownMenuItem<int?>(value: a.id, child: Text(a.name))),
                          ],
                          onChanged: (value) => setState(() => _selectedWatchAlbum = value),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Align(
                    alignment: Alignment.centerRight,
                    child: FilledButton.icon(
                      onPressed: _savingWatchFolder ? null : _addWatchFolder,
                      icon: const Icon(Icons.add_link),
                      label: Text(_savingWatchFolder ? 'Saving...' : 'Save Watched Folder'),
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          if (_watchFolders.isEmpty)
            const Card(child: ListTile(title: Text('No watched folders configured on this member/device.')))
          else
            ..._watchFolders.map((item) {
              final updating = _updatingWatchIds.contains(item.id);
              final deleting = _deletingWatchIds.contains(item.id);
              return Card(
                child: ListTile(
                  title: Text(item.folderUri, maxLines: 1, overflow: TextOverflow.ellipsis),
                  subtitle: Text('${item.scope} • ${item.platform}${item.lastSyncedAt != null ? ' • last sync ${_fmtDateTime(item.lastSyncedAt!)}' : ' • never synced'}'),
                  trailing: Wrap(
                    spacing: 4,
                    children: [
                      IconButton(
                        tooltip: item.isActive ? 'Pause sync' : 'Resume sync',
                        onPressed: updating ? null : () => _toggleWatchFolder(item),
                        icon: updating
                            ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                            : Icon(item.isActive ? Icons.pause_circle_outline : Icons.play_circle_outline),
                      ),
                      IconButton(
                        tooltip: 'Delete watched folder',
                        onPressed: deleting ? null : () => _deleteWatchFolder(item),
                        icon: deleting
                            ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                            : const Icon(Icons.delete_outline),
                      ),
                    ],
                  ),
                ),
              );
            }),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ErrorPanel(error: _error!, onRetry: _load);
    }

    final member = widget.api.member.toLowerCase();
    final isAdmin = member == 'admin' || member == 'administrator';

    return DefaultTabController(
      length: 3,
      child: Column(
        children: [
          const TabBar(
            tabs: [
              Tab(text: 'Files', icon: Icon(Icons.cloud_outlined)),
              Tab(text: 'Family Photos', icon: Icon(Icons.photo_library_outlined)),
              Tab(text: 'Watched Folders', icon: Icon(Icons.folder_special_outlined)),
            ],
          ),
          Expanded(
            child: TabBarView(
              children: [
                _buildFilesTab(isAdmin, member),
                _buildPhotosTab(isAdmin, member),
                _buildWatchFoldersTab(),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class ShoppingListPage extends StatefulWidget {
  const ShoppingListPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<ShoppingListPage> createState() => _ShoppingListPageState();
}

class _ShoppingListPageState extends State<ShoppingListPage> {
  bool _loading = true;
  String? _error;
  List<Map<String, dynamic>> _lists = const [];
  List<Map<String, dynamic>> _items = const [];
  List<Map<String, dynamic>> _repoItems = const [];
  int? _selectedListId;
  bool _adding = false;
  bool _savingList = false;
  bool _repoLoading = false;
  bool _repoSaving = false;
  final Set<int> _updatingItemIds = <int>{};
  final Set<int> _deletingItemIds = <int>{};
  final TextEditingController _itemCtrl = TextEditingController();
  final TextEditingController _priceCtrl = TextEditingController();
  final TextEditingController _qtyCtrl = TextEditingController(text: '1');
  final TextEditingController _storeCtrl = TextEditingController();
  final TextEditingController _tagsCtrl = TextEditingController();
  final TextEditingController _purchaseCategoryCtrl = TextEditingController();
  final TextEditingController _targetPriceCtrl = TextEditingController();
  final TextEditingController _budgetCeilingCtrl = TextEditingController();
  final TextEditingController _sizeRequirementsCtrl = TextEditingController();
  final TextEditingController _planningNotesCtrl = TextEditingController();
  final TextEditingController _repoSearchCtrl = TextEditingController();
  bool _showUncheckedOnly = false;
  String? _selectedTag;
  String? _selectedStore;
  String? _pendingPurchasePhotoFilename;
  String? _pendingPurchasePhotoUrl;
  String? _pendingPurchasePhotoLocalPath;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _itemCtrl.dispose();
    _priceCtrl.dispose();
    _qtyCtrl.dispose();
    _storeCtrl.dispose();
    _tagsCtrl.dispose();
    _purchaseCategoryCtrl.dispose();
    _targetPriceCtrl.dispose();
    _budgetCeilingCtrl.dispose();
    _sizeRequirementsCtrl.dispose();
    _planningNotesCtrl.dispose();
    _repoSearchCtrl.dispose();
    super.dispose();
  }

  String _shoppingAbsoluteUrl(String value) {
    final raw = value.trim();
    if (raw.isEmpty) return raw;
    if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
    final configured = widget.api.settings.baseUrl.trim();
    final base = configured.isEmpty ? _cloudBaseUrl : configured;
    final root = Uri.parse(_normalizeBaseUrl(base));
    final rootPath = root.path.endsWith('/') ? root.path.substring(0, root.path.length - 1) : root.path;
    final assetPath = raw.startsWith('/') ? raw : '/$raw';
    return root.replace(path: '$rootPath$assetPath').toString();
  }

  Map<String, dynamic>? get _selectedListRow {
    final selected = _selectedListId;
    if (selected == null) return null;
    for (final row in _lists) {
      if (_toInt(row['id']) == selected) return row;
    }
    return null;
  }

  List<String> _parseTags(String raw) {
    return raw
        .split(',')
        .map((e) => e.trim())
        .where((e) => e.isNotEmpty)
        .toList();
  }

  double? _parseNullableDouble(String raw) {
    final trimmed = raw.trim();
    if (trimmed.isEmpty) return null;
    return double.tryParse(trimmed);
  }

  Future<void> _pickAndUploadPurchasePhoto() async {
    try {
      final result = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowMultiple: false,
        allowedExtensions: const ['jpg', 'jpeg', 'png', 'webp', 'gif'],
      );
      final file = result?.files.first;
      if (file == null || file.path == null || file.path!.trim().isEmpty) return;
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Uploading photo...')));
      final uploaded = await widget.api.uploadShoppingPurchasePhoto(file.path!.trim());
      if (!mounted) return;
      setState(() {
        _pendingPurchasePhotoFilename = '${uploaded['filename'] ?? ''}'.trim().isEmpty ? null : '${uploaded['filename']}';
        _pendingPurchasePhotoUrl = '${uploaded['url'] ?? ''}'.trim().isEmpty ? null : '${uploaded['url']}';
        _pendingPurchasePhotoLocalPath = file.path;
      });
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Photo uploaded')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Photo upload failed: $err')));
    }
  }

  void _clearBigPurchaseInputs() {
    _purchaseCategoryCtrl.clear();
    _targetPriceCtrl.clear();
    _budgetCeilingCtrl.clear();
    _sizeRequirementsCtrl.clear();
    _planningNotesCtrl.clear();
    _pendingPurchasePhotoFilename = null;
    _pendingPurchasePhotoUrl = null;
    _pendingPurchasePhotoLocalPath = null;
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final lists = await widget.api.fetchShoppingLists();
      final nextListId = _selectedListId ?? (lists.isNotEmpty ? _toInt(lists.first['id']) : null);
      final items = nextListId == null ? <Map<String, dynamic>>[] : await widget.api.fetchShoppingItems(nextListId);
      final repoItems = await widget.api.fetchShoppingRepo();
      if (!mounted) return;
      setState(() {
        _lists = lists;
        _selectedListId = nextListId;
        _items = items;
        _repoItems = repoItems;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<void> _loadRepo({String? query}) async {
    setState(() => _repoLoading = true);
    try {
      final rows = await widget.api.fetchShoppingRepo(query: query);
      if (!mounted) return;
      setState(() {
        _repoItems = rows;
        _repoLoading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() => _repoLoading = false);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Repo load failed: $err')));
    }
  }

  Future<void> _changeList(int listId) async {
    setState(() {
      _selectedListId = listId;
      _loading = true;
      _error = null;
    });
    try {
      final items = await widget.api.fetchShoppingItems(listId);
      if (!mounted) return;
      setState(() {
        _items = items;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<void> _addItem() async {
    final itemName = _itemCtrl.text.trim();
    final listId = _selectedListId;
    if (itemName.isEmpty || listId == null) return;

    final price = _parseNullableDouble(_priceCtrl.text);
    final quantity = _parseNullableDouble(_qtyCtrl.text);
    final store = _storeCtrl.text.trim();
    final tags = _parseTags(_tagsCtrl.text);
    final selectedList = _selectedListRow;
    final listType = '${selectedList?['list_type'] ?? 'standard'}';

    setState(() => _adding = true);
    try {
      await widget.api.createShoppingItem(
        listId: listId,
        item: itemName,
        price: price,
        quantity: quantity,
        store: store.isEmpty ? null : store,
        tags: tags,
        purchaseCategory: _purchaseCategoryCtrl.text.trim().isEmpty ? null : _purchaseCategoryCtrl.text.trim(),
        planningNotes: _planningNotesCtrl.text.trim().isEmpty ? null : _planningNotesCtrl.text.trim(),
        sizeRequirements: _sizeRequirementsCtrl.text.trim().isEmpty ? null : _sizeRequirementsCtrl.text.trim(),
        targetPrice: _parseNullableDouble(_targetPriceCtrl.text),
        budgetCeiling: _parseNullableDouble(_budgetCeilingCtrl.text),
        purchasePhotoFilename: _pendingPurchasePhotoFilename,
      );
      _itemCtrl.clear();
      _priceCtrl.clear();
      _storeCtrl.clear();
      _tagsCtrl.clear();
      _qtyCtrl.text = '1';
      if (listType == 'big_purchase') {
        _clearBigPurchaseInputs();
      }
      await _changeList(listId);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add failed: $err')));
    } finally {
      if (mounted) setState(() => _adding = false);
    }
  }

  Future<void> _openListEditor({Map<String, dynamic>? source}) async {
    final nameCtrl = TextEditingController(text: '${source?['name'] ?? ''}');
    String listType = '${source?['list_type'] ?? 'standard'}';
    String selectedColorHex = '${source?['color'] ?? '#3B82F6'}';
    String? formError;

    String toHex(Color color) {
      final rgb = color.toARGB32() & 0x00FFFFFF;
      return '#${rgb.toRadixString(16).padLeft(6, '0').toUpperCase()}';
    }

    final colorPresets = <String>[
      '#3B82F6',
      '#0EA5E9',
      '#14B8A6',
      '#22C55E',
      '#EAB308',
      '#F97316',
      '#EF4444',
      '#EC4899',
      '#A855F7',
      '#6366F1',
      '#64748B',
      '#111827',
    ];

    final shouldSave = await showDialog<bool>(
      context: context,
      builder: (ctx) {
        return StatefulBuilder(
          builder: (ctx, setDialogState) {
            return AlertDialog(
              insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
              title: Text(source == null ? 'New List' : 'Edit List'),
              content: SizedBox(
                width: 420,
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      TextField(
                        controller: nameCtrl,
                        decoration: const InputDecoration(labelText: 'List name'),
                      ),
                      const SizedBox(height: 10),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Text(
                          'Color',
                          style: Theme.of(ctx).textTheme.bodySmall?.copyWith(color: const Color(0xFF475569), fontWeight: FontWeight.w600),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 10,
                        runSpacing: 10,
                        children: [
                          ...colorPresets.map((hex) {
                            final isSelected = selectedColorHex.toLowerCase() == hex.toLowerCase();
                            return InkWell(
                              onTap: () => setDialogState(() => selectedColorHex = hex),
                              borderRadius: BorderRadius.circular(999),
                              child: Container(
                                width: 30,
                                height: 30,
                                decoration: BoxDecoration(
                                  color: _colorFromHex(hex),
                                  shape: BoxShape.circle,
                                  border: Border.all(
                                    color: isSelected ? const Color(0xFF0F172A) : const Color(0xFFE2E8F0),
                                    width: isSelected ? 3 : 1,
                                  ),
                                ),
                              ),
                            );
                          }),
                          OutlinedButton.icon(
                            onPressed: () async {
                              Color pickerColor = _colorFromHex(selectedColorHex);
                              final picked = await showDialog<Color>(
                                context: ctx,
                                builder: (pickerCtx) {
                                  return StatefulBuilder(
                                    builder: (pickerCtx, setPickerState) {
                                      return AlertDialog(
                                        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
                                        title: const Text('Custom Color'),
                                        content: SizedBox(
                                          width: 380,
                                          child: SingleChildScrollView(
                                            child: Column(
                                              mainAxisSize: MainAxisSize.min,
                                              children: [
                                                Container(
                                                  height: 48,
                                                  width: double.infinity,
                                                  decoration: BoxDecoration(
                                                    color: pickerColor,
                                                    borderRadius: BorderRadius.circular(10),
                                                    border: Border.all(color: const Color(0xFFE2E8F0)),
                                                  ),
                                                ),
                                                const SizedBox(height: 8),
                                                Text(toHex(pickerColor), style: const TextStyle(fontWeight: FontWeight.w700)),
                                                const SizedBox(height: 12),
                                                Slider(
                                                  value: ((pickerColor.toARGB32() >> 16) & 0xFF).toDouble(),
                                                  min: 0,
                                                  max: 255,
                                                  activeColor: Colors.red,
                                                  onChanged: (value) {
                                                    final argb = pickerColor.toARGB32();
                                                    final g = (argb >> 8) & 0xFF;
                                                    final b = argb & 0xFF;
                                                    setPickerState(() {
                                                      pickerColor = Color.fromARGB(255, value.round(), g, b);
                                                    });
                                                  },
                                                ),
                                                Slider(
                                                  value: ((pickerColor.toARGB32() >> 8) & 0xFF).toDouble(),
                                                  min: 0,
                                                  max: 255,
                                                  activeColor: Colors.green,
                                                  onChanged: (value) {
                                                    final argb = pickerColor.toARGB32();
                                                    final r = (argb >> 16) & 0xFF;
                                                    final b = argb & 0xFF;
                                                    setPickerState(() {
                                                      pickerColor = Color.fromARGB(255, r, value.round(), b);
                                                    });
                                                  },
                                                ),
                                                Slider(
                                                  value: (pickerColor.toARGB32() & 0xFF).toDouble(),
                                                  min: 0,
                                                  max: 255,
                                                  activeColor: Colors.blue,
                                                  onChanged: (value) {
                                                    final argb = pickerColor.toARGB32();
                                                    final r = (argb >> 16) & 0xFF;
                                                    final g = (argb >> 8) & 0xFF;
                                                    setPickerState(() {
                                                      pickerColor = Color.fromARGB(255, r, g, value.round());
                                                    });
                                                  },
                                                ),
                                              ],
                                            ),
                                          ),
                                        ),
                                        actions: [
                                          TextButton(onPressed: () => Navigator.of(pickerCtx).pop(), child: const Text('Cancel')),
                                          FilledButton(onPressed: () => Navigator.of(pickerCtx).pop(pickerColor), child: const Text('Use Color')),
                                        ],
                                      );
                                    },
                                  );
                                },
                              );
                              if (picked == null) return;
                              setDialogState(() => selectedColorHex = toHex(picked));
                            },
                            icon: const Icon(Icons.palette_outlined, size: 18),
                            label: const Text('Custom'),
                          ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      Row(
                        children: [
                          Container(
                            width: 18,
                            height: 18,
                            decoration: BoxDecoration(
                              color: _colorFromHex(selectedColorHex),
                              shape: BoxShape.circle,
                              border: Border.all(color: const Color(0xFFE2E8F0)),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text(selectedColorHex, style: const TextStyle(fontWeight: FontWeight.w600)),
                        ],
                      ),
                      const SizedBox(height: 10),
                      DropdownButtonFormField<String>(
                        initialValue: listType,
                        decoration: const InputDecoration(labelText: 'List type'),
                        items: const [
                          DropdownMenuItem(value: 'standard', child: Text('Standard Shopping')),
                          DropdownMenuItem(value: 'big_purchase', child: Text('Big Purchase Planner')),
                        ],
                        onChanged: (value) {
                          if (value == null) return;
                          setDialogState(() => listType = value);
                        },
                      ),
                      if (formError != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 10),
                          child: Align(
                            alignment: Alignment.centerLeft,
                            child: Text(formError!, style: const TextStyle(color: Color(0xFFB91C1C), fontSize: 12)),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              actions: [
                TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
                FilledButton(
                  onPressed: _savingList
                      ? null
                      : () {
                          if (nameCtrl.text.trim().isEmpty) {
                            setDialogState(() => formError = 'List name is required');
                            return;
                          }
                          Navigator.of(ctx).pop(true);
                        },
                  child: Text(source == null ? 'Create' : 'Save'),
                ),
              ],
            );
          },
        );
      },
    );

    if (shouldSave != true) {
      nameCtrl.dispose();
      return;
    }

    setState(() => _savingList = true);
    try {
      if (source == null) {
        final created = await widget.api.createShoppingList(
          name: nameCtrl.text.trim(),
          color: selectedColorHex.trim().isEmpty ? '#3B82F6' : selectedColorHex.trim(),
          listType: listType,
        );
        final createdList = created['list'] as Map<String, dynamic>?;
        await _load();
        final createdId = createdList == null ? null : _toInt(createdList['id']);
        if (createdId != null && mounted) {
          await _changeList(createdId);
        }
      } else {
        await widget.api.updateShoppingList(
          listId: _toInt(source['id']),
          name: nameCtrl.text.trim(),
          color: selectedColorHex.trim().isEmpty ? '#3B82F6' : selectedColorHex.trim(),
          listType: listType,
        );
        await _load();
      }
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('List save failed: $err')));
    } finally {
      if (mounted) setState(() => _savingList = false);
      nameCtrl.dispose();
    }
  }

  Future<void> _deleteSelectedList() async {
    final list = _selectedListRow;
    if (list == null) return;

    String strategy = 'move';
    int? targetListId;
    final currentListId = _toInt(list['id']);
    final fallbackTarget = _lists
        .map((e) => _toInt(e['id']))
        .where((id) => id != currentListId)
        .cast<int?>()
        .firstWhere((id) => id != null, orElse: () => null);
    targetListId = fallbackTarget;

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) {
        return StatefulBuilder(
          builder: (ctx, setDialogState) {
            return AlertDialog(
              insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
              title: const Text('Delete Shopping List'),
              content: SizedBox(
                width: 460,
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('Delete "${list['name'] ?? 'List'}"?'),
                      const SizedBox(height: 10),
                      DropdownButtonFormField<String>(
                        initialValue: strategy,
                        decoration: const InputDecoration(labelText: 'Delete strategy'),
                        items: const [
                          DropdownMenuItem(value: 'move', child: Text('Move items to another list')),
                          DropdownMenuItem(value: 'delete_all', child: Text('Delete all items in this list')),
                        ],
                        onChanged: (value) {
                          if (value == null) return;
                          setDialogState(() => strategy = value);
                        },
                      ),
                      const SizedBox(height: 10),
                      DropdownButtonFormField<int>(
                        initialValue: targetListId,
                        decoration: const InputDecoration(labelText: 'Destination list'),
                        items: _lists
                            .where((row) => _toInt(row['id']) != currentListId)
                            .map(
                              (row) => DropdownMenuItem<int>(
                                value: _toInt(row['id']),
                                child: Text('${row['name'] ?? ''}'),
                              ),
                            )
                            .toList(),
                        onChanged: strategy == 'move'
                            ? (value) {
                                setDialogState(() => targetListId = value);
                              }
                            : null,
                      ),
                    ],
                  ),
                ),
              ),
              actions: [
                TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
                FilledButton(
                  onPressed: () => Navigator.of(ctx).pop(true),
                  style: FilledButton.styleFrom(backgroundColor: const Color(0xFFDC2626)),
                  child: const Text('Delete List'),
                ),
              ],
            );
          },
        );
      },
    );

    if (confirmed != true) return;

    try {
      await widget.api.deleteShoppingList(
        listId: currentListId,
        strategy: strategy,
        targetListId: strategy == 'move' ? targetListId : null,
      );
      await _load();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('List deleted')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete list failed: $err')));
    }
  }

  Future<void> _editItem(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    final nameCtrl = TextEditingController(text: '${row['item'] ?? ''}');
    final priceCtrl = TextEditingController(text: row['price'] == null ? '' : '${row['price']}');
    final qtyCtrl = TextEditingController(text: row['quantity'] == null ? '1' : '${row['quantity']}');

    final tagList = (row['tags'] is List ? (row['tags'] as List<dynamic>) : const <dynamic>[])
        .map((e) => '$e'.trim())
        .where((e) => e.isNotEmpty)
        .toList();
    final tagsCtrl = TextEditingController(text: tagList.join(', '));
    final purchaseCategoryCtrl = TextEditingController(text: '${row['purchase_category'] ?? ''}');
    final planningNotesCtrl = TextEditingController(text: '${row['planning_notes'] ?? ''}');
    final targetPriceCtrl = TextEditingController(text: row['target_price'] == null ? '' : '${row['target_price']}');
    final budgetCeilingCtrl = TextEditingController(text: row['budget_ceiling'] == null ? '' : '${row['budget_ceiling']}');
    final sizeReqCtrl = TextEditingController(text: '${row['size_requirements'] ?? ''}');
    int movedListId = _selectedListId ?? _toInt(row['list_id']);
    String purchasePhotoFilename = '${row['purchase_photo_filename'] ?? ''}'.trim();
    String purchasePhotoUrl = '${row['purchase_photo_url'] ?? ''}'.trim();
    bool uploadingPhoto = false;

    final shouldSave = await showDialog<bool>(
      context: context,
      builder: (ctx) {
        return StatefulBuilder(
          builder: (ctx, setDialogState) {
            return AlertDialog(
              insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
              title: const Text('Edit Item'),
              content: SizedBox(
                width: 460,
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Item')),
                      const SizedBox(height: 10),
                      Row(
                        children: [
                          Expanded(child: TextField(controller: priceCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Price'))),
                          const SizedBox(width: 10),
                          Expanded(child: TextField(controller: qtyCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Qty'))),
                        ],
                      ),
                      const SizedBox(height: 10),
                      TextField(controller: tagsCtrl, decoration: const InputDecoration(labelText: 'Tags (comma separated)')),
                      const SizedBox(height: 10),
                      TextField(controller: purchaseCategoryCtrl, decoration: const InputDecoration(labelText: 'Purchase category')),
                      const SizedBox(height: 10),
                      Row(
                        children: [
                          Expanded(child: TextField(controller: targetPriceCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Target price'))),
                          const SizedBox(width: 10),
                          Expanded(child: TextField(controller: budgetCeilingCtrl, keyboardType: const TextInputType.numberWithOptions(decimal: true), decoration: const InputDecoration(labelText: 'Budget ceiling'))),
                        ],
                      ),
                      const SizedBox(height: 10),
                      TextField(controller: sizeReqCtrl, decoration: const InputDecoration(labelText: 'Size requirements')),
                      const SizedBox(height: 10),
                      TextField(controller: planningNotesCtrl, maxLines: 2, decoration: const InputDecoration(labelText: 'Planning notes')),
                      const SizedBox(height: 10),
                      Row(
                        children: [
                          Expanded(
                            child: OutlinedButton.icon(
                              onPressed: uploadingPhoto
                                  ? null
                                  : () async {
                                      final result = await FilePicker.platform.pickFiles(
                                        type: FileType.custom,
                                        allowMultiple: false,
                                        allowedExtensions: const ['jpg', 'jpeg', 'png', 'webp', 'gif'],
                                      );
                                      final file = result?.files.first;
                                      if (file == null || file.path == null || file.path!.trim().isEmpty) return;
                                      setDialogState(() => uploadingPhoto = true);
                                      try {
                                        final uploaded = await widget.api.uploadShoppingPurchasePhoto(file.path!.trim());
                                        setDialogState(() {
                                          purchasePhotoFilename = '${uploaded['filename'] ?? ''}'.trim();
                                          purchasePhotoUrl = '${uploaded['url'] ?? ''}'.trim();
                                        });
                                      } finally {
                                        setDialogState(() => uploadingPhoto = false);
                                      }
                                    },
                              icon: const Icon(Icons.upload_file),
                              label: Text(uploadingPhoto ? 'Uploading...' : 'Upload photo'),
                            ),
                          ),
                        ],
                      ),
                      if (purchasePhotoUrl.isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(top: 10),
                          child: ClipRRect(
                            borderRadius: BorderRadius.circular(10),
                            child: Image.network(
                              _shoppingAbsoluteUrl(purchasePhotoUrl),
                              height: 120,
                              width: double.infinity,
                              fit: BoxFit.cover,
                              errorBuilder: (_, __, ___) => Container(
                                height: 90,
                                color: const Color(0xFFF1F5F9),
                                alignment: Alignment.center,
                                child: const Text('Photo unavailable'),
                              ),
                            ),
                          ),
                        ),
                      const SizedBox(height: 10),
                      DropdownButtonFormField<int>(
                        initialValue: movedListId,
                        decoration: const InputDecoration(labelText: 'List'),
                        items: _lists
                            .map(
                              (listRow) => DropdownMenuItem<int>(
                                value: _toInt(listRow['id']),
                                child: Text('${listRow['name'] ?? ''}'),
                              ),
                            )
                            .toList(),
                        onChanged: (value) {
                          if (value == null) return;
                          setDialogState(() => movedListId = value);
                        },
                      ),
                    ],
                  ),
                ),
              ),
              actions: [
                TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
                FilledButton(onPressed: () => Navigator.of(ctx).pop(true), child: const Text('Save')),
              ],
            );
          },
        );
      },
    );

    if (shouldSave != true) {
      nameCtrl.dispose();
      priceCtrl.dispose();
      qtyCtrl.dispose();
      tagsCtrl.dispose();
      purchaseCategoryCtrl.dispose();
      planningNotesCtrl.dispose();
      targetPriceCtrl.dispose();
      budgetCeilingCtrl.dispose();
      sizeReqCtrl.dispose();
      return;
    }

    setState(() => _updatingItemIds.add(id));
    try {
      await widget.api.updateShoppingItem(
        itemId: id,
        itemName: nameCtrl.text.trim(),
        price: _parseNullableDouble(priceCtrl.text),
        quantity: _parseNullableDouble(qtyCtrl.text),
        listId: movedListId,
        tags: _parseTags(tagsCtrl.text),
        purchaseCategory: purchaseCategoryCtrl.text.trim(),
        planningNotes: planningNotesCtrl.text.trim(),
        targetPrice: _parseNullableDouble(targetPriceCtrl.text),
        budgetCeiling: _parseNullableDouble(budgetCeilingCtrl.text),
        sizeRequirements: sizeReqCtrl.text.trim(),
        purchasePhotoFilename: purchasePhotoFilename.trim().isEmpty ? null : purchasePhotoFilename.trim(),
      );
      final listId = _selectedListId;
      if (listId != null) {
        await _changeList(listId);
      }
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update failed: $err')));
    } finally {
      if (mounted) setState(() => _updatingItemIds.remove(id));
      nameCtrl.dispose();
      priceCtrl.dispose();
      qtyCtrl.dispose();
      tagsCtrl.dispose();
      purchaseCategoryCtrl.dispose();
      planningNotesCtrl.dispose();
      targetPriceCtrl.dispose();
      budgetCeilingCtrl.dispose();
      sizeReqCtrl.dispose();
    }
  }

  Future<void> _openRepoEditor({Map<String, dynamic>? source}) async {
    final nameCtrl = TextEditingController(text: '${source?['name'] ?? ''}');
    final storeCtrl = TextEditingController(text: '${source?['store'] ?? ''}');
    final categoryCtrl = TextEditingController(text: '${source?['category'] ?? ''}');
    final priceCtrl = TextEditingController(text: source?['price'] == null ? '' : '${source?['price']}');
    String? localError;

    final save = await showDialog<bool>(
      context: context,
      builder: (ctx) {
        return StatefulBuilder(
          builder: (ctx, setDialogState) {
            return AlertDialog(
              insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
              title: Text(source == null ? 'Add Repo Item' : 'Edit Repo Item'),
              content: SizedBox(
                width: 460,
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Item')),
                      const SizedBox(height: 10),
                      TextField(controller: storeCtrl, decoration: const InputDecoration(labelText: 'Store')),
                      const SizedBox(height: 10),
                      TextField(controller: categoryCtrl, decoration: const InputDecoration(labelText: 'Category')),
                      const SizedBox(height: 10),
                      TextField(
                        controller: priceCtrl,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true),
                        decoration: const InputDecoration(labelText: 'Price'),
                      ),
                      if (localError != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 10),
                          child: Align(
                            alignment: Alignment.centerLeft,
                            child: Text(localError!, style: const TextStyle(color: Color(0xFFB91C1C), fontSize: 12)),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              actions: [
                TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
                FilledButton(
                  onPressed: _repoSaving
                      ? null
                      : () {
                          if (nameCtrl.text.trim().isEmpty) {
                            setDialogState(() => localError = 'Name is required');
                            return;
                          }
                          Navigator.of(ctx).pop(true);
                        },
                  child: Text(source == null ? 'Create' : 'Save'),
                ),
              ],
            );
          },
        );
      },
    );

    if (save != true) {
      nameCtrl.dispose();
      storeCtrl.dispose();
      categoryCtrl.dispose();
      priceCtrl.dispose();
      return;
    }

    setState(() => _repoSaving = true);
    try {
      if (source == null) {
        await widget.api.createShoppingRepoItem(
          name: nameCtrl.text.trim(),
          store: storeCtrl.text.trim().isEmpty ? null : storeCtrl.text.trim(),
          category: categoryCtrl.text.trim().isEmpty ? null : categoryCtrl.text.trim(),
          price: _parseNullableDouble(priceCtrl.text),
        );
      } else {
        await widget.api.updateShoppingRepoItem(
          repoId: _toInt(source['id']),
          name: nameCtrl.text.trim(),
          store: storeCtrl.text.trim(),
          category: categoryCtrl.text.trim(),
          price: _parseNullableDouble(priceCtrl.text),
        );
      }
      await _loadRepo(query: _repoSearchCtrl.text.trim());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Repo save failed: $err')));
    } finally {
      if (mounted) setState(() => _repoSaving = false);
      nameCtrl.dispose();
      storeCtrl.dispose();
      categoryCtrl.dispose();
      priceCtrl.dispose();
    }
  }

  Future<void> _deleteRepoItem(Map<String, dynamic> row) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Delete Repo Item'),
        content: Text('Delete "${row['name'] ?? 'Item'}" from saved repo?'),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            style: FilledButton.styleFrom(backgroundColor: const Color(0xFFDC2626)),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;

    try {
      await widget.api.deleteShoppingRepoItem(_toInt(row['id']));
      await _loadRepo(query: _repoSearchCtrl.text.trim());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Repo delete failed: $err')));
    }
  }

  Future<void> _addRepoItemToList(Map<String, dynamic> row) async {
    final listId = _selectedListId;
    if (listId == null) return;
    try {
      await widget.api.createShoppingItem(
        listId: listId,
        item: '${row['name'] ?? ''}',
        repoItemId: _toInt(row['id']),
        price: row['price'] == null ? null : _toDouble(row['price']),
      );
      await _changeList(listId);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Added ${row['name'] ?? 'item'}')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add from repo failed: $err')));
    }
  }

  List<Map<String, dynamic>> get _visibleItems {
    var rows = _items;
    if (_showUncheckedOnly) {
      rows = rows.where((row) => row['checked'] != true).toList();
    }
    if (_selectedTag != null && _selectedTag!.isNotEmpty) {
      rows = rows.where((row) {
        final tags = (row['tags'] is List ? (row['tags'] as List<dynamic>) : const <dynamic>[])
            .map((e) => '$e'.trim().toLowerCase())
            .where((e) => e.isNotEmpty)
            .toList();
        return tags.contains(_selectedTag!.toLowerCase());
      }).toList();
    }
    if (_selectedStore != null && _selectedStore!.isNotEmpty) {
      rows = rows.where((row) {
        final store = '${row['store'] ?? ''}'.trim();
        return store.toLowerCase() == _selectedStore!.toLowerCase();
      }).toList();
    }
    return rows;
  }

  List<String> get _availableTags {
    final set = <String>{};
    for (final row in _items) {
      final tags = (row['tags'] is List ? (row['tags'] as List<dynamic>) : const <dynamic>[])
          .map((e) => '$e'.trim())
          .where((e) => e.isNotEmpty);
      set.addAll(tags);
    }
    final out = set.toList();
    out.sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
    return out;
  }

  List<String> get _availableStores {
    final set = <String>{};
    for (final row in _items) {
      final store = '${row['store'] ?? ''}'.trim();
      if (store.isNotEmpty) set.add(store);
    }
    final out = set.toList();
    out.sort((a, b) => a.toLowerCase().compareTo(b.toLowerCase()));
    return out;
  }

  Future<void> _toggleChecked(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    final checked = row['checked'] == true;
    setState(() => _updatingItemIds.add(id));
    try {
      await widget.api.updateShoppingItem(itemId: id, checked: !checked);
      final listId = _selectedListId;
      if (listId != null) {
        await _changeList(listId);
      }
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update failed: $err')));
    } finally {
      if (mounted) setState(() => _updatingItemIds.remove(id));
    }
  }

  Future<void> _deleteItem(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    setState(() => _deletingItemIds.add(id));
    try {
      await widget.api.deleteShoppingItem(id);
      final listId = _selectedListId;
      if (listId != null) {
        await _changeList(listId);
      }
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    } finally {
      if (mounted) setState(() => _deletingItemIds.remove(id));
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ErrorPanel(error: _error!, onRetry: _load);
    }

    final selectedId = _selectedListId;
    final selectedList = _selectedListRow;
    final estimatedTotal = _toDouble(selectedList == null ? 0 : selectedList['estimated_total']);
    final listType = '${selectedList == null ? 'standard' : (selectedList['list_type'] ?? 'standard')}';
    final tags = _availableTags;
    final stores = _availableStores;
    final visibleItems = _visibleItems;

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Row(
            children: [
              Expanded(
                child: DropdownButtonFormField<int>(
                  initialValue: selectedId,
                  decoration: const InputDecoration(labelText: 'List'),
                  items: _lists
                      .map(
                        (row) => DropdownMenuItem<int>(
                          value: _toInt(row['id']),
                          child: Text('${row['name'] ?? ''} (${_toInt(row['active_count'])})'),
                        ),
                      )
                      .toList(),
                  onChanged: (value) {
                    if (value == null) return;
                    _changeList(value);
                  },
                ),
              ),
              const SizedBox(width: 8),
              IconButton(
                tooltip: 'New list',
                onPressed: _savingList ? null : () => _openListEditor(),
                icon: _savingList
                    ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.playlist_add),
              ),
              PopupMenuButton<String>(
                tooltip: 'List actions',
                onSelected: (value) {
                  if (value == 'edit' && selectedList != null) {
                    _openListEditor(source: selectedList);
                  }
                  if (value == 'delete' && selectedList != null) {
                    _deleteSelectedList();
                  }
                },
                itemBuilder: (context) => const [
                  PopupMenuItem<String>(value: 'edit', child: Text('Edit list')),
                  PopupMenuItem<String>(value: 'delete', child: Text('Delete list')),
                ],
              ),
            ],
          ),
          const SizedBox(height: 10),
          Card(
            color: listType == 'big_purchase' ? const Color(0xFFE0E7FF) : const Color(0xFFE0F2FE),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      'Estimated total: ${_currency(estimatedTotal)}',
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                  ),
                  Text(
                    listType == 'big_purchase' ? 'Big Purchase' : 'Standard',
                    style: const TextStyle(fontSize: 12, color: Color(0xFF475569), fontWeight: FontWeight.w600),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: _itemCtrl,
                  decoration: const InputDecoration(labelText: 'Add item', hintText: 'Milk, Bread, Eggs...'),
                  onSubmitted: (_) => _addItem(),
                ),
              ),
              const SizedBox(width: 8),
              FilledButton(
                onPressed: _adding ? null : _addItem,
                child: Text(_adding ? 'Adding...' : 'Add'),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: _storeCtrl,
                  decoration: const InputDecoration(labelText: 'Store (optional)', hintText: 'Drug Mart'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: TextField(
                  controller: _priceCtrl,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  decoration: const InputDecoration(labelText: 'Price', hintText: '0.00'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: TextField(
                  controller: _qtyCtrl,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  decoration: const InputDecoration(labelText: 'Qty', hintText: '1'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _tagsCtrl,
            decoration: const InputDecoration(labelText: 'Tags (comma separated)', hintText: 'produce, weekly, pantry'),
          ),
          if (listType == 'big_purchase') ...[
            const SizedBox(height: 8),
            TextField(
              controller: _purchaseCategoryCtrl,
              decoration: const InputDecoration(labelText: 'Purchase category', hintText: 'electronics, appliance, furniture...'),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _targetPriceCtrl,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    decoration: const InputDecoration(labelText: 'Target price', hintText: '999.00'),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: TextField(
                    controller: _budgetCeilingCtrl,
                    keyboardType: const TextInputType.numberWithOptions(decimal: true),
                    decoration: const InputDecoration(labelText: 'Budget ceiling', hintText: '1299.00'),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _sizeRequirementsCtrl,
              decoration: const InputDecoration(labelText: 'Size requirements', hintText: '36in W x 30in D'),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _planningNotesCtrl,
              maxLines: 2,
              decoration: const InputDecoration(labelText: 'Planning notes', hintText: 'Color, delivery, outlet requirements...'),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: _pickAndUploadPurchasePhoto,
                    icon: const Icon(Icons.upload_file),
                    label: const Text('Upload photo'),
                  ),
                ),
                if (_pendingPurchasePhotoFilename != null && _pendingPurchasePhotoFilename!.isNotEmpty) ...[
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      _pendingPurchasePhotoFilename!,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 12, color: Color(0xFF475569)),
                    ),
                  ),
                ],
              ],
            ),
            if ((_pendingPurchasePhotoUrl ?? '').isNotEmpty || (_pendingPurchasePhotoLocalPath ?? '').isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(10),
                  child: _pendingPurchasePhotoLocalPath != null && _pendingPurchasePhotoLocalPath!.isNotEmpty
                      ? Image.file(
                          File(_pendingPurchasePhotoLocalPath!),
                          height: 120,
                          width: double.infinity,
                          fit: BoxFit.cover,
                          errorBuilder: (_, __, ___) => Container(
                            height: 90,
                            color: const Color(0xFFF1F5F9),
                            alignment: Alignment.center,
                            child: const Text('Photo unavailable'),
                          ),
                        )
                      : Image.network(
                          _shoppingAbsoluteUrl(_pendingPurchasePhotoUrl ?? ''),
                          height: 120,
                          width: double.infinity,
                          fit: BoxFit.cover,
                          errorBuilder: (_, __, ___) => Container(
                            height: 90,
                            color: const Color(0xFFF1F5F9),
                            alignment: Alignment.center,
                            child: const Text('Photo unavailable'),
                          ),
                        ),
                ),
              ),
          ],
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    FilterChip(
                      label: const Text('Unchecked only'),
                      selected: _showUncheckedOnly,
                      onSelected: (value) {
                        setState(() => _showUncheckedOnly = value);
                      },
                    ),
                    ActionChip(
                      label: const Text('Clear filters'),
                      onPressed: () {
                        setState(() {
                          _showUncheckedOnly = false;
                          _selectedTag = null;
                          _selectedStore = null;
                        });
                      },
                    ),
                  ],
                ),
              ),
            ],
          ),
          if (tags.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                ChoiceChip(
                  label: const Text('All tags'),
                  selected: _selectedTag == null,
                  onSelected: (_) => setState(() => _selectedTag = null),
                ),
                ...tags.map(
                  (tag) => ChoiceChip(
                    label: Text(tag),
                    selected: _selectedTag == tag,
                    onSelected: (_) => setState(() => _selectedTag = _selectedTag == tag ? null : tag),
                  ),
                ),
              ],
            ),
          ],
          if (stores.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                ChoiceChip(
                  label: const Text('All stores'),
                  selected: _selectedStore == null,
                  onSelected: (_) => setState(() => _selectedStore = null),
                ),
                ...stores.map(
                  (store) => ChoiceChip(
                    label: Text(store),
                    selected: _selectedStore == store,
                    onSelected: (_) => setState(() => _selectedStore = _selectedStore == store ? null : store),
                  ),
                ),
              ],
            ),
          ],
          const SizedBox(height: 12),
          if (visibleItems.isEmpty)
            const Card(child: ListTile(title: Text('No items in this list.')))
          else
            ...visibleItems.map((row) {
              final id = _toInt(row['id']);
              final checked = row['checked'] == true;
              final updating = _updatingItemIds.contains(id);
              final deleting = _deletingItemIds.contains(id);
              final quantity = _toDouble(row['quantity']);
              final price = row['price'] == null ? null : _toDouble(row['price']);
              final store = '${row['store'] ?? ''}'.trim();
              final tags = (row['tags'] is List ? (row['tags'] as List<dynamic>) : const <dynamic>[])
                  .map((e) => '$e'.trim())
                  .where((e) => e.isNotEmpty)
                  .toList();
              final purchaseMeta = <String>[];
              if ('${row['purchase_category'] ?? ''}'.trim().isNotEmpty) purchaseMeta.add('${row['purchase_category']}');
              if (row['target_price'] != null) purchaseMeta.add('Target ${_currency(_toDouble(row['target_price']))}');
              if (row['budget_ceiling'] != null) purchaseMeta.add('Ceiling ${_currency(_toDouble(row['budget_ceiling']))}');

              final subtitleParts = <String>['${row['creator'] ?? ''}'];
              if (price != null) subtitleParts.add('Price ${_currency(price)}');
              if (quantity > 0) subtitleParts.add('Qty ${quantity % 1 == 0 ? quantity.toInt() : quantity.toStringAsFixed(1)}');
              if (store.isNotEmpty) subtitleParts.add(store);
              if (tags.isNotEmpty) subtitleParts.add(tags.join(', '));
              if (purchaseMeta.isNotEmpty) subtitleParts.add(purchaseMeta.join(' • '));

              return Card(
                child: ListTile(
                  onTap: updating || deleting ? null : () => _editItem(row),
                  leading: Checkbox(
                    value: checked,
                    onChanged: updating || deleting ? null : (_) => _toggleChecked(row),
                  ),
                  title: Text(
                    '${row['item'] ?? ''}',
                    style: TextStyle(
                      decoration: checked ? TextDecoration.lineThrough : null,
                      color: checked ? const Color(0xFF64748B) : null,
                    ),
                  ),
                  subtitle: Text(subtitleParts.join(' • ')),
                  trailing: IconButton(
                    tooltip: 'Delete item',
                    onPressed: deleting || updating ? null : () => _deleteItem(row),
                    icon: deleting
                        ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.delete_outline),
                  ),
                ),
              );
            }),
          const SizedBox(height: 12),
          Card(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      const Expanded(
                        child: Text('Saved Item Repo', style: TextStyle(fontWeight: FontWeight.w700)),
                      ),
                      IconButton(
                        tooltip: 'Add repo item',
                        onPressed: _repoSaving ? null : () => _openRepoEditor(),
                        icon: const Icon(Icons.add_circle_outline),
                      ),
                    ],
                  ),
                  Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _repoSearchCtrl,
                          decoration: const InputDecoration(labelText: 'Search repo', hintText: 'eggs, milk, detergent...'),
                          onSubmitted: (value) => _loadRepo(query: value.trim()),
                        ),
                      ),
                      const SizedBox(width: 8),
                      OutlinedButton(
                        onPressed: _repoLoading ? null : () => _loadRepo(query: _repoSearchCtrl.text.trim()),
                        child: const Text('Search'),
                      ),
                    ],
                  ),
                  const SizedBox(height: 10),
                  if (_repoLoading)
                    const Center(child: CircularProgressIndicator())
                  else if (_repoItems.isEmpty)
                    const Text('No repo items found.', style: TextStyle(color: Color(0xFF64748B)))
                  else
                    ..._repoItems.take(30).map((row) {
                      final price = row['price'] == null ? null : _toDouble(row['price']);
                      final store = '${row['store'] ?? ''}'.trim();
                      final category = '${row['category'] ?? ''}'.trim();
                      final meta = <String>[];
                      if (store.isNotEmpty) meta.add(store);
                      if (price != null) meta.add(_currency(price));
                      if (category.isNotEmpty) meta.add(category);
                      return ListTile(
                        dense: true,
                        contentPadding: EdgeInsets.zero,
                        title: Text('${row['name'] ?? ''}'),
                        subtitle: meta.isEmpty ? null : Text(meta.join(' • ')),
                        trailing: PopupMenuButton<String>(
                          onSelected: (value) {
                            if (value == 'add') _addRepoItemToList(row);
                            if (value == 'edit') _openRepoEditor(source: row);
                            if (value == 'delete') _deleteRepoItem(row);
                          },
                          itemBuilder: (context) => const [
                            PopupMenuItem<String>(value: 'add', child: Text('Add to list')),
                            PopupMenuItem<String>(value: 'edit', child: Text('Edit repo item')),
                            PopupMenuItem<String>(value: 'delete', child: Text('Delete repo item')),
                          ],
                        ),
                      );
                    }),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class ChoresPage extends StatefulWidget {
  const ChoresPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<ChoresPage> createState() => _ChoresPageState();
}

class _ChoresPageState extends State<ChoresPage> {
  bool _loading = true;
  String? _error;
  bool _adding = false;
  final Set<int> _updatingIds = <int>{};
  final Set<int> _deletingIds = <int>{};
  List<Map<String, dynamic>> _chores = const [];
  final TextEditingController _descriptionCtrl = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _descriptionCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final chores = await widget.api.fetchChores();
      if (!mounted) return;
      setState(() {
        _chores = chores;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<void> _addChore() async {
    final description = _descriptionCtrl.text.trim();
    if (description.isEmpty) return;
    setState(() => _adding = true);
    try {
      await widget.api.createChore(description);
      _descriptionCtrl.clear();
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add failed: $err')));
    } finally {
      if (mounted) setState(() => _adding = false);
    }
  }

  Future<void> _toggleDone(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    final done = row['done'] == true;
    setState(() => _updatingIds.add(id));
    try {
      await widget.api.setChoreDone(choreId: id, done: !done);
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update failed: $err')));
    } finally {
      if (mounted) setState(() => _updatingIds.remove(id));
    }
  }

  Future<void> _deleteChore(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    setState(() => _deletingIds.add(id));
    try {
      await widget.api.deleteChore(id);
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    } finally {
      if (mounted) setState(() => _deletingIds.remove(id));
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ErrorPanel(error: _error!, onRetry: _load);
    }

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: _descriptionCtrl,
                  decoration: const InputDecoration(labelText: 'Add chore', hintText: 'Take out trash, Vacuum living room...'),
                  onSubmitted: (_) => _addChore(),
                ),
              ),
              const SizedBox(width: 8),
              FilledButton(
                onPressed: _adding ? null : _addChore,
                child: Text(_adding ? 'Adding...' : 'Add'),
              ),
            ],
          ),
          const SizedBox(height: 12),
          if (_chores.isEmpty)
            const Card(child: ListTile(title: Text('No chores yet.')))
          else
            ..._chores.map((row) {
              final id = _toInt(row['id']);
              final done = row['done'] == true;
              final updating = _updatingIds.contains(id);
              final deleting = _deletingIds.contains(id);
              return Card(
                child: ListTile(
                  leading: Checkbox(
                    value: done,
                    onChanged: updating || deleting ? null : (_) => _toggleDone(row),
                  ),
                  title: Text(
                    '${row['description'] ?? ''}',
                    style: TextStyle(
                      decoration: done ? TextDecoration.lineThrough : null,
                      color: done ? const Color(0xFF64748B) : null,
                    ),
                  ),
                  subtitle: Text('${row['creator'] ?? ''}'),
                  trailing: IconButton(
                    tooltip: 'Delete chore',
                    onPressed: deleting || updating ? null : () => _deleteChore(row),
                    icon: deleting
                        ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.delete_outline),
                  ),
                ),
              );
            }),
        ],
      ),
    );
  }
}

class ProjectsSpacesPage extends StatefulWidget {
  const ProjectsSpacesPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<ProjectsSpacesPage> createState() => _ProjectsSpacesPageState();
}

class _ProjectsSpacesPageState extends State<ProjectsSpacesPage> {
  late Future<Map<String, dynamic>> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.api.fetchWorkspacesOverview();
  }

  Future<String?> _promptName(String title, String label) async {
    final controller = TextEditingController();
    final value = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: Text(title),
        content: SingleChildScrollView(
          child: TextField(
            controller: controller,
            decoration: InputDecoration(labelText: label),
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.of(context).pop(controller.text.trim()), child: const Text('Create')),
        ],
      ),
    );
    return value;
  }

  Future<void> _createProject() async {
    final name = await _promptName('Create Project', 'Project name');
    if (name == null || name.isEmpty) return;
    try {
      await widget.api.createWorkspaceProject(name);
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create project failed: $err')));
    }
  }

  Future<void> _createSpace() async {
    final name = await _promptName('Create Space', 'Space name');
    if (name == null || name.isEmpty) return;
    try {
      await widget.api.createWorkspaceSpace(name);
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create space failed: $err')));
    }
  }

  Future<void> _createTemplate() async {
    final nameCtrl = TextEditingController();
    final descriptionCtrl = TextEditingController();
    String type = 'project';
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Create Template'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Template name')),
                const SizedBox(height: 8),
                TextField(controller: descriptionCtrl, decoration: const InputDecoration(labelText: 'Description')),
                const SizedBox(height: 8),
                DropdownButtonFormField<String>(
                  initialValue: type,
                  decoration: const InputDecoration(labelText: 'Type'),
                  items: const [
                    DropdownMenuItem(value: 'project', child: Text('Project')),
                    DropdownMenuItem(value: 'space', child: Text('Space')),
                  ],
                  onChanged: (value) => setDialogState(() => type = value ?? 'project'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
          ],
        ),
      ),
    );
    if (should != true) return;
    try {
      await widget.api.createWorkspaceTemplate(name: nameCtrl.text.trim(), templateType: type, description: descriptionCtrl.text.trim());
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create template failed: $err')));
    }
  }

  Future<void> _createFromTemplate(Map<String, dynamic> template) async {
    final nameCtrl = TextEditingController();
    final descriptionCtrl = TextEditingController();
    String visibility = 'family';
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: Text('Use Template • ${template['name'] ?? ''}'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Name')),
                const SizedBox(height: 8),
                TextField(controller: descriptionCtrl, decoration: const InputDecoration(labelText: 'Description')),
                const SizedBox(height: 8),
                DropdownButtonFormField<String>(
                  initialValue: visibility,
                  decoration: const InputDecoration(labelText: 'Visibility'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'restricted', child: Text('Restricted')),
                    DropdownMenuItem(value: 'private', child: Text('Private')),
                  ],
                  onChanged: (value) => setDialogState(() => visibility = value ?? 'family'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Create')),
          ],
        ),
      ),
    );
    if (should != true) return;
    try {
      await widget.api.createWorkspaceEntity(
        entityType: '${template['template_type'] ?? 'project'}',
        name: nameCtrl.text.trim(),
        description: descriptionCtrl.text.trim(),
        templateId: _toInt(template['id']),
        visibility: visibility,
      );
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Create from template failed: $err')));
    }
  }

  Future<void> _deleteTemplate(Map<String, dynamic> template) async {
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Delete Template'),
        content: Text('Delete "${template['name'] ?? ''}"?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (should != true) return;
    try {
      await widget.api.deleteWorkspaceTemplate(_toInt(template['id']));
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete template failed: $err')));
    }
  }

  Future<void> _editProject(Map<String, dynamic> project) async {
    final nameCtrl = TextEditingController(text: '${project['name'] ?? ''}');
    final descriptionCtrl = TextEditingController(text: '${project['description'] ?? ''}');
    final allowedCtrl = TextEditingController(text: ((project['allowed_members'] as List<dynamic>? ?? const <dynamic>[]).join(', ')));
    String visibility = '${project['visibility'] ?? 'family'}';
    bool archived = project['archived'] == true;
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Edit Project'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Name')),
                const SizedBox(height: 8),
                TextField(controller: descriptionCtrl, decoration: const InputDecoration(labelText: 'Description')),
                const SizedBox(height: 8),
                DropdownButtonFormField<String>(
                  initialValue: visibility,
                  decoration: const InputDecoration(labelText: 'Visibility'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'restricted', child: Text('Restricted')),
                    DropdownMenuItem(value: 'private', child: Text('Private')),
                  ],
                  onChanged: (value) => setDialogState(() => visibility = value ?? 'family'),
                ),
                const SizedBox(height: 8),
                TextField(controller: allowedCtrl, decoration: const InputDecoration(labelText: 'Allowed members (comma separated)')),
                const SizedBox(height: 8),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: archived,
                  onChanged: (value) => setDialogState(() => archived = value),
                  title: const Text('Archived'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (should != true) return;
    try {
      final allowedMembers = allowedCtrl.text.split(',').map((value) => value.trim()).where((value) => value.isNotEmpty).toList();
      await widget.api.updateWorkspaceProject(
        projectId: _toInt(project['id']),
        name: nameCtrl.text.trim(),
        description: descriptionCtrl.text.trim(),
        visibility: visibility,
        allowedMembers: allowedMembers,
        archived: archived,
      );
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update project failed: $err')));
    }
  }

  Future<void> _editSpace(Map<String, dynamic> space) async {
    final nameCtrl = TextEditingController(text: '${space['name'] ?? ''}');
    final descriptionCtrl = TextEditingController(text: '${space['description'] ?? ''}');
    final allowedCtrl = TextEditingController(text: ((space['allowed_members'] as List<dynamic>? ?? const <dynamic>[]).join(', ')));
    String visibility = '${space['visibility'] ?? 'family'}';
    bool archived = space['archived'] == true;
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Edit Space'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Name')),
                const SizedBox(height: 8),
                TextField(controller: descriptionCtrl, decoration: const InputDecoration(labelText: 'Description')),
                const SizedBox(height: 8),
                DropdownButtonFormField<String>(
                  initialValue: visibility,
                  decoration: const InputDecoration(labelText: 'Visibility'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'restricted', child: Text('Restricted')),
                    DropdownMenuItem(value: 'private', child: Text('Private')),
                  ],
                  onChanged: (value) => setDialogState(() => visibility = value ?? 'family'),
                ),
                const SizedBox(height: 8),
                TextField(controller: allowedCtrl, decoration: const InputDecoration(labelText: 'Allowed members (comma separated)')),
                const SizedBox(height: 8),
                SwitchListTile(
                  contentPadding: EdgeInsets.zero,
                  value: archived,
                  onChanged: (value) => setDialogState(() => archived = value),
                  title: const Text('Archived'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (should != true) return;
    try {
      final allowedMembers = allowedCtrl.text.split(',').map((value) => value.trim()).where((value) => value.isNotEmpty).toList();
      await widget.api.updateWorkspaceSpace(
        spaceId: _toInt(space['id']),
        name: nameCtrl.text.trim(),
        description: descriptionCtrl.text.trim(),
        visibility: visibility,
        allowedMembers: allowedMembers,
        archived: archived,
      );
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update space failed: $err')));
    }
  }

  Future<void> _deleteProject(Map<String, dynamic> project) async {
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Delete Project'),
        content: Text('Delete "${project['name'] ?? ''}"? This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (should != true) return;
    try {
      await widget.api.deleteWorkspaceProject(_toInt(project['id']));
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete project failed: $err')));
    }
  }

  Future<void> _deleteSpace(Map<String, dynamic> space) async {
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Delete Space'),
        content: Text('Delete "${space['name'] ?? ''}"? This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Delete')),
        ],
      ),
    );
    if (should != true) return;
    try {
      await widget.api.deleteWorkspaceSpace(_toInt(space['id']));
      if (!mounted) return;
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete space failed: $err')));
    }
  }

  Future<void> _copyShareLink(String partialUrl) async {
    final full = _moduleUrl(widget.api.settings.baseUrl, partialUrl);
    await Clipboard.setData(ClipboardData(text: full));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Share link copied')));
  }

  Future<void> _openProject(Map<String, dynamic> project, Map<String, dynamic> data) async {
    final projectId = _toInt(project['id']);
    final columns = ((data['columns_by_project'] as Map<String, dynamic>? ?? const <String, dynamic>{})['$projectId'] as List<dynamic>? ?? const <dynamic>[])
        .whereType<Map<String, dynamic>>()
        .toList();
    final cards = ((data['cards_by_project'] as Map<String, dynamic>? ?? const <String, dynamic>{})['$projectId'] as List<dynamic>? ?? const <dynamic>[])
        .whereType<Map<String, dynamic>>()
        .toList();

    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => ProjectBoardPage(
          api: widget.api,
          project: project,
          initialColumns: columns,
          initialCards: cards,
        ),
      ),
    );
    if (changed == true && mounted) {
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    }
  }

  Future<void> _openSpace(Map<String, dynamic> space, Map<String, dynamic> data) async {
    final spaceId = _toInt(space['id']);
    final pages = ((data['pages_by_space'] as Map<String, dynamic>? ?? const <String, dynamic>{})['$spaceId'] as List<dynamic>? ?? const <dynamic>[])
        .whereType<Map<String, dynamic>>()
        .toList();

    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        builder: (_) => SpaceWikiPage(api: widget.api, space: space, initialPages: pages),
      ),
    );
    if (changed == true && mounted) {
      setState(() => _future = widget.api.fetchWorkspacesOverview());
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async {
        setState(() => _future = widget.api.fetchWorkspacesOverview());
        await _future;
      },
      child: FutureBuilder<Map<String, dynamic>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: '${snapshot.error}', onRetry: () => setState(() => _future = widget.api.fetchWorkspacesOverview()));
          }

          final data = snapshot.data ?? const <String, dynamic>{};
          final projects = (data['projects'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
          final spaces = (data['spaces'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
          final templates = (data['templates'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
          final cardsByProject = (data['cards_by_project'] as Map<String, dynamic>? ?? const <String, dynamic>{});
          final pagesBySpace = (data['pages_by_space'] as Map<String, dynamic>? ?? const <String, dynamic>{});

          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Row(
                children: [
                  Expanded(child: FilledButton.icon(onPressed: _createProject, icon: const Icon(Icons.add), label: const Text('New Project'))),
                  const SizedBox(width: 8),
                  Expanded(child: FilledButton.icon(onPressed: _createSpace, icon: const Icon(Icons.add), label: const Text('New Space'))),
                ],
              ),
              const SizedBox(height: 8),
              FilledButton.icon(onPressed: _createTemplate, icon: const Icon(Icons.auto_awesome_motion), label: const Text('New Template')),
              const SizedBox(height: 12),
              Text('Templates (${templates.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              if (templates.isEmpty)
                const Card(child: ListTile(title: Text('No templates yet.')))
              else
                ...templates.map((template) => Card(
                      child: ListTile(
                        title: Text('${template['name'] ?? ''}'),
                        subtitle: Text('${template['template_type'] ?? ''} • ${template['description'] ?? ''}'),
                        trailing: Wrap(
                          spacing: 4,
                          children: [
                            TextButton(onPressed: () => _createFromTemplate(template), child: const Text('Use')),
                            IconButton(onPressed: () => _deleteTemplate(template), icon: const Icon(Icons.delete_outline), tooltip: 'Delete'),
                          ],
                        ),
                      ),
                    )),
              const SizedBox(height: 14),
              Text('Projects (${projects.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              if (projects.isEmpty)
                const Card(child: ListTile(title: Text('No projects yet.')))
              else
                ...projects.map((project) {
                  final id = _toInt(project['id']);
                  final cards = (cardsByProject['$id'] as List<dynamic>? ?? const <dynamic>[]);
                  return Card(
                    child: ListTile(
                      title: Text('${project['name'] ?? ''}'),
                      subtitle: Text('${project['description'] ?? ''}\n${cards.length} cards • ${project['visibility'] ?? 'family'}${project['archived'] == true ? ' • archived' : ''}'),
                      isThreeLine: true,
                      trailing: PopupMenuButton<String>(
                        onSelected: (value) {
                          if (value == 'open') _openProject(project, data);
                          if (value == 'edit') _editProject(project);
                          if (value == 'delete') _deleteProject(project);
                          if (value == 'share') _copyShareLink('${project['share_url'] ?? ''}');
                        },
                        itemBuilder: (context) => const [
                          PopupMenuItem(value: 'open', child: Text('Open board')),
                          PopupMenuItem(value: 'edit', child: Text('Edit / visibility')),
                          PopupMenuItem(value: 'share', child: Text('Copy share link')),
                          PopupMenuItem(value: 'delete', child: Text('Delete')),
                        ],
                      ),
                      onTap: () => _openProject(project, data),
                    ),
                  );
                }),
              const SizedBox(height: 12),
              Text('Spaces (${spaces.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
              const SizedBox(height: 8),
              if (spaces.isEmpty)
                const Card(child: ListTile(title: Text('No spaces yet.')))
              else
                ...spaces.map((space) {
                  final id = _toInt(space['id']);
                  final pages = (pagesBySpace['$id'] as List<dynamic>? ?? const <dynamic>[]);
                  return Card(
                    child: ListTile(
                      title: Text('${space['name'] ?? ''}'),
                      subtitle: Text('${space['description'] ?? ''}\n${pages.length} pages • ${space['visibility'] ?? 'family'}${space['archived'] == true ? ' • archived' : ''}'),
                      isThreeLine: true,
                      trailing: PopupMenuButton<String>(
                        onSelected: (value) {
                          if (value == 'open') _openSpace(space, data);
                          if (value == 'edit') _editSpace(space);
                          if (value == 'delete') _deleteSpace(space);
                          if (value == 'share') _copyShareLink('${space['share_url'] ?? ''}');
                        },
                        itemBuilder: (context) => const [
                          PopupMenuItem(value: 'open', child: Text('Open wiki')),
                          PopupMenuItem(value: 'edit', child: Text('Edit / visibility')),
                          PopupMenuItem(value: 'share', child: Text('Copy share link')),
                          PopupMenuItem(value: 'delete', child: Text('Delete')),
                        ],
                      ),
                      onTap: () => _openSpace(space, data),
                    ),
                  );
                }),
            ],
          );
        },
      ),
    );
  }
}

class ProjectBoardPage extends StatefulWidget {
  const ProjectBoardPage({
    super.key,
    required this.api,
    required this.project,
    required this.initialColumns,
    required this.initialCards,
  });

  final HomeHubApi api;
  final Map<String, dynamic> project;
  final List<Map<String, dynamic>> initialColumns;
  final List<Map<String, dynamic>> initialCards;

  @override
  State<ProjectBoardPage> createState() => _ProjectBoardPageState();
}

class _ProjectBoardPageState extends State<ProjectBoardPage> {
  late List<Map<String, dynamic>> _columns;
  late List<Map<String, dynamic>> _cards;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _columns = [...widget.initialColumns]..sort((a, b) => _toInt(a['position']).compareTo(_toInt(b['position'])));
    _cards = [...widget.initialCards];
  }

  Future<void> _addColumn() async {
    final nameCtrl = TextEditingController();
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Add Column'),
        content: TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Column name')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Add')),
        ],
      ),
    );
    if (should != true) return;
    try {
      await widget.api.addProjectColumn(projectId: _toInt(widget.project['id']), name: nameCtrl.text.trim());
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add column failed: $err')));
    }
  }

  Future<void> _addCard(int columnId) async {
    final titleCtrl = TextEditingController();
    final descCtrl = TextEditingController();
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        title: const Text('Add Card'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(controller: titleCtrl, decoration: const InputDecoration(labelText: 'Title')),
              const SizedBox(height: 8),
              TextField(controller: descCtrl, minLines: 2, maxLines: 4, decoration: const InputDecoration(labelText: 'Description')),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Add')),
        ],
      ),
    );
    if (should != true) return;
    try {
      await widget.api.createProjectCard(
        projectId: _toInt(widget.project['id']),
        columnId: columnId,
        title: titleCtrl.text.trim(),
        description: descCtrl.text.trim(),
      );
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add card failed: $err')));
    }
  }

  Future<void> _moveCard(Map<String, dynamic> card) async {
    String? selected;
    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Move Card'),
          content: DropdownButtonFormField<String>(
            initialValue: selected,
            decoration: const InputDecoration(labelText: 'Target column'),
            items: _columns.map((col) => DropdownMenuItem<String>(value: '${col['id']}', child: Text('${col['name']}'))).toList(),
            onChanged: (value) => setDialogState(() => selected = value),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Move')),
          ],
        ),
      ),
    );
    if (should != true || selected == null) return;
    setState(() => _saving = true);
    try {
      await widget.api.moveProjectCard(cardId: _toInt(card['id']), columnId: int.parse(selected!));
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Move failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _deleteCard(Map<String, dynamic> card) async {
    setState(() => _saving = true);
    try {
      await widget.api.deleteProjectCard(_toInt(card['id']));
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final cardsByColumn = <int, List<Map<String, dynamic>>>{};
    for (final card in _cards) {
      cardsByColumn.putIfAbsent(_toInt(card['column_id']), () => []).add(card);
    }

    return Scaffold(
      appBar: AppBar(
        title: Text('${widget.project['name'] ?? 'Project'}'),
        actions: [
          IconButton(onPressed: _saving ? null : _addColumn, icon: const Icon(Icons.view_column_outlined), tooltip: 'Add column'),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          ..._columns.map((col) {
            final colId = _toInt(col['id']);
            final cards = cardsByColumn[colId] ?? const <Map<String, dynamic>>[];
            return Card(
              child: Padding(
                padding: const EdgeInsets.all(10),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(child: Text('${col['name'] ?? ''}', style: const TextStyle(fontWeight: FontWeight.w700))),
                        IconButton(onPressed: _saving ? null : () => _addCard(colId), icon: const Icon(Icons.add_circle_outline), tooltip: 'Add card'),
                      ],
                    ),
                    if (cards.isEmpty)
                      const Text('No cards')
                    else
                      ...cards.map((card) => Card(
                            color: const Color(0xFFF8FAFC),
                            child: ListTile(
                              title: Text('${card['title'] ?? ''}'),
                              subtitle: Text('${card['description'] ?? ''}'),
                              trailing: Wrap(
                                spacing: 4,
                                children: [
                                  IconButton(onPressed: _saving ? null : () => _moveCard(card), icon: const Icon(Icons.swap_horiz), tooltip: 'Move'),
                                  IconButton(onPressed: _saving ? null : () => _deleteCard(card), icon: const Icon(Icons.delete_outline), tooltip: 'Delete'),
                                ],
                              ),
                            ),
                          )),
                  ],
                ),
              ),
            );
          }),
        ],
      ),
    );
  }
}

class SpaceWikiPage extends StatefulWidget {
  const SpaceWikiPage({super.key, required this.api, required this.space, required this.initialPages});

  final HomeHubApi api;
  final Map<String, dynamic> space;
  final List<Map<String, dynamic>> initialPages;

  @override
  State<SpaceWikiPage> createState() => _SpaceWikiPageState();
}

class _SpaceWikiPageState extends State<SpaceWikiPage> {
  late List<Map<String, dynamic>> _pages;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _pages = [...widget.initialPages];
  }

  Future<void> _addPage() async {
    final titleCtrl = TextEditingController();
    final contentCtrl = TextEditingController();
    final should = await showModalBottomSheet<bool>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      builder: (context) => Padding(
        padding: EdgeInsets.fromLTRB(16, 16, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(controller: titleCtrl, decoration: const InputDecoration(labelText: 'Page title')),
              const SizedBox(height: 8),
              TextField(controller: contentCtrl, minLines: 4, maxLines: 10, decoration: const InputDecoration(labelText: 'Content (HTML/text)')),
              const SizedBox(height: 10),
              Row(
                children: [
                  Expanded(child: OutlinedButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel'))),
                  const SizedBox(width: 8),
                  Expanded(child: FilledButton(onPressed: () => Navigator.of(context).pop(true), child: const Text('Save'))),
                ],
              ),
            ],
          ),
        ),
      ),
    );
    if (should != true) return;
    setState(() => _saving = true);
    try {
      await widget.api.createSpacePage(spaceId: _toInt(widget.space['id']), title: titleCtrl.text.trim(), content: contentCtrl.text.trim());
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add page failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _deletePage(Map<String, dynamic> page) async {
    setState(() => _saving = true);
    try {
      await widget.api.deleteSpacePage(_toInt(page['id']));
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete page failed: $err')));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('${widget.space['name'] ?? 'Space'}'),
        actions: [
          IconButton(onPressed: _saving ? null : _addPage, icon: const Icon(Icons.note_add_outlined), tooltip: 'Add page'),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          if (_pages.isEmpty)
            const Card(child: ListTile(title: Text('No pages yet.')))
          else
            ..._pages.map((page) => Card(
                  child: ExpansionTile(
                    title: Text('${page['title'] ?? ''}'),
                    subtitle: Text('Updated ${page['updated_at'] ?? ''}'),
                    trailing: IconButton(
                      onPressed: _saving ? null : () => _deletePage(page),
                      icon: const Icon(Icons.delete_outline),
                    ),
                    children: [
                      Padding(
                        padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                        child: Align(
                          alignment: Alignment.centerLeft,
                          child: Text(_stripHtml('${page['content'] ?? ''}')),
                        ),
                      ),
                    ],
                  ),
                )),
        ],
      ),
    );
  }
}

class RecipeBookPage extends StatefulWidget {
  const RecipeBookPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<RecipeBookPage> createState() => _RecipeBookPageState();
}

class _RecipeBookPageState extends State<RecipeBookPage> {
  late Future<List<Map<String, dynamic>>> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.api.fetchRecipes();
  }

  Future<void> _addRecipe() async {
    final titleCtrl = TextEditingController();
    final ingredientsCtrl = TextEditingController();
    final instructionsCtrl = TextEditingController();
    final linkCtrl = TextEditingController();
    bool parsing = false;
    final shouldSave = await showModalBottomSheet<bool>(
      context: context,
      useSafeArea: true,
      isScrollControlled: true,
      builder: (context) => StatefulBuilder(
        builder: (context, setModalState) => Padding(
          padding: EdgeInsets.fromLTRB(16, 16, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: titleCtrl, decoration: const InputDecoration(labelText: 'Title')),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(child: TextField(controller: linkCtrl, decoration: const InputDecoration(labelText: 'Recipe URL'))),
                    const SizedBox(width: 8),
                    FilledButton(
                      onPressed: parsing
                          ? null
                          : () async {
                              final url = linkCtrl.text.trim();
                              if (url.isEmpty) return;
                              setModalState(() => parsing = true);
                              try {
                                final parsed = await widget.api.parseRecipeLink(url);
                                final recipe = parsed['recipe'] as Map<String, dynamic>? ?? const <String, dynamic>{};
                                titleCtrl.text = '${recipe['title'] ?? titleCtrl.text}';
                                ingredientsCtrl.text = '${recipe['ingredients_html'] ?? ingredientsCtrl.text}';
                                instructionsCtrl.text = '${recipe['instructions_html'] ?? instructionsCtrl.text}';
                                linkCtrl.text = '${recipe['link'] ?? url}';
                              } catch (err) {
                                if (!mounted) return;
                                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Parse failed: $err')));
                              } finally {
                                setModalState(() => parsing = false);
                              }
                            },
                      child: Text(parsing ? '...' : 'Parse'),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                TextField(controller: ingredientsCtrl, minLines: 3, maxLines: 6, decoration: const InputDecoration(labelText: 'Ingredients (HTML/text)')),
                const SizedBox(height: 8),
                TextField(controller: instructionsCtrl, minLines: 3, maxLines: 6, decoration: const InputDecoration(labelText: 'Instructions (HTML/text)')),
                const SizedBox(height: 10),
                Row(
                  children: [
                    Expanded(child: OutlinedButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel'))),
                    const SizedBox(width: 8),
                    Expanded(child: FilledButton(onPressed: () => Navigator.of(context).pop(true), child: const Text('Save'))),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
    if (shouldSave != true) return;
    try {
      await widget.api.saveRecipe(
        title: titleCtrl.text.trim(),
        ingredients: ingredientsCtrl.text.trim(),
        instructions: instructionsCtrl.text.trim(),
        link: linkCtrl.text.trim(),
      );
      if (!mounted) return;
      setState(() => _future = widget.api.fetchRecipes());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Save failed: $err')));
    }
  }

  Future<void> _deleteRecipe(int id) async {
    try {
      await widget.api.deleteRecipe(id);
      if (!mounted) return;
      setState(() => _future = widget.api.fetchRecipes());
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: () async {
        setState(() => _future = widget.api.fetchRecipes());
        await _future;
      },
      child: FutureBuilder<List<Map<String, dynamic>>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) return const Center(child: CircularProgressIndicator());
          if (snapshot.hasError) return ErrorPanel(error: '${snapshot.error}', onRetry: () => setState(() => _future = widget.api.fetchRecipes()));
          final rows = snapshot.data ?? const <Map<String, dynamic>>[];
          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton.icon(onPressed: _addRecipe, icon: const Icon(Icons.add), label: const Text('Add Recipe')),
              ),
              const SizedBox(height: 8),
              if (rows.isEmpty)
                const Card(child: ListTile(title: Text('No recipes yet.')))
              else
                ...rows.map((row) => Card(
                      child: ListTile(
                        title: Text('${row['title'] ?? ''}'),
                        subtitle: Text('${row['creator'] ?? ''}'),
                        trailing: Wrap(
                          spacing: 4,
                          children: [
                            IconButton(
                              onPressed: () async {
                                try {
                                  await widget.api.addRecipeIngredientsToShopping(_toInt(row['id']));
                                  if (!mounted) return;
                                  ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Ingredients added to shopping list.')));
                                } catch (err) {
                                  if (!mounted) return;
                                  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add ingredients failed: $err')));
                                }
                              },
                              icon: const Icon(Icons.shopping_cart_outlined),
                              tooltip: 'Add ingredients to shopping',
                            ),
                            IconButton(onPressed: () => _deleteRecipe(_toInt(row['id'])), icon: const Icon(Icons.delete_outline)),
                          ],
                        ),
                      ),
                    )),
            ],
          );
        },
      ),
    );
  }
}

class ExpiryTrackerPage extends StatefulWidget {
  const ExpiryTrackerPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<ExpiryTrackerPage> createState() => _ExpiryTrackerPageState();
}

class _ExpiryTrackerPageState extends State<ExpiryTrackerPage> {
  bool _loading = true;
  String? _error;
  List<Map<String, dynamic>> _items = const [];
  List<Map<String, dynamic>> _reminders = const [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final items = await widget.api.fetchExpiryItems();
      final reminders = await widget.api.fetchExpiryPendingReminders();
      if (!mounted) return;
      setState(() {
        _items = items;
        _reminders = reminders;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<void> _addItem() async {
    final nameCtrl = TextEditingController();
    DateTime selected = DateTime.now().add(const Duration(days: 7));
    final shouldSave = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Add Expiry Item'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Item name')),
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  onPressed: () async {
                    final picked = await showDatePicker(
                      context: context,
                      initialDate: selected,
                      firstDate: DateTime(2020),
                      lastDate: DateTime(2100),
                    );
                    if (picked != null) {
                      setDialogState(() => selected = picked);
                    }
                  },
                  icon: const Icon(Icons.calendar_today),
                  label: Text(_fmtDate(selected)),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.of(context).pop(true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (shouldSave != true) return;
    try {
      await widget.api.saveExpiryItem(name: nameCtrl.text.trim(), expiryDate: _fmtDate(selected));
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Save failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return ErrorPanel(error: _error!, onRetry: _load);

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          Align(
            alignment: Alignment.centerRight,
            child: FilledButton.icon(onPressed: _addItem, icon: const Icon(Icons.add), label: const Text('Add Item')),
          ),
          const SizedBox(height: 8),
          if (_reminders.isNotEmpty) ...[
            Text('Pending Reminders', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
            const SizedBox(height: 8),
            ..._reminders.map((row) => Card(
                  child: ListTile(
                    title: Text('${row['name'] ?? ''}'),
                    subtitle: Text('Days left: ${row['days_left'] ?? ''}'),
                    trailing: TextButton(
                      onPressed: () async {
                        await widget.api.ackExpiryReminder(_toInt(row['id']));
                        await _load();
                      },
                      child: const Text('Ack'),
                    ),
                  ),
                )),
            const SizedBox(height: 10),
          ],
          Text('Expiry Items (${_items.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          if (_items.isEmpty)
            const Card(child: ListTile(title: Text('No expiry items yet.')))
          else
            ..._items.map((row) => Card(
                  child: ListTile(
                    title: Text('${row['name'] ?? ''}'),
                    subtitle: Text('Expiry: ${row['expiry_date'] ?? ''} • Days left: ${row['days_left'] ?? ''}'),
                    trailing: IconButton(
                      onPressed: () async {
                        await widget.api.deleteExpiryItem(_toInt(row['id']));
                        await _load();
                      },
                      icon: const Icon(Icons.delete_outline),
                    ),
                  ),
                )),
        ],
      ),
    );
  }
}

class QrGeneratorPage extends StatefulWidget {
  const QrGeneratorPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<QrGeneratorPage> createState() => _QrGeneratorPageState();
}

class _QrGeneratorPageState extends State<QrGeneratorPage> {
  final _textCtrl = TextEditingController();
  bool _loading = true;
  String? _error;
  List<Map<String, dynamic>> _history = const [];
  String? _latestB64;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _textCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final history = await widget.api.fetchQrHistory();
      if (!mounted) return;
      setState(() {
        _history = history;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<void> _generate() async {
    final text = _textCtrl.text.trim();
    if (text.isEmpty) return;
    try {
      final item = await widget.api.createQr(text);
      if (!mounted) return;
      setState(() => _latestB64 = '${item['image_base64'] ?? ''}'.trim());
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Generate failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return ErrorPanel(error: _error!, onRetry: _load);

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          TextField(controller: _textCtrl, decoration: const InputDecoration(labelText: 'QR text / URL / WIFI payload')),
          const SizedBox(height: 8),
          FilledButton.icon(onPressed: _generate, icon: const Icon(Icons.qr_code_2), label: const Text('Generate QR')),
          if (_latestB64 != null && _latestB64!.isNotEmpty) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Image.memory(base64Decode(_latestB64!)),
              ),
            ),
          ],
          const SizedBox(height: 12),
          Text('History (${_history.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          if (_history.isEmpty)
            const Card(child: ListTile(title: Text('No QR history yet.')))
          else
            ..._history.map((row) => Card(
                  child: ListTile(
                    title: Text('${row['original_input'] ?? row['text'] ?? ''}'),
                    subtitle: Text('${row['creator'] ?? ''}'),
                    trailing: IconButton(
                      onPressed: () async {
                        await widget.api.deleteQr(_toInt(row['id']));
                        await _load();
                      },
                      icon: const Icon(Icons.delete_outline),
                    ),
                  ),
                )),
        ],
      ),
    );
  }
}

class SettingsNativePage extends StatefulWidget {
  const SettingsNativePage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<SettingsNativePage> createState() => _SettingsNativePageState();
}

class _SettingsNativePageState extends State<SettingsNativePage> {
  bool _loading = true;
  String? _error;
  final _hubCtrl = TextEditingController();
  final _memberCtrl = TextEditingController();
  String _newMemberRole = 'standard';
  List<Map<String, dynamic>> _members = const [];
  Map<String, dynamic> _menuVisibility = const {};
  Map<String, dynamic> _permissionSchemes = const {};

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _hubCtrl.dispose();
    _memberCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final settings = await widget.api.fetchHubSettings();
      final membersObj = await widget.api.fetchFamilyMembers();
      final menuObj = await widget.api.fetchMenuVisibility();
      final permissionObj = await widget.api.fetchPermissionSchemes();
      final members = (membersObj['members'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
      final menuVisibility = (menuObj['menu_visibility'] as Map<String, dynamic>? ?? const <String, dynamic>{});
      if (!mounted) return;
      setState(() {
        _hubCtrl.text = '${settings['hub_name'] ?? ''}';
        _members = members;
        _menuVisibility = menuVisibility;
        _permissionSchemes = permissionObj;
        _loading = false;
      });
    } catch (err) {
      if (!mounted) return;
      setState(() {
        _error = '$err';
        _loading = false;
      });
    }
  }

  Future<void> _editMember(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    if (id <= 0) return;
    final nameCtrl = TextEditingController(text: '${row['name'] ?? ''}');
    final emailCtrl = TextEditingController(text: '${row['email'] ?? ''}');
    final pinCtrl = TextEditingController();
    String role = '${row['role'] ?? 'standard'}';

    final shouldSave = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: const Text('Edit Member'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: nameCtrl, decoration: const InputDecoration(labelText: 'Name')),
                const SizedBox(height: 8),
                TextField(controller: emailCtrl, decoration: const InputDecoration(labelText: 'Email')),
                const SizedBox(height: 8),
                DropdownButtonFormField<String>(
                  initialValue: role,
                  decoration: const InputDecoration(labelText: 'Role'),
                  items: const [
                    DropdownMenuItem(value: 'standard', child: Text('Standard')),
                    DropdownMenuItem(value: 'admin', child: Text('Admin')),
                  ],
                  onChanged: (value) => setDialogState(() => role = value ?? 'standard'),
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: pinCtrl,
                  keyboardType: TextInputType.number,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: 'PIN (optional, 4 digits)'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.of(context).pop(true), child: const Text('Save')),
          ],
        ),
      ),
    );

    if (shouldSave != true) return;
    try {
      await widget.api.updateFamilyMember(
        memberId: id,
        name: nameCtrl.text.trim(),
        role: role,
        email: emailCtrl.text.trim(),
        pin: pinCtrl.text.trim().isEmpty ? null : pinCtrl.text.trim(),
      );
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update failed: $err')));
    }
  }

  Future<void> _deleteMember(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    if (id <= 0) return;
    try {
      await widget.api.deleteFamilyMember(id);
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Delete failed: $err')));
    }
  }

  Future<void> _resetPin(Map<String, dynamic> row) async {
    final id = _toInt(row['id']);
    if (id <= 0) return;
    try {
      await widget.api.resetMemberPin(id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('PIN reset.')));
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('PIN reset failed: $err')));
    }
  }

  Future<void> _setMenuVisibility(String key, bool visible) async {
    try {
      await widget.api.setMenuVisibility(menuItem: key, visible: visible);
      if (!mounted) return;
      setState(() => _menuVisibility = {..._menuVisibility, key: visible});
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Menu update failed: $err')));
    }
  }

  Future<void> _editAppModulePermission(Map<String, dynamic> appModule) async {
    final moduleKey = '${appModule['resource_key'] ?? ''}';
    if (moduleKey.isEmpty) return;
    final grants = (_permissionSchemes['grants'] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().where((row) {
      return '${row['resource_type'] ?? ''}' == 'app_module' && '${row['resource_key'] ?? ''}' == moduleKey;
    }).toList();
    final members = (_permissionSchemes['members'] as List<dynamic>? ?? const <dynamic>[]).map((value) => '$value').where((value) => value.isNotEmpty).toList();

    String visibility = 'family';
    final allowed = <String>{};
    for (final grant in grants) {
      if ('${grant['subject_type'] ?? ''}' == 'member' && grant['can_read'] == true) {
        final member = '${grant['subject_value'] ?? ''}'.trim();
        if (member.isNotEmpty) allowed.add(member);
      }
    }

    final should = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: Text('Permissions • $moduleKey'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                DropdownButtonFormField<String>(
                  initialValue: visibility,
                  decoration: const InputDecoration(labelText: 'Visibility'),
                  items: const [
                    DropdownMenuItem(value: 'family', child: Text('Family')),
                    DropdownMenuItem(value: 'restricted', child: Text('Restricted')),
                    DropdownMenuItem(value: 'private', child: Text('Private')),
                  ],
                  onChanged: (value) => setDialogState(() => visibility = value ?? 'family'),
                ),
                const SizedBox(height: 8),
                ...members.map(
                  (member) => CheckboxListTile(
                    dense: true,
                    contentPadding: EdgeInsets.zero,
                    value: allowed.contains(member),
                    onChanged: (value) {
                      setDialogState(() {
                        if (value == true) {
                          allowed.add(member);
                        } else {
                          allowed.remove(member);
                        }
                      });
                    },
                    title: Text(member),
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
          ],
        ),
      ),
    );
    if (should != true) return;
    try {
      await widget.api.updateAppPermissionScheme(moduleKey: moduleKey, visibility: visibility, allowedMembers: allowed.toList());
      await _load();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Permission update failed: $err')));
    }
  }

  Future<void> _editMemberProfile(Map<String, dynamic> memberRow) async {
    final memberId = _toInt(memberRow['id']);
    if (memberId <= 0) return;
    try {
      final profile = await widget.api.getMemberProfile(memberId);
      final emailCtrl = TextEditingController(text: '${profile['email'] ?? ''}');
      final statusCtrl = TextEditingController(text: '${profile['status'] ?? ''}');
      final birthdayCtrl = TextEditingController(text: '${profile['birthday'] ?? ''}');
      final colorCtrl = TextEditingController(text: '${profile['favorite_color'] ?? ''}');
      final songCtrl = TextEditingController(text: '${profile['favorite_song'] ?? ''}');
      final wishlistCtrl = TextEditingController(text: '${profile['amazon_wishlist_url'] ?? ''}');

      final should = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
          title: Text('Profile • ${memberRow['name'] ?? ''}'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(controller: emailCtrl, decoration: const InputDecoration(labelText: 'Email')),
                const SizedBox(height: 8),
                TextField(controller: statusCtrl, decoration: const InputDecoration(labelText: 'Status')),
                const SizedBox(height: 8),
                TextField(controller: birthdayCtrl, decoration: const InputDecoration(labelText: 'Birthday (YYYY-MM-DD)')),
                const SizedBox(height: 8),
                TextField(controller: colorCtrl, decoration: const InputDecoration(labelText: 'Favorite color')),
                const SizedBox(height: 8),
                TextField(controller: songCtrl, decoration: const InputDecoration(labelText: 'Favorite song')),
                const SizedBox(height: 8),
                TextField(controller: wishlistCtrl, decoration: const InputDecoration(labelText: 'Amazon wishlist URL')),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () async {
              try {
                await widget.api.testMemberEmail(memberId);
                if (!mounted) return;
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Test email sent.')));
              } catch (err) {
                if (!mounted) return;
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Test email failed: $err')));
              }
            }, child: const Text('Test Email')),
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Save')),
          ],
        ),
      );
      if (should != true) return;
      await widget.api.updateMemberProfile(memberId, {
        'email': emailCtrl.text.trim(),
        'status': statusCtrl.text.trim(),
        'birthday': birthdayCtrl.text.trim(),
        'favorite_color': colorCtrl.text.trim(),
        'favorite_song': songCtrl.text.trim(),
        'amazon_wishlist_url': wishlistCtrl.text.trim(),
      });
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Profile updated.')));
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Profile update failed: $err')));
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return ErrorPanel(error: _error!, onRetry: _load);

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(12),
        children: [
          TextField(controller: _hubCtrl, decoration: const InputDecoration(labelText: 'Hub Name')),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: () async {
              try {
                await widget.api.updateHubName(_hubCtrl.text.trim());
                if (!mounted) return;
                ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Hub updated.')));
              } catch (err) {
                if (!mounted) return;
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Update failed: $err')));
              }
            },
            child: const Text('Save Hub Name'),
          ),
          const SizedBox(height: 14),
          TextField(controller: _memberCtrl, decoration: const InputDecoration(labelText: 'Add Family Member')),
          const SizedBox(height: 8),
          DropdownButtonFormField<String>(
            initialValue: _newMemberRole,
            decoration: const InputDecoration(labelText: 'New Member Role'),
            items: const [
              DropdownMenuItem(value: 'standard', child: Text('Standard')),
              DropdownMenuItem(value: 'admin', child: Text('Admin')),
            ],
            onChanged: (value) => setState(() => _newMemberRole = value ?? 'standard'),
          ),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: () async {
              final name = _memberCtrl.text.trim();
              if (name.isEmpty) return;
              try {
                await widget.api.addFamilyMember(name: name, role: _newMemberRole);
                _memberCtrl.clear();
                await _load();
              } catch (err) {
                if (!mounted) return;
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Add failed: $err')));
              }
            },
            child: const Text('Add Member'),
          ),
          const SizedBox(height: 12),
          Text('Family Members (${_members.length})', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          ..._members.map((row) => Card(
                child: ListTile(
                  title: Text('${row['name'] ?? ''}'),
                  subtitle: Text('${row['role'] ?? 'standard'}${row['pin'] == true ? ' • PIN set' : ' • No PIN'}'),
                  trailing: Wrap(
                    spacing: 4,
                    children: [
                      IconButton(onPressed: () => _editMember(row), icon: const Icon(Icons.edit_outlined), tooltip: 'Edit'),
                      IconButton(onPressed: () => _resetPin(row), icon: const Icon(Icons.lock_reset), tooltip: 'Reset PIN'),
                      IconButton(onPressed: () => _deleteMember(row), icon: const Icon(Icons.delete_outline), tooltip: 'Delete'),
                    ],
                  ),
                ),
              )),
          const SizedBox(height: 12),
          Text('Menu Visibility', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          ..._menuVisibility.entries.map(
            (entry) => Card(
              child: SwitchListTile(
                value: entry.value == true,
                onChanged: (value) => _setMenuVisibility(entry.key, value),
                title: Text(entry.key.replaceAll('_', ' ')),
              ),
            ),
          ),
          const SizedBox(height: 12),
          Text('App Permissions', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          ...((_permissionSchemes['resources'] as Map<String, dynamic>? ?? const <String, dynamic>{})['app_modules'] as List<dynamic>? ?? const <dynamic>[])
              .whereType<Map<String, dynamic>>()
              .map(
                (moduleRow) => Card(
                  child: ListTile(
                    title: Text('${moduleRow['label'] ?? moduleRow['resource_key'] ?? ''}'),
                    subtitle: Text('${moduleRow['resource_key'] ?? ''}'),
                    trailing: TextButton(onPressed: () => _editAppModulePermission(moduleRow), child: const Text('Edit')),
                  ),
                ),
              ),
          const SizedBox(height: 12),
          Text('Member Profiles', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(height: 8),
          ..._members.map(
            (row) => Card(
              child: ListTile(
                title: Text('${row['name'] ?? ''}'),
                subtitle: Text('${row['email'] ?? ''}'),
                trailing: TextButton(onPressed: () => _editMemberProfile(row), child: const Text('Profile')),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class NotificationsPage extends StatefulWidget {
  const NotificationsPage({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<NotificationsPage> createState() => _NotificationsPageState();
}

class _NotificationsPageState extends State<NotificationsPage> {
  late Future<List<AppNotificationItem>> _future;
  bool _unreadOnly = false;
  String _eventTypeFilter = '';
  bool _markingAllRead = false;

  Future<List<AppNotificationItem>> _loadNotifications() {
    return widget.api.fetchNotifications(unreadOnly: _unreadOnly, eventType: _eventTypeFilter);
  }

  Future<void> _refresh() async {
    setState(() => _future = _loadNotifications());
    await _future;
  }

  Future<void> _markAllRead() async {
    setState(() => _markingAllRead = true);
    try {
      await widget.api.markAllNotificationsRead(eventType: _eventTypeFilter);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Marked notifications as read')));
      await _refresh();
    } catch (err) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Mark all failed: $err')));
    } finally {
      if (mounted) setState(() => _markingAllRead = false);
    }
  }

  @override
  void initState() {
    super.initState();
    _future = _loadNotifications();
  }

  @override
  Widget build(BuildContext context) {
    return RefreshIndicator(
      onRefresh: _refresh,
      child: FutureBuilder<List<AppNotificationItem>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snapshot.hasError) {
            return ErrorPanel(error: snapshot.error.toString(), onRetry: () => setState(() => _future = _loadNotifications()));
          }

          final rows = snapshot.data ?? [];
          final unreadCount = rows.where((item) => !item.isRead).length;
          final categories = rows.map((item) => item.eventType.trim()).where((v) => v.isNotEmpty).toSet().toList()..sort();
          if (rows.isEmpty) {
            return const Center(child: Text('No notifications yet.'));
          }

          return ListView(
            padding: const EdgeInsets.all(12),
            children: [
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Text('Notifications', style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                          const Spacer(),
                          Text('$unreadCount unread', style: const TextStyle(fontSize: 12, color: Color(0xFF64748B), fontWeight: FontWeight.w600)),
                        ],
                      ),
                      const SizedBox(height: 10),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: [
                          FilterChip(
                            label: const Text('Unread only'),
                            selected: _unreadOnly,
                            onSelected: (value) {
                              setState(() {
                                _unreadOnly = value;
                                _future = _loadNotifications();
                              });
                            },
                          ),
                          DropdownButtonHideUnderline(
                            child: DropdownButton<String>(
                              value: _eventTypeFilter,
                              items: [
                                const DropdownMenuItem<String>(value: '', child: Text('All categories')),
                                ...categories.map((value) => DropdownMenuItem<String>(value: value, child: Text(value))),
                              ],
                              onChanged: (value) {
                                setState(() {
                                  _eventTypeFilter = value ?? '';
                                  _future = _loadNotifications();
                                });
                              },
                            ),
                          ),
                          FilledButton.icon(
                            onPressed: _markingAllRead || unreadCount == 0 ? null : _markAllRead,
                            icon: const Icon(Icons.done_all),
                            label: Text(_markingAllRead ? 'Updating...' : 'Mark all read'),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              ...rows.map((item) {
                final timeText = item.createdAt == null ? '' : _relativeTime(item.createdAt!.toIso8601String());
                return Card(
                  child: ListTile(
                    tileColor: item.isRead ? null : const Color(0xFFEFF6FF),
                    onTap: item.isRead
                        ? null
                        : () async {
                            await widget.api.markNotificationRead(item.id);
                            if (!mounted) return;
                            setState(() => _future = _loadNotifications());
                          },
                    title: Text(item.title.isNotEmpty ? item.title : item.eventType),
                    subtitle: Text('${item.message}${timeText.isNotEmpty ? '\n$timeText' : ''}'),
                    isThreeLine: timeText.isNotEmpty,
                    trailing: item.isRead
                        ? const Icon(Icons.done, color: Colors.green)
                        : TextButton(
                            onPressed: () async {
                              await widget.api.markNotificationRead(item.id);
                              if (!mounted) return;
                              setState(() => _future = _loadNotifications());
                            },
                            child: const Text('Mark read'),
                          ),
                  ),
                );
              }),
            ],
          );
        },
      ),
    );
  }
}

class QuickAddSheet extends StatefulWidget {
  const QuickAddSheet({super.key, required this.api});

  final HomeHubApi api;

  @override
  State<QuickAddSheet> createState() => _QuickAddSheetState();
}

class _QuickAddSheetState extends State<QuickAddSheet> {
  final _formKey = GlobalKey<FormState>();
  final _titleCtrl = TextEditingController();
  final _categoryCtrl = TextEditingController(text: 'General');
  final _amountCtrl = TextEditingController();
  String _entryType = 'expense';
  String _recurrence = 'one-time';
  String _frequency = 'monthly';
  DateTime _date = DateTime.now();
  bool _saving = false;

  @override
  void dispose() {
    _titleCtrl.dispose();
    _categoryCtrl.dispose();
    _amountCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      child: SafeArea(
        top: false,
        child: SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(16, 16, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
          child: Form(
            key: _formKey,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text('Quick Add Entry', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700)),
                const SizedBox(height: 12),
                DropdownButtonFormField<String>(
                  initialValue: _entryType,
                  decoration: const InputDecoration(labelText: 'Type'),
                  items: const [
                    DropdownMenuItem(value: 'expense', child: Text('Expense')),
                    DropdownMenuItem(value: 'income', child: Text('Income')),
                  ],
                  onChanged: (v) => setState(() => _entryType = v ?? 'expense'),
                ),
                const SizedBox(height: 10),
                DropdownButtonFormField<String>(
                  initialValue: _recurrence,
                  decoration: const InputDecoration(labelText: 'Recurrence'),
                  items: const [
                    DropdownMenuItem(value: 'one-time', child: Text('One-time')),
                    DropdownMenuItem(value: 'recurring', child: Text('Recurring')),
                  ],
                  onChanged: (v) => setState(() => _recurrence = v ?? 'one-time'),
                ),
                const SizedBox(height: 10),
                if (_recurrence == 'recurring')
                  DropdownButtonFormField<String>(
                    initialValue: _frequency,
                    decoration: const InputDecoration(labelText: 'Frequency'),
                    items: const [
                      DropdownMenuItem(value: 'weekly', child: Text('Weekly')),
                      DropdownMenuItem(value: 'biweekly', child: Text('Biweekly')),
                      DropdownMenuItem(value: 'monthly', child: Text('Monthly')),
                    ],
                    onChanged: (v) => setState(() => _frequency = v ?? 'monthly'),
                  ),
                if (_recurrence == 'recurring') const SizedBox(height: 10),
                TextFormField(
                  controller: _titleCtrl,
                  decoration: const InputDecoration(labelText: 'Title'),
                  validator: (v) => (v == null || v.trim().isEmpty) ? 'Title required' : null,
                ),
                const SizedBox(height: 10),
                TextFormField(
                  controller: _categoryCtrl,
                  decoration: const InputDecoration(labelText: 'Category'),
                ),
                const SizedBox(height: 10),
                TextFormField(
                  controller: _amountCtrl,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  decoration: const InputDecoration(labelText: 'Amount'),
                  validator: (v) {
                    final amount = double.tryParse((v ?? '').trim());
                    if (amount == null || amount <= 0) return 'Valid amount required';
                    return null;
                  },
                ),
                const SizedBox(height: 10),
                OutlinedButton.icon(
                  onPressed: () async {
                    final picked = await showDatePicker(
                      context: context,
                      initialDate: _date,
                      firstDate: DateTime(2020),
                      lastDate: DateTime(2100),
                    );
                    if (picked != null) setState(() => _date = picked);
                  },
                  icon: const Icon(Icons.calendar_today),
                  label: Text('Date ${_fmtDate(_date)}'),
                ),
                const SizedBox(height: 14),
                FilledButton.icon(
                  onPressed: _saving
                      ? null
                      : () async {
                          if (!_formKey.currentState!.validate()) return;
                          final messenger = ScaffoldMessenger.of(context);
                          final navigator = Navigator.of(context);
                          setState(() => _saving = true);
                          try {
                            final result = await widget.api.quickAdd(
                              entryType: _entryType,
                              recurrence: _recurrence,
                              frequency: _frequency,
                              title: _titleCtrl.text.trim(),
                              category: _categoryCtrl.text.trim(),
                              date: _fmtDate(_date),
                              amount: double.parse(_amountCtrl.text.trim()),
                            );
                            if (!mounted) return;
                            messenger.showSnackBar(SnackBar(content: Text('${result['message'] ?? 'Saved'}')));
                            navigator.pop(true);
                          } catch (err) {
                            if (!mounted) return;
                            messenger.showSnackBar(SnackBar(content: Text('Save failed: $err')));
                          } finally {
                            if (mounted) setState(() => _saving = false);
                          }
                        },
                  icon: const Icon(Icons.save),
                  label: Text(_saving ? 'Saving...' : 'Save Entry'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class ErrorPanel extends StatelessWidget {
  const ErrorPanel({super.key, required this.error, required this.onRetry});

  final String error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        const SizedBox(height: 80),
        Icon(Icons.error_outline, color: Theme.of(context).colorScheme.error, size: 48),
        const SizedBox(height: 12),
        Text(error, textAlign: TextAlign.center),
        const SizedBox(height: 14),
        Center(child: FilledButton(onPressed: onRetry, child: const Text('Retry'))),
      ],
    );
  }
}

class _MetricCard extends StatelessWidget {
  const _MetricCard({required this.title, required this.value, required this.subtitle, required this.icon});

  final String title;
  final String value;
  final String subtitle;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Row(
          children: [
            Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                color: const Color(0xFFE0E7FF),
                borderRadius: BorderRadius.circular(12),
              ),
              child: Icon(icon, color: const Color(0xFF4338CA)),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: const TextStyle(fontSize: 12, color: Color(0xFF64748B), fontWeight: FontWeight.w600)),
                  const SizedBox(height: 2),
                  Text(value, style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700)),
                  const SizedBox(height: 2),
                  Text(subtitle, style: const TextStyle(fontSize: 12, color: Color(0xFF64748B))),
                ],
              ),
            )
          ],
        ),
      ),
    );
  }
}

int _toInt(dynamic value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  return int.tryParse('$value') ?? 0;
}

double _toDouble(dynamic value) {
  if (value is num) return value.toDouble();
  return double.tryParse('$value') ?? 0;
}

DateTime? _parseDateTime(dynamic value) {
  if (value == null) return null;
  return DateTime.tryParse('$value');
}

String _currency(double value) => '\$${value.toStringAsFixed(2)}';

String _fmtDate(DateTime d) {
  final y = d.year.toString().padLeft(4, '0');
  final m = d.month.toString().padLeft(2, '0');
  final day = d.day.toString().padLeft(2, '0');
  return '$y-$m-$day';
}

String _fmtDateTime(DateTime d) {
  final hh = d.hour.toString().padLeft(2, '0');
  final mm = d.minute.toString().padLeft(2, '0');
  return '${_fmtDate(d)} $hh:$mm';
}

String _moduleUrl(String baseUrl, String path) {
  final normalized = baseUrl.startsWith('http://') || baseUrl.startsWith('https://') ? baseUrl : 'http://$baseUrl';
  final root = Uri.parse(normalized);
  final rootPath = root.path.endsWith('/') ? root.path.substring(0, root.path.length - 1) : root.path;
  final modulePath = path.startsWith('/') ? path : '/$path';
  return root
      .replace(path: '$rootPath$modulePath', queryParameters: {'mobile': '1'})
      .toString();
}

String? _cleanNullableString(dynamic value) {
  final text = '$value'.trim();
  if (text.isEmpty || text.toLowerCase() == 'null') return null;
  return text;
}

DateTime _startOfWeek(DateTime d) {
  final date = DateTime(d.year, d.month, d.day);
  final weekday = date.weekday % 7;
  return date.subtract(Duration(days: weekday));
}

DateTime _endOfWeek(DateTime d) {
  final start = _startOfWeek(d);
  return start.add(const Duration(days: 6));
}

String _monthLabel(DateTime d) {
  const names = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  return '${names[d.month - 1]} ${d.year}';
}

String _weekdayLabel(DateTime d) {
  const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  return names[d.weekday - 1];
}

String _weekdayShort(String isoDate) {
  final parsed = DateTime.tryParse(isoDate);
  if (parsed == null) return isoDate;
  return _weekdayLabel(parsed);
}

String _weatherEmoji(int weatherCode) {
  const map = {
    0: '☀️',
    1: '🌤️',
    2: '⛅',
    3: '☁️',
    45: '🌫️',
    48: '🌫️',
    51: '🌧️',
    53: '🌧️',
    55: '🌧️',
    61: '🌧️',
    63: '🌧️',
    65: '⛈️',
    71: '❄️',
    73: '❄️',
    75: '❄️',
    77: '❄️',
    80: '🌧️',
    81: '🌧️',
    82: '⛈️',
    85: '❄️',
    86: '❄️',
    95: '⛈️',
    96: '⛈️',
    99: '⛈️',
  };
  return map[weatherCode] ?? '🌡️';
}

String _relativeTime(String iso) {
  final parsed = DateTime.tryParse(iso);
  if (parsed == null) return 'just now';
  final diffMinutes = DateTime.now().difference(parsed).inMinutes;
  if (diffMinutes < 1) return 'just now';
  if (diffMinutes == 1) return '1 minute ago';
  if (diffMinutes < 60) return '$diffMinutes minutes ago';
  final hours = (diffMinutes / 60).floor();
  if (hours == 1) return '1 hour ago';
  return '$hours hours ago';
}

String _mealTypeLabel(String mealType) {
  switch (mealType) {
    case 'breakfast':
      return 'Breakfast';
    case 'morning_snack':
      return 'Morning Snack';
    case 'lunch':
      return 'Lunch';
    case 'afternoon_snack':
      return 'Afternoon Snack';
    case 'dinner':
      return 'Dinner';
    default:
      return mealType.replaceAll('_', ' ');
  }
}

List<DateTime> _daysInMonth(DateTime month) {
  final first = DateTime(month.year, month.month, 1);
  final last = DateTime(month.year, month.month + 1, 0);
  final days = <DateTime>[];
  for (var day = first; !day.isAfter(last); day = day.add(const Duration(days: 1))) {
    days.add(day);
  }
  return days;
}

Color _colorFromHex(String hex) {
  final raw = hex.trim().replaceAll('#', '');
  if (raw.length == 6) {
    final v = int.tryParse(raw, radix: 16);
    if (v != null) return Color(0xFF000000 | v);
  }
  return const Color(0xFF6366F1);
}

String _stripHtml(String raw) {
  return raw.replaceAll(RegExp(r'<[^>]*>'), '').replaceAll('&nbsp;', ' ').trim();
}

String? _normalizeTimeInput(String raw) {
  final text = raw.trim();
  if (text.isEmpty) return null;
  final match = RegExp(r'^(\d{1,2}):(\d{2})$').firstMatch(text);
  if (match == null) return text;
  final hh = int.tryParse(match.group(1) ?? '');
  final mm = int.tryParse(match.group(2) ?? '');
  if (hh == null || mm == null || hh < 0 || hh > 23 || mm < 0 || mm > 59) return text;
  return '${hh.toString().padLeft(2, '0')}:${mm.toString().padLeft(2, '0')}';
}
