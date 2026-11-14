"""
guided_conversation.py — ``/kb-guided-conversation``: a workflow skill that
drives a chat from a goal to a finished output (2026-11).

Input: a goal ("design an event-sourced order service"), the output the
user wants at the end ("a design doc"), and optionally the KB topics it is
about. Output: one Agent Skill that Claude follows step by step, asking one
question at a time, keeping a running state, and ending with the output.

The steps come from the KB, not from a template:

    frame     restate the goal, settle audience, constraints, success test
    assess    one check per prerequisite (REQUIRES edges of the topics)
    decide    one choice per group of alternatives (CONTRASTS_WITH edges)
    work      one step per procedure linked to the topics
    produce   assemble the output from a per-type template
    review    check the output against the success test

Prerequisites and alternatives pass the topic gate (embedding similarity to
the topic), so a loosely linked concept can't pull the conversation off
topic; that drift is a known weakness of the Tutorial generator.

    <package>/
      <skill-name>/
        SKILL.md                   the conversation: rules, state, steps
        references/background.md   prerequisite explanations + sources
        references/options.md      the alternatives at each decision
        references/procedures.md   full procedure steps
        references/output-template.md
        references/sources.md
      conversation.json            the same flow, machine-readable
      README.md, _provenance.md, _validation.md
      _prompts/refine-conversation.md   optional subagent pass for wording
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

import duckdb

import agent_skills
import kb_scaffold as ks
from generator import GenFile, GenPlan, GenUnit, Generator, ValidationIssue
from topic_gate import DEFAULT_MIN_SIMILARITY, gate_concepts, procedure_similarities

LOG = logging.getLogger("mypub-guided-conversation")
GENERATOR_TYPE = "guided_conversation"

OUTPUT_TYPES = ("document", "plan", "decision", "code", "checklist")
_OUTPUT_KEYWORDS = [
    ("decision", ("decision", "adr", "choose", "choice", "recommendation", "evaluate")),
    ("plan", ("plan", "roadmap", "migration", "rollout", "schedule", "strategy")),
    ("code", ("code", "script", "implementation", "prototype", "scaffold", "notebook",
              "pipeline", "query", "sql")),
    ("checklist", ("checklist", "runbook", "playbook", "procedure", "steps")),
    ("document", ("doc", "document", "spec", "report", "article", "brief", "memo",
                  "outline", "proposal")),
]

OUTPUT_TEMPLATES: dict[str, list[str]] = {
    "document": ["Summary", "Context and goal", "{decisions}", "{work}", "Open questions",
                 "Sources"],
    "plan": ["Goal and success test", "Starting point", "{decisions}", "Milestones: {work}",
             "Risks", "Next actions"],
    "decision": ["Context", "Constraints", "Options considered", "Decision",
                 "Consequences", "Sources"],
    "code": ["What it does", "Files", "{work}", "How to run", "Tests", "Sources"],
    "checklist": ["Before you start", "{work}", "Verify", "Sources"],
}

@dataclass
class Step:
    key: str
    kind: str                    # frame | assess | decide | work | produce | review
    title: str
    purpose: str
    ask: list[str] = field(default_factory=list)
    do: list[str] = field(default_factory=list)
    done_when: str = ""
    captures: list[str] = field(default_factory=list)
    ref: Optional[str] = None    # references/<file>.md#anchor
    skip_if: Optional[str] = None


@dataclass
class Prereq:
    concept_id: int
    name: str
    description: Optional[str]
    depth: int
    source: Optional[ks.Citation] = None


@dataclass
class Option:
    concept_id: int
    name: str
    description: Optional[str]


@dataclass
class Conversation:
    goal: str
    output: str
    output_type: str
    audience: Optional[str]
    topics: list[ks.TopicMaterial]
    prereqs: list[Prereq]
    decisions: list[tuple[str, list[Option]]]     # (topic name, options incl. topic)
    procedures: list[ks.Procedure]
    proc_scores: dict[int, int] = field(default_factory=dict)   # procedure_id -> relevance
    comparison: bool = False
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Topic and output detection
# ---------------------------------------------------------------------------


def detect_output_type(output: str, hint: Optional[str] = None) -> str:
    if hint:
        h = hint.lower().strip()
        if h not in OUTPUT_TYPES:
            raise ValueError(f"output_type must be one of {', '.join(OUTPUT_TYPES)}")
        return h
    words = set(re.findall(r"[a-z]+", (output or "").lower()))
    for kind, kws in _OUTPUT_KEYWORDS:
        if words & set(kws):
            return kind
    return "document"


def _coverage(conn: duckdb.DuckDBPyConnection, cid: int) -> int:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM procedure_concept WHERE concept_id = ?)"
        " + (SELECT COUNT(*) FROM concept_relation WHERE from_concept_id = ? OR to_concept_id = ?)",
        [cid, cid, cid]).fetchone()
    return int(row[0] or 0)


def topics_from_goal(conn: duckdb.DuckDBPyConnection, resolver: Any, goal: str,
                     limit: int = 3) -> list[str]:
    """Concept names the goal mentions (see kb_scaffold.concepts_in_text)."""
    return ks.concepts_in_text(conn, resolver, goal, limit=limit)


_COMPARE_RE = re.compile(r"\b(between|versus|vs\.?|compare|comparing|or)\b", re.I)


def is_comparison(goal: str, n_topics: int) -> bool:
    """'Choose between Kafka and Pulsar', 'Kafka vs Pulsar', 'compare A with B'."""
    return n_topics >= 2 and bool(_COMPARE_RE.search(goal or ""))


def relevance(goal: str, text: str) -> int:
    """Specific goal words (generic ones excluded, crude stems) that ``text`` shares."""
    return len(ks.stems(goal) & ks.stems(text))


# ---------------------------------------------------------------------------
# KB retrieval
# ---------------------------------------------------------------------------


def _first_source(conn: duckdb.DuckDBPyConnection, cid: int) -> Optional[ks.Citation]:
    row = conn.execute(
        """
        SELECT source_type, source_id, COUNT(*) AS n FROM concept_relation
         WHERE (from_concept_id = ? OR to_concept_id = ?)
           AND source_type IN ('chapter', 'doc_section')
         GROUP BY source_type, source_id ORDER BY n DESC, source_id LIMIT 1
        """, [cid, cid]).fetchone()
    if not row:
        return None
    label = ks.source_label(conn, row[0], int(row[1]))
    return ks.Citation(row[0], int(row[1]), label) if label else None


def prerequisites(conn: duckdb.DuckDBPyConnection, topic_ids: Sequence[int], *,
                  max_depth: int = 2, limit: int = 4,
                  min_similarity: float = DEFAULT_MIN_SIMILARITY) -> tuple[list[Prereq], int]:
    """REQUIRES ancestors of the topics, nearest first, gated to the first topic.
    Returns (prereqs, number dropped by the gate)."""
    if not topic_ids:
        return [], 0
    ph = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"""
        WITH RECURSIVE pre(concept_id, depth) AS (
          SELECT to_concept_id, 1 FROM concept_relation
           WHERE from_concept_id IN ({ph}) AND relation_type = 'REQUIRES'
          UNION
          SELECT cr.to_concept_id, p.depth + 1 FROM concept_relation cr
            JOIN pre p ON p.concept_id = cr.from_concept_id
           WHERE cr.relation_type = 'REQUIRES' AND p.depth < ?
        )
        SELECT MIN(p.depth) AS depth, c.concept_id, c.name, c.description
          FROM pre p JOIN concept c USING (concept_id)
         WHERE c.concept_id NOT IN ({ph})
         GROUP BY c.concept_id, c.name, c.description
        """, list(topic_ids) + [max_depth] + list(topic_ids)).fetchall()
    rows = sorted(rows, key=lambda r: (r[0], -_coverage(conn, int(r[1])), r[2]))
    kept, dropped, _ = gate_concepts(conn, topic_ids[0], [int(r[1]) for r in rows],
                                     min_similarity)
    keep = set(kept)
    out: list[Prereq] = []
    seen: set[str] = set()
    for depth, cid, name, desc in rows:
        if int(cid) not in keep or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(Prereq(int(cid), name, desc, int(depth), _first_source(conn, int(cid))))
        if len(out) >= limit:
            break
    return out, len(dropped)


def alternatives(conn: duckdb.DuckDBPyConnection, tm: ks.TopicMaterial, *, limit: int = 3,
                 min_similarity: float = DEFAULT_MIN_SIMILARITY) -> tuple[list[Option], int]:
    """CONTRASTS_WITH neighbours of a topic (both directions), gated to it."""
    rows = conn.execute(
        """
        SELECT c.concept_id, c.name, c.description, max(r.confidence) AS conf
          FROM concept_relation r
          JOIN concept c ON c.concept_id = CASE WHEN r.from_concept_id = ?
                                                THEN r.to_concept_id ELSE r.from_concept_id END
         WHERE r.relation_type = 'CONTRASTS_WITH'
           AND (r.from_concept_id = ? OR r.to_concept_id = ?)
           AND c.concept_id != ?
         GROUP BY c.concept_id, c.name, c.description
         ORDER BY conf DESC, c.name
        """, [tm.concept_id] * 4).fetchall()
    kept, dropped_ids, _ = gate_concepts(conn, tm.concept_id, [int(r[0]) for r in rows],
                                         min_similarity)
    dropped = len(dropped_ids)
    keep = set(kept)
    # An alternative is the same kind of thing and not a part of the topic: drop other
    # concept types ("Topic" is not an alternative to the Kafka tool) and names that
    # contain the topic's words ("Transactions in Kafka", "kafka-topics.sh").
    types = {r[0] for r in conn.execute(
        "SELECT DISTINCT concept_type FROM concept WHERE lower(name) = lower(?)",
        [tm.name]).fetchall() if r[0]}
    topic_words = ks.stems(tm.name, drop_generic=False)
    ctype = {int(r[0]): r[1] for r in conn.execute(
        "SELECT concept_id, concept_type FROM concept WHERE concept_id IN ("
        + (",".join("?" * len(rows)) or "NULL") + ")", [int(r[0]) for r in rows]).fetchall()}
    out: list[Option] = []
    seen = {tm.name.lower()}
    for cid, name, desc, _conf in rows:
        if types and ctype.get(int(cid)) and ctype[int(cid)] not in types:
            dropped += 1
            continue
        if topic_words & ks.stems(name, drop_generic=False):
            dropped += 1
            continue
        if int(cid) in keep and name.lower() not in seen:
            seen.add(name.lower())
            out.append(Option(int(cid), name, desc))
        if len(out) >= limit:
            break
    return out, dropped


# ---------------------------------------------------------------------------
# Step planning
# ---------------------------------------------------------------------------


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _field(prefix: str, text: str, limit: int = 32) -> str:
    """State field name: prefix + snake words, cut on a word boundary."""
    words = _anchor(text).split("-")
    out = ""
    for w in words:
        if out and len(out) + 1 + len(w) > limit:
            break
        out = f"{out}_{w}" if out else w
    return f"{prefix}_{out}"


def _noun(output: str) -> str:
    """'a design doc' -> 'design doc', for 'the design doc'."""
    return re.sub(r"^(a|an|the|some)\s+", "", output.strip(), flags=re.I) or output


def plan_steps(c: Conversation, max_steps: int = 8) -> list[Step]:
    out_word = c.output_type
    noun = _noun(c.output)
    steps: list[Step] = [Step(
        key="frame", kind="frame", title="Frame the goal",
        purpose="Agree on what we are making, for whom, and how we'll know it's done.",
        ask=[f"Restate the goal in one sentence and ask the user to confirm or correct it: "
             f"\"{c.goal}\".",
             f"Who will use the {noun}, and for what?"
             + (f" (Default audience: {c.audience}.)" if c.audience else ""),
             "Any constraints: deadline, length, tools or stack, must-include items?",
             "What would make the result good enough to stop?"],
        do=["Keep each answer in the state block."],
        done_when="The user has confirmed the goal and given a success test.",
        captures=["goal", "audience", "constraints", "success_test"],
    )]
    for p in c.prereqs:
        steps.append(Step(
            key=f"assess-{_anchor(p.name)}", kind="assess", title=f"Check: {p.name}",
            purpose=f"The goal builds on {p.name}; make sure the user has it.",
            ask=[f"Ask how comfortable the user is with {p.name}: yes, roughly, or no."],
            do=[f"If \"roughly\" or \"no\", explain {p.name} in three or four sentences from "
                f"references/background.md#{_anchor(p.name)}, then ask one check question.",
                "If \"yes\", move on without explaining."],
            done_when=f"The user can say in their own words what {p.name} is for.",
            captures=[_field("knows", p.name)],
            ref=f"references/background.md#{_anchor(p.name)}",
            skip_if="the user says they already know it",
        ))
    for topic, opts in c.decisions:
        names = [o.name for o in opts]
        steps.append(Step(
            key=f"decide-{_anchor(topic)}", kind="decide",
            title=(f"Compare {' and '.join(names)}" if topic == "comparison"
                   else f"Choose an approach: {topic}"),
            purpose=(f"Compare {', '.join(names[:-1])} and {names[-1]} for the user's case; "
                     "the choice shapes the rest." if c.comparison and topic == "comparison" else
                     f"The sources set {', '.join(names[1:])} against {names[0]}; settle "
                     "which fits before going further."),
            ask=["Ask which constraints matter most here (from the frame step), then ask the "
                 f"user to pick: {', '.join(names)}, or none of these."],
            do=["Before asking, give each option in one line from references/options.md#"
                f"{_anchor(topic)}, with when it fits."]
            + (["Compare them on the criteria the user named, one criterion at a time, "
                "from the references; mark where the sources say nothing."]
               if topic == "comparison" else [])
            + ["Recommend one option against the user's constraints, and say why."],
            done_when="The user has picked an option and the reason is recorded.",
            captures=[_field("decision", topic)],
            ref=f"references/options.md#{_anchor(topic)}",
        ))
    hands_on = c.output_type in ("code", "checklist")
    if c.output_type == "decision" and c.procedures:
        c.notes.append("decision output: procedures stay in references/procedures.md "
                       "(what adopting an option involves) rather than becoming steps")
    for p in ([] if c.output_type == "decision" else c.procedures):
        pre = ks.first_sentence(p.preconditions, 200)
        ref = f"references/procedures.md#{_anchor(p.name)}"
        if hands_on:
            purpose = f"Work through this procedure for the user's case ({out_word} section)."
            do = [f"Walk through the steps in {ref} one at a time, adapting each to the "
                  "captured state; skip steps that don't apply and say so.",
                  "Draft the matching part of the output as you go."]
        else:
            purpose = (f"Use the sources' procedure as input to the {noun}: what this part "
                       "involves for the user's case, not running it.")
            do = [f"Summarise the steps in {ref} that matter for the goal; note the ones "
                  "that don't apply.",
                  f"Draft the matching section of the {noun}, with the user's specifics and "
                  "any decision it depends on."]
        steps.append(Step(
            key=f"work-{_anchor(p.name)[:40]}", kind="work", title=p.name,
            purpose=purpose,
            ask=([f"Confirm the starting point: {pre}"] if pre and hands_on else [])
            + ["Ask for the specifics this part needs from the user's situation."],
            do=do,
            done_when="The user has confirmed the result of the last step that applies.",
            captures=[_field("result", p.name)],
            ref=f"references/procedures.md#{_anchor(p.name)}",
        ))
    steps.append(Step(
        key="produce", kind="produce", title=f"Produce the {noun}",
        purpose="Assemble everything captured into the output.",
        ask=["Ask whether the user wants it in the chat or written to a file, and the format."],
        do=["Fill references/output-template.md with the state block's contents.",
            "Cite the sources the steps used; mark anything the user supplied as theirs."],
        done_when=f"The {noun} exists in full.",
        captures=["output_location"],
        ref="references/output-template.md",
    ))
    steps.append(Step(
        key="review", kind="review", title="Review against the success test",
        purpose="Check the output does what the frame step said it must.",
        ask=["Show how the output meets each part of the success test; ask what is missing."],
        do=["Fix what the user points out, then list open questions and next steps."],
        done_when="The user accepts the output or ends the conversation.",
    ))
    # Keep frame, decisions, produce and review; trim assess, then work, to fit.
    fixed = {"frame", "decide", "produce", "review"}
    while len(steps) > max_steps:
        for kind in ("assess", "work"):
            idx = [i for i, s in enumerate(steps) if s.kind == kind]
            if idx:
                c.notes.append(f"trimmed step {steps[idx[-1]].title!r} to fit max_steps")
                del steps[idx[-1]]
                break
        else:
            break
    if len([s for s in steps if s.kind not in fixed]) == 0 and c.topics:
        c.notes.append("no prerequisites, alternatives or procedures in the KB for these "
                       "topics; the conversation is frame → produce → review only")
    return steps


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def skill_description(c: Conversation) -> str:
    topics = ", ".join(t.name for t in c.topics) or "the goal"
    return agent_skills.fit_description(
        f"Guided conversation that takes the user from a goal to a finished {_noun(c.output)}: "
        f"{c.goal.rstrip('.')}. Asks one question at a time, checks prerequisites, settles "
        f"decisions and works through the procedures for {topics}. Use when the user wants "
        f"to work toward this {c.output_type} step by step.")


def render_body(c: Conversation, title: str) -> str:
    lines = [
        f"# {title}", "",
        f"**Goal:** {c.goal}  ",
        f"**Output:** {c.output} ({c.output_type})", "",
        "Run when the user types the command; `$ARGUMENTS` may carry their starting point.", "",
        "## How to run this conversation", "",
        "- Ask **one question at a time** and wait for the answer.",
        "- Keep a short **state block** (the fields below) and update it after each answer. "
        "Show it when the user asks where things stand or comes back later.",
        "- The user can say *skip*, *back* or *stop* at any point. Skipping a step records "
        "it as skipped; don't argue.",
        "- Explain from `references/` only; if a reference is thin, say so rather than guess.",
        "- Each step ends when its **Done when** holds. Then say which step is next.", "",
        "## State", "",
    ]
    caps = list(dict.fromkeys(cap for s in c.steps for cap in s.captures))
    lines += [f"- `{cap}`" for cap in caps] + ["", "## Steps", ""]
    for i, s in enumerate(c.steps, 1):
        lines.append(f"### {i}. {s.title}")
        lines.append("")
        lines.append(s.purpose)
        lines.append("")
        if s.ask:
            lines.append("**Ask:**")
            lines += [f"- {a}" for a in s.ask]
        if s.do:
            lines.append("")
            lines.append("**Do:**")
            lines += [f"- {d}" for d in s.do]
        lines.append("")
        lines.append(f"**Done when:** {s.done_when}")
        if s.skip_if:
            lines.append(f"  **Skip if:** {s.skip_if}.")
        lines.append("")
    lines += ["## Finishing", "",
              "End with the output, the state block, and the open questions. "
              "Sources are in `references/sources.md`.", ""]
    return "\n".join(lines)


def render_background(c: Conversation) -> str:
    lines = ["# Background", "",
             "Prerequisites of the goal's topics, nearest first (from REQUIRES edges).", ""]
    if not c.prereqs:
        lines.append("_No prerequisites recorded in the knowledge base._")
    for p in c.prereqs:
        lines += [f"## {p.name}", "",
                  " ".join((p.description or "No description in the KB.").split()), ""]
        if p.source:
            lines += [f"Source: {p.source.markdown()}", ""]
    return "\n".join(lines) + "\n"


def render_options(c: Conversation) -> str:
    lines = ["# Options", "", "Options at each decision: the ones the goal names, or "
             "alternatives the sources set against a topic (CONTRASTS_WITH edges, same kind "
             "of thing, topic-gated).", ""]
    if not c.decisions:
        lines.append("_No contrasting alternatives recorded for these topics._")
    for topic, opts in c.decisions:
        lines += [f"## {topic}", ""]
        if topic == "comparison":
            lines += ["The options the goal names, as the knowledge base describes them.", ""]
        for o in opts:
            lines.append(f"- **{o.name}**: {ks.first_sentence(o.description, 300) or 'no description'}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_procedures(c: Conversation) -> str:
    lines = ["# Procedures", ""]
    if not c.procedures:
        lines.append("_No procedures linked to these topics._")
    for p in c.procedures:
        lines += [f"## {p.name}", ""]
        if p.preconditions:
            lines += [f"**Before you start:** {' '.join(p.preconditions.split())}", ""]
        lines += [p.steps.strip(), ""]
        if p.failure_modes:
            lines += [f"**Watch out for:** {' '.join(p.failure_modes.split())}", ""]
        if p.source:
            lines += [f"Source: {p.source.markdown()}", ""]
    return "\n".join(lines) + "\n"


def render_template(c: Conversation) -> str:
    work = [s.title for s in c.steps if s.kind == "work"]
    decisions = [s.title.replace("Choose an approach: ", "Approach: ")
                 for s in c.steps if s.kind == "decide"]
    lines = [f"# Output template: {_noun(c.output)}", "",
             f"Type: **{c.output_type}**. Fill each section from the state block.", ""]
    for sec in OUTPUT_TEMPLATES[c.output_type]:
        if sec == "{work}":
            lines += [f"## {w}" for w in work] or ["## Main content"]
        elif sec == "{decisions}":
            lines += [f"## {d}" for d in decisions]
        elif sec.endswith("{work}"):
            lines.append(f"## {sec.replace(': {work}', '')}")
            lines += [f"- {w}" for w in work] or ["- (milestones)"]
        else:
            lines.append(f"## {sec}")
        lines.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)) + "\n"


def all_sources(c: Conversation) -> list[ks.Citation]:
    out: list[ks.Citation] = []
    for t in c.topics:
        out += t.sources
    out += [p.source for p in c.prereqs if p.source]
    out += [p.source for p in c.procedures if p.source]
    return out


def conversation_json(c: Conversation, name: str) -> str:
    return json.dumps({
        "skill": name, "goal": c.goal, "output": c.output, "output_type": c.output_type,
        "audience": c.audience, "topics": [t.name for t in c.topics],
        "state": list(dict.fromkeys(cap for s in c.steps for cap in s.captures)),
        "steps": [{k: v for k, v in asdict(s).items() if v not in (None, [], "")}
                  for s in c.steps],
    }, indent=2) + "\n"


REFINE_PROMPT = """\
# Refine the {name} conversation

Edit `{name}/SKILL.md` in place so the questions read naturally for this goal:
{goal}

Rules:
- Keep the frontmatter, the step order, every step's **Done when**, and the State list.
- Rewrite each **Ask** as the actual question Claude would say, specific to the goal and
  topics. Keep one question per bullet.
- Use only facts in `references/` and the current SKILL.md. Do not add steps, tools or
  sources. Where a reference is thin, keep the step and say so.
- Keep it under 500 lines.
"""


def readme(c: Conversation, name: str) -> str:
    return f"""\
# {name}

A guided conversation from **{c.goal}** to **{c.output}**, built from the myPub
knowledge base ({len(c.steps)} steps).

## Use it

- **One project:** copy `{name}/` into the project's `.claude/skills/`, then type
  `/{name}` in Claude Code.
- **Everywhere:** copy it into `~/.claude/skills/`.
- **In a plugin:** put it under `skills/` of a plugin (see `/kb-plugin-scaffold`).
- **Elsewhere:** `conversation.json` holds the same flow for another host or an MCP App.

The questions are assembled from KB rows; `_prompts/refine-conversation.md` is an
optional subagent pass that rewrites their wording without changing the flow.
"""


# ---------------------------------------------------------------------------
# Generator components
# ---------------------------------------------------------------------------


class ConversationDecomposer:
    def decompose(self, conn: duckdb.DuckDBPyConnection, resolver: Any, query: str,
                  **kw: Any) -> Conversation:
        goal = " ".join(query.split())
        output = (kw.get("output") or "").strip() or "a written summary"
        otype = detect_output_type(output, kw.get("output_type"))
        notes: list[str] = []
        names = list(kw.get("topics") or []) or topics_from_goal(conn, resolver, goal)
        if not kw.get("topics"):
            notes.append("topics from the goal: " + (", ".join(names) or "none found"))
        max_procs = max(1, int(kw.get("max_procedures", 3)))
        topics: list[ks.TopicMaterial] = []
        for n in names:
            tm = ks.topic_material(conn, resolver, n, purpose=goal, max_procedures=12)
            if tm is None:
                notes.append(f"topic {n!r} not in the knowledge base; skipped")
            elif all(t.concept_id != tm.concept_id for t in topics):
                topics.append(tm)
        ids = [t.concept_id for t in topics]
        prereqs, dropped = prerequisites(conn, ids, limit=int(kw.get("max_prereqs", 3)))
        if dropped:
            notes.append(f"topic gate dropped {dropped} off-topic prerequisite(s)")

        comparison = is_comparison(goal, len(topics))
        decisions: list[tuple[str, list[Option]]] = []
        if comparison:
            decisions.append(("comparison", [Option(t.concept_id, t.name, t.description)
                                             for t in topics]))
            notes.append("comparison goal: one decision across "
                         + ", ".join(t.name for t in topics))
        else:
            for t in topics:
                opts, d = alternatives(conn, t)
                if d:
                    notes.append(f"dropped {d} alternative(s) to {t.name} "
                                 "(off topic, a different kind of thing, or a part of it)")
                if opts:
                    decisions.append((t.name, [Option(t.concept_id, t.name, t.description)]
                                      + opts))

        # Procedures: pooled across topics, gated to their own topic, ranked by how
        # many specific goal words they share; keep the best few.
        pool: list[tuple[int, int, ks.Procedure]] = []
        seen: set[int] = set()
        gated = 0
        for ti, t in enumerate(topics):
            sims = procedure_similarities(conn, t.concept_id, [p.procedure_id for p in t.procedures])
            for p in t.procedures:
                if p.procedure_id in seen:
                    continue
                seen.add(p.procedure_id)
                sim = sims.get(p.procedure_id)
                if sim is not None and sim < DEFAULT_MIN_SIMILARITY:
                    gated += 1
                    continue
                score = relevance(goal, p.name + " " + (p.steps or "")[:400])
                pool.append((score, ti, p))
        if gated:
            notes.append(f"topic gate dropped {gated} off-topic procedure(s)")
        best = max((sc for sc, _, _ in pool), default=0)
        # Every pooled procedure is linked to a topic and passed the gate; the floor
        # only bites when some procedures match the goal's own words better.
        floor = min(2, best)
        chosen = sorted((x for x in pool if x[0] >= floor), key=lambda x: (-x[0], x[1]))
        weak = len(pool) - len(chosen)
        if weak:
            notes.append(f"left out {weak} procedure(s) that share fewer than {floor} "
                         "specific word(s) with the goal")
        procs = [p for _, _, p in chosen[:max_procs]]
        c = Conversation(goal=goal, output=output, output_type=otype,
                         audience=kw.get("audience"), topics=topics, prereqs=prereqs,
                         decisions=decisions, procedures=procs,
                         proc_scores={p.procedure_id: sc for sc, _, p in chosen},
                         comparison=comparison, notes=notes)
        c.steps = plan_steps(c, max_steps=int(kw.get("max_steps", 8)))
        return c


class ConversationPlanner:
    def plan(self, conn: duckdb.DuckDBPyConnection, c: Conversation, *,
             package_name: Optional[str] = None, skill_name: Optional[str] = None,
             invocation: str = "user", **_: Any) -> GenPlan:
        base = skill_name or ("guide-" + agent_skills.skill_name(c.goal))
        name = agent_skills.skill_name(base)[:60].rstrip("-")
        title = f"Guided: {c.goal.rstrip('.')}"
        body = render_body(c, title)
        md = agent_skills.build_skill_md(
            name, skill_description(c), body,
            metadata={"generator": "mypub guided_conversation",
                      "output_type": c.output_type})
        if invocation == "user":
            md = md.replace("\n---\n\n", '\ndisable-model-invocation: true\n'
                            'argument-hint: "[where you are starting from]"\n---\n\n', 1)
        problems = agent_skills.validate(name, skill_description(c), folder=name, body=body)
        cites = all_sources(c)
        plan = GenPlan(generator_type=GENERATOR_TYPE, package_name=package_name or name,
                       domain=", ".join(t.name for t in c.topics) or c.goal,
                       source_query=c.goal, notes=list(c.notes))
        plan.package_metadata = {
            "skill_name": name, "output_type": c.output_type, "n_steps": len(c.steps),
            "steps": [s.kind for s in c.steps], "topics": [t.name for t in c.topics],
            "topic_concept_ids": [t.concept_id for t in c.topics],
            "n_prereqs": len(c.prereqs), "n_decisions": len(c.decisions),
            "n_procedures": len(c.procedures), "spec_problems": problems,
        }
        plan.units.append(GenUnit(
            unit_type="guided_conversation", name=name, logical_key=name,
            content_markdown=body, metadata={"goal": c.goal, "output": c.output},
            sources=[(ct.source_type, ct.source_id, 1.0, 1.0, None) for ct in cites]
            + [("procedure", p.procedure_id, 1.0, 1.0, None) for p in c.procedures],
        ))
        files = [
            (f"{name}/SKILL.md", md, "skill"),
            (f"{name}/references/background.md", render_background(c), "reference"),
            (f"{name}/references/options.md", render_options(c), "reference"),
            (f"{name}/references/procedures.md", render_procedures(c), "reference"),
            (f"{name}/references/output-template.md", render_template(c), "reference"),
            (f"{name}/references/sources.md",
             ks.render_citations(cites, "Sources").replace(
                 "Run scripts/seed_agent_standards.py and refresh to add them.",
                 "The goal's topics had no linked chapters or doc sections."), "reference"),
            ("conversation.json", conversation_json(c, name), "spec"),
            ("README.md", readme(c, name), "readme"),
            ("_provenance.md", (
                f"# Provenance: {name}\n\nGoal: {c.goal}\n\nTopics: "
                + (", ".join(f"**{t.name}** (`concept:{t.concept_id}`)" for t in c.topics)
                   or "none resolved")
                + f"\n\nSteps: {' → '.join(s.kind for s in c.steps)}\n\n"
                + "\n".join(f"- {n}" for n in c.notes) + "\n\n"
                + ks.render_citations(cites, "Knowledge-base sources")), "provenance"),
            ("_validation.md", _validation_md(name, problems), "validation"),
            ("_prompts/refine-conversation.md",
             REFINE_PROMPT.format(name=name, goal=c.goal), "subagent_prompt"),
        ]
        for path, content, purpose in files:
            plan.files.append(GenFile(filename=path, content=content, purpose=purpose,
                                      unit_logical_key=name))
        return plan


def _validation_md(name: str, problems: Sequence[str]) -> str:
    lines = [f"# Agent Skills spec check: {name}", ""]
    lines += [f"- {p}" for p in problems] if problems else [
        "Passes: `name` is a valid slug matching its folder; `description` is present and "
        "within 1024 characters; the body is within the recommended length."]
    return "\n".join(lines) + "\n"


class ConversationValidator:
    def validate(self, conn: duckdb.DuckDBPyConnection, plan: GenPlan) -> list[ValidationIssue]:
        meta = plan.package_metadata
        out = []
        for p in meta.get("spec_problems", []):
            sev = "warning" if p.startswith("warning:") else "error"
            out.append(ValidationIssue(meta.get("skill_name", ""), sev, p))
        if not meta.get("topics"):
            out.append(ValidationIssue("", "warning",
                                       "no KB topics resolved; the conversation is generic"))
        elif not (meta.get("n_prereqs") or meta.get("n_decisions") or meta.get("n_procedures")):
            out.append(ValidationIssue("", "warning",
                                       "topics have no prerequisites, alternatives or procedures"))
        return out


def make_guided_conversation_generator() -> Generator:
    return Generator(generator_type=GENERATOR_TYPE, decomposer=ConversationDecomposer(),
                     planner=ConversationPlanner(), ranking_mode="generation",
                     validator=ConversationValidator(), materializer=ks.TreeMaterializer())
