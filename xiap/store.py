"""Tiny persistent key-value store for bot settings (JSON file on disk)."""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PATH = Path(os.getenv("DATA_FILE", "data.json"))


class Store:
    def __init__(self) -> None:
        self.context_limit: dict[int, int] = {}  # channel id -> max history messages (unset = default)
        self.optout: set[int] = set()  # user ids excluded from AI context
        self.paper_subs: dict[int, str] = {}  # channel id -> topic filter ("" = all)
        self.model_pick: dict[int, str] = {}  # user id -> "provider:model" for their AI replies
        try:
            data = json.loads(PATH.read_text())
            self.context_limit = {int(k): v for k, v in data.get("context_limit", {}).items()}
            # Migrate the old on/off format: "off" channels become a limit of 0.
            for channel_id in data.get("context_off", []):
                self.context_limit.setdefault(int(channel_id), 0)
            self.optout = set(data.get("optout", []))
            self.paper_subs = {int(k): v for k, v in data.get("paper_subs", {}).items()}
            self.model_pick = {int(k): v for k, v in data.get("model_pick", {}).items()}
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Failed to load %s, starting with defaults", PATH)

    def _save(self) -> None:
        PATH.write_text(
            json.dumps({
                "context_limit": {str(k): v for k, v in self.context_limit.items()},
                "optout": sorted(self.optout),
                "paper_subs": {str(k): v for k, v in self.paper_subs.items()},
                "model_pick": {str(k): v for k, v in self.model_pick.items()},
            })
        )

    def set_paper_sub(self, channel_id: int, topic: str) -> None:
        self.paper_subs[channel_id] = topic
        self._save()

    def remove_paper_sub(self, channel_id: int) -> bool:
        existed = self.paper_subs.pop(channel_id, None) is not None
        self._save()
        return existed

    def set_model_pick(self, user_id: int, pick: str | None) -> None:
        if pick:
            self.model_pick[user_id] = pick
        else:
            self.model_pick.pop(user_id, None)
        self._save()

    def get_model_pick(self, user_id: int) -> str | None:
        return self.model_pick.get(user_id)

    def set_context_limit(self, channel_id: int, limit: int) -> None:
        self.context_limit[channel_id] = limit
        self._save()

    def get_context_limit(self, channel_id: int, default: int) -> int:
        return self.context_limit.get(channel_id, default)

    def set_optout(self, user_id: int, opted_out: bool) -> None:
        (self.optout.add if opted_out else self.optout.discard)(user_id)
        self._save()

    def is_opted_out(self, user_id: int) -> bool:
        return user_id in self.optout


store = Store()
