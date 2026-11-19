"""Bounded per-scope scheduling and gateway validation (no bot import side effects)."""
import asyncio
import hmac
from functools import partial


def qq_id(value):
    text = str(value) if type(value) in (str, int) else ""
    return int(text) if text.isascii() and text.isdecimal() and 0 < len(text) <= 15 and int(text) else None


def token_matches(expected, supplied):
    return bool(expected and supplied and hmac.compare_digest(expected.encode(), supplied.encode()))


def authenticated(request, token):
    values = request.headers.get_all("Authorization")
    return len(values) == 1 and values[0].startswith("Bearer ") and token_matches(token, values[0][7:])


def message_event(raw, bot_qq=0):
    """Validate the routing fields; retain original OneBot fields for existing plugins."""
    if not isinstance(raw, dict) or raw.get("post_type") != "message":
        return None
    kind = raw.get("message_type")
    if kind not in ("private", "group"):
        return None
    uid = qq_id(raw.get("user_id"))
    gid = qq_id(raw.get("group_id")) if kind == "group" else None
    self_id = qq_id(raw.get("self_id"))
    if not uid or uid in (bot_qq, self_id) or (kind == "group" and not gid):
        return None
    if bot_qq and self_id != bot_qq:
        return None
    segments = raw.get("message")
    if not isinstance(segments, list) or len(segments) > 128:
        return None
    mid = raw.get("message_id")
    if type(mid) not in (int, str) or not str(mid) or len(str(mid)) > 64:
        return None
    event = {**raw, "user_id": uid, "message_id": mid}
    if gid:
        event["group_id"] = gid
    elif "group_id" in event:
        del event["group_id"]
    return event


class ScopeScheduler:
    """FIFO within each scope, concurrent across scopes; no idle worker retention."""

    def __init__(self, report, max_pending=20, max_scopes=128, max_total=256):
        self.report = report
        self.max_pending = max_pending
        self.max_scopes = max_scopes
        self.max_total = max_total
        self._outstanding = 0
        self.workers = {}
        self.waiters = set()

    def submit(self, scope, fn, *args):
        if self._outstanding >= self.max_total:
            return False
        if scope not in self.workers:
            if len(self.workers) >= self.max_scopes:
                return False
            queue = asyncio.Queue(self.max_pending)
            task = asyncio.create_task(self._worker(scope, queue))
            self.workers[scope] = queue, task
        queue, _ = self.workers[scope]
        try:
            queue.put_nowait(partial(fn, *args))
        except asyncio.QueueFull:
            return False
        self._outstanding += 1
        return True

    async def _worker(self, scope, queue):
        try:
            while not queue.empty():
                job = queue.get_nowait()
                try:
                    await job()
                except Exception as exc:
                    self.report(f"[队列] {scope}: {type(exc).__name__}: {exc}")
                finally:
                    queue.task_done()
                    self._outstanding -= 1
        finally:
            self._outstanding -= queue.qsize()
            self.workers.pop(scope, None)

    async def run(self, scope, fn):
        result = asyncio.get_running_loop().create_future()

        async def job():
            if result.done():
                return
            task = asyncio.create_task(fn())
            def cancel_child(future):
                if future.cancelled():
                    task.cancel()
            result.add_done_callback(cancel_child)
            try:
                value = await task
                if not result.done():
                    result.set_result(value)
            except asyncio.CancelledError:
                if not result.done():
                    result.cancel()
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)

        if not self.submit(scope, job):
            raise RuntimeError("会话队列已满")
        self.waiters.add(result)
        try:
            return await result
        finally:
            self.waiters.discard(result)

    async def close(self):
        tasks = [worker for _, worker in self.workers.values()]
        for pending in list(self.waiters):
            pending.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self._outstanding = 0
