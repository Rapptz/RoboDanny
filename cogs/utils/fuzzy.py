# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2017 Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

# help with: http://chairnerd.seatgeek.com/fuzzywuzzy-fuzzy-string-matching-in-python/

import re
import heapq
from difflib import SequenceMatcher

def ratio(a, b):
    m = SequenceMatcher(None, a, b)
    return int(round(100 * m.ratio()))

def quick_ratio(a, b):
    m = SequenceMatcher(None, a, b)
    return int(round(100 * m.quick_ratio()))

def partial_ratio(a, b):
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    m = SequenceMatcher(None, short, long)

    blocks = m.get_matching_blocks()

    scores = []
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

def _sort_tokens(a):
    a = _word_regex.sub(' ', a).lower().strip()
    return ' '.join(sorted(a.split()))

def token_sort_ratio(a, b):
    a = _sort_tokens(a)
    b = _sort_tokens(b)
    return ratio(a, b)

def quick_token_sort_ratio(a, b):
    a = _sort_tokens(a)
    b = _sort_tokens(b)
    return quick_ratio(a, b)

def partial_token_sort_ratio(a, b):
    a = _sort_tokens(a)
    b = _sort_tokens(b)
    return partial_ratio(a, b)

def _extraction_generator(query, choices, scorer=quick_ratio, score_cutoff=0):
    try:
        for key, value in choices.items():
            score = scorer(query, key)
            if score >= score_cutoff:
                yield (key, score, value)
    except AttributeError:
        for choice in choices:
            score = scorer(query, choice)
            if score >= score_cutoff:
                yield (choice, score)

def extract(query, choices, *, scorer=quick_ratio, score_cutoff=0, limit=10):
    it = _extraction_generator(query, choices, scorer, score_cutoff)
    key = lambda t: t[1]
    if limit is not None:
        return heapq.nlargest(limit, it, key=key)
    return sorted(it, key=key, reverse=True)

def extract_one(query, choices, *, scorer=quick_ratio, score_cutoff=0):
    it = _extraction_generator(query, choices, scorer, score_cutoff)
    key = lambda t: t[1]
    try:
        return max(it, key=key)
    except:
        # iterator could return nothing
        return None

def extract_or_exact(query, choices, *, limit=None, scorer=quick_ratio, score_cutoff=0):
    matches = extract(query, choices, scorer=scorer, score_cutoff=score_cutoff, limit=limit)
    if len(matches) == 0:
        return []

    if len(matches) == 1:
        return matches

    top = matches[0][1]
    second = matches[1][1]

    # check if the top one is exact or more than 30% more correct than the top
    if top == 100 or top > (second + 30):
        return [matches[0]]

    return matches

def extract_matches(query, choices, *, scorer=quick_ratio, score_cutoff=0):
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

def finder(text, collection, *, key=None, lazy=True):
    suggestions = []
    text = str(text)
    pat = '.*?'.join(map(re.escape, text))
    regex = re.compile(pat, flags=re.IGNORECASE)
    for item in collection:
        to_search = key(item) if key else item
        r = regex.search(to_search)
        if r:
            suggestions.append((len(r.group()), r.start(), item))

    def sort_key(tup):
        if key:
            return tup[0], tup[1], key(tup[2])
        return tup

    if lazy:
        return (z for _, _, z in sorted(suggestions, key=sort_key))
    else:
        return [z for _, _, z in sorted(suggestions, key=sort_key)]

def find(text, collection, *, key=None):
    try:
        return finder(text, collection, key=key, lazy=False)[0]
    except IndexError:
        return None

