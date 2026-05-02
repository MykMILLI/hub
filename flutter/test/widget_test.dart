import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:homehub_mobile/main.dart';

class _FakeHomeHubApi extends HomeHubApi {
  _FakeHomeHubApi()
      : super(const AppSettings(baseUrl: 'http://localhost:5000', member: 'tester'));

  CalendarEventDraft? lastDraft;
  String? lastUpdateScope;

  @override
  Future<CalendarEvent> updateCalendarEvent(
    int eventId,
    CalendarEventDraft draft, {
    String? photoPath,
    String? updateScope,
  }) async {
    lastDraft = draft;
    lastUpdateScope = updateScope;
    return CalendarEvent(
      id: eventId,
      title: draft.title,
      eventType: draft.eventType,
      startDate: draft.startDate,
      endDate: draft.endDate,
      createdBy: 'tester',
      color: draft.color,
      isAllDay: draft.isAllDay,
      visibility: draft.visibility,
      isRecurring: draft.isRecurring,
      invitedMembers: draft.invitedMembers,
    );
  }
}

Future<void> _openCalendarDaySheet(
  WidgetTester tester, {
  required HomeHubApi api,
  required CalendarEvent event,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) {
            return Center(
              child: ElevatedButton(
                onPressed: () {
                  showModalBottomSheet<bool>(
                    context: context,
                    isScrollControlled: true,
                    builder: (_) => CalendarDaySheet(
                      day: event.startDate,
                      events: [event],
                      meals: const [],
                      members: const [
                        CalendarMember(id: 1, name: 'Alex', role: 'adult'),
                      ],
                      api: api,
                    ),
                  );
                },
                child: const Text('Open sheet'),
              ),
            );
          },
        ),
      ),
    ),
  );

  await tester.tap(find.text('Open sheet'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('shows setup screen when settings are missing', (WidgetTester tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(const HomeHubMobileApp());
    await tester.pump(const Duration(milliseconds: 300));

    expect(find.text('HomeHub Setup'), findsOneWidget);
    expect(find.text('Save & Continue'), findsOneWidget);
  });

  testWidgets('quick +1d chip preserves event duration', (WidgetTester tester) async {
    final api = _FakeHomeHubApi();
    final event = CalendarEvent(
      id: 77,
      title: 'Trip',
      eventType: 'event',
      startDate: DateTime(2026, 4, 10),
      endDate: DateTime(2026, 4, 12),
      createdBy: 'Alex',
      color: '#2563eb',
      isAllDay: true,
      visibility: 'family',
      isRecurring: false,
      invitedMembers: const [],
      baseStartDate: DateTime(2026, 4, 10),
      baseEndDate: DateTime(2026, 4, 12),
    );

    await _openCalendarDaySheet(tester, api: api, event: event);
    await tester.tap(find.widgetWithText(ActionChip, '+1d'));
    await tester.pumpAndSettle();

    expect(api.lastDraft, isNotNull);
    expect(api.lastDraft!.startDate, DateTime(2026, 4, 11));
    expect(api.lastDraft!.endDate, DateTime(2026, 4, 13));
  });

  testWidgets('End -1d chip clamps end date to start date', (WidgetTester tester) async {
    final api = _FakeHomeHubApi();
    final event = CalendarEvent(
      id: 91,
      title: 'Single day',
      eventType: 'event',
      startDate: DateTime(2026, 4, 20),
      endDate: DateTime(2026, 4, 20),
      createdBy: 'Alex',
      color: '#16a34a',
      isAllDay: true,
      visibility: 'family',
      isRecurring: false,
      invitedMembers: const [],
      baseStartDate: DateTime(2026, 4, 20),
      baseEndDate: DateTime(2026, 4, 20),
    );

    await _openCalendarDaySheet(tester, api: api, event: event);
    await tester.tap(find.widgetWithText(ActionChip, 'End -1d'));
    await tester.pumpAndSettle();

    expect(api.lastDraft, isNotNull);
    expect(api.lastDraft!.startDate, DateTime(2026, 4, 20));
    expect(api.lastDraft!.endDate, DateTime(2026, 4, 20));
  });
}
