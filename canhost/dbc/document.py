"""Statement-level DBC document that reads and writes without losing anything.

A DBC file is parsed into an ordered list of statements.  Every statement keeps
the exact text it was read from; only statements an edit touches are rendered
again.  Saving an untouched document therefore reproduces the original file
byte for byte (including Vector's dummy ``VECTOR__INDEPENDENT_SIG_MSG``, CRLF
line ends, section order and spacing), and an edit changes only the lines that
belong to it.  Reference updates (renaming a signal, moving a message to a new
ID) are propagated to every statement that points at the renamed object.

The document is deliberately not a decoder.  cantools parses the generated
text for decoding and validation; this module is the writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Iterator

EXTENDED_FLAG = 0x80000000
ID_MASK = 0x1FFFFFFF
INDEPENDENT_MESSAGE_ID = 0xC0000000
INDEPENDENT_MESSAGE_NAME = "VECTOR__INDEPENDENT_SIG_MSG"
NO_NODE = "Vector__XXX"
GEN_MSG_CYCLE_TIME = "GenMsgCycleTime"
GEN_SIG_START_VALUE = "GenSigStartValue"
# CANdb++ stores only the first 32 characters of a long name in the statement
# and keeps the full name in a string attribute of the same object.
LONG_SYMBOL = {"BO_": "SystemMessageLongSymbol", "SG_": "SystemSignalLongSymbol",
               "BU_": "SystemNodeLongSymbol"}
SHORT_NAME_LIMIT = 32
MAX_NAME = 128


class DbcError(ValueError):
    """The text is not a DBC this module can represent."""


class DbcEditError(DbcError):
    """An edit was rejected; the document is unchanged."""


# ----------------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------------

_STRING = r'"((?:[^"\\]|\\.)*)"'
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[^\s;]+', re.S)


def unescape_text(text: str) -> str:
    """DBC strings escape only the quote and the backslash itself."""
    return re.sub(r'\\(["\\])', r"\1", text)


def escape_text(text: str) -> str:
    text = re.sub(r"\\(?=[\"\\]|$)", r"\\\\", text)
    return text.replace('"', '\\"')


def format_number(value: float | int) -> str:
    """Shortest decimal token that reads back as the same number."""
    if isinstance(value, bool):
        return "1" if value else "0"
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise DbcEditError("数值必须是有限数字")
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def parse_number(token: str, default: float = 0.0) -> float:
    try:
        return float(token)
    except (TypeError, ValueError):
        return default


def message_key(raw_id: int) -> str:
    """Stable UI key: ``s:291`` standard, ``e:419385573`` extended, ``v`` dummy."""
    if raw_id == INDEPENDENT_MESSAGE_ID:
        return "v"
    return f"{'e' if raw_id & EXTENDED_FLAG else 's'}:{raw_id & ID_MASK}"


def parse_message_key(key: Any) -> int:
    if isinstance(key, int):
        return key
    text = str(key)
    if text == "v":
        return INDEPENDENT_MESSAGE_ID
    match = re.fullmatch(r"([se]):(\d+)", text)
    if not match:
        raise DbcEditError(f"报文标识无效：{text}")
    number = int(match.group(2))
    if match.group(1) == "e":
        return number | EXTENDED_FLAG
    return number


def frame_to_raw_id(frame_id: int, extended: bool) -> int:
    if extended:
        if not 0 <= frame_id <= ID_MASK:
            raise DbcEditError("扩展帧 ID 必须在 0x0..0x1FFFFFFF 范围内")
        return frame_id | EXTENDED_FLAG
    if not 0 <= frame_id <= 0x7FF:
        raise DbcEditError("标准帧 ID 必须在 0x0..0x7FF 范围内")
    return frame_id


# ----------------------------------------------------------------------------
# Statements
# ----------------------------------------------------------------------------

@dataclass(eq=False, kw_only=True)
class Item:
    """One statement.  ``raw`` is the original text; ``None`` means regenerate."""

    raw: str | None = None
    kind = "RAW"

    def render(self, eol: str) -> str:
        return self.raw if self.raw is not None else self.generate(eol)

    def generate(self, eol: str) -> str:  # pragma: no cover - overridden
        return self.raw or ""

    def touch(self) -> None:
        self.raw = None


@dataclass(eq=False, kw_only=True)
class RawItem(Item):
    kind = "RAW"
    text: str = ""

    def generate(self, eol: str) -> str:
        return self.text


@dataclass(eq=False, kw_only=True)
class VersionItem(Item):
    kind = "VERSION"
    text: str = ""

    def generate(self, eol: str) -> str:
        return f'VERSION "{escape_text(self.text)}"{eol}'


@dataclass(eq=False, kw_only=True)
class NamespaceItem(Item):
    kind = "NS_"

    def generate(self, eol: str) -> str:
        return f"NS_ :{eol}"


@dataclass(eq=False, kw_only=True)
class BitTimingItem(Item):
    kind = "BS_"

    def generate(self, eol: str) -> str:
        return f"BS_:{eol}"


@dataclass(eq=False, kw_only=True)
class NodesItem(Item):
    kind = "BU_"
    names: list[str] = field(default_factory=list)

    def generate(self, eol: str) -> str:
        return "BU_:" + "".join(f" {name}" for name in self.names) + eol


@dataclass(eq=False, kw_only=True)
class MessageItem(Item):
    kind = "BO_"
    raw_id: int = 0
    name: str = ""
    dlc: int = 8
    sender: str = NO_NODE

    @property
    def virtual(self) -> bool:
        return self.raw_id == INDEPENDENT_MESSAGE_ID

    @property
    def extended(self) -> bool:
        return bool(self.raw_id & EXTENDED_FLAG) and not self.virtual

    @property
    def frame_id(self) -> int:
        return self.raw_id & ID_MASK

    def generate(self, eol: str) -> str:
        return f"BO_ {self.raw_id} {self.name}: {self.dlc} {self.sender or NO_NODE}{eol}"


@dataclass(eq=False, kw_only=True)
class SignalItem(Item):
    kind = "SG_"
    message: MessageItem | None = None
    name: str = ""
    mux: str = ""            # "", "M", "m3", "m3M"
    start: int = 0
    length: int = 1
    order: int = 1           # 1 Intel, 0 Motorola
    signed: bool = False
    factor: str = "1"
    offset: str = "0"
    minimum: str = "0"
    maximum: str = "0"
    unit: str = ""
    receivers: list[str] = field(default_factory=list)
    indent: str = " "

    def generate(self, eol: str) -> str:
        mux = f" {self.mux}" if self.mux else ""
        receivers = ",".join(self.receivers) if self.receivers else NO_NODE
        return (f'{self.indent}SG_ {self.name}{mux} : {self.start}|{self.length}@{self.order}'
                f'{"-" if self.signed else "+"} ({self.factor},{self.offset}) '
                f'[{self.minimum}|{self.maximum}] "{escape_text(self.unit)}" {receivers}{eol}')

    @property
    def mux_role(self) -> str:
        if not self.mux:
            return "none"
        if self.mux == "M":
            return "multiplexor"
        return "both" if self.mux.endswith("M") else "multiplexed"

    @property
    def mux_value(self) -> int | None:
        match = re.match(r"m(\d+)", self.mux)
        return int(match.group(1)) if match else None


@dataclass(eq=False, kw_only=True)
class TxBuItem(Item):
    kind = "BO_TX_BU_"
    raw_id: int = 0
    nodes: list[str] = field(default_factory=list)

    def generate(self, eol: str) -> str:
        return f"BO_TX_BU_ {self.raw_id} : {','.join(self.nodes)};{eol}"


@dataclass(eq=False, kw_only=True)
class CommentItem(Item):
    kind = "CM_"
    scope: str = ""          # "", "BU_", "BO_", "SG_", "EV_"
    raw_id: int | None = None
    target: str | None = None
    text: str = ""

    def generate(self, eol: str) -> str:
        head = "CM_ "
        if self.scope == "BO_":
            head += f"BO_ {self.raw_id} "
        elif self.scope == "SG_":
            head += f"SG_ {self.raw_id} {self.target} "
        elif self.scope in ("BU_", "EV_"):
            head += f"{self.scope} {self.target} "
        return f'{head}"{escape_text(self.text)}";{eol}'


@dataclass(eq=False, kw_only=True)
class AttrDefItem(Item):
    kind = "BA_DEF_"
    scope: str = ""
    name: str = ""
    vtype: str = "INT"
    args: str = ""           # text after the type, up to the ';'

    def generate(self, eol: str) -> str:
        scope = f"{self.scope} " if self.scope else " "
        args = self.args if self.args.startswith((" ", "\t")) or not self.args else f" {self.args}"
        return f'BA_DEF_ {scope}"{self.name}" {self.vtype}{args};{eol}'


@dataclass(eq=False, kw_only=True)
class AttrDefaultItem(Item):
    kind = "BA_DEF_DEF_"
    name: str = ""
    value: str = "0"

    def generate(self, eol: str) -> str:
        return f'BA_DEF_DEF_ "{self.name}" {self.value};{eol}'


@dataclass(eq=False, kw_only=True)
class AttrValueItem(Item):
    kind = "BA_"
    name: str = ""
    scope: str = ""
    raw_id: int | None = None
    target: str | None = None
    value: str = "0"

    def generate(self, eol: str) -> str:
        head = f'BA_ "{self.name}" '
        if self.scope == "BO_":
            head += f"BO_ {self.raw_id} "
        elif self.scope == "SG_":
            head += f"SG_ {self.raw_id} {self.target} "
        elif self.scope in ("BU_", "EV_"):
            head += f"{self.scope} {self.target} "
        return f"{head}{self.value};{eol}"


@dataclass(eq=False, kw_only=True)
class ValueDescItem(Item):
    kind = "VAL_"
    raw_id: int = 0
    signal: str = ""
    pairs: list[tuple[int, str]] = field(default_factory=list)

    def generate(self, eol: str) -> str:
        body = "".join(f' {value} "{escape_text(label)}"' for value, label in self.pairs)
        return f"VAL_ {self.raw_id} {self.signal}{body} ;{eol}"


@dataclass(eq=False, kw_only=True)
class ValueTableItem(Item):
    kind = "VAL_TABLE_"
    name: str = ""
    pairs: list[tuple[int, str]] = field(default_factory=list)

    def generate(self, eol: str) -> str:
        body = "".join(f' {value} "{escape_text(label)}"' for value, label in self.pairs)
        return f"VAL_TABLE_ {self.name}{body} ;{eol}"


@dataclass(eq=False, kw_only=True)
class SigValTypeItem(Item):
    kind = "SIG_VALTYPE_"
    raw_id: int = 0
    signal: str = ""
    vtype: int = 1           # 1 float32, 2 float64

    def generate(self, eol: str) -> str:
        return f"SIG_VALTYPE_ {self.raw_id} {self.signal} : {self.vtype};{eol}"


@dataclass(eq=False, kw_only=True)
class ExtMuxItem(Item):
    kind = "SG_MUL_VAL_"
    raw_id: int = 0
    signal: str = ""
    mux_signal: str = ""
    ranges: list[tuple[int, int]] = field(default_factory=list)

    def generate(self, eol: str) -> str:
        spans = ",".join(f"{low}-{high}" for low, high in self.ranges)
        return f"SG_MUL_VAL_ {self.raw_id} {self.signal} {self.mux_signal} {spans};{eol}"


@dataclass(eq=False, kw_only=True)
class SigGroupItem(Item):
    kind = "SIG_GROUP_"
    raw_id: int = 0
    name: str = ""
    repetitions: int = 1
    signals: list[str] = field(default_factory=list)

    def generate(self, eol: str) -> str:
        return f"SIG_GROUP_ {self.raw_id} {self.name} {self.repetitions} : {' '.join(self.signals)};{eol}"


@dataclass(eq=False, kw_only=True)
class OtherItem(Item):
    """A statement this module keeps verbatim (environment variables, ...)."""

    kind = "OTHER"
    keyword: str = ""
    text: str = ""

    def generate(self, eol: str) -> str:
        return self.text


# ----------------------------------------------------------------------------
# Scanner / parser
# ----------------------------------------------------------------------------

_LINE_KEYWORDS = {"VERSION", "BS_", "BU_", "BO_", "SG_"}
_TERMINATED = {
    "CM_", "BA_DEF_", "BA_DEF_DEF_", "BA_", "VAL_", "VAL_TABLE_", "SIG_VALTYPE_", "SG_MUL_VAL_",
    "SIG_GROUP_", "BO_TX_BU_", "EV_", "ENVVAR_DATA_", "SGTYPE_", "BA_DEF_REL_", "BA_DEF_DEF_REL_",
    "BA_REL_", "SIG_TYPE_REF_", "BU_SG_REL_", "BU_EV_REL_", "BU_BO_REL_", "SGTYPE_VAL_",
    "BA_DEF_SGTYPE_", "BA_SGTYPE_", "SIG_VALTYPE_",
}
_KEYWORDS = _LINE_KEYWORDS | _TERMINATED | {"NS_"}
_FIRST_WORD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")


def _split_lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _keyword(line: str) -> str | None:
    match = _FIRST_WORD.match(line)
    if match and match.group(1) in _KEYWORDS:
        return match.group(1)
    return None


def _scan(text: str) -> Iterator[tuple[str | None, str]]:
    """Yield ``(keyword, chunk)``; chunks concatenate back to ``text`` exactly."""
    lines = _split_lines(text)
    count = len(lines)
    index = 0
    while index < count:
        line = lines[index]
        if not line.strip():
            yield None, line
            index += 1
            continue
        keyword = _keyword(line)
        if keyword == "NS_":
            end = index + 1
            while end < count and lines[end][:1] in (" ", "\t") and lines[end].strip():
                end += 1
            yield "NS_", "".join(lines[index:end])
            index = end
        elif keyword in _LINE_KEYWORDS:
            yield keyword, line
            index += 1
        elif keyword in _TERMINATED:
            end = index
            in_string = escaped = done = False
            while end < count and not done:
                for char in lines[end]:
                    if escaped:
                        escaped = False
                    elif in_string:
                        if char == "\\":
                            escaped = True
                        elif char == '"':
                            in_string = False
                    elif char == '"':
                        in_string = True
                    elif char == ";":
                        done = True
                        break
                end += 1
                if (not done and not in_string and end < count
                        and lines[end][:1] not in (" ", "\t") and _keyword(lines[end])):
                    break
            yield keyword, "".join(lines[index:end])
            index = end
        else:
            yield None, line
            index += 1


_RE_VERSION = re.compile(r'^\s*VERSION\s*' + _STRING, re.S)
_RE_NODES = re.compile(r"^\s*BU_\s*:(?P<names>[^\r\n]*)$")
_RE_MESSAGE = re.compile(
    r"^\s*BO_\s+(?P<id>\d+)\s+(?P<name>[^\s:]+)\s*:\s*(?P<dlc>\d+)\s+(?P<sender>\S+)\s*$")
_RE_SIGNAL = re.compile(
    r"^(?P<indent>[ \t]*)SG_[ \t]+(?P<name>[^\s:]+)[ \t]*(?P<mux>M|m\d+M?)?[ \t]*:[ \t]*"
    r"(?P<start>\d+)\|(?P<length>\d+)@(?P<order>[01])(?P<sign>[+-])[ \t]*"
    r"\([ \t]*(?P<factor>[^,\s)]+)[ \t]*,[ \t]*(?P<offset>[^)\s]+)[ \t]*\)[ \t]*"
    r"\[[ \t]*(?P<min>[^|\s\]]+)[ \t]*\|[ \t]*(?P<max>[^\]\s]+)[ \t]*\][ \t]*"
    r'"(?P<unit>(?:[^"\\\r\n]|\\.)*)"[ \t]*(?P<rx>[^\r\n]*?)[ \t]*;?[ \t]*$')
_RE_COMMENT = re.compile(
    r"^\s*CM_[ \t]*(?:(?P<scope>BU_|BO_|SG_|EV_)[ \t]+(?P<a>[^\s\"]+)(?:[ \t]+(?P<b>[^\s\"]+))?[ \t]*)?"
    + _STRING + r"[ \t]*;", re.S)
_RE_ATTR_DEF = re.compile(
    r'^\s*BA_DEF_[ \t]+(?:(?P<scope>BU_|BO_|SG_|EV_)[ \t]+)?"(?P<name>[^"]*)"[ \t]+'
    r"(?P<type>INT|HEX|FLOAT|STRING|ENUM)(?P<args>.*?);", re.S)
_RE_ATTR_DEFAULT = re.compile(r'^\s*BA_DEF_DEF_[ \t]+"(?P<name>[^"]*)"[ \t]+(?P<value>.*?)[ \t]*;', re.S)
_RE_SIGVALTYPE = re.compile(r"^\s*SIG_VALTYPE_[ \t]+(\d+)[ \t]+(\S+?)[ \t]*:[ \t]*(\d+)[ \t]*;")
_RE_EXTMUX = re.compile(r"^\s*SG_MUL_VAL_[ \t]+(\d+)[ \t]+(\S+)[ \t]+(\S+)[ \t]+(.+?)[ \t]*;", re.S)
_RE_SIGGROUP = re.compile(r"^\s*SIG_GROUP_[ \t]+(\d+)[ \t]+(\S+)[ \t]+(\d+)[ \t]*:(.*?);", re.S)
_RE_TXBU = re.compile(r"^\s*BO_TX_BU_[ \t]+(\d+)[ \t]*:(.*?);", re.S)
_RE_PAIRS = re.compile(r'(-?\d+)\s+' + _STRING, re.S)


def _parse_pairs(tail: str) -> list[tuple[int, str]] | None:
    """Parse ``0 "a" 1 "b"``; ``None`` when something else is in the way."""
    pairs: list[tuple[int, str]] = []
    position = 0
    for match in _RE_PAIRS.finditer(tail):
        if tail[position:match.start()].strip():
            return None
        pairs.append((int(match.group(1)), unescape_text(match.group(2))))
        position = match.end()
    if tail[position:].strip():
        return None
    return pairs


def _split_names(text: str) -> list[str]:
    return [token for token in re.split(r"[,\s]+", text.strip()) if token]


def _parse_chunk(keyword: str | None, chunk: str, current: MessageItem | None,
                 warnings: list[str]) -> Item:
    body = chunk.rstrip("\r\n")
    try:
        if keyword is None:
            return RawItem(raw=chunk, text=chunk)
        if keyword == "VERSION":
            match = _RE_VERSION.match(body)
            if match:
                return VersionItem(raw=chunk, text=unescape_text(match.group(1)))
        elif keyword == "NS_":
            return NamespaceItem(raw=chunk)
        elif keyword == "BS_":
            return BitTimingItem(raw=chunk)
        elif keyword == "BU_":
            match = _RE_NODES.match(body)
            if match:
                return NodesItem(raw=chunk, names=match.group("names").split())
        elif keyword == "BO_":
            match = _RE_MESSAGE.match(body)
            if match:
                return MessageItem(raw=chunk, raw_id=int(match.group("id")), name=match.group("name"),
                                   dlc=int(match.group("dlc")), sender=match.group("sender"))
        elif keyword == "SG_":
            match = _RE_SIGNAL.match(body)
            if match and current is not None:
                return SignalItem(
                    raw=chunk, message=current, name=match.group("name"), mux=match.group("mux") or "",
                    start=int(match.group("start")), length=int(match.group("length")),
                    order=int(match.group("order")), signed=match.group("sign") == "-",
                    factor=match.group("factor"), offset=match.group("offset"),
                    minimum=match.group("min"), maximum=match.group("max"),
                    unit=unescape_text(match.group("unit")), receivers=_split_names(match.group("rx")),
                    indent=match.group("indent"))
        elif keyword == "CM_":
            match = _RE_COMMENT.match(chunk)
            if match:
                scope = match.group("scope") or ""
                a, b = match.group("a"), match.group("b")
                text = unescape_text(match.group(4))
                if scope == "BO_" and a.isdigit():
                    return CommentItem(raw=chunk, scope=scope, raw_id=int(a), text=text)
                if scope == "SG_" and a.isdigit() and b:
                    return CommentItem(raw=chunk, scope=scope, raw_id=int(a), target=b, text=text)
                if scope in ("BU_", "EV_") and b is None:
                    return CommentItem(raw=chunk, scope=scope, target=a, text=text)
                if not scope:
                    return CommentItem(raw=chunk, text=text)
        elif keyword == "BA_DEF_":
            match = _RE_ATTR_DEF.match(chunk)
            if match:
                return AttrDefItem(raw=chunk, scope=match.group("scope") or "", name=match.group("name"),
                                   vtype=match.group("type"), args=match.group("args"))
        elif keyword == "BA_DEF_DEF_":
            match = _RE_ATTR_DEFAULT.match(chunk)
            if match:
                return AttrDefaultItem(raw=chunk, name=match.group("name"), value=match.group("value"))
        elif keyword == "BA_":
            tokens = _TOKEN.findall(chunk)[1:]
            if len(tokens) >= 2 and tokens[0].startswith('"'):
                name, rest = tokens[0][1:-1], tokens[1:]
                if rest[0] in ("BO_", "SG_", "BU_", "EV_"):
                    scope = rest[0]
                    if scope == "BO_" and len(rest) == 3 and rest[1].isdigit():
                        return AttrValueItem(raw=chunk, name=name, scope=scope, raw_id=int(rest[1]), value=rest[2])
                    if scope == "SG_" and len(rest) == 4 and rest[1].isdigit():
                        return AttrValueItem(raw=chunk, name=name, scope=scope, raw_id=int(rest[1]),
                                             target=rest[2], value=rest[3])
                    if scope in ("BU_", "EV_") and len(rest) == 3:
                        return AttrValueItem(raw=chunk, name=name, scope=scope, target=rest[1], value=rest[2])
                elif len(rest) == 1:
                    return AttrValueItem(raw=chunk, name=name, value=rest[0])
        elif keyword == "VAL_":
            tokens = _TOKEN.findall(chunk)
            if len(tokens) >= 3 and tokens[1].isdigit():
                head = chunk.index(tokens[2]) + len(tokens[2])
                pairs = _parse_pairs(chunk[head:].rsplit(";", 1)[0])
                if pairs is not None:
                    return ValueDescItem(raw=chunk, raw_id=int(tokens[1]), signal=tokens[2], pairs=pairs)
        elif keyword == "VAL_TABLE_":
            tokens = _TOKEN.findall(chunk)
            if len(tokens) >= 2:
                head = chunk.index(tokens[1]) + len(tokens[1])
                pairs = _parse_pairs(chunk[head:].rsplit(";", 1)[0])
                if pairs is not None:
                    return ValueTableItem(raw=chunk, name=tokens[1], pairs=pairs)
        elif keyword == "SIG_VALTYPE_":
            match = _RE_SIGVALTYPE.match(chunk)
            if match:
                return SigValTypeItem(raw=chunk, raw_id=int(match.group(1)), signal=match.group(2),
                                      vtype=int(match.group(3)))
        elif keyword == "SG_MUL_VAL_":
            match = _RE_EXTMUX.match(chunk)
            if match:
                ranges = []
                for span in match.group(4).split(","):
                    low, _, high = span.strip().partition("-")
                    ranges.append((int(low), int(high if high else low)))
                return ExtMuxItem(raw=chunk, raw_id=int(match.group(1)), signal=match.group(2),
                                  mux_signal=match.group(3), ranges=ranges)
        elif keyword == "SIG_GROUP_":
            match = _RE_SIGGROUP.match(chunk)
            if match:
                return SigGroupItem(raw=chunk, raw_id=int(match.group(1)), name=match.group(2),
                                    repetitions=int(match.group(3)), signals=_split_names(match.group(4)))
        elif keyword == "BO_TX_BU_":
            match = _RE_TXBU.match(chunk)
            if match:
                return TxBuItem(raw=chunk, raw_id=int(match.group(1)), nodes=_split_names(match.group(2)))
    except DbcError:
        raise
    except (ValueError, IndexError):
        pass
    warnings.append(f"无法解析的 {keyword} 语句，已原样保留：{body.strip()[:60]}")
    return OtherItem(raw=chunk, keyword=keyword or "", text=chunk)


def detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf >= lf and crlf > 0 else "\n"


# ----------------------------------------------------------------------------
# Document
# ----------------------------------------------------------------------------

class DbcDocument:
    def __init__(self, items: list[Item], eol: str = "\r\n", warnings: list[str] | None = None) -> None:
        self.items = items
        self.eol = eol
        self.warnings = warnings or []

    # -- construction -----------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "DbcDocument":
        items: list[Item] = []
        warnings: list[str] = []
        current: MessageItem | None = None
        for keyword, chunk in _scan(text):
            item = _parse_chunk(keyword, chunk, current, warnings)
            if isinstance(item, MessageItem):
                current = item
            elif keyword not in (None, "SG_"):
                # Anything but a signal or a blank line ends the message block.
                current = None
            items.append(item)
        return cls(items, detect_eol(text), warnings)

    @classmethod
    def empty(cls, eol: str = "\r\n") -> "DbcDocument":
        items: list[Item] = [
            VersionItem(text=""), RawItem(text=eol), NamespaceItem(), RawItem(text=eol),
            BitTimingItem(), RawItem(text=eol), NodesItem(names=[]), RawItem(text=eol),
        ]
        return cls(items, eol)

    def to_text(self) -> str:
        return "".join(item.render(self.eol) for item in self.items)

    def clone(self) -> "DbcDocument":
        copy = DbcDocument.parse(self.to_text())
        copy.eol = self.eol
        return copy

    # -- lookups ----------------------------------------------------------

    def messages(self) -> list[MessageItem]:
        return [item for item in self.items if isinstance(item, MessageItem)]

    def signals(self, message: MessageItem) -> list[SignalItem]:
        return [item for item in self.items if isinstance(item, SignalItem) and item.message is message]

    def nodes(self) -> list[str]:
        for item in self.items:
            if isinstance(item, NodesItem):
                return list(item.names)
        return []

    def find_message(self, key: Any) -> MessageItem:
        raw_id = parse_message_key(key)
        for message in self.messages():
            if message.raw_id == raw_id:
                return message
        raise DbcEditError(f"找不到报文：{key}")

    def find_message_by_name(self, name: str) -> MessageItem | None:
        return next((m for m in self.messages() if name in (m.name, self.message_display(m))), None)

    def find_signal(self, message: MessageItem, name: str) -> SignalItem:
        for signal in self.signals(message):
            if name in (signal.name, self.signal_display(message, signal)):
                return signal
        raise DbcEditError(f"报文 {self.message_display(message)} 中找不到信号 {name}")

    def of_type(self, kind: type) -> list[Any]:
        return [item for item in self.items if isinstance(item, kind)]

    def _refs(self, raw_id: int) -> list[Item]:
        """Statements that refer to a message by its raw ID (not the BO_/SG_ itself)."""
        refs: list[Item] = []
        for item in self.items:
            if isinstance(item, CommentItem) and item.scope in ("BO_", "SG_") and item.raw_id == raw_id:
                refs.append(item)
            elif isinstance(item, AttrValueItem) and item.scope in ("BO_", "SG_") and item.raw_id == raw_id:
                refs.append(item)
            elif isinstance(item, (ValueDescItem, SigValTypeItem, ExtMuxItem, SigGroupItem, TxBuItem)) \
                    and item.raw_id == raw_id:
                refs.append(item)
        return refs

    # -- placement --------------------------------------------------------

    def _last_index(self, kinds: tuple[type, ...]) -> int:
        for index in range(len(self.items) - 1, -1, -1):
            if isinstance(self.items[index], kinds):
                return index
        return -1

    def _place(self, item: Item, *after: tuple[type, ...]) -> None:
        """Insert next to the last statement of the same kind, else after the
        last statement of the first listed predecessor kind, else at the end."""
        same = self._last_index((type(item),))
        if same >= 0:
            self.items.insert(same + 1, item)
            return
        for kinds in after:
            index = self._last_index(kinds)
            if index >= 0:
                self.items[index + 1:index + 1] = [RawItem(text=self.eol), item]
                return
        self.items.append(item)

    def _message_block_end(self, message: MessageItem) -> int:
        index = self.items.index(message)
        end = index
        for position in range(index + 1, len(self.items)):
            candidate = self.items[position]
            if isinstance(candidate, SignalItem) and candidate.message is message:
                end = position
            elif isinstance(candidate, RawItem) and not candidate.text.strip():
                continue
            else:
                break
        return end

    # -- generic edit plumbing ---------------------------------------------

    def _unique_message_name(self, name: str, exclude: MessageItem | None = None) -> None:
        if any(self.message_display(m) == name and m is not exclude for m in self.messages()):
            raise DbcEditError(f"报文名称已存在：{name}")

    def _assign_message_name(self, message: MessageItem, display: str) -> None:
        taken = {m.name for m in self.messages() if m is not message}
        file_name = self._file_name_for(display, taken)
        if file_name != message.name:
            message.name = file_name
            message.touch()
        self._set_long_name("BO_", message.raw_id, file_name, display if display != file_name else None)

    def _check_identifier(self, name: str, what: str) -> str:
        name = str(name or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise DbcEditError(f"{what}名称只能包含字母、数字和下划线，且不能以数字开头：{name or '空'}")
        if len(name) > MAX_NAME:
            raise DbcEditError(f"{what}名称不能超过 {MAX_NAME} 个字符")
        return name

    # -- long names -----------------------------------------------------------

    def long_name(self, scope: str, raw_id: int | None, file_name: str) -> str | None:
        item = self.attr_value(LONG_SYMBOL[scope], scope, raw_id if scope != "BU_" else None,
                               file_name if scope != "BO_" else None)
        if item is None:
            return None
        text = item.value.strip()
        return unescape_text(text[1:-1]) if text.startswith('"') and text.endswith('"') else text or None

    def message_display(self, message: MessageItem) -> str:
        return self.long_name("BO_", message.raw_id, message.name) or message.name

    def signal_display(self, message: MessageItem, signal: SignalItem) -> str:
        return self.long_name("SG_", message.raw_id, signal.name) or signal.name

    def node_display(self, file_name: str) -> str:
        return self.long_name("BU_", None, file_name) or file_name

    def node_file(self, name: str) -> str:
        """Map a display name (or file name) to the identifier used in the file."""
        for file_name in self.nodes():
            if file_name == name or self.node_display(file_name) == name:
                return file_name
        return name

    def _set_long_name(self, scope: str, raw_id: int | None, file_name: str, display: str | None) -> None:
        attribute = LONG_SYMBOL[scope]
        target = file_name if scope != "BO_" else None
        owner = raw_id if scope != "BU_" else None
        if display is None:
            self.set_attr_value(attribute, None, scope, owner, target)
            return
        self.ensure_attr_def(scope, attribute, "STRING", " ", '""')
        self.set_attr_value(attribute, f'"{escape_text(display)}"', scope, owner, target)

    @staticmethod
    def _file_name_for(display: str, taken: set[str]) -> str:
        """Identifier written to the file: the name itself, or its 32-character form."""
        if len(display) <= SHORT_NAME_LIMIT and display not in taken:
            return display
        base = display[:SHORT_NAME_LIMIT]
        if base not in taken and len(display) > SHORT_NAME_LIMIT:
            return base
        number = 1
        while True:
            suffix = f"_{number}"
            candidate = display[:SHORT_NAME_LIMIT - len(suffix)] + suffix
            if candidate not in taken:
                return candidate
            number += 1

    def _unique_raw_id(self, raw_id: int, exclude: MessageItem | None = None) -> None:
        if any(m.raw_id == raw_id and m is not exclude for m in self.messages()):
            raise DbcEditError(f"ID 已被其他报文使用：{message_key(raw_id)}")

    # -- comments / attributes / value tables -----------------------------

    def comment_of(self, scope: str, raw_id: int | None = None, target: str | None = None) -> str:
        for item in self.items:
            if (isinstance(item, CommentItem) and item.scope == scope and item.raw_id == raw_id
                    and item.target == target):
                return item.text
        return ""

    def set_comment(self, scope: str, text: str, raw_id: int | None = None, target: str | None = None) -> None:
        text = str(text or "")
        for item in self.items:
            if (isinstance(item, CommentItem) and item.scope == scope and item.raw_id == raw_id
                    and item.target == target):
                if not text.strip():
                    self.items.remove(item)
                elif item.text != text:
                    item.text = text
                    item.touch()
                return
        if text.strip():
            self._place(CommentItem(scope=scope, raw_id=raw_id, target=target, text=text),
                        (SigGroupItem, TxBuItem, SignalItem, MessageItem))

    def attr_def(self, scope: str, name: str) -> AttrDefItem | None:
        return next((i for i in self.items if isinstance(i, AttrDefItem) and i.scope == scope
                     and i.name == name), None)

    def attr_default(self, name: str) -> AttrDefaultItem | None:
        return next((i for i in self.items if isinstance(i, AttrDefaultItem) and i.name == name), None)

    def attr_value(self, name: str, scope: str = "", raw_id: int | None = None,
                   target: str | None = None) -> AttrValueItem | None:
        return next((i for i in self.items if isinstance(i, AttrValueItem) and i.name == name
                     and i.scope == scope and i.raw_id == raw_id and i.target == target), None)

    def ensure_attr_def(self, scope: str, name: str, vtype: str, args: str, default: str) -> None:
        if self.attr_def(scope, name) is None:
            self._place(AttrDefItem(scope=scope, name=name, vtype=vtype, args=args),
                        (CommentItem,), (SigGroupItem, TxBuItem, SignalItem, MessageItem))
        if self.attr_default(name) is None:
            self._place(AttrDefaultItem(name=name, value=default), (AttrDefItem,), (CommentItem,),
                        (SignalItem, MessageItem))

    def set_attr_value(self, name: str, value: str | None, scope: str = "", raw_id: int | None = None,
                       target: str | None = None) -> None:
        existing = self.attr_value(name, scope, raw_id, target)
        if value is None:
            if existing is not None:
                self.items.remove(existing)
            return
        if existing is not None:
            if existing.value != value:
                existing.value = value
                existing.touch()
            return
        self._place(AttrValueItem(name=name, scope=scope, raw_id=raw_id, target=target, value=value),
                    (AttrDefaultItem,), (AttrDefItem,), (CommentItem,), (SignalItem, MessageItem))

    def choices_of(self, raw_id: int, signal: str) -> list[tuple[int, str]] | None:
        for item in self.items:
            if isinstance(item, ValueDescItem) and item.raw_id == raw_id and item.signal == signal:
                return list(item.pairs)
        return None

    def set_choices(self, raw_id: int, signal: str, pairs: list[tuple[int, str]]) -> None:
        for item in self.items:
            if isinstance(item, ValueDescItem) and item.raw_id == raw_id and item.signal == signal:
                if not pairs:
                    self.items.remove(item)
                elif item.pairs != pairs:
                    item.pairs = pairs
                    item.touch()
                return
        if pairs:
            self._place(ValueDescItem(raw_id=raw_id, signal=signal, pairs=pairs),
                        (AttrValueItem,), (AttrDefaultItem,), (AttrDefItem,), (CommentItem,),
                        (SignalItem, MessageItem))

    # -- messages ----------------------------------------------------------

    def message_cycle_time(self, message: MessageItem) -> int | None:
        """Cycle in ms; the attribute default applies when the message has none,
        and 0 means "not cyclic"."""
        item = self.attr_value(GEN_MSG_CYCLE_TIME, "BO_", message.raw_id)
        default = self.attr_default(GEN_MSG_CYCLE_TIME)
        token = item.value if item is not None else default.value if default is not None else None
        if token is None:
            return None
        try:
            value = int(float(token))
        except ValueError:
            return None
        return value or None

    def add_message(self, name: str, frame_id: int, extended: bool, dlc: int = 8, sender: str = NO_NODE,
                    cycle_time: int | None = None, comment: str = "") -> MessageItem:
        name = self._check_identifier(name, "报文")
        self._unique_message_name(name)
        raw_id = frame_to_raw_id(int(frame_id), bool(extended))
        self._unique_raw_id(raw_id)
        dlc = int(dlc)
        if not 0 <= dlc <= 64:
            raise DbcEditError("DLC 必须在 0..64 范围内")
        sender = self.node_file(sender) if sender and sender != NO_NODE else NO_NODE
        if sender != NO_NODE and sender not in self.nodes():
            raise DbcEditError(f"发送节点不存在：{sender}")
        message = MessageItem(raw_id=raw_id, name=name, dlc=dlc, sender=sender)
        messages = self.messages()
        if messages:
            at = self._message_block_end(messages[-1]) + 1
            self.items[at:at] = [RawItem(text=self.eol), message]
        else:
            at = self._last_index((NodesItem, ValueTableItem))
            if at >= 0:
                self.items[at + 1:at + 1] = [RawItem(text=self.eol), message]
            else:
                self.items.append(message)
        self._assign_message_name(message, name)
        if cycle_time is not None:
            self._set_cycle_time(message, cycle_time)
        if comment:
            self.set_comment("BO_", comment, raw_id=raw_id)
        return message

    def _set_cycle_time(self, message: MessageItem, cycle_time: int | None) -> None:
        if cycle_time is None:
            self.set_attr_value(GEN_MSG_CYCLE_TIME, None, "BO_", message.raw_id)
            return
        value = int(cycle_time)
        if not 0 <= value <= 65535:
            raise DbcEditError("周期必须在 0..65535 ms 范围内")
        self.ensure_attr_def("BO_", GEN_MSG_CYCLE_TIME, "INT", " 0 65535", "0")
        self.set_attr_value(GEN_MSG_CYCLE_TIME, str(value), "BO_", message.raw_id)

    def set_message(self, key: Any, fields: dict[str, Any]) -> None:
        message = self.find_message(key)
        allowed = {"name", "id", "extended", "dlc", "sender", "senders", "cycle_time", "comment"}
        unknown = set(fields) - allowed
        if unknown:
            raise DbcEditError(f"未知报文字段：{', '.join(sorted(unknown))}")
        if message.virtual and fields:
            raise DbcEditError("Vector 的未使用信号容器不能编辑")
        if "name" in fields and fields["name"] != self.message_display(message):
            name = self._check_identifier(fields["name"], "报文")
            self._unique_message_name(name, message)
            self._assign_message_name(message, name)
        if "id" in fields or "extended" in fields:
            extended = bool(fields.get("extended", message.extended))
            frame_id = int(fields.get("id", message.frame_id))
            raw_id = frame_to_raw_id(frame_id, extended)
            if raw_id != message.raw_id:
                self._unique_raw_id(raw_id, message)
                old = message.raw_id
                for item in self._refs(old):
                    item.raw_id = raw_id  # type: ignore[attr-defined]
                    item.touch()
                message.raw_id = raw_id
                message.touch()
        if "dlc" in fields:
            dlc = int(fields["dlc"])
            if not 0 <= dlc <= 64:
                raise DbcEditError("DLC 必须在 0..64 范围内")
            if dlc != message.dlc:
                message.dlc = dlc
                message.touch()
        if "sender" in fields or "senders" in fields:
            senders = list(fields["senders"]) if "senders" in fields else [fields["sender"]]
            senders = [self.node_file(s) for s in senders if s]
            for node in senders:
                if node != NO_NODE and node not in self.nodes():
                    raise DbcEditError(f"发送节点不存在：{node}")
            primary = senders[0] if senders else NO_NODE
            if primary != message.sender:
                message.sender = primary
                message.touch()
            extra = senders[1:]
            tx = next((i for i in self.items if isinstance(i, TxBuItem) and i.raw_id == message.raw_id), None)
            if extra:
                if tx is None:
                    self._place(TxBuItem(raw_id=message.raw_id, nodes=extra), (SignalItem, MessageItem))
                elif tx.nodes != extra:
                    tx.nodes = extra
                    tx.touch()
            elif tx is not None:
                self.items.remove(tx)
        if "cycle_time" in fields:
            self._set_cycle_time(message, fields["cycle_time"])
        if "comment" in fields:
            self.set_comment("BO_", fields["comment"], raw_id=message.raw_id)

    def delete_message(self, key: Any) -> None:
        message = self.find_message(key)
        doomed = [message, *self.signals(message), *self._refs(message.raw_id)]
        first = self.items.index(message)
        self.items = [item for item in self.items if not any(item is d for d in doomed)]
        self._squeeze_blank_lines(first)

    def _squeeze_blank_lines(self, index: int) -> None:
        """Drop one of two blank lines that became adjacent at ``index``."""
        def blank(position: int) -> bool:
            return (0 <= position < len(self.items) and isinstance(self.items[position], RawItem)
                    and not self.items[position].text.strip())
        if blank(index - 1) and blank(index):
            del self.items[index]

    def duplicate_message(self, key: Any, name: str, frame_id: int, extended: bool) -> MessageItem:
        source = self.find_message(key)
        name = self._check_identifier(name, "报文")
        self._unique_message_name(name)
        raw_id = frame_to_raw_id(int(frame_id), bool(extended))
        self._unique_raw_id(raw_id)
        clone = MessageItem(raw_id=raw_id, name=name, dlc=source.dlc, sender=source.sender)
        clone.name = self._file_name_for(name, {m.name for m in self.messages()})
        position = self._message_block_end(source) + 1
        block: list[Item] = [RawItem(text=self.eol), clone]
        for signal in self.signals(source):
            copy = SignalItem(**{**signal.__dict__, "raw": None, "message": clone,
                                 "receivers": list(signal.receivers)})
            block.append(copy)
        self.items[position:position] = block
        for ref in self._refs(source.raw_id):
            if isinstance(ref, AttrValueItem) and ref.name == LONG_SYMBOL["BO_"]:
                continue
            if isinstance(ref, (CommentItem, AttrValueItem)) and ref.scope in ("BO_", "SG_"):
                twin: Item = type(ref)(**{**ref.__dict__, "raw": None, "raw_id": raw_id})
            elif isinstance(ref, ValueDescItem):
                twin = ValueDescItem(raw_id=raw_id, signal=ref.signal, pairs=list(ref.pairs))
            elif isinstance(ref, SigValTypeItem):
                twin = SigValTypeItem(raw_id=raw_id, signal=ref.signal, vtype=ref.vtype)
            elif isinstance(ref, ExtMuxItem):
                twin = ExtMuxItem(raw_id=raw_id, signal=ref.signal, mux_signal=ref.mux_signal,
                                  ranges=list(ref.ranges))
            elif isinstance(ref, SigGroupItem):
                twin = SigGroupItem(raw_id=raw_id, name=ref.name, repetitions=ref.repetitions,
                                    signals=list(ref.signals))
            elif isinstance(ref, TxBuItem):
                twin = TxBuItem(raw_id=raw_id, nodes=list(ref.nodes))
            else:
                continue
            self._place(twin, (CommentItem,), (SignalItem, MessageItem))
        self._set_long_name("BO_", raw_id, clone.name, name if name != clone.name else None)
        return clone

    def move_message(self, key: Any, index: int) -> None:
        message = self.find_message(key)
        messages = self.messages()
        index = max(0, min(len(messages) - 1, int(index)))
        start = self.items.index(message)
        end = self._message_block_end(message)
        block = self.items[start:end + 1]
        del self.items[start:end + 1]
        self._squeeze_blank_lines(start)
        remaining = [m for m in messages if m is not message]
        if index >= len(remaining):
            anchor = self._message_block_end(remaining[-1]) + 1 if remaining else start
            self.items[anchor:anchor] = [RawItem(text=self.eol), *block]
        else:
            anchor = self.items.index(remaining[index])
            self.items[anchor:anchor] = [*block, RawItem(text=self.eol)]

    # -- signals ----------------------------------------------------------

    def _signal_defaults(self) -> dict[str, Any]:
        return {"start": 0, "length": 8, "byte_order": "intel", "signed": False, "value_type": "int",
                "factor": 1, "offset": 0, "minimum": 0, "maximum": 0, "unit": "", "receivers": [],
                "mux_role": "none", "mux_value": 0}

    _SIGNAL_FIELDS = {
        "name", "start", "length", "byte_order", "signed", "value_type", "factor", "offset", "minimum",
        "maximum", "unit", "receivers", "mux_role", "mux_value", "ext_mux", "comment", "choices",
        "initial_raw",
    }

    def add_signal(self, key: Any, fields: dict[str, Any]) -> SignalItem:
        message = self.find_message(key)
        unknown = set(fields) - self._SIGNAL_FIELDS
        if unknown:
            raise DbcEditError(f"未知信号字段：{', '.join(sorted(unknown))}")
        merged = {**self._signal_defaults(), **fields}
        name = self._check_identifier(merged.get("name"), "信号")
        siblings = self.signals(message)
        if any(self.signal_display(message, s) == name for s in siblings):
            raise DbcEditError(f"报文 {self.message_display(message)} 中已有信号 {name}")
        file_name = self._file_name_for(name, {s.name for s in siblings})
        signal = SignalItem(message=message, name=file_name)
        position = self._message_block_end(message) + 1
        self.items.insert(position, signal)
        try:
            self._apply_signal_fields(message, signal, {**merged, "name": name}, creating=True)
        except DbcEditError:
            if signal in self.items:
                self.items.remove(signal)
            raise
        return signal

    def set_signal(self, key: Any, signal_name: str, fields: dict[str, Any]) -> None:
        message = self.find_message(key)
        unknown = set(fields) - self._SIGNAL_FIELDS
        if unknown:
            raise DbcEditError(f"未知信号字段：{', '.join(sorted(unknown))}")
        signal = self.find_signal(message, signal_name)
        self._apply_signal_fields(message, signal, fields, creating=False)

    def _apply_signal_fields(self, message: MessageItem, signal: SignalItem, fields: dict[str, Any],
                             creating: bool) -> None:
        changed = creating
        old_name = signal.name
        if "name" in fields and (creating or fields["name"] != self.signal_display(message, signal)):
            new = self._check_identifier(fields["name"], "信号")
            others = [s for s in self.signals(message) if s is not signal]
            if any(self.signal_display(message, s) == new for s in others):
                raise DbcEditError(f"报文 {self.message_display(message)} 中已有信号 {new}")
            file_name = self._file_name_for(new, {s.name for s in others})
            if file_name != old_name:
                self._rename_signal_refs(message, old_name, file_name)
                signal.name = file_name
                changed = True
            self._set_long_name("SG_", message.raw_id, signal.name, new if new != file_name else None)
        if "start" in fields:
            start = int(fields["start"])
            if start < 0 or start > 511:
                raise DbcEditError("起始位必须在 0..511 范围内")
            changed |= start != signal.start
            signal.start = start
        if "length" in fields:
            length = int(fields["length"])
            if not 1 <= length <= 64:
                raise DbcEditError("信号长度必须在 1..64 位范围内")
            changed |= length != signal.length
            signal.length = length
        if "byte_order" in fields:
            order = {"intel": 1, "motorola": 0, "little_endian": 1, "big_endian": 0}.get(fields["byte_order"])
            if order is None:
                raise DbcEditError("字节序只能是 intel 或 motorola")
            changed |= order != signal.order
            signal.order = order
        if "signed" in fields:
            changed |= bool(fields["signed"]) != signal.signed
            signal.signed = bool(fields["signed"])
        for field_name, attribute in (("factor", "factor"), ("offset", "offset"),
                                      ("minimum", "minimum"), ("maximum", "maximum")):
            if field_name in fields:
                token = format_number(fields[field_name])
                if parse_number(token) != parse_number(getattr(signal, attribute), float("nan")):
                    setattr(signal, attribute, token)
                    changed = True
        if "unit" in fields and str(fields["unit"] or "") != signal.unit:
            signal.unit = str(fields["unit"] or "")
            changed = True
        if "receivers" in fields:
            receivers = [self.node_file(str(n)) for n in fields["receivers"] if n]
            known = set(self.nodes())
            for node in receivers:
                if node != NO_NODE and node not in known:
                    raise DbcEditError(f"接收节点不存在：{node}")
            if receivers != [r for r in signal.receivers]:
                signal.receivers = receivers
                changed = True
        if "mux_role" in fields or "mux_value" in fields:
            role = fields.get("mux_role", signal.mux_role)
            value = int(fields.get("mux_value", signal.mux_value or 0))
            if role == "none":
                marker = ""
            elif role == "multiplexor":
                marker = "M"
            elif role == "multiplexed":
                marker = f"m{value}"
            elif role == "both":
                marker = f"m{value}M"
            else:
                raise DbcEditError("多路复用角色无效")
            if value < 0:
                raise DbcEditError("多路复用值不能为负")
            changed |= marker != signal.mux
            signal.mux = marker
        if changed:
            signal.touch()
        raw_id = message.raw_id
        if "value_type" in fields:
            self._set_value_type(raw_id, signal.name, str(fields["value_type"]))
        if "ext_mux" in fields:
            self._set_ext_mux(raw_id, signal.name, fields["ext_mux"])
        if "comment" in fields:
            self.set_comment("SG_", fields["comment"], raw_id=raw_id, target=signal.name)
        if "choices" in fields:
            self.set_choices(raw_id, signal.name, normalize_choices(fields["choices"]))
        if "initial_raw" in fields:
            self._set_initial(raw_id, signal.name, fields["initial_raw"])

    def _set_value_type(self, raw_id: int, signal: str, value_type: str) -> None:
        code = {"int": None, "float32": 1, "float64": 2}.get(value_type, "bad")
        if code == "bad":
            raise DbcEditError("数值类型只能是 int、float32 或 float64")
        existing = next((i for i in self.items if isinstance(i, SigValTypeItem)
                         and i.raw_id == raw_id and i.signal == signal), None)
        if code is None:
            if existing is not None:
                self.items.remove(existing)
        elif existing is None:
            self._place(SigValTypeItem(raw_id=raw_id, signal=signal, vtype=code),
                        (ValueDescItem,), (AttrValueItem,), (CommentItem,), (SignalItem, MessageItem))
        elif existing.vtype != code:
            existing.vtype = code
            existing.touch()

    def _set_ext_mux(self, raw_id: int, signal: str, spec: dict[str, Any] | None) -> None:
        existing = next((i for i in self.items if isinstance(i, ExtMuxItem)
                         and i.raw_id == raw_id and i.signal == signal), None)
        if not spec:
            if existing is not None:
                self.items.remove(existing)
            return
        ranges = [(int(low), int(high)) for low, high in spec.get("ranges", [])]
        parent = str(spec.get("signal") or "")
        if not parent or not ranges or any(low > high or low < 0 for low, high in ranges):
            raise DbcEditError("扩展多路复用需要父信号和有效的取值范围")
        owner = next((m for m in self.messages() if m.raw_id == raw_id), None)
        if owner is not None:
            parent = self.find_signal(owner, parent).name
        if existing is None:
            self.items.append(ExtMuxItem(raw_id=raw_id, signal=signal, mux_signal=parent, ranges=ranges))
        else:
            existing.mux_signal, existing.ranges = parent, ranges
            existing.touch()

    def _set_initial(self, raw_id: int, signal: str, value: Any) -> None:
        """``GenSigStartValue`` holds the raw (unscaled) value a sender starts with."""
        if value is None or value == "":
            self.set_attr_value(GEN_SIG_START_VALUE, None, "SG_", raw_id, signal)
            return
        self.ensure_attr_def("SG_", GEN_SIG_START_VALUE, "FLOAT", " 0 100000000000", "0")
        definition = self.attr_def("SG_", GEN_SIG_START_VALUE)
        number = float(value)
        token = format_number(number if definition is None or definition.vtype == "FLOAT" else round(number))
        self.set_attr_value(GEN_SIG_START_VALUE, token, "SG_", raw_id, signal)

    def _rename_signal_refs(self, message: MessageItem, old: str, new: str) -> None:
        for item in self._refs(message.raw_id):
            hit = False
            if isinstance(item, (CommentItem, AttrValueItem)) and item.scope == "SG_" and item.target == old:
                item.target, hit = new, True
            elif isinstance(item, (ValueDescItem, SigValTypeItem)) and item.signal == old:
                item.signal, hit = new, True
            elif isinstance(item, ExtMuxItem):
                if item.signal == old:
                    item.signal, hit = new, True
                if item.mux_signal == old:
                    item.mux_signal, hit = new, True
            elif isinstance(item, SigGroupItem) and old in item.signals:
                item.signals = [new if name == old else name for name in item.signals]
                hit = True
            if hit:
                item.touch()

    def delete_signal(self, key: Any, signal_name: str) -> None:
        message = self.find_message(key)
        signal = self.find_signal(message, signal_name)
        signal_name = signal.name
        doomed: list[Item] = [signal]
        for item in self._refs(message.raw_id):
            if isinstance(item, (CommentItem, AttrValueItem)) and item.scope == "SG_" and item.target == signal_name:
                doomed.append(item)
            elif isinstance(item, (ValueDescItem, SigValTypeItem)) and item.signal == signal_name:
                doomed.append(item)
            elif isinstance(item, ExtMuxItem) and signal_name in (item.signal, item.mux_signal):
                doomed.append(item)
            elif isinstance(item, SigGroupItem) and signal_name in item.signals:
                item.signals = [name for name in item.signals if name != signal_name]
                item.touch()
        self.items = [item for item in self.items if not any(item is d for d in doomed)]

    def duplicate_signal(self, key: Any, signal_name: str, new_name: str) -> SignalItem:
        message = self.find_message(key)
        source = self.find_signal(message, signal_name)
        signal_name = source.name
        new_name = self._check_identifier(new_name, "信号")
        siblings = self.signals(message)
        if any(self.signal_display(message, s) == new_name for s in siblings):
            raise DbcEditError(f"报文 {self.message_display(message)} 中已有信号 {new_name}")
        file_name = self._file_name_for(new_name, {s.name for s in siblings})
        copy = SignalItem(**{**source.__dict__, "raw": None, "name": file_name,
                             "receivers": list(source.receivers)})
        self.items.insert(self.items.index(source) + 1, copy)
        for ref in self._refs(message.raw_id):
            twin: Item | None = None
            if isinstance(ref, AttrValueItem) and ref.name == LONG_SYMBOL["SG_"]:
                continue
            if isinstance(ref, (CommentItem, AttrValueItem)) and ref.scope == "SG_" and ref.target == signal_name:
                twin = type(ref)(**{**ref.__dict__, "raw": None, "target": file_name})
            elif isinstance(ref, ValueDescItem) and ref.signal == signal_name:
                twin = ValueDescItem(raw_id=ref.raw_id, signal=file_name, pairs=list(ref.pairs))
            elif isinstance(ref, SigValTypeItem) and ref.signal == signal_name:
                twin = SigValTypeItem(raw_id=ref.raw_id, signal=file_name, vtype=ref.vtype)
            if twin is not None:
                self._place(twin, (CommentItem,), (SignalItem, MessageItem))
        self._set_long_name("SG_", message.raw_id, file_name, new_name if new_name != file_name else None)
        return copy

    def move_signal(self, key: Any, signal_name: str, index: int) -> None:
        message = self.find_message(key)
        signal = self.find_signal(message, signal_name)
        siblings = self.signals(message)
        index = max(0, min(len(siblings) - 1, int(index)))
        self.items.remove(signal)
        remaining = [s for s in siblings if s is not signal]
        if index >= len(remaining):
            anchor = self.items.index(remaining[-1]) + 1 if remaining else self.items.index(message) + 1
        else:
            anchor = self.items.index(remaining[index])
        self.items.insert(anchor, signal)

    # -- nodes -------------------------------------------------------------

    def _nodes_item(self) -> NodesItem:
        for item in self.items:
            if isinstance(item, NodesItem):
                return item
        item = NodesItem(names=[])
        anchor = self._last_index((BitTimingItem, NamespaceItem, VersionItem))
        self.items[anchor + 1:anchor + 1] = [item, RawItem(text=self.eol)]
        return item

    def add_node(self, name: str, comment: str = "") -> None:
        name = self._check_identifier(name, "节点")
        item = self._nodes_item()
        if any(name in (file_name, self.node_display(file_name)) for file_name in item.names):
            raise DbcEditError(f"节点已存在：{name}")
        file_name = self._file_name_for(name, set(item.names))
        item.names.append(file_name)
        item.touch()
        self._set_long_name("BU_", None, file_name, name if name != file_name else None)
        if comment:
            self.set_comment("BU_", comment, target=file_name)

    def rename_node(self, old: str, new: str) -> None:
        new_display = self._check_identifier(new, "节点")
        item = self._nodes_item()
        old = self.node_file(old)
        if old not in item.names:
            raise DbcEditError(f"找不到节点：{old}")
        if any(new_display in (n, self.node_display(n)) for n in item.names if n != old):
            raise DbcEditError(f"节点已存在：{new_display}")
        new = self._file_name_for(new_display, {n for n in item.names if n != old})
        item.names = [new if name == old else name for name in item.names]
        item.touch()
        for other in self.items:
            hit = False
            if isinstance(other, MessageItem) and other.sender == old:
                other.sender, hit = new, True
            elif isinstance(other, SignalItem) and old in other.receivers:
                other.receivers = [new if name == old else name for name in other.receivers]
                hit = True
            elif isinstance(other, TxBuItem) and old in other.nodes:
                other.nodes = [new if name == old else name for name in other.nodes]
                hit = True
            elif isinstance(other, (CommentItem, AttrValueItem)) and other.scope == "BU_" and other.target == old:
                other.target, hit = new, True
            if hit:
                other.touch()
        self._set_long_name("BU_", None, new, new_display if new_display != new else None)

    def delete_node(self, name: str) -> None:
        item = self._nodes_item()
        name = self.node_file(name)
        if name not in item.names:
            raise DbcEditError(f"找不到节点：{name}")
        senders = [self.message_display(m) for m in self.messages() if m.sender == name]
        if senders:
            raise DbcEditError(f"节点 {name} 仍是报文 {', '.join(senders[:3])} 的发送者，请先改发送节点")
        item.names = [n for n in item.names if n != name]
        item.touch()
        for other in list(self.items):
            if isinstance(other, SignalItem) and name in other.receivers:
                other.receivers = [n for n in other.receivers if n != name]
                other.touch()
            elif isinstance(other, TxBuItem) and name in other.nodes:
                other.nodes = [n for n in other.nodes if n != name]
                if other.nodes:
                    other.touch()
                else:
                    self.items.remove(other)
            elif isinstance(other, (CommentItem, AttrValueItem)) and other.scope == "BU_" and other.target == name:
                self.items.remove(other)

    # -- global value tables / attribute definitions ------------------------

    def set_value_table(self, name: str, pairs: list[tuple[int, str]]) -> None:
        name = self._check_identifier(name, "值表")
        existing = next((i for i in self.items if isinstance(i, ValueTableItem) and i.name == name), None)
        if not pairs:
            if existing is not None:
                self.items.remove(existing)
            return
        if existing is None:
            self._place(ValueTableItem(name=name, pairs=pairs), (NodesItem,), (BitTimingItem,))
        elif existing.pairs != pairs:
            existing.pairs = pairs
            existing.touch()

    def set_attr_def(self, scope: str, name: str, vtype: str, args: str, default: str | None) -> None:
        if scope not in ("", "BU_", "BO_", "SG_"):
            raise DbcEditError("属性作用域只能是 全局、节点、报文 或 信号")
        if vtype not in ("INT", "HEX", "FLOAT", "STRING", "ENUM"):
            raise DbcEditError("属性类型只能是 INT、HEX、FLOAT、STRING 或 ENUM")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
            raise DbcEditError("属性名称无效")
        existing = self.attr_def(scope, name)
        args = (" " + args.strip()) if args.strip() else ""
        if existing is None:
            self._place(AttrDefItem(scope=scope, name=name, vtype=vtype, args=args),
                        (CommentItem,), (SigGroupItem, TxBuItem, SignalItem, MessageItem))
        elif (existing.vtype, existing.args) != (vtype, args):
            existing.vtype, existing.args = vtype, args
            existing.touch()
        if default is not None:
            current = self.attr_default(name)
            if current is None:
                self._place(AttrDefaultItem(name=name, value=default), (AttrDefItem,))
            elif current.value != default:
                current.value = default
                current.touch()

    def delete_attr_def(self, scope: str, name: str) -> None:
        existing = self.attr_def(scope, name)
        if existing is None:
            raise DbcEditError(f"找不到属性定义：{name}")
        self.items.remove(existing)
        if not any(isinstance(i, AttrDefItem) and i.name == name for i in self.items):
            self.items = [i for i in self.items
                          if not (isinstance(i, (AttrDefaultItem, AttrValueItem)) and i.name == name)]

    def set_version(self, text: str) -> None:
        for item in self.items:
            if isinstance(item, VersionItem):
                if item.text != text:
                    item.text = text
                    item.touch()
                return
        self.items.insert(0, VersionItem(text=text))

    # -- operation dispatcher ----------------------------------------------

    def apply(self, op: dict[str, Any]) -> None:
        """Apply one JSON operation (see ``OPERATIONS``).  Atomic: on error the
        document is restored."""
        if not isinstance(op, dict) or "op" not in op:
            raise DbcEditError("编辑操作格式无效")
        name = op["op"]
        handler = OPERATIONS.get(name)
        if handler is None:
            raise DbcEditError(f"未知编辑操作：{name}")
        snapshot = list(self.items)
        states = [(item, {key: list(value) if isinstance(value, list) else value
                          for key, value in item.__dict__.items()}) for item in snapshot]
        try:
            handler(self, op)
        except DbcEditError:
            self.items = snapshot
            for item, state in states:
                item.__dict__.clear()
                item.__dict__.update(state)
            raise
        except (KeyError, TypeError, ValueError) as exc:
            self.items = snapshot
            for item, state in states:
                item.__dict__.clear()
                item.__dict__.update(state)
            raise DbcEditError(f"编辑参数无效：{exc}") from exc

    # -- CANdb++ compatible text ---------------------------------------------

    def candbpp_text(self) -> str:
        """Section-ordered, single-line-comment text that CANdb++ accepts.

        Same transformation as the team's ``normalize_candbpp`` script: header,
        message blocks, comments, attribute definitions, attribute values, the
        remaining statements, value descriptions and signal value types, with
        CRLF line ends.  Extended multiplexing goes last (Vector's own order).
        """
        text = self.to_text()
        version = namespace = timing = nodes = None
        blocks: list[list[str]] = []
        comments: list[list[str]] = []
        defs: list[list[str]] = []
        defaults: list[list[str]] = []
        values: list[list[str]] = []
        attrs: list[list[str]] = []
        others: list[list[str]] = []
        valtypes: list[list[str]] = []
        extmux: list[list[str]] = []
        txbu: list[list[str]] = []
        current: list[str] | None = None

        def lines(item: Item) -> list[str]:
            return item.render("\r\n").replace("\r\n", "\n").rstrip("\n").split("\n")

        for item in self.items:
            if isinstance(item, RawItem):
                if item.text.strip():
                    current = None
                    others.append(lines(item))
                continue
            if isinstance(item, VersionItem):
                version = lines(item)
            elif isinstance(item, NamespaceItem):
                namespace = lines(item)
            elif isinstance(item, BitTimingItem):
                timing = lines(item)
            elif isinstance(item, NodesItem):
                nodes = lines(item)
            elif isinstance(item, MessageItem):
                current = lines(item)
                blocks.append(current)
            elif isinstance(item, SignalItem):
                if current is None:
                    raise DbcError("SG_ 之前没有 BO_ 报文定义")
                current.extend(lines(item))
            elif isinstance(item, CommentItem):
                current = None
                comments.append(_flatten_comment(lines(item)))
            elif isinstance(item, ValueDescItem):
                current = None
                values.append(_space_semicolon(lines(item)))
            elif isinstance(item, SigValTypeItem):
                current = None
                valtypes.append(_space_semicolon(lines(item)))
            elif isinstance(item, AttrDefItem):
                current = None
                defs.append(lines(item))
            elif isinstance(item, AttrDefaultItem):
                current = None
                defaults.append(lines(item))
            elif isinstance(item, AttrValueItem):
                current = None
                attrs.append(lines(item))
            elif isinstance(item, ExtMuxItem):
                current = None
                extmux.append(lines(item))
            elif isinstance(item, TxBuItem):
                current = None
                txbu.append(lines(item))
            else:
                current = None
                others.append(lines(item))

        if nodes and NO_NODE in text and NO_NODE not in nodes[0]:
            nodes[0] = nodes[0].rstrip() + f" {NO_NODE}"
        out: list[str] = []
        if version:
            out += version + [""]
        if namespace:
            out += namespace + [""]
        out += (timing or ["BS_:"]) + [""]
        if nodes:
            out += nodes + [""]
        out.append("")
        for block in blocks:
            out += block + [""]
        out.append("")
        for block in txbu:
            out += block
        if txbu:
            out.append("")
        for block in comments:
            out += block
        if comments:
            out.append("")
        for block in defs + defaults:
            out += block
        if defs or defaults:
            out.append("")
        for block in attrs:
            out += block
        if attrs:
            out.append("")
        for block in others:
            out += block
        if others:
            out.append("")
        for block in values:
            out += block
        if values:
            out.append("")
        for block in valtypes + extmux:
            out += block
        while out and not out[-1]:
            out.pop()
        return "\r\n".join(out) + "\r\n"


def _flatten_comment(chunk: list[str]) -> list[str]:
    """Join a multi-line quoted comment into one physical line."""
    text = "\n".join(chunk)
    out: list[str] = []
    in_quote = escaped = False
    for char in text:
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\" and in_quote:
            out.append(char)
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            out.append(char)
            continue
        if in_quote and char in "\r\n":
            if out and out[-1] != " ":
                out.append(" ")
            continue
        out.append(char)
    line = "".join(out)
    while "  " in line:
        line = line.replace("  ", " ")
    return [line.rstrip()]


def _space_semicolon(chunk: list[str]) -> list[str]:
    last = chunk[-1].rstrip()
    if last.endswith(";") and not last.endswith(" ;"):
        return chunk[:-1] + [last[:-1].rstrip() + " ;"]
    return chunk


def _to_int(value: Any) -> int:
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 0)
        except ValueError:
            return int(text, 10)
    return int(value)


def normalize_choices(value: Any) -> list[tuple[int, str]]:
    """Accept ``{"0": "Off"}`` or ``[{"value": 0, "label": "Off"}]``."""
    if not value:
        return []
    pairs: list[tuple[int, str]] = []
    if isinstance(value, dict):
        entries = [(key, label) for key, label in value.items()]
    else:
        entries = [(entry["value"], entry["label"]) for entry in value]
    seen: set[int] = set()
    for raw_value, label in entries:
        number = _to_int(raw_value)
        if number in seen:
            raise DbcEditError(f"值表中重复的取值：{number}")
        seen.add(number)
        text = str(label)
        if not text.strip():
            raise DbcEditError(f"取值 {number} 的描述不能为空")
        pairs.append((number, text))
    return pairs


# ----------------------------------------------------------------------------
# Operations (JSON in, document mutation out)
# ----------------------------------------------------------------------------

def _op_add_message(doc: DbcDocument, op: dict[str, Any]) -> None:
    doc.add_message(op["name"], op["id"], bool(op.get("extended", False)), op.get("dlc", 8),
                    op.get("sender") or NO_NODE, op.get("cycle_time"), op.get("comment", ""))


def _op_duplicate_message(doc: DbcDocument, op: dict[str, Any]) -> None:
    doc.duplicate_message(op["message"], op["name"], op["id"], bool(op.get("extended", False)))


def _op_set_choices(doc: DbcDocument, op: dict[str, Any]) -> None:
    message = doc.find_message(op["message"])
    signal = doc.find_signal(message, op["signal"])
    doc.set_choices(message.raw_id, signal.name, normalize_choices(op.get("choices")))


def _op_set_comment(doc: DbcDocument, op: dict[str, Any]) -> None:
    scope = op.get("scope")
    if scope == "database":
        doc.set_comment("", op.get("comment", ""))
    elif scope == "node":
        node = doc.node_file(op["node"])
        if node not in doc.nodes():
            raise DbcEditError(f"找不到节点：{op['node']}")
        doc.set_comment("BU_", op.get("comment", ""), target=node)
    elif scope == "message":
        doc.set_comment("BO_", op.get("comment", ""), raw_id=doc.find_message(op["message"]).raw_id)
    elif scope == "signal":
        message = doc.find_message(op["message"])
        signal = doc.find_signal(message, op["signal"])
        doc.set_comment("SG_", op.get("comment", ""), raw_id=message.raw_id, target=signal.name)
    else:
        raise DbcEditError("注释范围无效")


def _op_set_attr_value(doc: DbcDocument, op: dict[str, Any]) -> None:
    scope = op.get("scope", "")
    raw_id: int | None = None
    target: str | None = None
    if scope in ("BO_", "SG_"):
        message = doc.find_message(op["message"])
        raw_id = message.raw_id
        if scope == "SG_":
            target = doc.find_signal(message, op["signal"]).name
    elif scope == "BU_":
        target = doc.node_file(op["node"])
    elif scope != "":
        raise DbcEditError("属性作用域无效")
    definition = doc.attr_def(scope, op["name"])
    if definition is None:
        raise DbcEditError(f"属性 {op['name']} 尚未定义")
    value = op.get("value")
    if value is None:
        doc.set_attr_value(op["name"], None, scope, raw_id, target)
        return
    if definition.vtype == "STRING":
        token = f'"{escape_text(str(value))}"'
    elif definition.vtype == "ENUM":
        options = re.findall(_STRING, definition.args)
        if str(value) not in options:
            raise DbcEditError(f"属性 {op['name']} 的取值必须是：{', '.join(options)}")
        token = f'"{escape_text(str(value))}"'
    else:
        token = format_number(float(value) if definition.vtype == "FLOAT" else int(float(value)))
    doc.set_attr_value(op["name"], token, scope, raw_id, target)


OPERATIONS: dict[str, Callable[[DbcDocument, dict[str, Any]], None]] = {
    "add_message": _op_add_message,
    "set_message": lambda d, o: d.set_message(o["message"], o["fields"]),
    "delete_message": lambda d, o: d.delete_message(o["message"]),
    "duplicate_message": _op_duplicate_message,
    "move_message": lambda d, o: d.move_message(o["message"], o["index"]),
    "add_signal": lambda d, o: d.add_signal(o["message"], o["fields"]),
    "set_signal": lambda d, o: d.set_signal(o["message"], o["signal"], o["fields"]),
    "delete_signal": lambda d, o: d.delete_signal(o["message"], o["signal"]),
    "duplicate_signal": lambda d, o: d.duplicate_signal(o["message"], o["signal"], o["name"]),
    "move_signal": lambda d, o: d.move_signal(o["message"], o["signal"], o["index"]),
    "set_choices": _op_set_choices,
    "add_node": lambda d, o: d.add_node(o["name"], o.get("comment", "")),
    "rename_node": lambda d, o: d.rename_node(o["node"], o["name"]),
    "delete_node": lambda d, o: d.delete_node(o["node"]),
    "set_comment": _op_set_comment,
    "set_value_table": lambda d, o: d.set_value_table(o["name"], normalize_choices(o.get("choices"))),
    "set_attr_def": lambda d, o: d.set_attr_def(o.get("scope", ""), o["name"], o["type"], o.get("args", ""),
                                                o.get("default")),
    "delete_attr_def": lambda d, o: d.delete_attr_def(o.get("scope", ""), o["name"]),
    "set_attr_value": _op_set_attr_value,
    "set_version": lambda d, o: d.set_version(str(o.get("version", ""))),
}
