"""The event log behind the Logs page."""

from __future__ import annotations

from clockd.eventlog import CATEGORIES, EventLog


def test_events_are_recorded_with_ids_and_timestamps():
    log = EventLog(100)
    first = log.action("jogged evan")
    second = log.error("something broke")
    assert first.id == 1 and second.id == 2
    assert first.at > 0
    assert log.last_id() == 2


def test_the_buffer_is_bounded():
    log = EventLog(10)
    for index in range(50):
        log.system(f"line {index}")
    assert len(log.events) == 10
    assert log.events[-1].message == "line 49"
    assert log.last_id() == 50


def test_tail_returns_oldest_first():
    log = EventLog(100)
    for index in range(5):
        log.system(f"line {index}")
    messages = [e["message"] for e in log.tail()]
    assert messages == ["line 0", "line 1", "line 2", "line 3", "line 4"]


def test_tail_respects_the_limit_and_keeps_the_newest():
    log = EventLog(100)
    for index in range(20):
        log.system(f"line {index}")
    messages = [e["message"] for e in log.tail(limit=3)]
    assert messages == ["line 17", "line 18", "line 19"]


def test_filter_by_category():
    log = EventLog(100)
    log.action("moved")
    log.error("broke")
    log.fix("accepted")
    assert [e["message"] for e in log.tail(categories=["action"])] == ["moved"]
    assert len(log.tail(categories=["action", "error"])) == 2


def test_filter_by_search_is_case_insensitive():
    log = EventLog(100)
    log.action("Evan jogged")
    log.action("Micah jogged")
    assert len(log.tail(search="evan")) == 1
    assert len(log.tail(search="JOGGED")) == 2


def test_since_id_returns_only_newer_events():
    log = EventLog(100)
    log.system("one")
    marker = log.last_id()
    log.system("two")
    log.system("three")
    assert [e["message"] for e in log.tail(since_id=marker)] == ["two", "three"]


def test_counts_cover_every_category():
    log = EventLog(100)
    log.action("a")
    log.action("b")
    log.error("c")
    counts = log.counts()
    assert set(counts) == set(CATEGORIES)
    assert counts["action"] == 2
    assert counts["error"] == 1
    assert counts["mqtt-in"] == 0


def test_structured_data_rides_along():
    log = EventLog(100)
    event = log.action("jogged", person="evan", degrees=10)
    assert event.to_dict()["data"] == {"person": "evan", "degrees": 10}


def test_as_text_is_human_readable():
    log = EventLog(100)
    log.action("moved evan")
    text = log.as_text()
    assert "moved evan" in text
    assert "[action]" in text


def test_writing_to_a_file(tmp_path):
    path = tmp_path / "events.log"
    log = EventLog(100, path=path)
    log.action("written to disk")
    for handler in log._logger.handlers:
        handler.flush()
    assert "written to disk" in path.read_text(encoding="utf-8")
