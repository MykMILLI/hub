import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:homehub_mobile/session_client.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  // flutter_test installs a mock HttpClient; these tests exercise real sockets
  // against a local server, which is the behaviour being verified.
  setUpAll(() => HttpOverrides.global = null);

  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  group('splitSetCookieHeader', () {
    test('keeps an Expires date with a comma in one cookie', () {
      final parts = splitSetCookieHeader(
        'session=abc; Expires=Wed, 09 Jun 2021 10:18:14 GMT; Path=/',
      );
      expect(parts, hasLength(1));
      expect(parts.single, contains('09 Jun 2021'));
    });

    test('splits two cookies joined into one header', () {
      final parts = splitSetCookieHeader(
        'session=abc; Path=/, remember=yes; Path=/',
      );
      expect(parts, hasLength(2));
      expect(parts.first, contains('session=abc'));
      expect(parts.last, contains('remember=yes'));
    });
  });

  group('SessionCookieClient', () {
    late HttpServer server;
    late List<String> seenCookies;
    late SessionCookieClient client;

    setUp(() async {
      seenCookies = <String>[];
      server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      server.listen((request) {
        seenCookies.add(request.headers['cookie']?.join('; ') ?? '');
        if (request.uri.path == '/api/member-login') {
          request.response.headers.add('Set-Cookie', 'session=signed-in; Path=/');
          request.response.write('ok');
        } else if (request.headers['cookie'] == null) {
          request.response.statusCode = 401;
          request.response.write('unauthorized');
        } else {
          request.response.write('authorized');
        }
        request.response.close();
      });
      client = SessionCookieClient(IOClient(HttpClient()));
    });

    tearDown(() async {
      client.close();
      await server.close(force: true);
    });

    String url(String path) => 'http://127.0.0.1:${server.port}$path';

    test('replays the session cookie on later requests', () async {
      final before = await client.get(Uri.parse(url('/api/timeline')));
      expect(before.statusCode, 401, reason: 'no session yet');

      await client.post(Uri.parse(url('/api/member-login')));

      final after = await client.get(Uri.parse(url('/api/timeline')));
      expect(after.statusCode, 200);
      expect(after.body, 'authorized');
      expect(seenCookies.last, contains('session=signed-in'));
    });

    test('persists the session so a fresh client stays signed in', () async {
      await client.post(Uri.parse(url('/api/member-login')));
      // Let the debounced write to SharedPreferences settle.
      await Future<void>.delayed(const Duration(milliseconds: 50));

      final restarted = SessionCookieClient(IOClient(HttpClient()));
      addTearDown(restarted.close);
      final response = await restarted.get(Uri.parse(url('/api/timeline')));

      expect(response.statusCode, 200);
      expect(await restarted.currentCookies(), containsPair('session', 'signed-in'));
    });

    test('clear drops the stored session', () async {
      await client.post(Uri.parse(url('/api/member-login')));
      await client.clear();
      final response = await client.get(Uri.parse(url('/api/timeline')));
      expect(response.statusCode, 401);
    });

    test('multipart uploads carry the session too', () async {
      await client.post(Uri.parse(url('/api/member-login')));
      final request = http.MultipartRequest(
        'POST',
        Uri.parse(url('/api/timeline/upload-image')),
      )..fields['user'] = 'Mykal';
      final streamed = await client.send(request);
      await streamed.stream.drain<void>();

      expect(streamed.statusCode, 200);
      expect(seenCookies.last, contains('session=signed-in'));
    });
  });
}
