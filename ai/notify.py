"""Version 7: phone notifications through ntfy or a Telegram bot. Off until you turn them on in rules.yaml:

    notify:
      enabled: true
      service: ntfy
      ntfy_topic: my-long-private-topic-name     # then subscribe to it in the ntfy app

Only events with importance I ≥ 8 (a new castle level, an attack, war, a fall…) and the Chronicle's
headline at 07:00 are sent, at most 10 a day. Sending runs on its own thread: a slow network never
holds up the world.
"""
from __future__ import annotations

import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .chronicle import importance


class Notifier:
    def __init__(self, cfg: dict, sender: Callable[[str, str], None] | None = None):
        self.cfg = cfg or {}
        self.sent_on: dict[str, int] = {}                 # date → messages sent that day
        self._send = sender or self._http
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify") if sender is None else None

    @property
    def enabled(self) -> bool:
        c = self.cfg
        if not c.get("enabled"):
            return False
        if c.get("service", "ntfy") == "telegram":
            return bool(c.get("telegram_token") and c.get("telegram_chat_id"))
        return bool(c.get("ntfy_topic"))

    def event(self, ev, date: str, kingdom: str | None = None) -> bool:
        """A journal event; sent if it is big enough and today's limit isn't reached."""
        if not self.enabled or importance(ev.kind, ev.text) < float(self.cfg.get("min_importance", 8)):
            return False
        return self.send(f"Age of AI — {kingdom or 'the world'}", ev.text, date)

    def chronicle(self, headline: str, date: str) -> bool:
        if not self.enabled or not self.cfg.get("chronicle", True):
            return False
        return self.send("The Chronicle of the Kingdoms", headline, date)

    def send(self, title: str, text: str, date: str) -> bool:
        if self.sent_on.get(date, 0) >= int(self.cfg.get("max_per_day", 10)):
            return False
        self.sent_on = {date: self.sent_on.get(date, 0) + 1}
        if self._pool is not None:
            self._pool.submit(self._safe, title, text)
        else:
            self._send(title, text)
        return True

    def _safe(self, title: str, text: str) -> None:
        try:
            self._send(title, text)
        except Exception:                                  # a phone that can't be reached never stops the world
            pass

    def _http(self, title: str, text: str) -> None:
        c = self.cfg
        if c.get("service", "ntfy") == "telegram":
            url = f"https://api.telegram.org/bot{c['telegram_token']}/sendMessage"
            body = urllib.parse.urlencode({"chat_id": c["telegram_chat_id"], "text": f"{title}\n{text}"}).encode()
            req = urllib.request.Request(url, data=body)
        else:
            url = f"{str(c.get('ntfy_url', 'https://ntfy.sh')).rstrip('/')}/{urllib.parse.quote(str(c['ntfy_topic']))}"
            req = urllib.request.Request(url, data=text.encode("utf-8"),
                                         headers={"Title": title.encode("ascii", "ignore").decode() or "Age of AI"})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
