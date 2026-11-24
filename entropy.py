# LUMEN - Adaptive Arithmetic (Range) Coder
# Pure Python 3.12+, standard library only.
#
# Design: interval-based arithmetic coder with Witten-Neal-Cleary(E1/E2/E3)
# renormalization on a bit stream, and an adaptive frequency model backed
# by a Fenwick tree (O(log n) per symbol).

from .constants import LumenError


class FenwickCDF:
    """Fenwick tree over symbol frequencies; padded with zero-frequency
    symbols up to the next power of two. Supports cumulative lookup and
    symbol search via binary descent (O(log n) per query)."""

    def __init__(self, n_symbols: int):
        self.n = n_symbols
        self.size = 1
        while self.size < n_symbols:
            self.size <<= 1
        self.freq = [1] * n_symbols
        self.total = n_symbols
        # cap scales with alphabet size; halving keeps freq >= 1 so
        # total can never drop below n_symbols
        self.cap = max(1 << 15, 2 * n_symbols)
        self.tree = [0] * (self.size + 1)
        self.rebuild()

    def rebuild(self):
        # Standard O(n) in-place Fenwick build:
        # 1) fill leaves with padded frequencies, 2) propagate upwards.
        freq = self.freq
        n = self.n
        s = self.size
        t = self.tree
        for i in range(1, s + 1):
            if i <= n:
                t[i] = freq[i - 1]
            else:
                t[i] = 0
        for i in range(1, s + 1):
            parent = i + (i & -i)
            if parent <= s:
                t[parent] += t[i]

    def _halve(self):
        # halve all frequencies, keep >= 1, rebuild tree
        self.freq = [(f + 1) >> 1 for f in self.freq]
        self.total = sum(self.freq)
        self.rebuild()

    def add(self, symbol: int, delta: int = 1):
        self.freq[symbol] += delta
        self.total += delta
        i = symbol + 1
        t = self.tree
        s = self.size
        while i <= s:
            t[i] += delta
            i += i & -i
        if self.total > self.cap:
            self._halve()

    def cum(self, symbol: int) -> int:
        """Cumulative frequency strictly below `symbol`."""
        if symbol <= 0:
            return 0
        s = 0
        while symbol > 0:
            s += self.tree[symbol]
            symbol -= symbol & -symbol
        return s

    def find(self, target: int) -> tuple[int, int, int]:
        """Find symbol s such that cum(s) <= target < cum(s+1).
        Returns (symbol, cum_low, freq)."""
        pos = 0
        remaining = target
        bitmask = self.size
        t = self.tree
        while bitmask:
            nxt = pos + bitmask
            if nxt <= self.size and t[nxt] <= remaining:
                pos = nxt
                remaining -= t[nxt]
            bitmask >>= 1
        # pos = largest 1-based tree index whose prefix sum <= target;
        # symbol index (0-based) = pos, cum_low = prefix(pos)
        if pos >= self.n:
            # target >= total; clamp to last symbol (defensive)
            pos = self.n - 1
            cumlow = self.cum(pos)
        else:
            cumlow = target - remaining
        return pos, cumlow, self.freq[pos]


class AdaptiveModel:
    def __init__(self, n_symbols: int):
        if n_symbols < 1:
            raise LumenError("model needs at least 1 symbol")
        self.cdf = FenwickCDF(n_symbols)

    def update(self, symbol: int):
        self.cdf.add(symbol, 1)


class RangeEncoder:
    """Arithmetic encoder on a bit stream with WNC renormalization."""

    def __init__(self, n_symbols: int):
        self.model = AdaptiveModel(n_symbols)
        # the bits are packed into bytes while they are produced
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0
        self.pending = 0
        self.low = 0
        self.high = (1 << 32) - 1  # inclusive

    def encode_symbol(self, symbol: int):
        cdf = self.model.cdf
        cum, f = cdf.cum(symbol), cdf.freq[symbol]
        total = cdf.total
        span = self.high - self.low + 1
        r = span // total
        if r < 1:
            r = 1
        cum_high = cum + f
        new_low = self.low + r * cum
        new_high = self.low + r * cum_high - 1
        if new_high < new_low:
            new_high = new_low
        self.low = new_low
        self.high = new_high
        self._renormalize()
        self.model.update(symbol)

    def _renormalize(self):
        half = 1 << 31
        quarter = 1 << 30
        while True:
            if self.high < half:
                # E1: output 0 + pending 1s
                self._put(0)
                while self.pending:
                    self._put(1)
                    self.pending -= 1
                self.low <<= 1
                self.high = (self.high << 1) | 1
            elif self.low >= half:
                # E2: output 1 + pending 0s
                self._put(1)
                while self.pending:
                    self._put(0)
                    self.pending -= 1
                self.low = (self.low - half) << 1
                self.high = ((self.high - half) << 1) | 1
            elif self.low >= quarter and self.high < 3 * quarter:
                # E3: straddle, pending bit
                self.pending += 1
                self.low = (self.low - quarter) << 1
                self.high = ((self.high - quarter) << 1) | 1
            else:
                break

    def _put(self, bit: int):
        self.acc = (self.acc << 1) | (1 if bit else 0)
        self.nbits += 1
        if self.nbits == 8:
            self.out.append(self.acc)
            self.acc = 0
            self.nbits = 0

    def flush(self) -> bytes:
        # Emit a bit string inside [low, high]:
        # Choose 2^30-1 if low < quarter else 2^31
        # (classic WNC flush: pending+1 ones or zeros after leading bit)
        self.pending += 1
        quarter = 1 << 30
        if self.low < quarter:
            self._put(0)
            for _ in range(self.pending):
                self._put(1)
        else:
            self._put(1)
            for _ in range(self.pending):
                self._put(0)
        # the last byte is zero padded
        if self.nbits:
            self.out.append(self.acc << (8 - self.nbits))
            self.acc = 0
            self.nbits = 0
        return bytes(self.out)


class ContextRangeEncoder:
    """Arithmetic encoder with one adaptive model per context.

    The bit stream is shared by all models; only the frequency model
    depends on the context. The caller supplies the context of every
    symbol, which the decoder must be able to derive from the already
    decoded data (that is what makes the stream self-describing).

    This is the "context modelling" step of the format: instead of one
    probability distribution for the whole stream, the distribution of a
    symbol depends on where it appears. Medical residuals are strongly
    correlated with their predecessor, so the gain is large.
    """

    def __init__(self, n_symbols: int, n_contexts: int):
        if n_symbols < 1:
            raise LumenError("model needs at least 1 symbol")
        if n_contexts < 1:
            raise LumenError("needs at least one context")
        self.models = [AdaptiveModel(n_symbols) for _ in range(n_contexts)]
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0
        self.pending = 0
        self.low = 0
        self.high = (1 << 32) - 1  # inclusive

    def encode_symbol(self, symbol: int, context: int):
        model = self.models[context]
        cdf = model.cdf
        cum, f = cdf.cum(symbol), cdf.freq[symbol]
        total = cdf.total
        span = self.high - self.low + 1
        r = span // total
        if r < 1:
            r = 1
        cum_high = cum + f
        new_low = self.low + r * cum
        new_high = self.low + r * cum_high - 1
        if new_high < new_low:
            new_high = new_low
        self.low = new_low
        self.high = new_high
        self._renormalize()
        model.update(symbol)

    _renormalize = RangeEncoder._renormalize
    _put = RangeEncoder._put
    flush = RangeEncoder.flush


class RangeDecoder:
    def __init__(self, data: bytes, n_symbols: int):
        self.model = AdaptiveModel(n_symbols)
        self.data = data
        self.pos = 0      # bit position
        self.low = 0
        self.high = (1 << 32) - 1
        self.value = 0
        for _ in range(32):
            self.value = (self.value << 1) | self._next_bit()

    def _next_bit(self) -> int:
        idx = self.pos
        self.pos += 1
        byte_i = idx >> 3
        if byte_i >= len(self.data):
            return 0
        bit_i = idx & 7
        return (self.data[byte_i] >> (7 - bit_i)) & 1

    def decode_symbol(self) -> int:
        cdf = self.model.cdf
        total = cdf.total
        span = self.high - self.low + 1
        r = span // total
        if r < 1:
            r = 1
        target = (self.value - self.low) // r
        if target < 0:
            target = 0
        if target >= total:
            target = total - 1
        sym, cum, f = cdf.find(target)
        self.low = self.low + r * cum
        self.high = self.low + r * f - 1
        self._renormalize()
        self.model.update(sym)
        return sym

    def _renormalize(self):
        half = 1 << 31
        quarter = 1 << 30
        while True:
            if self.high < half:
                self.low <<= 1
                self.high = (self.high << 1) | 1
                self.value = (self.value << 1) | self._next_bit()
            elif self.low >= half:
                self.low = (self.low - half) << 1
                self.high = ((self.high - half) << 1) | 1
                self.value = ((self.value - half) << 1) | self._next_bit()
            elif self.low >= quarter and self.high < 3 * quarter:
                self.low = (self.low - quarter) << 1
                self.high = ((self.high - quarter) << 1) | 1
                self.value = ((self.value - quarter) << 1) | self._next_bit()
            else:
                break


class ContextRangeDecoder:
    """Inverse of :class:`ContextRangeEncoder`."""

    def __init__(self, data: bytes, n_symbols: int, n_contexts: int):
        if n_symbols < 1:
            raise LumenError("model needs at least 1 symbol")
        if n_contexts < 1:
            raise LumenError("needs at least one context")
        self.models = [AdaptiveModel(n_symbols) for _ in range(n_contexts)]
        self.data = data
        self.pos = 0      # bit position
        self.low = 0
        self.high = (1 << 32) - 1
        self.value = 0
        for _ in range(32):
            self.value = (self.value << 1) | self._next_bit()

    def _next_bit(self) -> int:
        idx = self.pos
        self.pos += 1
        byte_i = idx >> 3
        if byte_i >= len(self.data):
            return 0
        bit_i = idx & 7
        return (self.data[byte_i] >> (7 - bit_i)) & 1

    def decode_symbol(self, context: int) -> int:
        model = self.models[context]
        cdf = model.cdf
        total = cdf.total
        span = self.high - self.low + 1
        r = span // total
        if r < 1:
            r = 1
        target = (self.value - self.low) // r
        if target < 0:
            target = 0
        if target >= total:
            target = total - 1
        sym, cum, f = cdf.find(target)
        self.low = self.low + r * cum
        self.high = self.low + r * f - 1
        self._renormalize()
        model.update(sym)
        return sym

    _renormalize = RangeDecoder._renormalize
