"""library_landscape.py — Phase 17 Library Landscape / Ecosystem Map Generator.

Where Tech Assessment compares N candidates for ONE decision, Library
Landscape spans M jobs-to-be-done × N candidates with editorial framing.
The audience is "I'm new to this domain — what's out there, how do I
think about it, where do I start?"

Inputs:
  - domain (str): e.g. "PDF processing", "Rust async runtimes"
  - candidates (Optional[list[str]]): explicit library list; else auto-
    discover top doc_sources whose concept neighborhoods overlap with
    the domain anchor.
  - jobs (Optional[list[str]]): jobs-to-be-done labels; else fall back
    to a single "Overview" job.
  - max_candidates (int): cap auto-discovery (default 12).

Output package: data/generated-packages/library-landscape-<slug>/
  _landscape.md   full assembled doc
  _matrix.md      jobs × candidates coverage matrix
  jobs/<slug>.md  per-job detail (one per job)
  _decisions.md   "if building X, start with Y" shortcuts

Coverage score per (candidate, job) cell: count of concepts that appear
in BOTH the candidate's doc_source neighborhood AND the job's concept
cluster. The matrix sorts candidates per job by this score.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import duckdb

from generator import (
    GenFile,
    GenPlan,
    GenUnit,
    Generator,
    MaterializeReport,
    ValidationIssue,
)

LOG = logging.getLogger("mypub-library-landscape")

GENERATOR_TYPE = "library_landscape"

DEFAULT_MAX_CANDIDATES = 12
MIN_OVERLAP_FOR_DISCOVERY = 3  # at least N shared concepts to surface a doc_source


# ---------------------------------------------------------------------------
# Decomposition shapes
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """One library/tool to position in the landscape."""

    name: str                       # display name (doc_source.name)
    doc_source_id: Optional[int]    # backing doc_source row (None if book-only)
    concept_id: Optional[int]       # principal concept anchor for this candidate
    description: Optional[str]      # framing snippet
    n_doc_sections: int             # currency signal
    n_chapters: int                 # book-coverage signal
    overlap_score: int              # concepts shared with domain anchor
    neighborhood_size: int          # 1-hop concept neighborhood size
    has_current_docs: bool


@dataclass
class _Job:
    """One job-to-be-done within the domain."""

    name: str                       # display label, e.g. "extract content"
    concept_cluster: set[int] = field(default_factory=set)
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class _CandidateForJob:
    """One cell in the (candidate, job) matrix.

    ``coverage`` is the raw count of shared concepts (interpretable for
    the matrix display). ``score`` is the rarity-weighted score used to
    rank candidates within a job — overlaps on rare concepts (a concept
    that appears in few candidates' neighborhoods) contribute more than
    overlaps on common ones. This removes the bias that pure raw counts
    give to libraries with larger doc_source neighborhoods.
    """

    candidate_name: str
    job_name: str
    coverage: int                   # |candidate_concepts ∩ job_concepts|
    score: float                    # rarity-weighted score for ranking
    top_concepts: list[str]         # up to 5 shared concept names, rarest first


@dataclass
class _Decomposition:
    domain: str
    anchor_concept_ids: list[int]
    candidates: list[_Candidate]
    jobs: list[_Job]
    matrix: list[_CandidateForJob]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-").replace("/", "-").replace("_", "-")
    keep = "abcdefghijklmnopqrstuvwxyz0123456789-"
    out = "".join(c for c in s if c in keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "landscape"


def _short(text: str, limit: int = 200) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip(",.;:") + "…"


def _domain_keywords(domain: str) -> list[str]:
    """Pull alphanumeric tokens, drop tiny stop-words."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", domain)
    stop = {"and", "or", "the", "of", "for", "to", "in", "on"}
    return [t.lower() for t in tokens if len(t) > 2 and t.lower() not in stop]


# ---------------------------------------------------------------------------
# Decomposer
# ---------------------------------------------------------------------------


class LandscapeDecomposer:
    """Resolve the domain anchor, gather candidates, score the matrix."""

    def decompose(
        self,
        conn: duckdb.DuckDBPyConnection,
        resolver: Any,
        query: str,
        *,
        candidates: Optional[list[str]] = None,
        jobs: Optional[list[str]] = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        **_: Any,
    ) -> _Decomposition:
        domain = query
        notes: list[str] = []

        # ---- 1. Resolve domain anchor concept(s) ----
        anchor_ids = self._resolve_anchor(conn, resolver, domain, notes)
        if not anchor_ids:
            notes.append(f"could not resolve any anchor concept for {domain!r}; "
                         "candidates will be sourced from explicit list only")

        anchor_neighborhood = self._concept_neighborhood(conn, anchor_ids)

        # ---- 2. Resolve / discover candidates ----
        if candidates:
            cands = self._resolve_explicit_candidates(
                conn, resolver, candidates, anchor_neighborhood, notes,
            )
        else:
            cands = self._discover_candidates(
                conn, anchor_neighborhood, max_candidates, notes,
            )

        # ---- 3. Jobs-to-be-done ----
        jobs_list = self._resolve_jobs(conn, resolver, jobs, domain, anchor_ids)
        if len(jobs_list) == 1 and not jobs:
            notes.append("no explicit jobs supplied; falling back to single "
                         "'Overview' job — supply --jobs for cross-job comparison")

        # ---- 4. Score (candidate × job) matrix ----
        matrix = self._score_matrix(conn, cands, jobs_list)

        return _Decomposition(
            domain=domain,
            anchor_concept_ids=anchor_ids,
            candidates=cands,
            jobs=jobs_list,
            matrix=matrix,
            notes=notes,
        )

    # -- Sub-steps -----------------------------------------------------------

    def _resolve_anchor(
        self, conn: duckdb.DuckDBPyConnection, resolver: Any,
        domain: str, notes: list[str],
    ) -> list[int]:
        """Return candidate anchor concept_ids ordered by exact > keyword match."""
        # Try exact name lookup
        cid = resolver.resolve_lookup_only(domain)
        if cid:
            return [int(cid)]

        # Fall back to keyword match against concept.name
        kw = _domain_keywords(domain)
        if not kw:
            return []
        # Match concepts that contain ALL keywords (case-insensitive substring)
        clauses = " AND ".join(["LOWER(name) LIKE ?"] * len(kw))
        params = [f"%{k}%" for k in kw]
        rows = conn.execute(
            f"""
            SELECT concept_id, name FROM concept
             WHERE {clauses}
             ORDER BY LENGTH(name) ASC LIMIT 5
            """,
            params,
        ).fetchall()
        if rows:
            return [int(r[0]) for r in rows]

        # Last resort: any concept matching ANY keyword
        clauses = " OR ".join(["LOWER(name) LIKE ?"] * len(kw))
        rows = conn.execute(
            f"""
            SELECT concept_id, name FROM concept
             WHERE {clauses}
             ORDER BY LENGTH(name) ASC LIMIT 10
            """,
            params,
        ).fetchall()
        if rows:
            notes.append(f"anchor resolved via fuzzy keyword match: "
                         f"{', '.join(r[1] for r in rows[:5])}")
            return [int(r[0]) for r in rows]
        return []

    def _concept_neighborhood(
        self, conn: duckdb.DuckDBPyConnection, anchor_ids: list[int],
    ) -> set[int]:
        """1-hop concept neighborhood of the anchor set (anchors included)."""
        if not anchor_ids:
            return set()
        ph = ",".join(["?"] * len(anchor_ids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT cid FROM (
              SELECT to_concept_id   AS cid FROM concept_relation
                WHERE from_concept_id IN ({ph})
              UNION
              SELECT from_concept_id      FROM concept_relation
                WHERE to_concept_id   IN ({ph})
              UNION
              SELECT UNNEST(?::INTEGER[])  -- include anchors themselves
            )
            """,
            anchor_ids + anchor_ids + [anchor_ids],
        ).fetchall()
        return {int(r[0]) for r in rows}

    def _doc_source_concepts(
        self, conn: duckdb.DuckDBPyConnection, doc_source_id: int,
    ) -> set[int]:
        """Concepts that appear in any concept_relation sourced from this doc_source's sections."""
        rows = conn.execute(
            """
            SELECT DISTINCT cid FROM (
              SELECT cr.from_concept_id AS cid
                FROM concept_relation cr
                JOIN doc_section sec ON cr.source_type='doc_section'
                                    AND cr.source_id = sec.doc_section_id
                JOIN doc_snapshot sn USING(snapshot_id)
               WHERE sn.doc_source_id = ?
              UNION
              SELECT cr.to_concept_id
                FROM concept_relation cr
                JOIN doc_section sec ON cr.source_type='doc_section'
                                    AND cr.source_id = sec.doc_section_id
                JOIN doc_snapshot sn USING(snapshot_id)
               WHERE sn.doc_source_id = ?
            )
            """,
            [doc_source_id, doc_source_id],
        ).fetchall()
        return {int(r[0]) for r in rows}

    def _resolve_explicit_candidates(
        self,
        conn: duckdb.DuckDBPyConnection, resolver: Any,
        candidate_names: list[str], anchor_nbhd: set[int],
        notes: list[str],
    ) -> list[_Candidate]:
        out: list[_Candidate] = []
        for name in candidate_names:
            # Try doc_source lookup first (preferred — these are libraries)
            ds = conn.execute(
                "SELECT doc_source_id, name FROM doc_source WHERE LOWER(name) = LOWER(?)",
                [name],
            ).fetchone()
            doc_source_id = int(ds[0]) if ds else None
            display = (ds[1] if ds else name)

            # Try to anchor on a same-named concept
            concept_id = resolver.resolve_lookup_only(display)

            out.append(self._build_candidate(
                conn, display, doc_source_id, concept_id, anchor_nbhd,
            ))
            if doc_source_id is None and concept_id is None:
                notes.append(
                    f"candidate {name!r} not found in doc_source or concept; "
                    "kept as a stub"
                )
        return out

    def _discover_candidates(
        self,
        conn: duckdb.DuckDBPyConnection, anchor_nbhd: set[int],
        max_candidates: int, notes: list[str],
    ) -> list[_Candidate]:
        """Rank doc_sources by concept-overlap with the anchor neighborhood."""
        if not anchor_nbhd:
            notes.append("no anchor neighborhood; auto-discovery skipped")
            return []
        anchor_list = list(anchor_nbhd)
        ph = ",".join(["?"] * len(anchor_list))
        rows = conn.execute(
            f"""
            WITH library_concepts AS (
              SELECT sn.doc_source_id,
                     cr.from_concept_id AS cid
                FROM concept_relation cr
                JOIN doc_section sec ON cr.source_type='doc_section'
                                    AND cr.source_id = sec.doc_section_id
                JOIN doc_snapshot sn USING(snapshot_id)
              UNION
              SELECT sn.doc_source_id, cr.to_concept_id
                FROM concept_relation cr
                JOIN doc_section sec ON cr.source_type='doc_section'
                                    AND cr.source_id = sec.doc_section_id
                JOIN doc_snapshot sn USING(snapshot_id)
            )
            SELECT lc.doc_source_id, ds.name,
                   COUNT(DISTINCT lc.cid)
                     FILTER (WHERE lc.cid IN ({ph})) AS overlap,
                   COUNT(DISTINCT lc.cid) AS total
              FROM library_concepts lc
              JOIN doc_source ds USING(doc_source_id)
             GROUP BY lc.doc_source_id, ds.name
             HAVING overlap >= ?
             ORDER BY overlap DESC, total DESC
             LIMIT ?
            """,
            anchor_list + [MIN_OVERLAP_FOR_DISCOVERY, max_candidates],
        ).fetchall()

        cands: list[_Candidate] = []
        for doc_source_id, name, _overlap, _total in rows:
            # Try to find a matching concept anchor
            concept_row = conn.execute(
                "SELECT concept_id FROM concept WHERE LOWER(name) = LOWER(?)",
                [name],
            ).fetchone()
            concept_id = int(concept_row[0]) if concept_row else None
            cands.append(self._build_candidate(
                conn, name, int(doc_source_id), concept_id, anchor_nbhd,
            ))
        return cands

    def _build_candidate(
        self,
        conn: duckdb.DuckDBPyConnection, name: str,
        doc_source_id: Optional[int], concept_id: Optional[int],
        anchor_nbhd: set[int],
    ) -> _Candidate:
        # Doc section count
        n_sections = 0
        if doc_source_id is not None:
            n_sections = int(conn.execute(
                "SELECT COUNT(*) FROM doc_section sec "
                " JOIN doc_snapshot sn USING(snapshot_id) "
                " WHERE sn.doc_source_id = ?",
                [doc_source_id],
            ).fetchone()[0] or 0)

        # Chapter count (concept-anchored)
        n_chapters = 0
        if concept_id is not None:
            n_chapters = int(conn.execute(
                """
                SELECT COUNT(DISTINCT cr.source_id) FROM concept_relation cr
                 WHERE cr.source_type='chapter'
                   AND (cr.from_concept_id = ? OR cr.to_concept_id = ?)
                """,
                [concept_id, concept_id],
            ).fetchone()[0] or 0)

        # Description
        description = None
        if concept_id is not None:
            r = conn.execute(
                "SELECT description FROM concept WHERE concept_id = ?",
                [concept_id],
            ).fetchone()
            description = r[0] if r else None

        # Overlap + neighborhood
        candidate_concepts = (
            self._doc_source_concepts(conn, doc_source_id)
            if doc_source_id is not None else set()
        )
        if concept_id is not None:
            candidate_concepts.add(concept_id)
        overlap = len(candidate_concepts & anchor_nbhd)
        neighborhood_size = len(candidate_concepts)

        return _Candidate(
            name=name,
            doc_source_id=doc_source_id,
            concept_id=concept_id,
            description=description,
            n_doc_sections=n_sections,
            n_chapters=n_chapters,
            overlap_score=overlap,
            neighborhood_size=neighborhood_size,
            has_current_docs=n_sections > 0,
        )

    def _seed_concepts_for_keywords(
        self, conn: duckdb.DuckDBPyConnection, keywords: list[str],
        limit: int = 80,
    ) -> set[int]:
        """Seed concepts from name + description + alias matches.

        Pure-name matching missed conceptually-relevant concepts whose
        name didn't contain the keyword (e.g., 'JoinHandle' for an 'async
        runtime' job). Expanding to description + concept_alias seeds
        catches these without resorting to vector search.
        """
        if not keywords:
            return set()
        ids: set[int] = set()
        params = [f"%{k}%" for k in keywords]

        # Name match (current behavior)
        name_clause = " OR ".join(["LOWER(name) LIKE ?"] * len(keywords))
        rows = conn.execute(
            f"SELECT concept_id FROM concept WHERE {name_clause} LIMIT ?",
            params + [limit],
        ).fetchall()
        ids.update(int(r[0]) for r in rows)

        # Description match — catches semantic relevance without name overlap
        desc_clause = " OR ".join(["LOWER(description) LIKE ?"] * len(keywords))
        rows = conn.execute(
            f"SELECT concept_id FROM concept "
            f"WHERE description IS NOT NULL AND ({desc_clause}) LIMIT ?",
            params + [limit],
        ).fetchall()
        ids.update(int(r[0]) for r in rows)

        # Alias match — synonyms / acronym variants
        alias_clause = " OR ".join(["LOWER(alias) LIKE ?"] * len(keywords))
        rows = conn.execute(
            f"SELECT DISTINCT concept_id FROM concept_alias "
            f"WHERE {alias_clause} LIMIT ?",
            params + [limit],
        ).fetchall()
        ids.update(int(r[0]) for r in rows)

        return ids

    def _seed_concepts_by_vector(
        self, conn: duckdb.DuckDBPyConnection, resolver: Any,
        job_text: str, top_k: int = 40, max_distance: float = 0.50,
    ) -> set[int]:
        """Seed concepts by cosine-distance to the job string's embedding.

        Catches semantically-relevant concepts whose name and description
        don't textually contain the job's keywords. Example: a job like
        "async runtime" should find ``Future``, ``Executor``, ``Task``
        even though "async" / "runtime" don't appear in their names.

        Falls back to an empty set if the embedding model can't be
        loaded — never raises. Keyword seeding always runs alongside,
        so a vector miss doesn't kill the job cluster.
        """
        try:
            qvec = resolver._embed(job_text)  # pylint: disable=protected-access
        except Exception:  # noqa: BLE001
            return set()
        try:
            rows = conn.execute(
                f"""
                SELECT e.concept_id,
                       array_cosine_distance(
                         e.embedding, ?::FLOAT[{resolver.embed_dim}]) AS d
                  FROM concept_embedding e
                 ORDER BY d ASC
                 LIMIT ?
                """,
                [qvec, top_k],
            ).fetchall()
        except Exception:  # noqa: BLE001
            return set()
        return {int(r[0]) for r in rows if float(r[1]) <= max_distance}

    def _resolve_jobs(
        self,
        conn: duckdb.DuckDBPyConnection, resolver: Any,
        jobs: Optional[list[str]], domain: str, anchor_ids: list[int],
    ) -> list[_Job]:
        if not jobs:
            cluster = set(anchor_ids)
            for aid in anchor_ids:
                # 1-hop expansion
                cluster.update(self._concept_neighborhood(conn, [aid]))
            return [_Job(name="Overview", concept_cluster=cluster,
                         matched_keywords=_domain_keywords(domain))]
        out: list[_Job] = []
        for job in jobs:
            kw = _domain_keywords(job)
            if not kw:
                continue
            # Two-source seeding: textual keyword matches (high precision)
            # + vector-similar concepts (high recall on semantic relevance).
            keyword_seeds = self._seed_concepts_for_keywords(conn, kw)
            vector_seeds = self._seed_concepts_by_vector(
                conn, resolver, job, top_k=40, max_distance=0.50,
            )
            seed_ids = keyword_seeds | vector_seeds
            cluster = set(seed_ids)
            if seed_ids:
                cluster.update(self._concept_neighborhood(conn, list(seed_ids)))
            out.append(_Job(name=job, concept_cluster=cluster,
                            matched_keywords=kw))
        return out or [_Job(name="Overview", concept_cluster=set(anchor_ids),
                            matched_keywords=_domain_keywords(domain))]

    def _score_matrix(
        self, conn: duckdb.DuckDBPyConnection,
        candidates: list[_Candidate], jobs: list[_Job],
    ) -> list[_CandidateForJob]:
        """Build the (candidate, job) matrix with rarity-weighted scoring.

        ``coverage`` is the raw shared-concept count (interpretable for
        the matrix display). ``score`` weights each shared concept by
        its rarity across candidates' neighborhoods: a concept that
        appears in N candidates contributes ``1/log(N + 1)``. Rare
        concepts (high information) dominate; common-to-everyone
        concepts (low information) contribute little.

        This eliminates the bias where candidates with larger doc_source
        neighborhoods trivially win on raw counts.
        """
        import math

        # Build all candidate concept sets up front
        cand_concepts: dict[str, set[int]] = {}
        for c in candidates:
            cs: set[int] = set()
            if c.doc_source_id is not None:
                cs = self._doc_source_concepts(conn, c.doc_source_id)
            if c.concept_id is not None:
                cs.add(c.concept_id)
            cand_concepts[c.name] = cs

        # Concept rarity: how many candidate neighborhoods contain each concept
        concept_doc_freq: dict[int, int] = {}
        for cs in cand_concepts.values():
            for cid in cs:
                concept_doc_freq[cid] = concept_doc_freq.get(cid, 0) + 1

        def _rarity_weight(cid: int) -> float:
            df = concept_doc_freq.get(cid, 1)
            return 1.0 / math.log(df + 1.0)

        matrix: list[_CandidateForJob] = []
        for job in jobs:
            for c in candidates:
                shared = cand_concepts.get(c.name, set()) & job.concept_cluster
                if not shared:
                    matrix.append(_CandidateForJob(
                        candidate_name=c.name, job_name=job.name,
                        coverage=0, score=0.0, top_concepts=[],
                    ))
                    continue
                # Weighted score
                weighted = sum(_rarity_weight(cid) for cid in shared)
                # Order top concepts by rarity (rarest first) — these are
                # the highest-signal shared concepts
                ranked = sorted(shared, key=_rarity_weight, reverse=True)[:5]
                ph = ",".join(["?"] * len(ranked))
                rows = conn.execute(
                    f"SELECT concept_id, name FROM concept WHERE concept_id IN ({ph})",
                    list(ranked),
                ).fetchall()
                name_by_id = {int(r[0]): r[1] for r in rows}
                top_names = [name_by_id[cid] for cid in ranked if cid in name_by_id]
                matrix.append(_CandidateForJob(
                    candidate_name=c.name, job_name=job.name,
                    coverage=len(shared),
                    score=weighted,
                    top_concepts=top_names,
                ))
        return matrix


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_matrix(d: _Decomposition) -> str:
    lines = [f"# {d.domain} — Coverage Matrix", ""]
    if not d.candidates or not d.jobs:
        lines.append("_Empty matrix — no candidates or jobs._")
        return "\n".join(lines)

    header = ["Candidate"] + [j.name for j in d.jobs] + ["Docs", "Chapters"]
    sep = ["---"] + [":---:"] * len(d.jobs) + ["---:", "---:"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(sep) + "|")
    by_pair = {(m.candidate_name, m.job_name): m for m in d.matrix}
    # Order candidates by total rarity-weighted score across jobs (desc).
    # Raw count totals would re-introduce the neighborhood-size bias.
    totals = {c.name: sum(by_pair[(c.name, j.name)].score for j in d.jobs)
              for c in d.candidates}
    ordered = sorted(d.candidates, key=lambda c: -totals[c.name])
    for c in ordered:
        cells = [f"**{c.name}**"]
        for j in d.jobs:
            cell = by_pair[(c.name, j.name)]
            if cell.coverage == 0:
                cells.append("—")
            else:
                # Display raw coverage with rarity-weighted score in parens
                cells.append(f"{cell.coverage} (·{cell.score:.1f})")
        cells.append(str(c.n_doc_sections))
        cells.append(str(c.n_chapters))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "_Each cell shows **raw shared-concept count** (left) and "
        "**rarity-weighted score** (in parens). The weighted score "
        "ranks within each job — a shared concept that appears in many "
        "candidates' neighborhoods (low signal) contributes less than "
        "a rare/specific one. Row order uses the cross-job total of "
        "the weighted score._"
    )
    return "\n".join(lines)


def _render_job(d: _Decomposition, job: _Job) -> str:
    by_pair = {(m.candidate_name, m.job_name): m for m in d.matrix}
    # Rank by weighted score; ties broken by raw coverage.
    ranked = sorted(
        d.candidates,
        key=lambda c: (-by_pair[(c.name, job.name)].score,
                       -by_pair[(c.name, job.name)].coverage),
    )
    lines = [f"# {job.name}", ""]
    if job.matched_keywords:
        lines.append(f"_Keywords: {', '.join(job.matched_keywords)}_")
        lines.append("")
    lines.append(f"**Cluster size:** {len(job.concept_cluster)} concepts")
    lines.append("")
    lines.append("## Candidates ranked by rarity-weighted score")
    lines.append("")
    for c in ranked:
        m = by_pair[(c.name, job.name)]
        if m.coverage == 0:
            continue
        lines.append(f"### {c.name}  _(score: {m.score:.1f}, "
                     f"coverage: {m.coverage})_")
        lines.append("")
        if c.description:
            lines.append(_short(c.description, 220))
            lines.append("")
        if m.top_concepts:
            lines.append("Shared concepts (rarest first): " + ", ".join(
                f"`{n}`" for n in m.top_concepts))
            lines.append("")
    zero = [c for c in ranked if by_pair[(c.name, job.name)].coverage == 0]
    if zero:
        lines.append("---")
        lines.append("")
        lines.append("_Candidates with no measurable coverage for this job: "
                     + ", ".join(c.name for c in zero) + "_")
        lines.append("")
    return "\n".join(lines)


def _render_landscape(d: _Decomposition) -> str:
    lines = [f"# {d.domain} — Library Landscape", ""]
    lines.append(f"_{len(d.candidates)} candidates × {len(d.jobs)} "
                 "jobs-to-be-done. Coverage signals are from the corpus.  "
                 "This is a discovery doc, not an endorsement._")
    lines.append("")

    # Matrix
    lines.append(_render_matrix(d))
    lines.append("")

    # Jobs sections inline
    for job in d.jobs:
        lines.append("---")
        lines.append("")
        lines.append(_render_job(d, job))
        lines.append("")

    return "\n".join(lines)


def _render_decisions(d: _Decomposition) -> str:
    lines = [f"# {d.domain} — Decision Shortcuts", ""]
    if not d.candidates or not d.jobs:
        lines.append("_Insufficient data for decision shortcuts._")
        return "\n".join(lines)

    by_pair = {(m.candidate_name, m.job_name): m for m in d.matrix}
    shortcuts: list[tuple[str, str]] = []
    for job in d.jobs:
        # Rank by weighted score (specificity-aware) — eliminates the
        # neighborhood-size bias of raw coverage.
        ranked = sorted(
            d.candidates,
            key=lambda c, jn=job.name: (-by_pair[(c.name, jn)].score,
                                        -by_pair[(c.name, jn)].coverage),
        )
        top = [c for c in ranked if by_pair[(c.name, job.name)].coverage > 0]
        if not top:
            continue
        winner = top[0]
        m = by_pair[(winner.name, job.name)]
        shortcuts.append((
            f"If your job is **{job.name}**",
            f"start with **{winner.name}** "
            f"(score {m.score:.1f}, coverage {m.coverage}"
            + (f", {winner.n_doc_sections} doc sections" if winner.has_current_docs else "")
            + ")",
        ))
    if not shortcuts:
        lines.append("_No job had a clear leader._")
    else:
        for q, a in shortcuts:
            lines.append(f"- {q} → {a}")
    lines.append("")
    lines.append("_Shortcuts pick the candidate with the highest "
                 "**rarity-weighted score** per job — a shared concept "
                 "common to all candidates contributes little; a rare "
                 "shared concept contributes a lot. Coverage is a "
                 "corpus signal, not a substitute for evaluating fit "
                 "against your own constraints (license, language "
                 "ecosystem, runtime, maturity)._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class LandscapePlanner:
    def plan(
        self,
        conn: duckdb.DuckDBPyConnection,
        decomposition: _Decomposition,
        *,
        package_name: Optional[str] = None,
        **_: Any,
    ) -> GenPlan:
        d = decomposition
        pkg = package_name or f"library-landscape-{_slugify(d.domain)}"
        plan = GenPlan(
            generator_type=GENERATOR_TYPE,
            package_name=pkg,
            domain=d.domain,
            source_query=d.domain,
            package_metadata={
                "n_candidates": len(d.candidates),
                "n_jobs": len(d.jobs),
                "anchor_concept_ids": list(d.anchor_concept_ids),
            },
            notes=list(d.notes),
        )

        # Domain framing unit
        plan.units.append(GenUnit(
            unit_type="domain_framing",
            name=d.domain,
            ordinal=0,
            logical_key="domain_framing",
            content_markdown=(
                f"_Anchored on {len(d.anchor_concept_ids)} concept(s); "
                f"discovered {len(d.candidates)} candidates._"
            ),
            sources=[("concept", cid, 1.0, 1.0, None)
                     for cid in d.anchor_concept_ids],
        ))

        # Job units
        for i, job in enumerate(d.jobs, start=1):
            plan.units.append(GenUnit(
                unit_type="job",
                name=job.name,
                ordinal=i,
                logical_key=f"job_{_slugify(job.name)}",
                metadata={
                    "cluster_size": len(job.concept_cluster),
                    "matched_keywords": job.matched_keywords,
                },
            ))

        # Candidate-for-job cells
        by_pair = {(m.candidate_name, m.job_name): m for m in d.matrix}
        for c in d.candidates:
            for job in d.jobs:
                m = by_pair.get((c.name, job.name))
                if not m or m.coverage == 0:
                    continue
                src: list[tuple[str, int, float, float, Optional[str]]] = []
                if c.concept_id is not None:
                    src.append(("concept", c.concept_id, float(m.coverage), 1.0, None))
                plan.units.append(GenUnit(
                    unit_type="candidate_for_job",
                    name=f"{c.name} × {job.name}",
                    # Ordinal uses score (×100, integer-quantised) so that
                    # candidates rank by specificity-aware fit, not raw
                    # neighborhood overlap.
                    ordinal=int(round(m.score * 100)),
                    parent_unit_key=f"job_{_slugify(job.name)}",
                    logical_key=f"cell_{_slugify(c.name)}_{_slugify(job.name)}",
                    content_markdown=(
                        ", ".join(m.top_concepts) if m.top_concepts else ""
                    ),
                    metadata={
                        "coverage": m.coverage,
                        "score": m.score,
                        "doc_source_id": c.doc_source_id,
                    },
                    sources=src,
                ))

        # Decision shortcut units — rank by score, tie-break on coverage
        empty_cell = _CandidateForJob("", "", 0, 0.0, [])
        for i, job in enumerate(d.jobs, start=1):
            ranked = sorted(
                d.candidates,
                key=lambda c, jn=job.name: (
                    -by_pair.get((c.name, jn), empty_cell).score,
                    -by_pair.get((c.name, jn), empty_cell).coverage,
                ),
            )
            top = [c for c in ranked
                   if by_pair.get((c.name, job.name), empty_cell).coverage > 0]
            if not top:
                continue
            winner = top[0]
            wcell = by_pair[(winner.name, job.name)]
            plan.units.append(GenUnit(
                unit_type="decision_shortcut",
                name=f"For {job.name}, start with {winner.name}",
                ordinal=i,
                logical_key=f"decision_{_slugify(job.name)}",
                metadata={
                    "winner": winner.name, "job": job.name,
                    "score": wcell.score, "coverage": wcell.coverage,
                },
            ))

        # Files
        plan.files.append(GenFile(
            filename="_landscape.md",
            content=_render_landscape(d),
            purpose="landscape",
        ))
        plan.files.append(GenFile(
            filename="_matrix.md",
            content=_render_matrix(d),
            purpose="matrix",
        ))
        plan.files.append(GenFile(
            filename="_decisions.md",
            content=_render_decisions(d),
            purpose="decisions",
        ))
        for job in d.jobs:
            plan.files.append(GenFile(
                filename=f"jobs/{_slugify(job.name)}.md",
                content=_render_job(d, job),
                purpose="job",
                unit_logical_key=f"job_{_slugify(job.name)}",
            ))
        return plan


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class LandscapeValidator:
    def validate(
        self,
        conn: duckdb.DuckDBPyConnection,
        plan: GenPlan,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        candidates = [u for u in plan.units if u.unit_type == "candidate_for_job"]
        jobs = [u for u in plan.units if u.unit_type == "job"]
        framing = [u for u in plan.units if u.unit_type == "domain_framing"]

        if not framing:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="domain framing missing — anchor unresolved",
            ))
        if not jobs:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="error",
                message="no jobs-to-be-done emitted",
            ))
        if not candidates:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message="zero candidate × job cells with non-zero coverage; "
                        "the landscape will be empty — try explicit candidates",
            ))
        # Every job should have at least one candidate cell
        job_keys = {u.logical_key for u in jobs}
        cells_by_job = {u.parent_unit_key for u in candidates if u.parent_unit_key}
        orphan_jobs = job_keys - cells_by_job
        if orphan_jobs and candidates:
            issues.append(ValidationIssue(
                unit_logical_key="", severity="warning",
                message=f"{len(orphan_jobs)} job(s) had no candidate coverage: "
                        f"{', '.join(sorted(orphan_jobs))[:200]}",
            ))
        return issues


# ---------------------------------------------------------------------------
# Materializer
# ---------------------------------------------------------------------------


class LandscapeMaterializer:
    def materialize(
        self,
        conn: duckdb.DuckDBPyConnection,
        package_id: int,
        output_root: str,
        *,
        overwrite: bool = True,
    ) -> MaterializeReport:
        row = conn.execute(
            "SELECT name FROM generated_package WHERE package_id = ?",
            [package_id],
        ).fetchone()
        if row is None:
            raise ValueError(f"package_id={package_id} not found")
        pkg_name = row[0]
        out_dir = Path(output_root) / pkg_name
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = conn.execute(
            "SELECT filename, content FROM generated_file "
            "WHERE package_id = ? ORDER BY file_id",
            [package_id],
        ).fetchall()
        written: list[str] = []
        skipped: list[str] = []
        for filename, content in rows:
            target = out_dir / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not overwrite:
                skipped.append(str(target))
                continue
            target.write_text(content)
            written.append(str(target))
        notes: list[str] = []
        if skipped:
            notes.append(f"skipped {len(skipped)} existing files (overwrite=False)")
        return MaterializeReport(
            package_id=package_id, package_name=pkg_name,
            output_root=output_root, file_paths=written, notes=notes,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_library_landscape_generator() -> Generator:
    return Generator(
        generator_type=GENERATOR_TYPE,
        decomposer=LandscapeDecomposer(),
        planner=LandscapePlanner(),
        ranking_mode="generation",
        validator=LandscapeValidator(),
        materializer=LandscapeMaterializer(),
    )
