"""Private, bounded operation receipts. Call only under ConfigStore.transition.

An intent is durable before the effect. A pending intent is never executed again:
we cannot infer whether a child restart or token change completed before a crash.
This is a single-writer journal, matching the product's single admin process.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

from fastapi import HTTPException

TTL_MS = 24 * 60 * 60 * 1000
MAX_RECEIPTS = 128
MAX_BYTES = 262144
ID_PATTERN = re.compile(r"[0-9]{13}\.[A-Za-z0-9_-]{16,64}\Z")
SAFE_RESULT_KEYS = frozenset({"status", "token_state", "child_state", "saved", "apply_state"})
RESULT_VALUES = {
    "status": {"ok", "error", "partial"}, "token_state": {"applied", "revoked"},
    "child_state": {"unchanged"},
    "apply_state": {"applied", "pending", "failed", "unconfigured", "unknown"},
}


def fail(status, code):
    raise HTTPException(status, detail={"code": code})


class OperationJournal:
    def __init__(self, config_path: Path):
        self.path = config_path.with_name("." + config_path.name + ".operations.json")

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"version": 1, "key": secrets.token_hex(32), "expired_before_ms": 0, "receipts": {}}
        try:
            before = os.fstat(fd)
            visible = self.path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid()
                    or (before.st_dev, before.st_ino) != (visible.st_dev, visible.st_ino)
                    or before.st_size > MAX_BYTES):
                raise ValueError("Unsafe operation journal")
            # kubelet fsGroup can widen an owned regular file on remount.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", closefd=False) as stream:
                data = json.load(stream)
            if (not isinstance(data, dict)
                    or set(data) != {"version", "key", "expired_before_ms", "receipts"}
                    or data.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{64}", data["key"])
                    or type(data["expired_before_ms"]) is not int
                    or data["expired_before_ms"] < 0
                    or not isinstance(data["receipts"], dict) or len(data["receipts"]) > MAX_RECEIPTS):
                raise ValueError("Invalid operation journal")
            for key, item in data["receipts"].items():
                if (not isinstance(item, dict) or not ID_PATTERN.fullmatch(key)
                        or set(item) - {"state", "fingerprint", "kind", "result"}
                        or item["state"] not in {"pending", "completed"}
                        or not re.fullmatch(r"[0-9a-f]{64}", item["fingerprint"])
                        or item["kind"] not in {"token.rotate", "token.set", "token.revoke", "mcp.restart"}
                        or not isinstance(item.get("result", {}), dict)
                        or set(item.get("result", {})) - SAFE_RESULT_KEYS):
                    raise ValueError("Invalid operation receipt")
                if item["state"] == "completed" and "result" not in item:
                    raise ValueError("Incomplete operation receipt")
                for field, value in item.get("result", {}).items():
                    if field == "saved":
                        if type(value) is not bool:
                            raise ValueError("Invalid operation result")
                    elif not isinstance(value, str) or value not in RESULT_VALUES[field]:
                        raise ValueError("Invalid operation result")
            return data
        finally:
            os.close(fd)

    def _write(self, data):
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_BYTES:
            raise ValueError("Operation journal capacity exceeded")
        tmp = self.path.with_name(self.path.name + ".tmp." + secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            tmp.unlink(missing_ok=True)

    def _load(self, operation_id):
        now = int(time.time() * 1000)
        if not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id):
            fail(400, "operation_id_invalid")
        timestamp = int(operation_id.split(".", 1)[0])
        if timestamp > now:
            fail(400, "operation_id_invalid")
        try:
            data = self._read()
        except (OSError, ValueError, KeyError, TypeError):
            fail(503, "operation_journal_unavailable")
        floor = max(data["expired_before_ms"], now - TTL_MS)
        if timestamp <= floor:
            fail(410, "operation_id_expired")
        data["expired_before_ms"] = floor
        data["receipts"] = {key: item for key, item in data["receipts"].items()
                            if int(key.split(".", 1)[0]) > floor}
        return data

    @staticmethod
    def public(operation_id, item, *, replayed=False):
        return {"id": operation_id, "kind": item["kind"],
                "state": "completed" if item["state"] == "completed" else "unknown",
                "replayed": replayed}

    def inspect(self, operation_id, kind, payload):
        data = self._load(operation_id)
        raw = json.dumps([kind, payload], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        fingerprint = hmac.new(bytes.fromhex(data["key"]), raw, hashlib.sha256).hexdigest()
        item = data["receipts"].get(operation_id)
        if item and not hmac.compare_digest(item["fingerprint"], fingerprint):
            fail(409, "operation_id_conflict")
        return data, item, fingerprint

    def begin(self, operation_id, data, kind, fingerprint):
        if len(data["receipts"]) >= MAX_RECEIPTS:
            fail(503, "operation_journal_full")
        data["receipts"][operation_id] = {"kind": kind, "state": "pending", "fingerprint": fingerprint}
        try:
            self._write(data)
        except (OSError, ValueError):
            fail(503, "operation_journal_unavailable")

    def complete(self, operation_id, data, result):
        item = data["receipts"][operation_id]
        item.update(state="completed", result={key: value for key, value in result.items() if key in SAFE_RESULT_KEYS})
        self._write(data)
        return self.public(operation_id, item)

    def query(self, operation_id):
        data = self._load(operation_id)
        item = data["receipts"].get(operation_id)
        if not item:
            fail(404, "operation_not_found")
        return {"operation": self.public(operation_id, item), "result": item.get("result", {})}
