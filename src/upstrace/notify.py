"""Slack notifications for Upstrace drift runs.

Uses stdlib urllib so the package stays dependency-free for alerting.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Sequence

# Same vocabulary and ordering as cli.drift: lower number = more severe.
SEVERITY_ORDER = {"critical": 0, "high": 1, "warning": 2}
SEVERITY_ICON = {"critical": "🔴", "high": "🟠", "warning": "🟡"}
MAX_ROWS = 6
TIMEOUT = 10


class SlackError(RuntimeError):
    """Raised when Slack rejects the payload or is unreachable."""


def resolve_webhook(explicit: str | None = None) -> str | None:
    """CLI flag wins, then UPSTRACE_SLACK_WEBHOOK, else None."""
    url = (explicit or os.getenv("UPSTRACE_SLACK_WEBHOOK") or "").strip()
    return url or None


def rank(severity: str | None) -> int:
    return SEVERITY_ORDER.get((severity or "warning").lower(), 99)


def meets(severity: str | None, threshold: str) -> bool:
    """True when `severity` is at or above `threshold`."""
    return rank(severity) <= rank(threshold)


def _number(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000 and float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _value(text: str | None, number: float | None) -> str:
    return text if text else _number(number)


def _change(signal: Any) -> str:
    if signal.metric in ("min_value", "max_value"):
        return "changed"
    return f"{signal.change:.1%}"


def _row(signal: Any) -> str:
    icon = SEVERITY_ICON.get(signal.severity, "🟡")
    left = _value(getattr(signal, "baseline_text", None), signal.baseline)
    right = _value(getattr(signal, "current_text", None), signal.current)
    head = f"{icon}  *{signal.model_name}* · `{signal.column_name}` · {signal.metric}"
    body = f"{left} → {right}   ({_change(signal)})"

    tail = []
    partitions = getattr(signal, "partitions", 0) or 0
    if partitions:
        tail.append(f"{partitions} day{'s' if partitions != 1 else ''}")
    else:
        tail.append("whole table")
    first = getattr(signal, "first_partition", None)
    if first:
        tail.append(f"since {first}")
    if tail:
        body += "   ·   " + " · ".join(tail)

    return f"{head}\n{body}"


def _incident_line(incident: Any) -> str:
    kind = "source" if incident.is_source else "model"
    columns = ", ".join(f"`{c}`" for c in incident.columns)
    blast = ""
    if incident.blast_radius:
        blast = f"  →  {len(incident.blast_radius)} downstream: {', '.join(incident.blast_radius)}"
    return f"*{incident.root}* ({kind}) · {columns}{blast}"

def build_message(
    signals: Sequence[Any],
    *,
    project: str,
    baseline_mode: str = "previous_run",
    threshold: str = "warning",
    report_url: str | None = None,
    run_label: str | None = None,
    incidents: Sequence[Any] = (),
) -> dict | None:
    """Block Kit payload, or None when nothing meets the threshold."""
    hits = [s for s in signals if meets(s.severity, threshold)]
    if not hits:
        return None

    roots = {i.root for i in incidents}
    hits = sorted(hits, key=lambda s: (s.model_name not in roots, rank(s.severity), -abs(s.change or 0.0)))
    worst = hits[0].severity
    models = sorted({s.model_name for s in hits})
    icon = SEVERITY_ICON.get(worst, "🔵")

    noun = "signal" if len(hits) == 1 else "signals"
    if incidents:
        cause = "root cause" if len(incidents) == 1 else "root causes"
        headline = f"{icon} Upstrace: {len(incidents)} {cause}, {len(hits)} {noun}"
    else:
        where = models[0] if len(models) == 1 else f"{len(models)} models"
        headline = f"{icon} Upstrace: {len(hits)} {noun} on {where}"

    context = f"*{project}*  ·  baseline `{baseline_mode}`  ·  threshold `{threshold}`"
    if run_label:
        context += f"  ·  {run_label}"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150], "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
        {"type": "divider"},
    ]
    
    if incidents:
        lines = "\n".join(f"•  {_incident_line(i)}" for i in incidents[:3])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause*\n{lines}"}})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Evidence*"}})

    for signal in hits[:MAX_ROWS]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _row(signal)}})

    if len(hits) > MAX_ROWS:
        rest = len(hits) - MAX_ROWS
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_+{rest} more signal(s) not shown_"}]}
        )

    if report_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open report", "emoji": True},
                        "url": report_url,
                        "style": "primary" if worst == "critical" else "default",
                    }
                ],
            }
        )

    return {"text": headline, "blocks": blocks}


_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_RESOLVED = False


def _ssl_context() -> ssl.SSLContext | None:
    """Python on macOS ships without a usable CA bundle for urllib.

    certifi arrives with almost every data stack, so prefer its bundle and
    fall back to the interpreter default when it is absent.
    """
    global _SSL_CONTEXT, _SSL_RESOLVED
    if _SSL_RESOLVED:
        return _SSL_CONTEXT
    _SSL_RESOLVED = True
    try:
        import certifi
    except ImportError:
        _SSL_CONTEXT = None
    else:
        _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return _SSL_CONTEXT

def post(webhook: str, payload: dict, timeout: int = TIMEOUT) -> str:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            body = response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise SlackError(f"Slack returned {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(exc.reason):
            hint = (
                " - your Python has no usable CA bundle. Install certifi "
                "(pip install certifi), or on macOS run "
                "'/Applications/Python 3.x/Install Certificates.command'."
            )
        raise SlackError(f"could not reach Slack: {exc.reason}{hint}") from exc
    except OSError as exc:  # socket timeouts etc.
        raise SlackError(f"could not reach Slack: {exc}") from exc

    if body.lower() != "ok":
        raise SlackError(f"Slack returned: {body or '(empty body)'}")
    return body


def notify(
    signals: Sequence[Any],
    *,
    webhook: str,
    project: str,
    baseline_mode: str = "previous_run",
    threshold: str = "warning",
    report_url: str | None = None,
    run_label: str | None = None,
    incidents: Sequence[Any] = (),
) -> bool:
    """Post to Slack. Returns False when there was nothing worth sending."""
    payload = build_message(
        signals,
        project=project,
        baseline_mode=baseline_mode,
        threshold=threshold,
        report_url=report_url,
        run_label=run_label,
        incidents=incidents,
    )
    if payload is None:
        return False
    post(webhook, payload)
    return True