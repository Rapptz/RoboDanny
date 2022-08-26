# -*- coding: utf-8 -*-

"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

# help with: http://chairnerd.seatgeek.com/fuzzywuzzy-fuzzy-string-matching-in-python/

from __future__ import annotations

import re
import heapq
from typing import Callable, Iterable, Literal, Optional, Sequence, TypeVar, Generator, overload
from difflib import SequenceMatcher

T = TypeVar('T')


def ratio(a: str, b: str) -> int:
    m = SequenceMatcher(None, a, b)
    return int(round(100 * m.ratio()))


def quick_ratio(a: str, b: str) -> int:
    m = SequenceMatcher(None, a, b)
    return int(round(100 * m.quick_ratio()))


def partial_ratio(a: str, b: str) -> int:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    m = SequenceMatcher(None, short, long)

    blocks = m.get_matching_blocks()

    scores: list[float] = []
    for i, j, n in blocks:
        start = max(j - i, 0)
        end = start + len(short)
        o = SequenceMatcher(None, short, long[start:end])
        r = o.ratio()

        if 100 * r > 99:
            return 100
        scores.append(r)

    return int(round(100 * max(scores)))


_word_regex = re.compile(r'\W', re.IGNORECASE)


def _sort_tokens(a: str) -> str:
    a = _word_regex.sub(' ', a).lower().strip()
    return ' '.join(sorted(a.split()))


def token_sort_ratio(a: str, b: str) -> int:
    a = _sort_tokens(a)
    b = _sort_tokens(b)
    return ratio(a, b)


def quick_token_sort_ratio(a: str, b: str) -> int:
    a = _sort_tokens(a)
    b = _sort_tokens(b)
    return quick_ratio(a, b)


def partial_token_sort_ratio(a: str, b: str) -> int:
    a = _sort_tokens(a)
    b = _sort_tokens(b)
    return partial_ratio(a, b)


@overload
def _extraction_generator(
    query: str,
    choices: Sequence[str],
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
) -> Generator[tuple[str, int], None, None]:
    ...


@overload
def _extraction_generator(
    query: str,
    choices: dict[str, T],
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
) -> Generator[tuple[str, int, T], None, None]:
    ...


def _extraction_generator(
    query: str,
    choices: Sequence[str] | dict[str, T],
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> Generator[tuple[str, int, T] | tuple[str, int], None, None]:
    if isinstance(choices, dict):
        for key, value in choices.items():
            score = scorer(query, key)
            if score >= score_cutoff:
                yield (key, score, value)
    else:
        for choice in choices:
            score = scorer(query, choice)
            if score >= score_cutoff:
                yield (choice, score)


@overload
def extract(
    query: str,
    choices: Sequence[str],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
    limit: Optional[int] = ...,
) -> list[tuple[str, int]]:
    ...


@overload
def extract(
    query: str,
    choices: dict[str, T],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
    limit: Optional[int] = ...,
) -> list[tuple[str, int, T]]:
    ...


def extract(
    query: str,
    choices: dict[str, T] | Sequence[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
    limit: Optional[int] = 10,
) -> list[tuple[str, int]] | list[tuple[str, int, T]]:
    it = _extraction_generator(query, choices, scorer, score_cutoff)
    key = lambda t: t[1]
    if limit is not None:
        return heapq.nlargest(limit, it, key=key)  # type: ignore
    return sorted(it, key=key, reverse=True)  # type: ignore


@overload
def extract_one(
    query: str,
    choices: Sequence[str],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
) -> Optional[tuple[str, int]]:
    ...


@overload
def extract_one(
    query: str,
    choices: dict[str, T],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
) -> Optional[tuple[str, int, T]]:
    ...


def extract_one(
    query: str,
    choices: dict[str, T] | Sequence[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> Optional[tuple[str, int]] | Optional[tuple[str, int, T]]:
    it = _extraction_generator(query, choices, scorer, score_cutoff)
    key = lambda t: t[1]
    try:
        return max(it, key=key)
    except:
        # iterator could return nothing
        return None


@overload
def extract_or_exact(
    query: str,
    choices: Sequence[str],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
    limit: Optional[int] = ...,
) -> list[tuple[str, int]]:
    ...


@overload
def extract_or_exact(
    query: str,
    choices: dict[str, T],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
    limit: Optional[int] = ...,
) -> list[tuple[str, int, T]]:
    ...


def extract_or_exact(
    query: str,
    choices: dict[str, T] | Sequence[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
    limit: Optional[int] = None,
) -> list[tuple[str, int]] | list[tuple[str, int, T]]:
    matches = extract(query, choices, scorer=scorer, score_cutoff=score_cutoff, limit=limit)
    if len(matches) == 0:
        return []

    if len(matches) == 1:
        return matches

    top = matches[0][1]
    second = matches[1][1]

    # check if the top one is exact or more than 30% more correct than the top
    if top == 100 or top > (second + 30):
        return [matches[0]]  # type: ignore

    return matches


@overload
def extract_matches(
    query: str,
    choices: Sequence[str],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
) -> list[tuple[str, int]]:
    ...


@overload
def extract_matches(
    query: str,
    choices: dict[str, T],
    *,
    scorer: Callable[[str, str], int] = ...,
    score_cutoff: int = ...,
) -> list[tuple[str, int, T]]:
    ...


def extract_matches(
    query: str,
    choices: dict[str, T] | Sequence[str],
    *,
    scorer: Callable[[str, str], int] = quick_ratio,
    score_cutoff: int = 0,
) -> list[tuple[str, int]] | list[tuple[str, int, T]]:
    matches = extract(query, choices, scorer=scorer, score_cutoff=score_cutoff, limit=None)
    if len(matches) == 0:
        return []

    top_score = matches[0][1]
    to_return = []
    index = 0
    while True:
        try:
            match = matches[index]
        except IndexError:
            break
        else:
            index += 1

        if match[1] != top_score:
            break

        to_return.append(match)
    return to_return


@overload
def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Optional[Callable[[T], str]] = ...,
    raw: Literal[True],
) -> list[tuple[int, int, T]]:
    ...


@overload
def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Optional[Callable[[T], str]] = ...,
    raw: Literal[False],
) -> list[T]:
    ...


@overload
def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Optional[Callable[[T], str]] = ...,
    raw: bool = ...,
) -> list[T]:
    ...


def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Optional[Callable[[T], str]] = None,
    raw: bool = False,
) -> list[tuple[int, int, T]] | list[T]:
    suggestions: list[tuple[int, int, T]] = []
    text = str(text)
    pat = '.*?'.join(map(re.escape, text))
    regex = re.compile(pat, flags=re.IGNORECASE)
    for item in collection:
        to_search = key(item) if key else str(item)
        r = regex.search(to_search)
        if r:
            suggestions.append((len(r.group()), r.start(), item))

    def sort_key(tup: tuple[int, int, T]) -> tuple[int, int, str | T]:
        if key:
            return tup[0], tup[1], key(tup[2])
        return tup

    if raw:
        return sorted(suggestions, key=sort_key)
    else:
        return [z for _, _, z in sorted(suggestions, key=sort_key)]


def find(text: str, collection: Iterable[str], *, key: Optional[Callable[[str], str]] = None) -> Optional[str]:
    try:
        return finder(text, collection, key=key)[0]
    except IndexError:
        return None
