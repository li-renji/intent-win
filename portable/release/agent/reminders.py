# -*- coding: utf-8 -*-
"""轻量定时提醒：后台线程到点把提醒推到当前对话。

- 持久化到 <project_root>/reminders.json（重启不丢）；
- when 支持：in 10m / in 2h / HH:MM（今天或明天）/ 2026-10-26 15:30；
- 到点通过注入的 push 回调调用前端 window.__onReminder(...)。
"""
import json
import os
import re
import threading
import time


class ReminderManager:
    def __init__(self, project_root, push=None):
        self.path = os.path.join(project_root, "reminders.json")
        self.push = push
        self.lock = threading.Lock()
        self.items = self._load()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _load(self):
        if os.path.exists(self.path):
            try:
                return json.load(open(self.path, encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    @staticmethod
    def parse_when(when):
        when = (when or "").strip()
        now = time.time()
        m = re.match(r"^in\s+(\d+)\s*(m|min|分钟|h|小时)$", when, re.I)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            return now + (n * 60 if unit.startswith("m") else n * 3600)
        m = re.match(r"^(\d{1,2}):(\d{2})$", when)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            t = time.localtime()
            target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi,
                                 0, 0, 0, 0))
            if target <= now:
                target += 86400
            return target
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
            try:
                return time.mktime(time.strptime(when, fmt))
            except ValueError:
                continue
        return None

    def add(self, text, when):
        at = self.parse_when(when)
        if at is None:
            return ("无法解析时间「%s」。支持：in 10m / in 2h / HH:MM（今天或明天）"
                    " / 2026-10-26 15:30" % when)
        item = {"id": int(time.time() * 1000) % 1000000,
                "text": text, "at": at, "done": False}
        with self.lock:
            self.items.append(item)
            self._save()
        return "已设提醒：%s（%s）" % (
            text, time.strftime("%Y-%m-%d %H:%M", time.localtime(at)))

    def list(self):
        with self.lock:
            pend = [i for i in self.items if not i.get("done")]
        if not pend:
            return "（无待办提醒）"
        lines = ["待办提醒："]
        for i in sorted(pend, key=lambda x: x["at"]):
            lines.append("  · %s — %s" % (
                time.strftime("%m-%d %H:%M", time.localtime(i["at"])), i["text"]))
        return "\n".join(lines)

    def clear(self):
        with self.lock:
            self.items = [i for i in self.items if i.get("done")]
            self._save()
        return "已清空待办提醒。"

    def _run(self):
        while not self._stop.is_set():
            try:
                now = time.time()
                due = []
                with self.lock:
                    for i in self.items:
                        if not i.get("done") and i["at"] <= now:
                            i["done"] = True
                            due.append(i)
                    if due:
                        self._save()
                for i in due:
                    if self.push:
                        try:
                            self.push('window.__onReminder(%s)',
                                      {"text": i["text"], "at": i["at"]})
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(5)

    def stop(self):
        self._stop.set()
