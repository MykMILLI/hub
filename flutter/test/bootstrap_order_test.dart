import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('web authentication precedes gated notification registration', () {
    final source = File('lib/main.dart').readAsStringSync();
    final bootstrapStart = source.indexOf('Future<void> _bootstrapApi()');
    final bootstrapEnd = source.indexOf(
      'String get _sectionCacheKey',
      bootstrapStart,
    );
    final bootstrap = source.substring(bootstrapStart, bootstrapEnd);

    final authSetup = bootstrap.indexOf('await _loadPrivacyMask();');
    final sections = bootstrap.indexOf('await _loadSections();');
    final rendered = bootstrap.indexOf('_bootstrapping = false;');
    final notifications = bootstrap.indexOf(
      'unawaited(_setupNotificationsAfterBootstrap());',
    );

    expect(bootstrapStart, greaterThanOrEqualTo(0));
    expect(authSetup, greaterThanOrEqualTo(0));
    expect(sections, greaterThan(authSetup));
    expect(rendered, greaterThan(sections));
    expect(notifications, greaterThan(rendered));
  });

  test('mobile startup does not use member PIN authentication', () {
    final source = File('lib/main.dart').readAsStringSync();

    expect(source, isNot(contains('_ensurePinUnlocked')));
    expect(source, isNot(contains('SettingsStore.loadPin')));
    expect(source, isNot(contains('SettingsStore.savePin')));
    expect(source, isNot(contains('body: jsonEncode({\'name\': name, \'pin\'')));
  });

  test('notification setup failures remain non-fatal and retryable', () {
    final source = File('lib/main.dart').readAsStringSync();

    expect(source, contains('Future<void> _initializeNotificationsSafely()'));
    expect(source, contains('HomeHub loaded, but notifications could not'));
    expect(source, contains('onPressed: _retryBootstrap'));
    expect(
      source,
      contains('onPressed: () => unawaited(_retryNotificationSetup())'),
    );
  });

  test('server modules always use fresh web surfaces for feature parity', () {
    final source = File('lib/main.dart').readAsStringSync();

    expect(source, isNot(contains('final _nativeModulePages')));
    expect(source, isNot(contains('nativeBuilder: _nativeModulePages')));
    expect(source, contains('final activePage = ModuleWebPage('));
    expect(source, contains('await _controller.clearCache();'));
    expect(source, contains("headers: const {'Cache-Control': 'no-cache'}"));
  });
}
