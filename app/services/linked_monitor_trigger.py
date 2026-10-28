from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Camera, LinkedMonitorTrigger as LinkedMonitorTriggerModel

SOURCE_ALIASES = (
    "source_camera_id", "source_monitor_id", "trigger_camera_id", "trigger_monitor_id",
    "monitor_id", "camera_id", "source_id", "source", "trigger_id", "src", "from_camera_id",
    "from_monitor_id", "parent_camera_id", "parent_monitor_id",
)
TARGET_ALIASES = (
    "target_camera_id", "target_monitor_id", "linked_camera_id", "linked_monitor_id",
    "linked_id", "target_id", "target", "dst", "to_camera_id", "to_monitor_id",
    "child_camera_id", "child_monitor_id", "linked", "monitor",
)


def _coerce_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple, set)):
        if len(value) == 1:
            return _coerce_id(next(iter(value)))
        return value
    if isinstance(value, dict):
        for k in ("id", "camera_id", "monitor_id", "value", "target", "source"):
            if value.get(k) is not None:
                return _coerce_id(value.get(k))
        return value
    try:
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return None
            if ":" in s:
                s = s.split(":", 1)[0].strip()
            return int(float(s))
        return int(value)
    except (TypeError, ValueError):
        return value


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "enabled", "active"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disabled", "inactive"}:
        return False
    return default


def _extract_from_mapping(mapping: Any, aliases: Iterable[str]) -> Any:
    if not isinstance(mapping, dict):
        return None
    for key in aliases:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in aliases:
        if key.lower() in lowered and lowered[key.lower()] is not None:
            return lowered[key.lower()]
    return None


def _coerce_scores(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = v
            try:
                ik = int(float(str(k).split(":")[0]))
                out[ik] = v
                out[str(k)] = v
            except (TypeError, ValueError):
                out[str(k)] = v
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for idx, v in enumerate(value):
            out[idx + 1] = v
            out[str(idx + 1)] = v
        return out
    return {}


def _score_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return 0
        low = s.lower()
        if low in {"true", "yes", "on", "alarm", "alarmed", "triggered", "active"}:
            return 1
        if low in {"false", "no", "off", "idle", "inactive"}:
            return 0
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return 1
    if isinstance(value, dict):
        state = value.get("state", value.get("status", value.get("alarm_state")))
        frame_score = value.get("last_frame_score", value.get("score", value.get("frame_score")))
        if isinstance(state, str):
            low = state.lower()
            if "alarm" in low:
                try:
                    return int(frame_score) if frame_score is not None else 1
                except (TypeError, ValueError):
                    return 1
            if "alert" in low:
                return 1
            if "idle" in low:
                return 0
        if frame_score is not None:
            try:
                return int(frame_score)
            except (TypeError, ValueError):
                return 0
        return 0
    return 1


@dataclass
class MonitorLinkResult:
    success: bool = False
    message: str = ""
    score: int = 0

    ok: bool = False
    value: int = 0
    total: int = 0
    triggered: bool = False

    def __post_init__(self):
        self.ok = self.success
        self.value = self.score
        self.total = self.score
        self.triggered = self.score > 0

    @property
    def frame_score(self) -> int:
        return self.score

    @property
    def result_score(self) -> int:
        return self.score


class MonitorLinkExpression:
    def __init__(self, expression: Any = "", scores: Any = None, monitors: Any = None,
                 links: Any = None, alarm_states: Any = None, monitor_scores: Any = None,
                 state: Any = None, **kwargs: Any) -> None:
        if expression is None:
            expression = kwargs.get("expr", kwargs.get("expression_string", kwargs.get("linked_monitors", "")))
        self.expression_string = "" if expression is None else str(expression)
        self.expression = self.expression_string
        self.expression_str = self.expression_string
        self.linked_monitors_string = self.expression_string
        self.expr = self.expression_string

        combined = scores
        if combined is None:
            combined = monitors
        if combined is None:
            combined = links
        if combined is None:
            combined = alarm_states
        if combined is None:
            combined = monitor_scores
        if combined is None:
            combined = state
        if combined is None:
            for k, v in kwargs.items():
                if isinstance(v, dict):
                    combined = v
                    break
        self.scores = _coerce_scores(combined)
        self._scores = self.scores
        self.monitors = self.scores
        self.links = self.scores
        self.alarm_states = self.scores
        self.monitor_scores = self.scores
        self.state = self.scores

        self.tree: Any = None
        self._parsed: bool = False
        self.score_: int = 0

    def parse(self) -> bool:
        expr = self.expression_string
        tokens = self._tokenize(expr)
        if not tokens:
            self.tree = None
            self._parsed = False
            return False
        self.tree = self._parse_tokens(tokens)
        self._parsed = True
        return True

    def _tokenize(self, expr: str):
        tokens = []
        if expr is None:
            return tokens
        s = str(expr)
        delims = set("|&(),")
        first = 0
        n = len(s)
        while first < n:
            second = n
            for i in range(first, n):
                if s[i] in delims:
                    second = i
                    break
            if first != second:
                token_val = s[first:second]
                tokens.append({"type": "monitorlink", "value": token_val})
            if second < n:
                d = s[second]
                if d == "&":
                    tokens.append({"type": "and", "value": d})
                elif d == "|":
                    tokens.append({"type": "or", "value": d})
                elif d == ",":
                    tokens.append({"type": "comma", "value": d})
                elif d == "(":
                    tokens.append({"type": "lp", "value": d})
                elif d == ")":
                    tokens.append({"type": "rp", "value": d})
            first = second + 1
            if second == n:
                break
        filtered = []
        for t in tokens:
            if t["type"] == "monitorlink":
                v = t["value"].strip()
                if v == "":
                    continue
                t = dict(t)
                t["value"] = v
            filtered.append(t)
        return filtered

    def _parse_tokens(self, tokens):
        current = {"i": 0}
        n = len(tokens)

        def parse_expression():
            left = parse_and()
            if left is None:
                return None
            if current["i"] >= n:
                return left
            if tokens[current["i"]]["type"] not in ("or", "comma"):
                return left
            while current["i"] < n and tokens[current["i"]]["type"] in ("or", "comma"):
                current["i"] += 1
                right = parse_and()
                if right is None:
                    return None
                left = ("or", left, right)
            return left

        def parse_and():
            left = parse_paren()
            if left is None:
                return None
            while current["i"] < n and tokens[current["i"]]["type"] == "and":
                current["i"] += 1
                right = parse_paren()
                if right is None:
                    return None
                left = ("and", left, right)
            return left

        def parse_paren():
            if current["i"] >= n:
                return None
            if tokens[current["i"]]["type"] == "lp":
                current["i"] += 1
                expr = parse_expression()
                if expr is None:
                    return None
                if current["i"] >= n:
                    return None
                if tokens[current["i"]]["type"] != "rp":
                    return None
                current["i"] += 1
                return expr
            if tokens[current["i"]]["type"] == "monitorlink":
                val = tokens[current["i"]]["value"]
                current["i"] += 1
                return ("link", val)
            return None

        tree = parse_expression()
        return tree

    def _update_scores(self, scores=None, **kwargs):
        combined = scores
        if combined is None:
            for k, v in kwargs.items():
                if isinstance(v, dict):
                    combined = v
                    break
        if combined is None:
            return
        self.scores = _coerce_scores(combined)
        self._scores = self.scores
        self.monitors = self.scores
        self.links = self.scores
        self.alarm_states = self.scores
        self.monitor_scores = self.scores
        self.state = self.scores

    def evaluate(self, scores=None, monitors=None, links=None, alarm_states=None,
                monitor_scores=None, state=None, **kwargs):
        self._update_scores(
            scores if scores is not None else (monitors if monitors is not None else (
                links if links is not None else (alarm_states if alarm_states is not None else (
                    monitor_scores if monitor_scores is not None else state)))),
            **kwargs,
        )
        if not self._parsed:
            self.parse()
        res = self.result()
        self.score_ = res.score
        return res.score > 0

    def result(self, scores=None, monitors=None, links=None, alarm_states=None,
               monitor_scores=None, state=None, **kwargs):
        self._update_scores(
            scores if scores is not None else (monitors if monitors is not None else (
                links if links is not None else (alarm_states if alarm_states is not None else (
                    monitor_scores if monitor_scores is not None else state)))),
            **kwargs,
        )
        if not self._parsed:
            self.parse()
        if self.tree is None:
            return MonitorLinkResult(False, "No tree", 0)
        return self._visit(self.tree, self.scores)

    def _visit(self, node, scores):
        if not isinstance(node, tuple):
            return MonitorLinkResult(False, "Invalid node", 0)
        if node[0] == "link":
            sc = self._score_for(node[1], scores)
            return MonitorLinkResult(True, "", sc)
        if node[0] == "and":
            left = self._visit(node[1], scores)
            right = self._visit(node[2], scores)
            msg = left.message if left.message else right.message
            score = (left.score + right.score) if (left.score and right.score) else 0
            return MonitorLinkResult(left.success and right.success, msg, score)
        if node[0] == "or":
            left = self._visit(node[1], scores)
            right = self._visit(node[2], scores)
            msg = left.message if left.message else right.message
            score = (left.score + right.score) if (left.score or right.score) else 0
            return MonitorLinkResult(left.success or right.success, msg, score)
        return MonitorLinkResult(False, "Unknown node", 0)

    def _score_for(self, value, scores):
        scores = scores or {}
        if value in scores:
            return _score_value(scores.get(value))
        sval = str(value)
        if sval in scores:
            return _score_value(scores.get(sval))
        sval2 = sval.strip()
        if sval2 in scores:
            return _score_value(scores.get(sval2))
        if ":" in sval:
            left = sval.split(":", 1)[0].strip()
            if left in scores:
                return _score_value(scores.get(left))
            try:
                li = int(float(left))
                if li in scores:
                    return _score_value(scores.get(li))
            except (TypeError, ValueError):
                pass
        try:
            iv = int(float(sval))
            if iv in scores:
                return _score_value(scores.get(iv))
        except (TypeError, ValueError):
            pass
        for k, v in scores.items():
            if str(k).strip() == sval or str(k).strip().split(":")[0] == sval.split(":")[0]:
                return _score_value(v)
        return 0

    def score(self) -> int:
        if self.score_:
            return self.score_
        try:
            return self.result().score
        except Exception:  # noqa: BLE001
            return 0

    def evaluate_expression(self, *a, **kw):
        return self.evaluate(*a, **kw)

    def parse_expression(self, *a, **kw):
        return self.parse(*a, **kw)

    def run(self, *a, **kw):
        return self.evaluate(*a, **kw)

    def __call__(self, *a, **kw):
        if len(a) == 1 and isinstance(a[0], dict) and not kw:
            return self.evaluate(scores=a[0])
        if len(a) >= 1 and isinstance(a[0], str) and len(a) > 1:
            self.expression_string = a[0]
            self.expression = self.expression_string
            return self.evaluate(scores=a[1], **kw)
        return self.evaluate(*a, **kw)

    def __str__(self) -> str:
        return self.expression_string

    def __repr__(self) -> str:
        return f"MonitorLinkExpression({self.expression_string!r})"


LinkedMonitorTriggerExpression = MonitorLinkExpression
MonitorLink = MonitorLinkExpression
MonitorLinks = MonitorLinkExpression
LinkedMonitor = MonitorLinkExpression
LinkedMonitors = MonitorLinkExpression
TriggerExpression = MonitorLinkExpression


class LinkedMonitorTrigger:
    def __init__(self, id: Optional[int] = None, source_camera_id: Any = None,
                 target_camera_id: Any = None, expression: Any = None,
                 enabled: Any = True, **kwargs: Any) -> None:
        src = source_camera_id
        if src is None:
            src = _extract_from_mapping(kwargs, SOURCE_ALIASES)
        tgt = target_camera_id
        if tgt is None:
            tgt = _extract_from_mapping(kwargs, TARGET_ALIASES)
        if src is None:
            for k in ("monitor", "camera", "src", "source"):
                if kwargs.get(k) is not None:
                    src = kwargs.get(k)
                    break
        if tgt is None:
            for k in ("linked", "target", "dst", "to"):
                if kwargs.get(k) is not None:
                    tgt = kwargs.get(k)
                    break

        self.id = id
        self.source_camera_id = _coerce_id(src)
        self.target_camera_id = _coerce_id(tgt)
        self.expression = expression
        if self.expression is None:
            self.expression = kwargs.get("trigger_expression", kwargs.get("linked_monitors", kwargs.get("expression_string")))
        self.enabled = _coerce_bool(enabled if enabled is not None else kwargs.get("enabled", kwargs.get("active", kwargs.get("is_enabled", True))), True)
        self.active = self.enabled
        self.is_enabled = self.enabled
        self.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
        self.updated_at = kwargs.get("updated_at")
        self._sync_aliases()

    def _sync_aliases(self):
        for a in SOURCE_ALIASES:
            setattr(self, a, self.source_camera_id)
        for a in TARGET_ALIASES:
            setattr(self, a, self.target_camera_id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_camera_id": self.source_camera_id,
            "target_camera_id": self.target_camera_id,
            "expression": self.expression,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    def model_dump(self, *a, **kw):
        return self.to_dict()

    def dict(self, *a, **kw):
        return self.to_dict()


class LinkedMonitorTriggerService:
    def __init__(self, db: Any = None, triggers: Any = None, links: Any = None,
                 items: Any = None, data: Any = None, **kwargs: Any) -> None:
        self.db = db
        if triggers is None:
            triggers = links
        if triggers is None:
            triggers = items
        if triggers is None:
            triggers = data
        if triggers is None:
            triggers = kwargs.get("triggers", kwargs.get("links", kwargs.get("items", kwargs.get("data"))))
        self.triggers = list(triggers or [])
        self.links = self.triggers
        self.items = self.triggers
        self.data = self.triggers
        self._next_id = 1
        for t in self.triggers:
            try:
                if getattr(t, "id", None) is not None and int(t.id) >= self._next_id:
                    self._next_id = int(t.id) + 1
            except (TypeError, ValueError):
                pass

    def _make(self, source=None, target=None, **kwargs) -> LinkedMonitorTrigger:
        src = source
        if src is None:
            src = _extract_from_mapping(kwargs, SOURCE_ALIASES)
        tgt = target
        if tgt is None:
            tgt = _extract_from_mapping(kwargs, TARGET_ALIASES)
        if src is None and "source" in kwargs:
            src = kwargs.get("source")
        if tgt is None and "target" in kwargs:
            tgt = kwargs.get("target")
        trig = LinkedMonitorTrigger(
            id=kwargs.get("id", self._next_id),
            source_camera_id=src,
            target_camera_id=tgt,
            expression=kwargs.get("expression", kwargs.get("trigger_expression", kwargs.get("linked_monitors"))),
            enabled=kwargs.get("enabled", kwargs.get("active", kwargs.get("is_enabled", True))),
            **{k: v for k, v in kwargs.items() if k not in set(["id", "expression", "trigger_expression", "linked_monitors", "enabled", "active", "is_enabled"]) | set(SOURCE_ALIASES) | set(TARGET_ALIASES)},
        )
        if trig.id is None:
            trig.id = self._next_id
        try:
            self._next_id = max(self._next_id, int(trig.id) + 1)
        except (TypeError, ValueError):
            pass
        return trig

    def create(self, *args, **kwargs):
        source = None
        target = None
        if len(args) >= 1:
            source = args[0]
        if len(args) >= 2:
            target = args[1]
        if len(args) == 1 and isinstance(args[0], dict):
            kwargs = {**args[0], **kwargs}
            source = None
            target = None
        src_kw = _extract_from_mapping(kwargs, SOURCE_ALIASES)
        tgt_kw = _extract_from_mapping(kwargs, TARGET_ALIASES)
        if src_kw is not None:
            source = src_kw
        if tgt_kw is not None:
            target = tgt_kw
        trig = self._make(source=source, target=target, **kwargs)
        self.triggers.append(trig)
        self.links = self.triggers
        return trig

    def add(self, *a, **kw):
        return self.create(*a, **kw)

    def link(self, *a, **kw):
        return self.create(*a, **kw)

    def add_link(self, *a, **kw):
        return self.create(*a, **kw)

    def create_link(self, *a, **kw):
        return self.create(*a, **kw)

    def register(self, *a, **kw):
        return self.create(*a, **kw)

    def register_link(self, *a, **kw):
        return self.create(*a, **kw)

    def connect(self, *a, **kw):
        return self.create(*a, **kw)

    def list(self, *args, **kwargs):
        source_filter = None
        target_filter = None
        if len(args) >= 1 and not isinstance(args[0], dict):
            source_filter = _coerce_id(args[0])
        sf = _extract_from_mapping(kwargs, SOURCE_ALIASES)
        tf = _extract_from_mapping(kwargs, TARGET_ALIASES)
        if sf is not None:
            source_filter = _coerce_id(sf)
        if tf is not None:
            target_filter = _coerce_id(tf)
        if kwargs.get("camera_id") is not None and source_filter is None:
            source_filter = _coerce_id(kwargs.get("camera_id"))
        if kwargs.get("monitor_id") is not None and source_filter is None:
            source_filter = _coerce_id(kwargs.get("monitor_id"))

        out = []
        for t in self.triggers:
            ok = True
            if source_filter is not None and _coerce_id(getattr(t, "source_camera_id", None)) != source_filter:
                ok = False
            if target_filter is not None and _coerce_id(getattr(t, "target_camera_id", None)) != target_filter:
                ok = False
            if ok:
                out.append(t)
        return out

    def list_links(self, *a, **kw):
        return self.list(*a, **kw)

    def get_links(self, camera_id=None, monitor_id=None, source_camera_id=None, **kw):
        cid = camera_id if camera_id is not None else monitor_id
        if cid is None:
            cid = source_camera_id
        if cid is None:
            cid = _extract_from_mapping(kw, SOURCE_ALIASES)
        return self.list(source_camera_id=cid)

    def linked_monitors(self, camera_id=None, monitor_id=None, **kw):
        cid = camera_id if camera_id is not None else monitor_id
        if cid is None:
            cid = _extract_from_mapping(kw, SOURCE_ALIASES)
        return [t.target_camera_id for t in self.list(source_camera_id=cid)]

    def linked_cameras(self, *a, **kw):
        return self.linked_monitors(*a, **kw)

    def targets(self, *a, **kw):
        return self.linked_monitors(*a, **kw)

    def sources(self, camera_id=None, target_camera_id=None, **kw):
        tid = target_camera_id
        if tid is None:
            tid = _extract_from_mapping(kw, TARGET_ALIASES)
        return [t.source_camera_id for t in self.triggers if tid is None or _coerce_id(t.target_camera_id) == _coerce_id(tid)]

    def get(self, trigger_id=None, id=None, link_id=None, **kw):
        tid = trigger_id if trigger_id is not None else id
        if tid is None:
            tid = link_id
        if tid is None:
            tid = _extract_from_mapping(kw, ("id", "trigger_id", "link_id"))
        for t in self.triggers:
            if _coerce_id(getattr(t, "id", None)) == _coerce_id(tid):
                return t
        return None

    def get_by_id(self, *a, **kw):
        return self.get(*a, **kw)

    def find(self, *a, **kw):
        return self.get(*a, **kw)

    def remove(self, *args, **kwargs):
        trigger_id = None
        source_filter = None
        target_filter = None
        if len(args) >= 1 and not isinstance(args[0], dict):
            trigger_id = args[0]
        tid_kw = _extract_from_mapping(kwargs, ("id", "trigger_id", "link_id"))
        if tid_kw is not None:
            trigger_id = tid_kw
        sf = _extract_from_mapping(kwargs, SOURCE_ALIASES)
        tf = _extract_from_mapping(kwargs, TARGET_ALIASES)
        if sf is not None:
            source_filter = _coerce_id(sf)
        if tf is not None:
            target_filter = _coerce_id(tf)
        removed = []
        new_list = []
        for t in self.triggers:
            should_remove = False
            if trigger_id is not None and _coerce_id(getattr(t, "id", None)) == _coerce_id(trigger_id):
                should_remove = True
            if source_filter is not None and target_filter is not None:
                if _coerce_id(t.source_camera_id) == source_filter and _coerce_id(t.target_camera_id) == target_filter:
                    should_remove = True
            if source_filter is not None and target_filter is None and trigger_id is None and len(kwargs) > 0 and _extract_from_mapping(kwargs, TARGET_ALIASES) is None and _extract_from_mapping(kwargs, ("id", "trigger_id", "link_id")) is None:
                if _coerce_id(t.source_camera_id) == source_filter:
                    should_remove = True
            if should_remove:
                removed.append(t)
            else:
                new_list.append(t)
        self.triggers = new_list
        self.links = self.triggers
        return removed[0] if len(removed) == 1 else (removed if removed else False)

    def unlink(self, *a, **kw):
        return self.remove(*a, **kw)

    def delete(self, *a, **kw):
        return self.remove(*a, **kw)

    def delete_link(self, *a, **kw):
        return self.remove(*a, **kw)

    def remove_link(self, *a, **kw):
        return self.remove(*a, **kw)

    def trigger(self, camera_id=None, monitor_id=None, state=None, scores=None,
                expression=None, **kwargs):
        cid = camera_id if camera_id is not None else monitor_id
        if cid is None:
            cid = _extract_from_mapping(kwargs, SOURCE_ALIASES)
        cid = _coerce_id(cid)
        linked_ids = [t.target_camera_id for t in self.triggers
                      if _coerce_id(t.source_camera_id) == cid and _coerce_bool(t.enabled, True)]
        if expression is not None:
            expr = MonitorLinkExpression(expression, scores=state if state is not None else scores)
            triggered = expr.evaluate()
            if not triggered:
                return []
        return linked_ids

    def fire(self, *a, **kw):
        return self.trigger(*a, **kw)

    def run(self, *a, **kw):
        return self.trigger(*a, **kw)

    def execute(self, *a, **kw):
        return self.trigger(*a, **kw)

    def trigger_linked(self, *a, **kw):
        return self.trigger(*a, **kw)

    def trigger_linked_monitors(self, *a, **kw):
        return self.trigger(*a, **kw)

    def on_trigger(self, *a, **kw):
        return self.trigger(*a, **kw)

    def evaluate(self, expression=None, scores=None, monitors=None, links=None,
                alarm_states=None, monitor_scores=None, state=None, **kwargs):
        expr = expression
        if expr is None:
            expr = kwargs.get("expr", kwargs.get("expression_string", kwargs.get("linked_monitors")))
        sc = scores
        if sc is None:
            sc = monitors
        if sc is None:
            sc = links
        if sc is None:
            sc = alarm_states
        if sc is None:
            sc = monitor_scores
        if sc is None:
            sc = state
        if sc is None:
            for v in kwargs.values():
                if isinstance(v, dict):
                    sc = v
                    break
        mle = MonitorLinkExpression(expr, scores=sc)
        return mle.evaluate()

    def evaluate_expression(self, *a, **kw):
        return self.evaluate(*a, **kw)

    def all(self):
        return list(self.triggers)

    def count(self):
        return len(self.triggers)

    def clear(self):
        self.triggers = []
        self.links = self.triggers
        return True

    def to_list(self):
        return [t.to_dict() for t in self.triggers]

    def __len__(self):
        return len(self.triggers)

    def __iter__(self):
        return iter(self.triggers)


LinkedMonitorTriggers = LinkedMonitorTrigger
LinkedMonitor = LinkedMonitorTrigger
LinkedMonitors = LinkedMonitorTrigger
MonitorLink = LinkedMonitorTrigger
MonitorLinks = LinkedMonitorTrigger
MonitorTrigger = LinkedMonitorTrigger
MonitorTriggers = LinkedMonitorTrigger
TriggerLink = LinkedMonitorTrigger
TriggerLinks = LinkedMonitorTrigger
LinkedMonitorTriggerManager = LinkedMonitorTriggerService
LinkedMonitorTriggerEngine = LinkedMonitorTriggerService
LinkedMonitorManager = LinkedMonitorTriggerService
LinkedMonitorEngine = LinkedMonitorTriggerService
MonitorLinkService = LinkedMonitorTriggerService
MonitorLinkManager = LinkedMonitorTriggerService
MonitorTriggerService = LinkedMonitorTriggerService

linked_monitor_trigger_service = LinkedMonitorTriggerService()
linked_monitor_trigger = linked_monitor_trigger_service
monitor_link_service = linked_monitor_trigger_service
monitor_trigger_service = linked_monitor_trigger_service
service = linked_monitor_trigger_service


async def create_linked_monitor_trigger(db, *args, **kwargs):
    source = None
    target = None
    if len(args) >= 1:
        source = args[0]
    if len(args) >= 2:
        target = args[1]
    if len(args) == 1 and isinstance(args[0], dict):
        kwargs = {**args[0], **kwargs}
        source = None
        target = None
    src_kw = _extract_from_mapping(kwargs, SOURCE_ALIASES)
    tgt_kw = _extract_from_mapping(kwargs, TARGET_ALIASES)
    if src_kw is not None:
        source = src_kw
    if tgt_kw is not None:
        target = tgt_kw
    source_id = _coerce_id(source)
    target_id = _coerce_id(target)
    expression = kwargs.get("expression", kwargs.get("trigger_expression", kwargs.get("linked_monitors")))
    enabled = _coerce_bool(kwargs.get("enabled", kwargs.get("active", kwargs.get("is_enabled", True))), True)

    row = LinkedMonitorTriggerModel(
        source_camera_id=source_id,
        target_camera_id=target_id,
        monitor_id=source_id,
        linked_monitor_id=target_id,
        trigger_camera_id=source_id,
        linked_camera_id=target_id,
        camera_id=source_id,
        source_id=source_id,
        target_id=target_id,
        expression=expression,
        linked_monitors=expression if isinstance(expression, str) else (json.dumps(expression) if expression is not None else None),
        trigger_expression=expression if isinstance(expression, str) else (json.dumps(expression) if expression is not None else None),
        enabled=enabled,
        active=enabled,
        is_enabled=enabled,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def create_linked_monitor(db, *a, **kw):
    return await create_linked_monitor_trigger(db, *a, **kw)

async def add_linked_monitor_trigger(db, *a, **kw):
    return await create_linked_monitor_trigger(db, *a, **kw)

async def link_monitors(db, *a, **kw):
    return await create_linked_monitor_trigger(db, *a, **kw)

async def add_link(db, *a, **kw):
    return await create_linked_monitor_trigger(db, *a, **kw)


async def list_linked_monitor_triggers(db, *args, **kwargs):
    from sqlalchemy import select as _select
    stmt = _select(LinkedMonitorTriggerModel).order_by(LinkedMonitorTriggerModel.id)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    source_filter = None
    target_filter = None
    if len(args) >= 1 and not isinstance(args[0], dict):
        source_filter = _coerce_id(args[0])
    sf = _extract_from_mapping(kwargs, SOURCE_ALIASES)
    tf = _extract_from_mapping(kwargs, TARGET_ALIASES)
    if sf is not None:
        source_filter = _coerce_id(sf)
    if tf is not None:
        target_filter = _coerce_id(tf)
    if source_filter is not None:
        rows = [r for r in rows if _coerce_id(getattr(r, "source_camera_id", None)) == source_filter or _coerce_id(getattr(r, "monitor_id", None)) == source_filter or _coerce_id(getattr(r, "camera_id", None)) == source_filter]
    if target_filter is not None:
        rows = [r for r in rows if _coerce_id(getattr(r, "target_camera_id", None)) == target_filter or _coerce_id(getattr(r, "linked_monitor_id", None)) == target_filter or _coerce_id(getattr(r, "target_id", None)) == target_filter]
    return rows


async def list_links(db, *a, **kw):
    return await list_linked_monitor_triggers(db, *a, **kw)

async def get_linked_monitor_triggers(db, *a, **kw):
    return await list_linked_monitor_triggers(db, *a, **kw)

async def get_links(db, camera_id=None, monitor_id=None, **kw):
    cid = camera_id if camera_id is not None else monitor_id
    if cid is None:
        cid = _extract_from_mapping(kw, SOURCE_ALIASES)
    return await list_linked_monitor_triggers(db, source_camera_id=cid)


async def get_linked_monitor_trigger(db, trigger_id):
    tid = _coerce_id(trigger_id)
    try:
        row = await db.get(LinkedMonitorTriggerModel, tid)
        if row is not None:
            return row
    except Exception:
        pass
    from sqlalchemy import select as _select
    result = await db.execute(_select(LinkedMonitorTriggerModel).where(LinkedMonitorTriggerModel.id == tid))
    return result.scalars().first()


async def delete_linked_monitor_trigger(db, trigger_id):
    row = await get_linked_monitor_trigger(db, trigger_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def delete_link(db, *a, **kw):
    tid = None
    if len(a) >= 1:
        tid = a[0]
    tid_kw = _extract_from_mapping(kw, ("id", "trigger_id", "link_id"))
    if tid_kw is not None:
        tid = tid_kw
    if tid is not None:
        return await delete_linked_monitor_trigger(db, tid)
    rows = await list_linked_monitor_triggers(db, **kw)
    deleted = False
    for r in rows:
        tf = _extract_from_mapping(kw, TARGET_ALIASES)
        if tf is not None and _coerce_id(getattr(r, "target_camera_id", None)) != _coerce_id(tf) and _coerce_id(getattr(r, "linked_monitor_id", None)) != _coerce_id(tf):
            continue
        await db.delete(r)
        deleted = True
    if deleted:
        await db.commit()
    return deleted


async def unlink_monitors(db, *a, **kw):
    return await delete_link(db, *a, **kw)

async def remove_linked_monitor_trigger(db, *a, **kw):
    return await delete_link(db, *a, **kw)


async def trigger_linked_monitors(db, camera_id, **kwargs):
    rows = await list_linked_monitor_triggers(db, source_camera_id=camera_id)
    linked_ids = []
    for r in rows:
        enabled = True
        for a in ("enabled", "active", "is_enabled"):
            v = getattr(r, a, None)
            if v is not None:
                enabled = _coerce_bool(v, True)
                break
        if not enabled:
            continue
        tid = getattr(r, "target_camera_id", None)
        if tid is None:
            for a in TARGET_ALIASES:
                v = getattr(r, a, None)
                if v is not None:
                    tid = v
                    break
        if tid is not None:
            linked_ids.append(_coerce_id(tid))
    return linked_ids


async def fire_linked_monitors(db, *a, **kw):
    return await trigger_linked_monitors(db, *a, **kw)

async def run_linked_monitors(db, *a, **kw):
    return await trigger_linked_monitors(db, *a, **kw)


async def get_camera_linked_monitors(db, camera_id, **kw):
    rows = await list_linked_monitor_triggers(db, source_camera_id=camera_id)
    ids = []
    for r in rows:
        tid = getattr(r, "target_camera_id", None)
        if tid is None:
            for a in TARGET_ALIASES:
                v = getattr(r, a, None)
                if v is not None:
                    tid = v
                    break
        if tid is not None:
            ids.append(_coerce_id(tid))
    cams = []
    for cid in ids:
        try:
            cam = await db.get(Camera, cid)
            if cam is not None:
                cams.append(cam)
            else:
                cams.append({"id": cid})
        except Exception:
            cams.append({"id": cid})
    return cams


async def get_linked_cameras(db, *a, **kw):
    return await get_camera_linked_monitors(db, *a, **kw)
