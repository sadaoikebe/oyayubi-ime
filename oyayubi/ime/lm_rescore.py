"""Stage 5b: optional causal-LM rescoring of the conversion k-best (Space only).

Off by default. Nothing here imports torch/transformers until a scorer is
constructed, so the default IME path has no new dependency.

score(candidate) = (cost - cost of the current 1-best) + alpha * 500 * NLL_LM
NLL is the sum of the candidate's token negative log-likelihoods (nats) given
the left context (Fable E6; Mozc costs are about -500 * ln p).

Latency work, all exact (no approximation of the E6 score):
- Left-context KV cache: the context window is run through the model once and
  reused by every candidate. When the next context extends the previous one
  (the usual case: the session appends each commit), only the new tokens are
  run (incremental extension). The window is the last ctx_tokens..ctx_max-1
  tokens with a start that moves in fixed steps, so scores are deterministic.
  ctx_max == ctx_tokens reproduces E6's fixed last-48 window.
- Token trie (default ``share="tree"``): the candidates' token sequences are
  merged into a trie and every internal node is run exactly once, in a single
  forward call over the cached context, with an ancestor-only attention mask
  and per-node position ids. No padding, no repeated prefixes, and a node that
  ends every candidate through it (a leaf) is never run, only predicted.
  ``share="lcp"`` runs the common prefix once and the rest as a right-padded
  batch (portable fallback); ``share="none"`` is the plain padded batch.
- Results are memoized per (context, candidate).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import time
from typing import Protocol, Sequence

from tools.wp0.decode import DecodeResult
from tools.wp0.mozc_dict import MozcLex
from tools.wp0.token_viterbi import nbest_tokens

DEFAULT_MODEL = "rinna/japanese-gpt2-small"
DEFAULT_ALPHA = 1.0
DEFAULT_K = 8
COST_PER_NAT = 500  # Mozc cost scale: cost ~ -500 ln p
CONTEXT_CHARS = 200  # left context kept per connection (characters)


class Scorer(Protocol):
    def nll(self, context: str, candidates: Sequence[str]) -> list[float]: ...


class CausalLmScorer:
    """Sum NLL (nats) of each candidate given a left context."""

    def __init__(self, model: str = DEFAULT_MODEL, *, threads: int | None = None,
                 ctx_tokens: int = 48, ctx_max: int = 128, kv_cache: bool = True,
                 share: str = "tree", incremental: bool = True,
                 memo_size: int = 4096) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if threads:
            torch.set_num_threads(threads)
        self.torch = torch
        # rinna's GPT-2 ships a SentencePiece (T5Tokenizer) vocabulary.
        self.tok = AutoTokenizer.from_pretrained(model, use_fast=False)
        # Eager attention: the trie forward passes a 4-D additive mask to the blocks.
        self.model = AutoModelForCausalLM.from_pretrained(
            model, attn_implementation="eager").eval()
        self.bos = (self.tok.bos_token_id if self.tok.bos_token_id is not None
                    else self.tok.eos_token_id)
        self.pad = self.tok.pad_token_id or 0
        self.ctx_tokens = ctx_tokens
        self.ctx_max = max(ctx_max, ctx_tokens)
        self.kv_cache = kv_cache
        if share not in ("tree", "lcp", "none"):
            raise ValueError("share must be tree, lcp or none")
        self.share = share
        self.incremental = incremental
        self.memo: OrderedDict[tuple[str, str], float] = OrderedDict()
        self.memo_size = memo_size
        self._enc: OrderedDict[str, list[int]] = OrderedDict()
        # Context cache: full ids of the cached text, window start, model state.
        self._ctx_full: list[int] | None = None
        self._ctx_start = 0
        self._ctx_past = None
        self._ctx_logp = None  # log-probs of the token after the window
        self.stats = {"ctx_tokens_run": 0, "ctx_fresh": 0, "ctx_extend": 0,
                      "ctx_hit": 0, "cand_tokens_run": 0, "memo_hits": 0}

    def encode(self, text: str) -> list[int]:
        ids = self._enc.get(text)
        if ids is None:
            ids = self.tok.encode(text, add_special_tokens=False)
            self._enc[text] = ids
            if len(self._enc) > 1024:
                self._enc.popitem(last=False)
        return ids

    # -- model helpers -------------------------------------------------
    def _run(self, ids, past):
        torch = self.torch
        out = self.model(input_ids=torch.tensor(ids, dtype=torch.long),
                         past_key_values=past, use_cache=True)
        return out.past_key_values, torch.log_softmax(out.logits.float(), -1)

    @staticmethod
    def _expand(past, n: int):
        if hasattr(past, "to_legacy_cache"):  # transformers Cache object
            legacy = past.to_legacy_cache()
            return type(past).from_legacy_cache(
                tuple((k.expand(n, -1, -1, -1), v.expand(n, -1, -1, -1)) for k, v in legacy))
        return tuple((k.expand(n, -1, -1, -1), v.expand(n, -1, -1, -1)) for k, v in past)

    def _window_start(self, n: int) -> int:
        """Deterministic window: the last ctx_tokens..ctx_max-1 tokens.

        The start only moves in steps of (ctx_max - ctx_tokens), so a growing
        context usually keeps its start and can be extended in place, and the
        score never depends on what was cached before.
        """
        step = self.ctx_max - self.ctx_tokens
        start = max(0, n - self.ctx_tokens)
        return start // step * step if step > 0 else start

    def _context(self, context: str):
        """Return (past, log-probs for the next token) for BOS + context window."""
        full = self.encode(context) if context else []
        start = self._window_start(len(full))
        cached = self._ctx_full
        if cached is not None and full == cached:
            self.stats["ctx_hit"] += 1
            return self._ctx_past, self._ctx_logp
        if (self.incremental and cached is not None and start == self._ctx_start
                and len(full) > len(cached) and full[:len(cached)] == cached):
            delta = full[len(cached):]
            past, logp = self._run([delta], self._ctx_past)
            self.stats["ctx_extend"] += 1
            self.stats["ctx_tokens_run"] += len(delta)
        else:
            ids = [self.bos] + full[start:]
            past, logp = self._run([ids], None)
            self.stats["ctx_fresh"] += 1
            self.stats["ctx_tokens_run"] += len(ids)
        self._ctx_full, self._ctx_start = full, start
        self._ctx_past, self._ctx_logp = past, logp[0, -1]
        return self._ctx_past, self._ctx_logp

    # -- scoring ---------------------------------------------------------
    def nll(self, context: str, candidates: Sequence[str]) -> list[float]:
        todo = list(dict.fromkeys(c for c in candidates if (context, c) not in self.memo))
        self.stats["memo_hits"] += len(candidates) - len(todo)
        if todo:
            with self.torch.no_grad():
                scores = (self._kv(context, todo) if self.kv_cache
                          else self._naive(context, todo))
            for c, s in zip(todo, scores):
                self.memo[(context, c)] = s
            while len(self.memo) > self.memo_size:
                self.memo.popitem(last=False)
        return [self.memo[(context, c)] for c in candidates]

    def _kv(self, context: str, cands: list[str]) -> list[float]:
        past, logp = self._context(context)
        ids = [self.encode(c) for c in cands]
        shared = 0.0
        if self.share == "tree":
            return self._trie(past, logp, ids)
        if self.share == "lcp" and len(ids) > 1:
            lcp = 0
            shortest = min(len(x) for x in ids)
            while lcp < shortest and all(x[lcp] == ids[0][lcp] for x in ids):
                lcp += 1
            # Keep at least one token per candidate so every row has a suffix
            # (the suffix batch needs a nonempty sequence; cost is identical).
            lcp = min(lcp, shortest - 1) if shortest else 0
            if lcp > 0:
                prefix = ids[0][:lcp]
                shared = -float(logp[prefix[0]])
                past, lp = self._run([prefix], past)
                for j in range(1, lcp):
                    shared -= float(lp[0, j - 1, prefix[j]])
                logp = lp[0, -1]
                self.stats["cand_tokens_run"] += lcp
                ids = [x[lcp:] for x in ids]
        out = [shared] * len(ids)
        rows = [i for i, x in enumerate(ids) if x]
        for i in rows:
            out[i] -= float(logp[ids[i][0]])
        rows = [i for i in rows if len(ids[i]) > 1]
        if rows:
            # Run x[:-1]: the last token is only predicted, never conditioned on.
            width = max(len(ids[i]) - 1 for i in rows)
            batch = [ids[i][:-1] + [self.pad] * (width - len(ids[i]) + 1) for i in rows]
            _, lp = self._run(batch, self._expand(past, len(rows)))
            self.stats["cand_tokens_run"] += sum(len(ids[i]) - 1 for i in rows)
            for r, i in enumerate(rows):
                x = ids[i]
                out[i] -= sum(float(lp[r, j - 1, x[j]]) for j in range(1, len(x)))
        return out

    def _trie(self, past, logp, ids: list[list[int]]) -> list[float]:
        torch = self.torch
        node_of: dict[tuple[int, ...], int] = {}
        tokens: list[int] = []
        parents: list[int] = []
        depth: list[int] = []
        for x in ids:  # internal nodes only: x[:j+1] for j < len(x) - 1
            parent = -1
            for j in range(len(x) - 1):
                key = tuple(x[:j + 1])
                node = node_of.get(key)
                if node is None:
                    node = node_of[key] = len(tokens)
                    tokens.append(x[j])
                    parents.append(parent)
                    depth.append(j)
                parent = node
        out = [-float(logp[x[0]]) if x else 0.0 for x in ids]
        if not tokens:
            return out
        n = len(tokens)
        self.stats["cand_tokens_run"] += n
        tr = self.model.transformer
        plen = past[0][0].shape[-2]
        allow = torch.zeros(n, n, dtype=torch.bool)
        for a in range(n):
            b = a
            while b >= 0:
                allow[a, b] = True
                b = parents[b]
        mask = torch.zeros(1, 1, n, plen + n)
        mask[0, 0, :, plen:].masked_fill_(~allow, torch.finfo(mask.dtype).min)
        pos = torch.tensor([[plen + d for d in depth]])
        h = tr.wte(torch.tensor([tokens])) + tr.wpe(pos)
        for block, layer_past in zip(tr.h, past):
            h = block(h, layer_past=layer_past, attention_mask=mask)[0]
        lp = torch.log_softmax(self.model.lm_head(tr.ln_f(h))[0].float(), -1)
        for i, x in enumerate(ids):
            for j in range(1, len(x)):
                out[i] -= float(lp[node_of[tuple(x[:j])], x[j]])
        return out

    def _naive(self, context: str, cands: list[str]) -> list[float]:
        """Fable E6 as written: context + candidate re-run for every candidate."""
        torch = self.torch
        full = self.encode(context) if context else []
        cids = [self.bos] + full[self._window_start(len(full)):]
        out = []
        for b in range(0, len(cands), 32):
            batch = [self.encode(c) for c in cands[b:b + 32]]
            width = max(len(x) for x in batch)
            inp = torch.full((len(batch), len(cids) + width), self.pad, dtype=torch.long)
            att = torch.zeros_like(inp)
            for i, x in enumerate(batch):
                inp[i, :len(cids)] = torch.tensor(cids)
                inp[i, len(cids):len(cids) + len(x)] = torch.tensor(x)
                att[i, :len(cids) + len(x)] = 1
            lp = torch.log_softmax(self.model(input_ids=inp, attention_mask=att).logits, -1)
            self.stats["cand_tokens_run"] += int(att.sum())
            for i, x in enumerate(batch):
                out.append(-sum(float(lp[i, len(cids) + j - 1, t]) for j, t in enumerate(x)))
        return out


@dataclass
class LmRescorer:
    """Pick, per kana run, among the decode 1-best and the true k-best."""
    scorer: Scorer
    alpha: float = DEFAULT_ALPHA
    k: int = DEFAULT_K
    last: dict = field(default_factory=dict)  # stats of the most recent conversion

    def pool(self, best: DecodeResult, tokens: list[dict], lex: MozcLex) -> list[DecodeResult]:
        """1-best (with learning, if any) first, then k-best surfaces, k in total."""
        nb, _ = nbest_tokens(tokens, lex, k=self.k)
        return ([best] + [d for d in nb if d.surface != best.surface])[:max(1, self.k)]

    def choose(self, context: str, pool: list[DecodeResult]) -> DecodeResult:
        if len(pool) < 2 or self.alpha == 0:
            return pool[0]
        nlls = self.scorer.nll(context, [d.surface for d in pool])
        base = pool[0].score
        # min() keeps the earliest on ties, i.e. the current 1-best.
        return min(zip(pool, nlls),
                   key=lambda dn: (dn[0].score - base) + self.alpha * COST_PER_NAT * dn[1])[0]

    def rescore(self, context: str, best: DecodeResult, tokens: list[dict],
                lex: MozcLex) -> DecodeResult:
        t0 = time.perf_counter()
        pool = self.pool(best, tokens, lex)
        t1 = time.perf_counter()
        pick = self.choose(context, pool)
        t2 = time.perf_counter()
        self.last["nbest_ms"] = self.last.get("nbest_ms", 0.0) + (t1 - t0) * 1000
        self.last["lm_ms"] = self.last.get("lm_ms", 0.0) + (t2 - t1) * 1000
        self.last["runs"] = self.last.get("runs", 0) + 1
        self.last["pool"] = self.last.get("pool", 0) + len(pool)
        self.last["changed"] = self.last.get("changed", 0) + (pick is not best)
        return pick
