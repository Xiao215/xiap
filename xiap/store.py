"""Tiny persistent key-value store for bot settings (JSON file on disk)."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PATH = Path(os.getenv("DATA_FILE", "data.json"))


class Store:
    def __init__(self) -> None:
        self.context_off: set[int] = set()  # channel ids with history reading disabled
        self.optout: set[int] = set()  # user ids excluded from AI context
        self.paper_subs: dict[int, str] = {}  # channel id -> topic filter ("" = all)
        try:
            data = json.loads(PATH.read_text())
            self.context_off = set(data.get("context_off", []))
            self.optout = set(data.get("optout", []))
            self.paper_subs = {int(k): v for k, v in data.get("paper_subs", {}).items()}
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Failed to load %s, starting with defaults", PATH)

    def _save(self) -> None:
        PATH.write_text(
            json.dumps({
                "context_off": sorted(self.context_off),
                "optout": sorted(self.optout),
                "paper_subs": {str(k): v for k, v in self.paper_subs.items()},
            })
        )

    def set_paper_sub(self, channel_id: int, topic: str) -> None:
        self.paper_subs[channel_id] = topic
        self._save()

    def remove_paper_sub(self, channel_id: int) -> bool:
        existed = self.paper_subs.pop(channel_id, None) is not None
        self._save()
        return existed

    def set_context(self, channel_id: int, enabled: bool) -> None:
        (self.context_off.discard if enabled else self.context_off.add)(channel_id)
        self._save()

    def context_enabled(self, channel_id: int) -> bool:
        return channel_id not in self.context_off

    def set_optout(self, user_id: int, opted_out: bool) -> None:
        (self.optout.add if opted_out else self.optout.discard)(user_id)
        self._save()

    def is_opted_out(self, user_id: int) -> bool:
        return user_id in self.optout


store = Store()
