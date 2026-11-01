import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

/// Splits a joined `set-cookie` header without breaking on the comma inside
/// `Expires=Wed, 09 Jun 2021 ...`. A new cookie only starts when the text after
/// the comma looks like `name=`.
List<String> splitSetCookieHeader(String value) {
  final parts = <String>[];
  final newCookie = RegExp(r'^\s*[^=;,\s]+\s*=');
  var start = 0;
  for (var i = 0; i < value.length; i++) {
    if (value[i] != ',') continue;
    if (newCookie.firstMatch(value.substring(i + 1)) == null) continue;
    parts.add(value.substring(start, i));
    start = i + 1;
  }
  parts.add(value.substring(start));
  return parts.where((part) => part.trim().isNotEmpty).toList();
}

/// Persists the Flask `session` cookie across requests and app restarts.
///
/// `IOClient` keeps no cookie jar, so before this existed every gated `/api/*`
/// call returned 401 even straight after a successful member login.
class SessionCookieClient extends http.BaseClient {
  SessionCookieClient(this._inner);

  static const String storageKey = 'homehub_session_cookies';

  final http.Client _inner;
  final Map<String, String> _cookies = <String, String>{};
  bool _restored = false;

  Future<void> _restore() async {
    if (_restored) return;
    _restored = true;
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(storageKey) ?? '';
      if (raw.isEmpty) return;
      final decoded = jsonDecode(raw);
      if (decoded is Map) {
        decoded.forEach((key, value) => _cookies['$key'] = '$value');
      }
    } catch (_) {}
  }

  Future<void> _persist() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(storageKey, jsonEncode(_cookies));
    } catch (_) {}
  }

  Future<Map<String, String>> currentCookies() async {
    await _restore();
    return Map<String, String>.from(_cookies);
  }

  Future<void> clear() async {
    await _restore();
    _cookies.clear();
    await _persist();
  }

  String get cookieHeader =>
      _cookies.entries.map((e) => '${e.key}=${e.value}').join('; ');

  void _ingest(http.BaseResponse response) {
    final raw = response.headers['set-cookie'] ?? '';
    if (raw.isEmpty) return;
    var changed = false;
    for (final cookie in splitSetCookieHeader(raw)) {
      final pair = cookie.split(';').first.trim();
      final sep = pair.indexOf('=');
      if (sep <= 0) continue;
      final name = pair.substring(0, sep).trim();
      final value = pair.substring(sep + 1).trim();
      if (name.isEmpty) continue;
      if (value.isEmpty || value == '""') {
        changed = _cookies.remove(name) != null || changed;
      } else {
        changed = changed || _cookies[name] != value;
        _cookies[name] = value;
      }
    }
    if (changed) unawaited(_persist());
  }

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async {
    await _restore();
    final header = cookieHeader;
    if (header.isNotEmpty) {
      request.headers['cookie'] = header;
    }
    final response = await _inner.send(request);
    _ingest(response);
    return response;
  }

  @override
  void close() => _inner.close();
}
