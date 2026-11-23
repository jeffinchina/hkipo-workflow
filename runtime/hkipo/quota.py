"""Read account quota without a model call or exposing authentication material."""
import json
import math
import os
import queue
import shutil
import subprocess
import threading
import time


class QuotaUnavailable(RuntimeError):
    pass


class QuotaDeferred(QuotaUnavailable):
    def __init__(self, observation):
        super().__init__("Verified live quota window requires deferral")
        self.observation = observation


def quota_deferral(observation, now=None):
    """Only fresh, bounded observations may release a job for a timed retry."""
    now = time.time() if now is None else now
    if not isinstance(observation, dict):
        return None
    observed = observation.get('observed_at')
    reset = observation.get('blocking_resets_at')
    numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if (observation.get('allowed') is not False or observation.get('limit_id') != 'codex'
            or observation.get('defer_reason') not in {'quota_reserve', 'quota_window_exhausted'}
            or not numeric(observed) or not 0 <= now-observed <= 60
            or not numeric(reset) or reset <= now):
        return None
    windows = observation.get('blocking_windows')
    reserve = observation.get('reserve')
    if not isinstance(windows, list) or not windows or not numeric(reserve) or not 0 <= reserve <= 1:
        return None
    for window in windows:
        if not isinstance(window, dict):
            return None
        used, duration, deadline = (window.get(key) for key in ('usedPercent', 'windowDurationMins', 'resetsAt'))
        if (not all(numeric(value) for value in (used, duration, deadline))
                or not 0 <= used <= 100 or deadline <= now
                or not ((duration == 300 and (100-used)/100 <= reserve)
                        or (duration > 300 and used == 100)
                        or (duration == 10080 and observation.get('window_kind') == 'weekly'
                            and observation.get('window_duration_mins') == 10080
                            and (100-used)/100 <= reserve))):
            return None
    if max(window['resetsAt'] for window in windows) != reset:
        return None
    return {'reason': observation['defer_reason'], 'retry_at': reset + 120,
            'observation': observation}


def read_limits(timeout=25, executable=None, settings_path=None):
    from .network import child_environment, resolve_cli
    settings_path = settings_path or os.environ.get("HKIPO_SETTINGS")
    binary = executable or (resolve_cli(settings_path) if settings_path else shutil.which("codex"))
    env = child_environment(settings_path) if settings_path else None
    if not binary:
        raise QuotaUnavailable("Codex CLI is unavailable")
    proc = subprocess.Popen([binary, "app-server", "--stdio"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    messages = queue.Queue()

    def reader():
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
        messages.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(payload):
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def receive(request_id):
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                raise QuotaUnavailable("Timed out reading live quota")
            if message is None:
                raise QuotaUnavailable("Quota transport closed")
            if message.get("id") == request_id:
                if "error" in message:
                    raise QuotaUnavailable("Quota request failed")
                return message.get("result", {})

    try:
        send({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "hkipo-local", "version": "0.1.0"}}})
        receive(0)
        send({"method": "initialized", "params": {}})
        send({"id": 1, "method": "account/rateLimits/read"})
        return receive(1)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in [proc.stdin, proc.stdout]:
            stream.close()


def evaluate_limits(limits, reserve=.2, limit_id="codex", now=None):
    now = time.time() if now is None else now
    buckets = limits.get("rateLimitsByLimitId")
    bucket = buckets.get(limit_id) if buckets else limits.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") != limit_id:
        raise QuotaUnavailable("Applicable quota bucket was not returned")
    windows = [w for w in bucket.values() if isinstance(w, dict)
               and isinstance(w.get("windowDurationMins"), (int, float))]
    five_hour = [w for w in windows if w["windowDurationMins"] == 300]
    weekly_only = (not five_hour and limit_id == "codex"
                   and bucket.get("planType") == "prolite"
                   and limits.get("ordinaryUsageAllowed") is True
                   and isinstance(bucket.get("primary"), dict)
                   and bucket["primary"].get("windowDurationMins") == 10080
                   and bucket.get("secondary") is None
                   and len(windows) == 1)
    if not five_hour and not weekly_only:
        raise QuotaUnavailable("No live five-hour window")
    # A weekly-only plan must describe its real current week, not an
    # arbitrary future deadline masquerading as a fresh observation.
    active_windows = windows if weekly_only else five_hour
    window_kind = "weekly" if weekly_only else "five_hour"
    if any(isinstance(w.get('windowDurationMins'), bool)
           or not math.isfinite(w['windowDurationMins']) or w['windowDurationMins'] <= 0
           or isinstance(w.get('usedPercent'), bool)
           or not isinstance(w.get('usedPercent'), (int, float))
           or not math.isfinite(w['usedPercent']) or not 0 <= w['usedPercent'] <= 100
           or isinstance(w.get('resetsAt'), bool)
           or not isinstance(w.get('resetsAt'), (int, float))
           or not math.isfinite(w['resetsAt']) or w['resetsAt'] <= now for w in windows):
        raise QuotaUnavailable("Unverifiable quota window")
    if any(not isinstance(w.get("usedPercent"), (int, float))
           or not math.isfinite(w['usedPercent']) or not 0 <= w['usedPercent'] <= 100
           or not isinstance(w.get("resetsAt"), (int, float))
           or not math.isfinite(w['resetsAt'])
           or w["resetsAt"] <= now for w in five_hour):
        raise QuotaUnavailable("Missing or expired five-hour quota observation")
    if weekly_only and windows[0]["resetsAt"] > now + 10080 * 60:
        raise QuotaUnavailable("Weekly reset is outside the current window")
    remaining = min(max(0, min(100, 100 - w["usedPercent"])) / 100 for w in active_windows)
    hard = bool(bucket.get("spendControlReached") or bucket.get("rateLimitReachedType"))
    hard = hard or any(w["windowDurationMins"] > 300
                       and isinstance(w.get("usedPercent"), (int, float))
                       and w["usedPercent"] >= 100 for w in windows)
    crossed = [w for w in active_windows if (100 - w["usedPercent"]) / 100 <= reserve]
    exhausted = [w for w in windows if w["windowDurationMins"] > 300
                 and isinstance(w.get('usedPercent'), (int, float)) and w['usedPercent'] >= 100]
    blocking = crossed + [w for w in exhausted if w not in crossed]
    # Account/spend flags do not identify a verified recovery window. Never
    # pretend the five-hour reset clears an unknown hard limit.
    known_reset = (max(w['resetsAt'] for w in blocking)
                   if blocking and not bucket.get('spendControlReached')
                   and not bucket.get('rateLimitReachedType')
                   and all(isinstance(w.get('resetsAt'), (int, float))
                           and math.isfinite(w['resetsAt']) and w['resetsAt'] > now for w in blocking)
                   else None)
    reset = known_reset or (max(w["resetsAt"] for w in crossed) if crossed else min(w["resetsAt"] for w in active_windows))
    return {"allowed": remaining > reserve and not hard, "remaining": remaining,
            "reserve": reserve, "hard_limit": hard, "resets_at": reset,
            "blocking_resets_at": known_reset,
            "blocking_windows": blocking if known_reset else [],
            "defer_reason": ('quota_window_exhausted' if exhausted else 'quota_reserve') if known_reset else None,
            "observed_at": now, "limit_id": limit_id,
            "window_kind": window_kind,
            "window_duration_mins": 10080 if weekly_only else 300}


def quota_gate(reserve=.2, settings_path=None):
    last_error = None
    for _ in range(2):
        try:
            result = evaluate_limits(read_limits(settings_path=settings_path), reserve)
            if not result["allowed"]:
                if quota_deferral(result):
                    raise QuotaDeferred(result)
                raise QuotaUnavailable("Hard limit or reserved account quota reached")
            return result
        except QuotaDeferred:
            raise
        except QuotaUnavailable as exc:
            last_error = exc
    raise last_error
