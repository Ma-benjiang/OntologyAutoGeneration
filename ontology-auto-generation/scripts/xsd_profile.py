"""Closed raw-lexical XSD datatype profile for ABox v1."""

from __future__ import annotations

import re

XSD_PREFIX = "http://www.w3.org/2001/XMLSchema#"
ALLOWED_XSD_DATATYPES = {
    XSD_PREFIX + local
    for local in ("string", "boolean", "integer", "decimal", "double", "date", "time", "dateTime", "anyURI")
}


def literal_is_valid(value: str, datatype: str) -> bool:
    """Validate the verbatim lexical form without whitespace or value normalization."""
    if not isinstance(value, str) or datatype not in ALLOWED_XSD_DATATYPES:
        return False
    local = datatype.removeprefix(XSD_PREFIX)
    if local == "string":
        return True
    if local == "boolean":
        return value in {"true", "false", "1", "0"}
    if local == "integer":
        return re.fullmatch(r"[+-]?[0-9]+", value) is not None
    if local == "decimal":
        return re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value) is not None
    if local == "double":
        return value in {"INF", "-INF", "NaN"} or re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value
        ) is not None
    if local == "anyURI":
        return True

    timezone = r"(?:Z|[+-](?:0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)?"

    def valid_date(lexical: str) -> bool:
        match = re.fullmatch(
            rf"(-?)((?:[0-9]{{4}}|[1-9][0-9]{{4,}}))-([0-9]{{2}})-([0-9]{{2}}){timezone}",
            lexical,
        )
        if match is None:
            return False
        year, month, day = int(match.group(2)), int(match.group(3)), int(match.group(4))
        if not 1 <= month <= 12:
            return False
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        return 1 <= day <= days[month - 1]

    def valid_time(lexical: str) -> bool:
        match = re.fullmatch(
            rf"([0-9]{{2}}):([0-9]{{2}}):([0-9]{{2}})(\.[0-9]+)?{timezone}", lexical
        )
        if match is None:
            return False
        hour, minute, second = (int(match.group(index)) for index in (1, 2, 3))
        fraction = match.group(4)
        if hour == 24:
            return minute == 0 and second == 0 and (fraction is None or set(fraction[1:]) == {"0"})
        return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59

    if local == "date":
        return valid_date(value)
    if local == "time":
        return valid_time(value)
    if local == "dateTime":
        if "T" not in value:
            return False
        date_part, time_part = value.split("T", 1)
        timezone_match = re.search(r"(Z|[+-][0-9]{2}:[0-9]{2})$", time_part)
        timezone_part = timezone_match.group(1) if timezone_match else ""
        if timezone_part:
            time_part = time_part[: -len(timezone_part)]
        return valid_date(date_part + timezone_part) and valid_time(time_part + timezone_part)
    return False
