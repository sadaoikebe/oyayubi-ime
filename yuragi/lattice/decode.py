"""Decode result types shared by the token Viterbi, n-best and bunsetsu layers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Piece:
    surface: str
    reading: str
    lid: int
    cost: int
    # Token DAG metadata.
    start: int = 0
    end: int = 0
    rid: int = 0
    literal: bool = False
    learned: bool = False


@dataclass
class DecodeResult:
    reading: str
    surface: str
    score: int
    pieces: list[Piece]
