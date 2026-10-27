"""Keeps email addresses out of server logs.

Discovery and auth messages are echoed to stdout/stderr, and database error
text can quote an address (`Key (email_key)=(...) already exists`). Rather than
auditing every print, `install()` masks addresses in everything written to the
process streams and in every logging record. The user's own live log keeps the
full address; only the server-side copy is masked.
"""

import logging
import re
import sys

EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
)


def redact(text):
    """o***@shop.com: the first character and domain stay for debugging."""
    if not isinstance(text, str) or "@" not in text:
        return text
    return EMAIL_PATTERN.sub(r"\1***@\2", text)


class RedactingStream:
    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        return self._stream.write(redact(text))

    def writelines(self, lines):
        return self._stream.writelines(redact(line) for line in lines)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(record.exc_text)
            record.stack_info = redact(record.stack_info)
        except Exception:
            pass  # A malformed record is left for logging's own error handling.
        return True


FILTER = RedactingFilter()


def _wrap(stream):
    if stream is None or isinstance(stream, RedactingStream):
        return stream
    return RedactingStream(stream)


def install():
    """Idempotent. Covers handlers that exist now (gunicorn configures its own
    before importing the app); handlers created later write to the wrapped
    streams."""
    sys.stdout = _wrap(sys.stdout)
    sys.stderr = _wrap(sys.stderr)
    loggers = [logging.getLogger()] + [
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    handlers = {handler for logger in loggers for handler in logger.handlers}
    try:
        from flask.logging import default_handler
        handlers.add(default_handler)
    except ImportError:
        pass
    for handler in handlers:
        if FILTER not in handler.filters:
            handler.addFilter(FILTER)
