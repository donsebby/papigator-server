"""Zugriffs-Log ohne Geheimnisse (27.11.2026): uvicorn schreibt jede Anfrage samt Pfad und Suchanfrage ins Log, und das
liest auch der eingeschränkte Deploy-Zugang. Tokens in /invites/<t>, /s/<t>, /calendar/<t>.ics öffnen Konten, Freigaben
und den Fristen-Kalender; Suchanfragen (?q=…) verraten, wonach jemand sucht. Beides wird gekürzt."""
import logging
import re

_PATHS = re.compile(r"(/(?:invites|s|calendar)/)(?!\d+(?:$|[/?\s\"]))[^/?\s\"]+")
_QUERY = re.compile(r"\?[^\s\"]*")


def redact(s: str) -> str:
    return _QUERY.sub("?…", _PATHS.sub(r"\1…", s))


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in args)
        return True


def install() -> None:
    log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactFilter) for f in log.filters):
        log.addFilter(RedactFilter())


install()
