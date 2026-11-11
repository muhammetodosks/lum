"""Deterministic entropy-cost estimation.

The encoder uses this to choose between coding alternatives (colour
transform, row filter, compression method) before it commits to a real
entropy-coding pass.

The cost is the classic "ideal code length" of the symbol histogram,
rounded to whole bits per symbol:

    cost = sum(count_i * ceil(log2(total / count_i)))

Everything is integer arithmetic, so the Rust and C++ back-ends compute
exactly the same number as this reference implementation and always pick
the same alternative.
"""


def _ceil_log2_ratio(total: int, count: int) -> int:
    """ceil(log2(total / count)) with integer arithmetic only."""
    m = (total + count - 1) // count      # ceil(total / count)
    if m <= 1:
        return 0
    return (m - 1).bit_length()


def entropy_cost(counts, total: int) -> int:
    """Total number of bits needed for a histogram (ideal code lengths)."""
    if total <= 0:
        return 0
    cost = 0
    for count in counts:
        if count:
            cost += count * _ceil_log2_ratio(total, count)
    return cost


def histogram_cost(symbols, alphabet: int) -> int:
    """Entropy cost of a symbol sequence (counts via a plain list)."""
    counts = [0] * alphabet
    total = 0
    for s in symbols:
        counts[s] += 1
        total += 1
    return entropy_cost(counts, total)
