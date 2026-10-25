"""Local story clustering and headline similarity (TF-IDF cosine, numpy only; no API calls, no cost).

Near-duplicate headlines inside a 24-hour window share a `cluster_id` (the id of the earliest article in the
cluster). Each new article joins the cluster of its most similar earlier article if the similarity reaches
CLUSTER_SIMILARITY_THRESHOLD; clusters are never merged afterwards, which limits chaining of distinct stories.
`text_similarity` is the same measure, used to check that a Serper result really reports the same story.
"""
import math
import re
from datetime import datetime, timedelta

import numpy as np

import config
import storage

WINDOW_HOURS = 24
BACKFILL_HOURS = 48  # how far back unclustered rows are picked up

_STOPWORDS = frozenset("""a an and are as at be been but by for from has have he her his how i if in into is it its
more new not of on or our over says say said she than that the their them they this to up us was we were what when
where which who why will with you your after amid about against also can could may might would should""".split())
_TOKEN = re.compile(r"[a-z0-9]+")
_SOURCE_SUFFIX = re.compile(r"\s[-|:]\s[^-|:]{2,40}$")  # " - Reuters", " | Rigzone"


def tokens(text):
    """Lowercase word tokens without stopwords, a trailing outlet suffix, or simple plural endings."""
    text = _SOURCE_SUFFIX.sub("", (text or "").lower())
    out = []
    for t in _TOKEN.findall(text):
        if t in _STOPWORDS or len(t) < 2:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


def tfidf_matrix(texts):
    """Row-normalised TF-IDF matrix (sublinear tf) for a list of texts, fitted on those texts."""
    docs = [tokens(t) for t in texts]
    vocab = {}
    for d in docs:
        for t in d:
            vocab.setdefault(t, len(vocab))
    n = len(docs)
    x = np.zeros((n, max(len(vocab), 1)), dtype=np.float32)
    for i, d in enumerate(docs):
        for t in d:
            x[i, vocab[t]] += 1
    df = (x > 0).sum(axis=0)
    idf = np.log((n + 1) / (df + 1)) + 1.0
    x = np.where(x > 0, 1 + np.log(np.maximum(x, 1)), 0) * idf
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.where(norms == 0, 1, norms)


def text_similarity(a, b_list):
    """Cosine similarity of text `a` against each text in b_list (IDF fitted on all of them)."""
    if not b_list:
        return []
    x = tfidf_matrix([a] + list(b_list))
    return (x[1:] @ x[0]).tolist()


def cluster_new_articles(now=None, hours=BACKFILL_HOURS, threshold=None):
    """Assign cluster_id to every article that has none and was collected in the last `hours` hours.

    Idempotent: only fills empty values, in id order, so earlier new rows can be matched by later ones."""
    threshold = config.CLUSTER_SIMILARITY_THRESHOLD if threshold is None else threshold
    now = now or datetime.fromisoformat(storage.utcnow())
    since = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    pending = storage.articles_needing_cluster(since)
    if not pending:
        return 0
    earliest = datetime.fromisoformat(pending[0]["collected_at"]) - timedelta(hours=WINDOW_HOURS)
    context = storage.clustered_articles_since(earliest.isoformat(timespec="seconds"))
    rows = context + pending                      # context rows are already assigned; pending ones in id order
    x = tfidf_matrix([r["title"] for r in rows])
    times = [datetime.fromisoformat(r["collected_at"]) for r in rows]
    cluster = {i: r["cluster_id"] for i, r in enumerate(rows) if r["cluster_id"]}
    assigned = {}
    for i in range(len(context), len(rows)):
        lo = [j for j in cluster if times[i] - times[j] <= timedelta(hours=WINDOW_HOURS) and j != i]
        best, best_sim = None, 0.0
        if lo:
            sims = x[lo] @ x[i]
            k = int(np.argmax(sims))
            best, best_sim = lo[k], float(sims[k])
        cid = cluster[best] if best is not None and best_sim >= threshold else rows[i]["id"]
        cluster[i] = cid
        assigned[rows[i]["id"]] = cid
    storage.set_clusters(assigned)
    return len(assigned)


def one_per_cluster(articles):
    """One article per story: highest confidence, then highest magnitude. Rows without a cluster stand alone."""
    best = {}
    for a in articles:
        key = a.get("cluster_id") or ("solo", a.get("id", id(a)))
        cur = best.get(key)
        rank = (a.get("confidence") or 0, a.get("magnitude") or 0)
        if cur is None or rank > (cur.get("confidence") or 0, cur.get("magnitude") or 0):
            best[key] = a
    return list(best.values())


def outlet_counts(articles):
    """{cluster_id: number of distinct outlets} over the given articles."""
    outlets = {}
    for a in articles:
        if a.get("cluster_id"):
            outlets.setdefault(a["cluster_id"], set()).add(a.get("source"))
    return {k: len(v) for k, v in outlets.items()}


def title_overlap(a, b):
    """Similarity of two titles, for matching stories across days (key points diff)."""
    return text_similarity(a, [b])[0]
