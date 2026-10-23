"""Opt-in local diagnostics; never writes to stdout or a fallback directory."""
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path
import threading
from time import perf_counter

MAX_LOG_BYTES = 200 * 1024
_level = None
_original_excepthook = sys.excepthook
_original_thread_hook = threading.excepthook


def _unhandled(kind, error, traceback):
    get_logger("unhandled").error("uncaught.exception", exc_info=(kind, error, traceback))
    _original_excepthook(kind, error, traceback)


def _thread_unhandled(args):
    get_logger("unhandled").error("uncaught.thread name=%s", args.thread.name,
                                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    _original_thread_hook(args)


class ByteRotatingFileHandler(RotatingFileHandler):
    """Measure encoded bytes, including tracebacks and very large messages."""

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors, newline="\n")

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        original = getattr(self, "_original_filename", self.baseFilename)
        self._original_filename = original
        sequence = getattr(self, "_sequence", 0) + 1
        while Path(f"{original}.{sequence}").exists():
            sequence += 1
        self._sequence = sequence
        self.baseFilename = f"{original}.{sequence}"
        self.stream = self._open()

    def emit(self, record):
        try:
            formatted = self.format(record)
            prefix = f"{datetime.fromtimestamp(record.created):%Y-%m-%d %H:%M:%S} {record.thread} {record.levelname} "
            for line in formatted.splitlines() or [""]:
                content = line if line.startswith(prefix) else prefix + line
                remaining = content.encode("utf-8")
                while remaining:
                    # Every physical line has its own timestamp/thread/level.
                    capacity = self.maxBytes - len(prefix.encode("utf-8")) - 1
                    fragment = remaining[:capacity].decode("utf-8", errors="ignore")
                    consumed = len(fragment.encode("utf-8"))
                    remaining = remaining[consumed:]
                    output = (fragment if fragment.startswith(prefix) else prefix + fragment) + "\n"
                    if self.stream is None:
                        self.stream = self._open()
                    if self.stream.tell() + len(output.encode("utf-8")) > self.maxBytes:
                        self.doRollover()
                    self.stream.write(output)
                    self.flush()
        except Exception:
            self.handleError(record)


def get_logger(name):
    return logging.getLogger("deskflow." + name)


def configure_logging(level=None, directory=None, role="overlay"):
    """Enable UTF-8 logs beside the installed application, or fail explicitly."""
    global _level
    logger = logging.getLogger("deskflow")
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    _level = level
    sys.excepthook = _unhandled if level else _original_excepthook
    threading.excepthook = _thread_unhandled if level else _original_thread_hook
    if level is None:
        logger.addHandler(logging.NullHandler())
        return None
    from app_runtime import APP_ROOT
    destination = Path(directory) if directory is not None else APP_ROOT
    filename = destination / f"{datetime.now():%Y-%m-%d}-{role}-{os.getpid()}.log"
    try:
        handler = ByteRotatingFileHandler(filename, maxBytes=MAX_LOG_BYTES, backupCount=0,
                                      encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"DESKFLOW cannot write diagnostic log {filename}: {error}") from error
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(thread)d %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.addLevelName(logging.DEBUG, "debug")
    logging.addLevelName(logging.INFO, "info")
    logging.addLevelName(logging.WARNING, "warn")
    logging.addLevelName(logging.ERROR, "error")
    logger.setLevel({"debug": logging.DEBUG, "info": logging.INFO,
                     "warn": logging.WARNING, "error": logging.ERROR}[level])
    logger.addHandler(handler)
    logger.info("diagnostics.started role=%s pid=%s thread=%s max_bytes=%s",
                role, os.getpid(), threading.get_ident(), MAX_LOG_BYTES)
    return filename


def logging_arguments():
    return ["--log-level", _level] if _level else []


@contextmanager
def timed_operation(label, logger=None):
    logger = logger or get_logger("timing")
    started = perf_counter()
    logger.debug("%s.begin", label)
    try:
        yield
    except Exception:
        logger.exception("%s.failed", label)
        raise
    finally:
        elapsed = (perf_counter() - started) * 1000
        (logger.warning if elapsed >= 50 else logger.debug)("%s.end elapsed_ms=%.3f", label, elapsed)


def traced(label):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with timed_operation(label):
                return function(*args, **kwargs)
        return wrapped
    return decorate


logging.getLogger("deskflow").addHandler(logging.NullHandler())
