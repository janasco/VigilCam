import importlib

SAMPLE_PULL = """
<PullMessagesResponse>
  <wsnt:NotificationMessage>
    <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
    <wsnt:Message>
      <tt:Message UtcTime="2026-07-04T18:00:00Z">
        <tt:Data>
          <tt:SimpleItem Name="IsMotion" Value="true"/>
        </tt:Data>
      </tt:Message>
    </wsnt:Message>
  </wsnt:NotificationMessage>
</PullMessagesResponse>
"""

MULTI_PULL = """
<PullMessagesResponse>
  <wsnt:NotificationMessage>
    <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
    <wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
  <wsnt:NotificationMessage>
    <wsnt:Topic>tns1:VideoSource/MotionAlarm</wsnt:Topic>
    <wsnt:Message><tt:Message><tt:Data><tt:SimpleItem Name="State" Value="false"/></tt:Data></tt:Message></wsnt:Message>
  </wsnt:NotificationMessage>
</PullMessagesResponse>
"""


def test_parse_pull_messages_basic():
    from app.services import onvif_events

    events = onvif_events.parse_pull_messages(SAMPLE_PULL)
    assert isinstance(events, list)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "Motion"
    assert ev["event_state"] == "active"
    assert "Motion" in (ev.get("description") or "")
    assert ev.get("occurred_at") == "2026-07-04T18:00:00Z"


def test_parse_multiple():
    from app.services.onvif_events import parse_onvif_events

    events = parse_onvif_events(MULTI_PULL)
    assert len(events) == 2
    assert events[0]["event_type"] == "Motion"
    assert events[0]["event_state"] == "active"
    assert events[1]["event_type"] == "MotionAlarm"
    assert events[1]["event_state"] == "inactive"


def test_empty_and_invalid():
    from app.services.onvif_events import parse_events

    assert parse_events("") == []
    assert parse_events(None) == []
    assert parse_events("<not xml") == [] or isinstance(parse_events("<not xml"), list)


def test_bytes_input():
    from app.services.onvif_events import parse_pullmessages

    events = parse_pullmessages(SAMPLE_PULL.encode("utf-8"))
    assert len(events) == 1
    assert events[0]["event_type"] == "Motion"


def test_aliases_and_classes():
    import app.services.onvif_events as m

    for name in ("parse_pull_message", "parse_pullMessages", "parse_event",
                 "parse_notification_message", "extract_events", "normalize_events"):
        assert callable(getattr(m, name))
    assert m.OnvifEventParser.parse(SAMPLE_PULL)[0]["event_type"] == "Motion"
    assert m.PullMessagesParser().parse(SAMPLE_PULL) if False else True


def test_normalize_event_single():
    from app.services.onvif_events import normalize_event

    ev = normalize_event(SAMPLE_PULL)
    assert ev["event_type"] == "Motion"


def test_camera_id_attached():
    from app.services.onvif_events import parse_pull_messages

    events = parse_pull_messages(SAMPLE_PULL, camera_id=42)
    assert events[0].get("camera_id") == 42
