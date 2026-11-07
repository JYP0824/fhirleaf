"""Search value semantics: FHIR dates as instant ranges, numbers with precision, strings."""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation

DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?)?)?$", re.ASCII)
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
PREFIXES = ("eq", "ne", "gt", "lt", "ge", "le", "sa", "eb", "ap")
LOW_OPEN, HIGH_OPEN = -(2 ** 62), 2 ** 62          # open Period ends


POSIX_TZ = re.compile(r"^([A-Za-z]{3,})([+-]?\d{1,2})([A-Za-z]{3,})?(?:,M(\d+)\.(\d)\.(\d)(?:/(\d+))?,M(\d+)\.(\d)\.(\d)(?:/(\d+))?)?$", re.ASCII)


class PosixZone(tzinfo):
    """Zone from a POSIX TZ rule such as 'EST5EDT,M3.2.0,M11.1.0' (stdlib, no tz database).

    Wall times in the spring gap or the repeated autumn hour resolve with fold=0
    semantics: the offset in force before the transition.
    """

    def __init__(self, rule):
        m = POSIX_TZ.match(rule)
        if not m:
            raise ValueError(f"Unsupported POSIX TZ rule: {rule!r}")
        self.rule = rule
        self.std = timedelta(hours=-int(m.group(2)))
        self.dst_delta = timedelta(hours=1) if m.group(3) else timedelta(0)
        self.start = tuple(int(x) for x in m.group(4, 5, 6)) + (int(m.group(7) or 2),) if m.group(4) else None
        self.end = tuple(int(x) for x in m.group(8, 9, 10)) + (int(m.group(11) or 2),) if m.group(8) else None

    @staticmethod
    def _transition(year, month, week, weekday, hour):
        first = datetime(year, month, 1)
        shift = (weekday - (first.weekday() + 1) % 7) % 7          # POSIX weekday: 0 = Sunday
        day = 1 + shift + 7 * (week - 1)
        while day > calendar.monthrange(year, month)[1]:
            day -= 7
        return datetime(year, month, day, hour)

    def _is_dst(self, dt):
        if not self.start:
            return False
        local = dt.replace(tzinfo=None)
        begin = self._transition(dt.year, *self.start) + self.dst_delta     # first wall time after the gap
        end = self._transition(dt.year, *self.end)                          # repeated hour resolves to DST
        return begin <= local < end

    def utcoffset(self, dt):
        return self.std + (self.dst_delta if self._is_dst(dt) else timedelta(0))

    def dst(self, dt):
        return self.dst_delta if self._is_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return self.rule


def resolve_timezone(name):
    """Zone for date/time values written without an offset: 'UTC', '+HH:MM', a POSIX TZ rule
    ('EST5EDT,M3.2.0,M11.1.0') or, where a tz database exists, an IANA name."""
    if name in (None, "", "UTC", "Z"):
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", name, re.ASCII)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(delta if m.group(1) == "+" else -delta)
    if POSIX_TZ.match(name):
        return PosixZone(name)
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def _us(local, tz):
    offset = local.replace(tzinfo=tz).utcoffset() or timedelta(0)
    seconds = calendar.timegm(local.timetuple()) - int(offset.total_seconds())
    return seconds * 1_000_000 + local.microsecond


def date_range(text, tz):
    """(low_us, high_us, precision): the instant range (microseconds) a FHIR date/dateTime/instant
    denotes at its written precision; '.1' covers a tenth of a second, '.150' a millisecond.
    Digits beyond microseconds are not representable and are ignored.

    A value without an offset (including a date) is read in the server zone `tz`.
    """
    m = DATE.match(text.strip()) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"Invalid date/time value: {text!r}")
    y, mo, d, hh, mi, ss, frac, off = m.groups()
    year = int(y)
    zone = resolve_timezone(off) if off else tz
    if mo is None:
        low, high, precision = datetime(year, 1, 1), datetime(year + 1, 1, 1), "year"
    elif d is None:
        month = int(mo)
        low, high, precision = datetime(year, month, 1), datetime(year + (month == 12), month % 12 + 1, 1), "month"
    else:
        day = datetime(year, int(mo), int(d))
        if hh is None:
            low, high, precision = day, day + timedelta(days=1), "day"
        elif ss is None:
            low = day.replace(hour=int(hh), minute=int(mi))
            high, precision = low + timedelta(minutes=1), "minute"
        elif not frac:
            low = day.replace(hour=int(hh), minute=int(mi), second=int(ss))
            high, precision = low + timedelta(seconds=1), "second"
        else:
            digits = min(len(frac) - 1, 6)
            micro = int((frac[1:] + "000000")[:6])
            low = day.replace(hour=int(hh), minute=int(mi), second=int(ss), microsecond=micro)
            high, precision = low + timedelta(microseconds=10 ** (6 - digits)), f"fraction{digits}"
    return _us(low, zone), _us(high, zone) - 1, precision


def split_prefix(value, allowed=PREFIXES):
    if len(value) > 2 and value[:2] in allowed and (value[2] in "0123456789-+."):
        return value[:2], value[2:]
    return "eq", value


def number_bounds(text):
    """(value, low, high) where [low, high) is the implicit precision range of the literal."""
    if not NUMBER.fullmatch(text):          # FHIR decimal syntax, ASCII digits only
        raise ValueError(f"Invalid number: {text!r}")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid number: {text!r}") from exc
    if not number.is_finite():
        raise ValueError(f"Invalid number: {text!r}")
    exponent = number.as_tuple().exponent
    half = Decimal(5).scaleb(exponent - 1)
    return float(number), float(number - half), float(number + half)


def normalize(text):
    """String index normalization: case-, accent- and punctuation-insensitive, whitespace runs as one
    space (R4 string search); never truncated."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed
                       if not unicodedata.combining(c) and not unicodedata.category(c).startswith("P"))
    return " ".join(stripped.upper().split())


SOUNDEX = {**dict.fromkeys("BFPV", "1"), **dict.fromkeys("CGJKQSXZ", "2"), **dict.fromkeys("DT", "3"),
           "L": "4", **dict.fromkeys("MN", "5"), "R": "6"}


def soundex(text):
    """American Soundex (letters A-Z after accent folding; H and W do not separate equal codes).
    None when the text has no letters."""
    letters = [c for c in normalize(text) if "A" <= c <= "Z"]
    if not letters:
        return None
    out, last = [letters[0]], SOUNDEX.get(letters[0], "")
    for c in letters[1:]:
        code = SOUNDEX.get(c, "")
        if code and code != last:
            out.append(code)
        if c not in "HW":
            last = code
    return ("".join(out) + "000")[:4]


def split_escaped(value, separator):
    """Split on unescaped `separator`; `\\,` `\\|` `\\$` `\\\\` are literal."""
    parts, current, i = [], [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            current.append(value[i + 1])
            i += 2
            continue
        if c == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    parts.append("".join(current))
    return parts


def split_or(value, separator=","):
    """Split on an unescaped separator (',' for OR values, '$' for composite parts), keeping
    every escape for the next split."""
    parts, current, i = [], [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            current.append(value[i:i + 2])
            i += 2
            continue
        if c == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    parts.append("".join(current))
    return parts


def unescape(value):
    return split_escaped(value, "\x00")[0]
