from pathlib import Path


def test_home_header_exposes_missed_updates_without_profile_chevron():
    base = Path('templates/base.html').read_text(encoding='utf-8')
    index = Path('templates/index.html').read_text(encoding='utf-8')

    assert 'id="openMissedUpdatesHeader"' in base
    assert 'openMissedUpdatesHeader\')?.addEventListener' in index
    profile_start = base.index('id="profileSwitcherBtn"')
    profile_end = base.index('</button>', profile_start)
    assert 'fa-chevron-down' not in base[profile_start:profile_end]


def test_launcher_cards_are_single_click_links_with_explicit_drag_handle():
    base = Path('templates/base.html').read_text(encoding='utf-8')

    assert 'card.draggable = false' in base
    assert 'launcher-drag-handle' in base
    assert 'cursor-pointer' in base
    assert 'cursor-grab' not in base
    assert "launcherGrid?.addEventListener('dragstart'" not in base
    assert "handle.addEventListener('dragstart'" in base


def test_welcome_slideshow_renders_video_assets_with_controls():
    index = Path('templates/index.html').read_text(encoding='utf-8')

    assert 'id="immichSlideshowVideo"' in index
    assert 'id="immichVideoPlay"' in index
    assert "String(asset.type || '').toUpperCase() === 'VIDEO'" in index
    assert "/playback`" in index
    assert 'slideshowVideo.onended' in index
    assert 'Play video with sound' in index


def test_weather_surfaces_render_city_and_narrative():
    index = Path('templates/index.html').read_text(encoding='utf-8')
    agenda = Path('templates/daily_agenda.html').read_text(encoding='utf-8')
    weather_js = Path('static/js/weather-new.js').read_text(encoding='utf-8')

    assert 'id="weather-narrative"' in index
    assert 'id="weather-narrative-panel"' in index
    assert "Today's outlook" in index
    assert "document.getElementById('weather-narrative')" in weather_js
    assert "d.location_name||'Location unavailable'" in agenda
    assert "d.narrative||''" in agenda
