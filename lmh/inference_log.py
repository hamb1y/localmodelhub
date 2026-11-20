"""Opt-in private capture of inference bodies, without request headers."""

import json
import os
import stat
import tempfile
import time
from pathlib import Path


class InferenceLog:
    def __init__(self, directory, model, route, request_type, request_body):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise OSError("Inference log directory must be private")
        self.directory = Path(tempfile.mkdtemp(prefix=f"{time.time_ns()}-", dir=directory))
        self.metadata = {"model": model, "route": route, "request_type": request_type, "complete": False}
        with self._create("input.body") as handle:
            handle.write(request_body)
        self.output = None

    def _create(self, name):
        path = self.directory / name
        return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb")

    def headers(self, status, media):
        self.metadata.update(status=status, response_type=media)

    def write(self, data):
        if self.output is None:
            self.output = self._create("output.body")
        self.output.write(data)

    def finish(self):
        try:
            if self.output:
                self.output.close()
        finally:
            with self._create("metadata.json") as handle:
                handle.write((json.dumps(self.metadata, indent=2) + "\n").encode())
