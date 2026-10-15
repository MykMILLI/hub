import 'dart:async';
import 'dart:io';

import 'package:supabase_flutter/supabase_flutter.dart';

String _normalizeSupabaseUrl(String raw) {
  var value = raw.trim();
  if (value.startsWith('http:/') && !value.startsWith('http://')) {
    value = 'http://${value.substring('http:/'.length)}';
  } else if (value.startsWith('https:/') && !value.startsWith('https://')) {
    value = 'https://${value.substring('https:/'.length)}';
  }
  return value.endsWith('/') ? value.substring(0, value.length - 1) : value;
}

bool _looksLikeValidSupabaseBaseUrl(String input) {
  final uri = Uri.tryParse(input);
  if (uri == null) return false;
  if (uri.scheme != 'http' && uri.scheme != 'https') return false;
  if (uri.host.isEmpty) return false;

  final path = uri.path.trim();
  if (path == '/auth/v1' || path == '/rest/v1') {
    return false;
  }
  return true;
}

List<String> _splitSupabaseUrlCandidates(String raw) {
  final parts = raw
      .split(',')
      .map(_normalizeSupabaseUrl)
      .where((value) => value.isNotEmpty)
      .toList();

  final out = <String>[];
  final seen = <String>{};
  for (final part in parts) {
    if (!seen.add(part)) continue;
    if (!_looksLikeValidSupabaseBaseUrl(part)) continue;
    out.add(part);
  }
  return out;
}

bool _isLikelyPrivateHost(String host) {
  final lower = host.trim().toLowerCase();
  if (lower == 'localhost' || lower.endsWith('.local')) return true;

  final ip = InternetAddress.tryParse(lower);
  if (ip == null || ip.type != InternetAddressType.IPv4) {
    return false;
  }
  final octets = lower.split('.').map(int.tryParse).toList();
  if (octets.length != 4 || octets.any((value) => value == null)) return false;
  final a = octets[0]!;
  final b = octets[1]!;
  if (a == 10) return true;
  if (a == 172 && b >= 16 && b <= 31) return true;
  if (a == 192 && b == 168) return true;
  if (a == 127) return true;
  return false;
}

String _pickBestFallbackCandidate(List<String> candidates) {
  if (candidates.isEmpty) return '';
  final scored = [...candidates]
    ..sort((a, b) {
      final ua = Uri.parse(a);
      final ub = Uri.parse(b);
      final aPrivate = _isLikelyPrivateHost(ua.host);
      final bPrivate = _isLikelyPrivateHost(ub.host);
      if (aPrivate != bPrivate) return aPrivate ? 1 : -1;
      if (ua.scheme != ub.scheme) {
        if (ua.scheme == 'https') return -1;
        if (ub.scheme == 'https') return 1;
      }
      return 0;
    });
  return scored.first;
}

Future<bool> _isSupabaseCandidateReachable({
  required String baseUrl,
  required String anonKey,
}) async {
  final client = HttpClient();
  try {
    final request = await client.getUrl(Uri.parse('$baseUrl/auth/v1/settings'));
    request.headers.set('apikey', anonKey);
    final response = await request.close().timeout(const Duration(seconds: 4));
    final code = response.statusCode;
    return code >= 200 && code < 500;
  } catch (_) {
    return false;
  } finally {
    client.close(force: true);
  }
}

class HomeHubSupabaseConfig {
  const HomeHubSupabaseConfig({
    required this.supabaseUrl,
    required this.supabaseAnonKey,
  });

  factory HomeHubSupabaseConfig.fromEnvironment() {
    return const HomeHubSupabaseConfig(
      supabaseUrl: String.fromEnvironment('SUPABASE_URL', defaultValue: ''),
      supabaseAnonKey: String.fromEnvironment(
        'SUPABASE_ANON_KEY',
        defaultValue: '',
      ),
    );
  }

  final String supabaseUrl;
  final String supabaseAnonKey;

  String get normalizedSupabaseUrl {
    final values = normalizedSupabaseUrls;
    return values.isEmpty ? '' : values.first;
  }

  List<String> get normalizedSupabaseUrls =>
      _splitSupabaseUrlCandidates(supabaseUrl);

  bool get hasSupabaseConfig =>
      normalizedSupabaseUrls.isNotEmpty &&
      supabaseAnonKey.isNotEmpty;

  String? get configurationIssue {
    final raw = supabaseUrl.trim();
    if (raw.isEmpty && supabaseAnonKey.isEmpty) {
      return null;
    }
    if (raw.isEmpty || supabaseAnonKey.isEmpty) {
      return 'Missing SUPABASE_URL and/or SUPABASE_ANON_KEY.';
    }
    if (normalizedSupabaseUrls.isEmpty) {
      return 'SUPABASE_URL is invalid. Use one or more root URLs separated by commas (without /auth/v1 or /rest/v1).';
    }
    return null;
  }
}

class HomeHubSupabaseBootstrapResult {
  const HomeHubSupabaseBootstrapResult({
    required this.initialized,
    required this.configured,
    required this.message,
  });

  final bool initialized;
  final bool configured;
  final String message;
}

Future<HomeHubSupabaseBootstrapResult> _initializeHomeHubSupabaseFromConfig(
  HomeHubSupabaseConfig config,
) async {
  final issue = config.configurationIssue;

  if (!config.hasSupabaseConfig) {
    if (issue == null) {
      return const HomeHubSupabaseBootstrapResult(
        initialized: false,
        configured: false,
        message:
            'Supabase runtime defines are not set; skipping Supabase initialization.',
      );
    }
    return HomeHubSupabaseBootstrapResult(
      initialized: false,
      configured: false,
      message: issue,
    );
  }

  final candidates = config.normalizedSupabaseUrls;
  final reachable = <String>[];
  for (final candidate in candidates) {
    final ok = await _isSupabaseCandidateReachable(
      baseUrl: candidate,
      anonKey: config.supabaseAnonKey,
    );
    if (ok) reachable.add(candidate);
  }

  final initOrder = <String>[];
  initOrder.addAll(reachable);
  if (initOrder.isEmpty) {
    final fallback = _pickBestFallbackCandidate(candidates);
    if (fallback.isNotEmpty) {
      initOrder.add(fallback);
    }
  }

  Object? lastError;
  for (final candidate in initOrder) {
    try {
      await Supabase.initialize(
        url: candidate,
        publishableKey: config.supabaseAnonKey,
      );
      return HomeHubSupabaseBootstrapResult(
        initialized: true,
        configured: true,
        message:
            'Supabase initialized successfully using $candidate${reachable.isEmpty ? ' (fallback)' : ''}.',
      );
    } catch (err) {
      lastError = err;
    }
  }

  return HomeHubSupabaseBootstrapResult(
    initialized: false,
    configured: true,
    message: 'Supabase initialization failed: ${lastError ?? 'no usable endpoint'}',
  );
}

Future<HomeHubSupabaseBootstrapResult> initializeHomeHubSupabase() async {
  final config = HomeHubSupabaseConfig.fromEnvironment();
  return _initializeHomeHubSupabaseFromConfig(config);
}

Future<HomeHubSupabaseBootstrapResult> initializeHomeHubSupabaseWithConfig({
  required String supabaseUrl,
  required String supabaseAnonKey,
}) async {
  final config = HomeHubSupabaseConfig(
    supabaseUrl: supabaseUrl,
    supabaseAnonKey: supabaseAnonKey,
  );
  return _initializeHomeHubSupabaseFromConfig(config);
}
