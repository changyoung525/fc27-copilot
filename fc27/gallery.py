import json
import os
import tempfile
import threading
import time

from .errors import FC27Error
from .runtime import utc_now


PAGE_RETRY_DELAYS = (2, 5)
TRANSIENT_PAGE_ERRORS = {
    "BRIDGE_NOT_CONNECTED", "BRIDGE_TIMEOUT", "CONTENT_BRIDGE_UNREACHABLE",
    "PAGE_BRIDGE_TIMEOUT", "EA_SERVICE_TIMEOUT",
}


class GallerySync:
    def __init__(self, browser_read):
        self.browser_read = browser_read
        self._lock = threading.Lock()
        self._job = {"state": "idle"}

    def start(self, runtime):
        with self._lock:
            if self._job["state"] == "running":
                if self._job["persona_id"] != runtime.persona_id:
                    raise FC27Error("GALLERY_SYNC_BUSY", "A Gallery sync is already running.")
                return dict(self._job)
            self._job = {"state": "running", "persona_id": runtime.persona_id, "scanned": 0}
            threading.Thread(target=self._sync, args=(runtime,), daemon=True).start()
            return dict(self._job)

    def query(self, runtime, offset, limit):
        with self._lock:
            job = dict(self._job)
        if runtime is None or job.get("persona_id") != runtime.persona_id:
            job = {"state": "idle"}
        if runtime is None:
            return self._empty_result(job)
        try:
            snapshot = json.loads(self._path(runtime).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_result(job)
        cards = snapshot["cards"]
        next_offset = offset + limit
        return {
            "sync": job,
            "snapshot_at": snapshot["snapshot_at"],
            "scanned": snapshot["scanned"],
            "total": len(cards),
            "cards": cards[offset:next_offset],
            "next_offset": next_offset if next_offset < len(cards) else None,
        }

    @staticmethod
    def _empty_result(job):
        return {"sync": job, "snapshot_at": None, "total": None, "cards": [], "next_offset": None}

    @staticmethod
    def _path(runtime):
        return runtime.path.with_name("gallery.json")

    def _sync(self, runtime):
        try:
            cards, scanned = self._read(runtime.persona_id)
            snapshot_at = utc_now()
            path = self._path(runtime)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as output:
                temporary_path = output.name
                try:
                    json.dump({"snapshot_at": snapshot_at, "scanned": scanned, "cards": cards}, output)
                    output.close()
                    os.replace(temporary_path, path)
                finally:
                    if os.path.exists(temporary_path):
                        os.unlink(temporary_path)
            outcome = {"state": "complete", "snapshot_at": snapshot_at, "total": len(cards)}
        except FC27Error as error:
            outcome = {"state": "failed", "error": error.as_dict()}
        except OSError:
            outcome = {"state": "failed", "error": FC27Error(
                "GALLERY_STORAGE_FAILED", "Could not write the local Gallery snapshot."
            ).as_dict()}
        with self._lock:
            self._job.update(outcome)

    def _read(self, persona_id):
        seen = set()
        collected = {}
        offset = 0
        has_collection_flags = False
        advancing_pages = 0
        repeated_page = False
        end_reason = "page_limit"
        for _ in range(500):
            page = self._read_page(persona_id, offset)
            rows = page.get("cards") if isinstance(page, dict) else None
            if not isinstance(rows, list):
                end_reason = "missing_cards"
                break
            if not rows:
                if has_collection_flags:
                    return [collected[card_id] for card_id in sorted(collected)], len(seen)
                end_reason = "missing_collection_flags"
                break
            previous_count = len(seen)
            for card in rows:
                card_id = card.get("card_ea_id") if isinstance(card, dict) else None
                if type(card_id) is not int or card_id <= 0:
                    raise FC27Error("GALLERY_RESPONSE_INVALID", "EA returned an invalid Gallery card ID.")
                seen.add(card_id)
                flag = card.get("is_collected")
                has_collection_flags |= type(flag) is bool
                if flag is True:
                    collected[card_id] = card
            if len(seen) == previous_count:
                if advancing_pages < 2:
                    end_reason = "pagination_did_not_advance"
                    break
                if repeated_page:
                    if has_collection_flags:
                        return [collected[card_id] for card_id in sorted(collected)], len(seen)
                    end_reason = "missing_collection_flags"
                    break
                # EA can replay known cards at the tail. Confirm with the next offset.
                repeated_page = True
            else:
                advancing_pages += 1
                repeated_page = False
            offset += len(rows)
            with self._lock:
                self._job.update(scanned=len(seen), collected=len(collected))
            time.sleep(0.9)
        raise FC27Error(
            "GALLERY_SCAN_INCOMPLETE",
            "EA did not provide a complete card scan with Gallery collection flags.",
            retryable=True,
            details={"offset": offset, "scanned": len(seen), "reason": end_reason},
        )

    def _read_page(self, persona_id, offset):
        params = {"persona_id": persona_id, "offset": offset}
        for delay in (*PAGE_RETRY_DELAYS, None):
            try:
                result = self.browser_read("getGalleryPage", params)
            except FC27Error as error:
                if delay is None or error.code not in TRANSIENT_PAGE_ERRORS:
                    raise
                with self._lock:
                    self._job["retrying_offset"] = offset
                time.sleep(delay)
            else:
                with self._lock:
                    self._job.pop("retrying_offset", None)
                return result
