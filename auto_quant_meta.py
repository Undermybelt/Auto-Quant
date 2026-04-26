"""
auto_quant_meta — strategy AUTO_QUANT_META block parser

The AUTO_QUANT_META block lives inside a FreqTrade strategy's module-level
docstring. It is a machine-readable contract that both `run_ibkr.py` (which
echoes meta into its `---` blocks) and `export_strategy_library.py` (which
ships meta to ict-engine) consume.

Block format
------------
The block is delimited by two literal marker lines:

    # AUTO_QUANT_META v1
    Field: value
    Field: value
    ...
    # END_AUTO_QUANT_META

Field names are case-insensitive but normalised to the canonical lowercase
form. Multi-line values are supported by indenting continuation lines (any
leading whitespace beyond the first content character is stripped).

Required fields (REQUIRED_FIELDS): a strategy is REJECTED with a clear
error if any required field is missing or contains an unfilled
`<placeholder>` value.

This module is intentionally dependency-free (no pandas / talib) so it can
be imported without paying FreqTrade startup cost.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

META_VERSION = "v1"
START_MARKER = "# AUTO_QUANT_META"
END_MARKER = "# END_AUTO_QUANT_META"

# Required field names in canonical lowercase form. Optional fields
# (`Created` is recommended but not strictly required for parsing) are
# captured but not enforced.
REQUIRED_FIELDS: tuple[str, ...] = (
    "strategy",
    "mutation_id",
    "base_factor",
    "hypothesis",
    "paradigm",
    "expected_regime",
    "factors_used",
    "parent",
    "asset_class",
    "status",
)

# Placeholder values — strategies left at the template level fail validation.
_PLACEHOLDER_RE = re.compile(r"<[^>]*>")


class StrategyMetaError(ValueError):
    """Raised when a strategy's AUTO_QUANT_META block fails validation."""


@dataclass
class StrategyMeta:
    """Parsed AUTO_QUANT_META block.

    Field semantics match the docstring contract in
    `user_data/strategies_ibkr/_template.py.example`. Lists (currently
    `factors_used`) are split on commas and stripped.
    """
    strategy: str
    mutation_id: str
    base_factor: str
    hypothesis: str
    paradigm: str
    expected_regime: str
    factors_used: list[str]
    parent: str
    asset_class: str
    status: str
    created: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, object]:
        """Stable wire format (consumed by ict-engine)."""
        return {
            "strategy":         self.strategy,
            "mutation_id":      self.mutation_id,
            "base_factor":      self.base_factor,
            "hypothesis":       self.hypothesis,
            "paradigm":         self.paradigm,
            "expected_regime":  self.expected_regime,
            "factors_used":     self.factors_used,
            "parent":           self.parent,
            "asset_class":      self.asset_class,
            "status":           self.status,
            "created":          self.created,
        }


def _extract_block(source: str) -> str | None:
    """Pull the meta block out of a Python source string.

    Returns the lines between the START and END markers (markers excluded),
    or None if no block is present. The first START marker wins; an
    unmatched END is treated as malformed and yields None.
    """
    lines = source.splitlines()
    start = end = -1
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if start < 0 and stripped.startswith(START_MARKER):
            start = i
            continue
        if start >= 0 and stripped == END_MARKER:
            end = i
            break
    if start < 0 or end < 0 or end <= start:
        return None
    return "\n".join(lines[start + 1:end])


def _parse_fields(block: str) -> dict[str, str]:
    """Parse a `Field: value` block with multi-line continuation support.

    Continuation lines are detected by absence of a top-level `Field:`
    pattern and are joined to the previous field's value with a single
    space. Comment lines (starting with `#`) are skipped.
    """
    fields: dict[str, str] = {}
    current: str | None = None
    field_re = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
    for raw in block.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = field_re.match(raw)
        if m:
            name = m.group(1).lower()
            value = m.group(2).strip()
            fields[name] = value
            current = name
        elif current is not None:
            cont = stripped
            if cont:
                fields[current] = (fields[current] + " " + cont).strip()
    return fields


def parse_meta_from_source(source: str, *, source_path: Path | None = None) -> StrategyMeta:
    """Parse AUTO_QUANT_META from a Python source string.

    Raises StrategyMetaError with a precise diagnostic if the block is
    missing, malformed, or has unfilled placeholders.
    """
    block = _extract_block(source)
    where = f" in {source_path}" if source_path else ""
    if block is None:
        raise StrategyMetaError(
            f"AUTO_QUANT_META block not found{where}. "
            f"Add a `{START_MARKER} v1` ... `{END_MARKER}` block to the "
            f"module docstring (see _template.py.example)."
        )
    raw = _parse_fields(block)

    missing = [f for f in REQUIRED_FIELDS if f not in raw or not raw[f]]
    if missing:
        raise StrategyMetaError(
            f"AUTO_QUANT_META{where} is missing required fields: "
            f"{', '.join(missing)}"
        )
    placeholder_fields = [
        f for f in REQUIRED_FIELDS
        if _PLACEHOLDER_RE.search(raw[f])
    ]
    if placeholder_fields:
        raise StrategyMetaError(
            f"AUTO_QUANT_META{where} still contains template placeholders "
            f"in fields: {', '.join(placeholder_fields)}. Fill them in or "
            f"prefix the file with `_` so it is excluded from runs."
        )

    factors = [f.strip() for f in raw["factors_used"].split(",") if f.strip()]

    return StrategyMeta(
        strategy=raw["strategy"],
        mutation_id=raw["mutation_id"],
        base_factor=raw["base_factor"],
        hypothesis=raw["hypothesis"],
        paradigm=raw["paradigm"],
        expected_regime=raw["expected_regime"],
        factors_used=factors,
        parent=raw["parent"],
        asset_class=raw["asset_class"],
        status=raw["status"],
        created=raw.get("created", ""),
        raw=raw,
    )


def parse_meta_from_file(path: str | Path) -> StrategyMeta:
    """Convenience wrapper that reads from disk."""
    p = Path(path)
    return parse_meta_from_source(p.read_text(), source_path=p)


def discover_meta_in_dir(
    directory: str | Path,
    *,
    skip_underscore: bool = True,
) -> Iterable[tuple[Path, StrategyMeta | StrategyMetaError]]:
    """Yield (path, meta_or_error) for every `*.py` in directory.

    Files starting with `_` are skipped by default (consistent with
    run_ibkr.py's discovery). Errors are returned in-band rather than
    raised, so callers can summarise validation across the whole library.
    """
    d = Path(directory)
    for path in sorted(d.glob("*.py")):
        if skip_underscore and path.stem.startswith("_"):
            continue
        try:
            yield path, parse_meta_from_file(path)
        except StrategyMetaError as e:
            yield path, e


if __name__ == "__main__":
    # Self-test invariants. Run with: python auto_quant_meta.py
    _HAPPY = '''"""
MyBreakoutICT - 5m manipulation breakout

# AUTO_QUANT_META v1
Strategy:        MyBreakoutICT
Mutation_id:     mb-001
Base_factor:     ict_breakout_5m
Hypothesis:      SPY 5m breaks out of manipulation phase ranges with
                 >55% win rate when ATR > 0.6 of 30-bar mean
Paradigm:        breakout
Expected_regime: expansion
Factors_used:    bos, fvg, atr
Parent:          root
Asset_class:     equities
Status:          active
Created:         abc1234
# END_AUTO_QUANT_META

Implementation notes.
"""'''
    m = parse_meta_from_source(_HAPPY)
    assert m.strategy == "MyBreakoutICT", m
    assert m.factors_used == ["bos", "fvg", "atr"], m.factors_used
    assert "ATR" in m.hypothesis, "multi-line hypothesis not joined"
    assert m.created == "abc1234", m.created
    print(f"[happy] strategy={m.strategy} factors={m.factors_used}")

    try:
        parse_meta_from_source('"""no meta"""')
    except StrategyMetaError as e:
        print(f"[no-block] raises: {str(e)[:60]}")
    else:
        raise AssertionError("missing block should raise")

    _SHORT = (
        '"""\n# AUTO_QUANT_META v1\nStrategy: X\n'
        'Mutation_id: m-1\n# END_AUTO_QUANT_META\n"""'
    )
    try:
        parse_meta_from_source(_SHORT)
    except StrategyMetaError as e:
        print(f"[missing-fields] raises: {str(e)[:60]}")
    else:
        raise AssertionError("missing fields should raise")

    _PLACEHOLDER = '''"""
# AUTO_QUANT_META v1
Strategy:        <YourName>
Mutation_id:     mb-001
Base_factor:     ict_breakout_5m
Hypothesis:      something
Paradigm:        breakout
Expected_regime: expansion
Factors_used:    a, b
Parent:          root
Asset_class:     equities
Status:          active
# END_AUTO_QUANT_META
"""'''
    try:
        parse_meta_from_source(_PLACEHOLDER)
    except StrategyMetaError as e:
        print(f"[placeholder] raises: {str(e)[:60]}")
    else:
        raise AssertionError("placeholder should raise")

    print("PASS")
