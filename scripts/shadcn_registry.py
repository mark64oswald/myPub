"""
shadcn_registry.py — turn shadcn/ui registry JSON into Markdown doc pages.

ui.shadcn.com publishes its components, blocks and charts as registry
items (``/r/styles/<style>/<name>.json``): metadata plus the full source
of every file the CLI would copy into a project. The rendered /blocks and
/charts pages are 1-2 MB of live previews with little text, so the
``shadcn-registry`` doc site reads the registry instead: one page per
item, with its description, dependencies, install command and source.

``/r/registries.json`` is the community registry directory (the list
behind ui.shadcn.com/docs/directory and ``npx shadcn add @namespace/…``);
the ``shadcn-directory`` site turns it into one page with a section per
registry.

The docs pages themselves (``/docs/*.md``) are MDX with JSX wrappers;
``clean_mdx`` strips the wrappers and keeps their text.

Pure functions, no network: doc_sites.fetch_page calls them on the
response body.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

SITE = "https://ui.shadcn.com"

# Registry item types the registry site indexes. Examples (registry:example)
# are the demos the docs pages already inline; fonts, themes, styles and
# internal items carry no reusable UI.
ITEM_TYPES = ("registry:ui", "registry:block")

# Sample-data files (dashboard-01/data.json is 12 KB of fake rows) are
# described, not inlined.
MAX_DATA_FILE_CHARS = 1500

_LANG = {".tsx": "tsx", ".ts": "ts", ".jsx": "jsx", ".js": "js", ".css": "css",
         ".json": "json", ".md": "markdown", ".mdx": "mdx"}


def index_item_urls(registry_json: str, registry_url: str,
                    types: Iterable[str] = ITEM_TYPES) -> list[str]:
    """Item URLs for the wanted types in a registry.json, in registry order."""
    data = json.loads(registry_json)
    items = data.get("items", data) if isinstance(data, dict) else data
    wanted = set(types)
    out: dict[str, None] = {}
    for it in items:
        if isinstance(it, dict) and it.get("type") in wanted and it.get("name"):
            out.setdefault(urljoin(registry_url, f"{it['name']}.json"), None)
    return list(out)


def _kind(item: dict) -> str:
    cats = item.get("categories") or []
    if item.get("type") == "registry:ui":
        return "Component source"
    if "charts" in cats or item.get("name", "").startswith("chart-"):
        return "Chart"
    return "Block"


def _docs_link(item: dict) -> Optional[str]:
    name = item.get("name", "")
    cats = item.get("categories") or []
    if item.get("type") == "registry:ui":
        return f"{SITE}/docs/components/{name}"
    for c in cats:
        if c.startswith("charts-"):
            return f"{SITE}/charts/{c.split('-', 1)[1]}#{name}"
    if cats:
        return f"{SITE}/blocks/{cats[0]}#{name}"
    return f"{SITE}/blocks#{name}"


def _lang(path: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]+)$", path)
    return _LANG.get(m.group(1).lower(), "") if m else ""


def _fence(code: str, lang: str) -> str:
    ticks = "```"
    while ticks in code:
        ticks += "`"
    return f"{ticks}{lang}\n{code.rstrip()}\n{ticks}"


def _short_path(path: str, name: str) -> str:
    """registry/new-york-v4/blocks/dashboard-01/components/x.tsx → components/x.tsx"""
    marker = f"/{name}/"
    if marker in path:
        return path.split(marker, 1)[1]
    return re.sub(r"^registry/[^/]+/", "", path)


def item_to_markdown(item: dict) -> str:
    """One registry item as a Markdown page: H1, summary, then one H2 per file."""
    name = item.get("name", "item")
    kind = _kind(item)
    lines = [f"# {kind}: {name}", ""]
    if item.get("title") and item["title"] != name:
        lines += [f"**{item['title']}**", ""]
    if item.get("description"):
        lines += [item["description"].strip(), ""]
    facts = [f"- Registry item `{name}` ({item.get('type', '?')}), "
             f"install with `npx shadcn@latest add {name}`."]
    if item.get("categories"):
        facts.append("- Categories: " + ", ".join(item["categories"]))
    if item.get("registryDependencies"):
        facts.append("- Uses shadcn/ui components: "
                     + ", ".join(f"`{d}`" for d in item["registryDependencies"]))
    if item.get("dependencies"):
        facts.append("- npm dependencies: " + ", ".join(f"`{d}`" for d in item["dependencies"]))
    if item.get("devDependencies"):
        facts.append("- npm dev dependencies: "
                     + ", ".join(f"`{d}`" for d in item["devDependencies"]))
    link = _docs_link(item)
    if link:
        facts.append(f"- Docs and preview: {link}")
    lines += facts + [""]
    if item.get("cssVars"):
        lines += ["## CSS variables", "", _fence(json.dumps(item["cssVars"], indent=2), "json"), ""]
    for f in item.get("files") or []:
        path = f.get("path", "")
        short = _short_path(path, name)
        target = f.get("target")
        head = f"## {short}"
        lines += [head, ""]
        meta = f"File type `{f.get('type', '?')}`"
        if target:
            meta += f", installed at `{target}`"
        lines += [meta + ".", ""]
        content = f.get("content") or ""
        if not content:
            continue
        if f.get("type") == "registry:file" and path.endswith(".json") \
                and len(content) > MAX_DATA_FILE_CHARS:
            lines += [f"Sample data, {len(content):,} characters, not reproduced here.", ""]
            continue
        lines += [_fence(content, _lang(path)), ""]
    return "\n".join(lines).rstrip() + "\n"


def item_json_to_markdown(text: str) -> str:
    return item_to_markdown(json.loads(text))


def directory_to_markdown(text: str) -> str:
    """The community registry directory (/r/registries.json) as one page."""
    regs = json.loads(text)
    if isinstance(regs, dict):
        regs = regs.get("registries") or regs.get("items") or []
    shown = [r for r in regs if isinstance(r, dict) and r.get("name")
             and not (r.get("health") or {}).get("hidden")]
    lines = [
        "# shadcn/ui registry directory", "",
        "Community registries built into the shadcn CLI. Each one installs with "
        "`npx shadcn@latest add @namespace/item` (or through the shadcn MCP "
        f"server). {len(shown)} registries listed.", "",
    ]
    for r in shown:
        lines += [f"## {r['name']}", ""]
        if r.get("description"):
            lines += [r["description"].strip(), ""]
        facts = []
        if r.get("homepage"):
            facts.append(f"- Homepage: {r['homepage']}")
        if r.get("url"):
            facts.append(f"- Item URL template: `{r['url']}`")
        facts.append(f"- Install: `npx shadcn@latest add {r['name']}/<item>`")
        h = r.get("health") or {}
        if h.get("status"):
            score = h.get("score")
            facts.append(f"- Health: {h['status']}"
                         + (f" (score {score:.0f}/100)" if isinstance(score, (int, float)) else ""))
        lines += facts + [""]
    return "\n".join(lines).rstrip() + "\n"


# --- MDX cleanup for the docs pages ----------------------------------------

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_COMPONENT_SOURCE = re.compile(r"<ComponentSource\b([^>]*?)/?>", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
_TAG_LINE = re.compile(r"^\s*</?[A-Z][A-Za-z0-9.]*(\s[^<>]*)?/?>\s*$")
_INLINE_WRAP = re.compile(r"^\s*<([A-Z][A-Za-z0-9]*)(\s[^<>]*)?>(.*?)</\1>\s*$")


def _component_source(m: re.Match) -> str:
    attrs = dict(_ATTR.findall(m.group(1)))
    name = attrs.get("name", "?")
    title = attrs.get("title")
    where = f" ({title})" if title else ""
    return (f"Source code: shadcn/ui registry item `{name}`{where}; "
            f"`npx shadcn@latest add {name}` copies it into the project.")


def clean_mdx(md: str) -> str:
    """Drop JSX wrapper lines (<CodeTabs>, <TabsContent …>, <Steps>) outside
    code fences, keep the text of one-line wrappers (<Step>Install…</Step>),
    and say what a <ComponentSource …/> placeholder stood for."""
    md = _COMPONENT_SOURCE.sub(_component_source, md)
    out: list[str] = []
    fence: Optional[str] = None
    pending_tag: list[str] = []
    for line in md.split("\n"):
        m = _FENCE.match(line)
        if fence is not None:
            out.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and line.strip() == m.group(1):
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        if pending_tag:
            # a multi-line opening tag (<ComponentPreview\n  name="x"\n/>)
            pending_tag.append(line)
            if line.rstrip().endswith(">"):
                pending_tag = []
            continue
        wrap = _INLINE_WRAP.match(line)
        if wrap:
            out.append(wrap.group(3).strip())
            continue
        if _TAG_LINE.match(line):
            continue
        if re.match(r"^\s*<[A-Z][A-Za-z0-9.]*\b[^>]*$", line):
            pending_tag = [line]
            continue
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text)


# Docs sections whose pages are titled by framework alone: installation/vite,
# dark-mode/vite and rtl/vite are all "Vite". Their section goes in front.
_SECTION_LABELS = {"installation": "Installation", "dark-mode": "Dark Mode",
                   "rtl": "RTL", "forms": "Forms"}


def qualify_title(url: str, title: Optional[str]) -> Optional[str]:
    """'Vite' on /docs/dark-mode/vite → 'Dark Mode / Vite'."""
    m = re.match(re.escape(SITE) + r"/docs/([^/]+)/[^/]+/?$", url.split("#", 1)[0])
    if not (title and m and m.group(1) in _SECTION_LABELS):
        return title
    label = _SECTION_LABELS[m.group(1)]
    return title if label.lower() in title.lower() else f"{label} / {title}"


def is_shadcn_url(url: str) -> bool:
    return url.startswith(SITE + "/")


__all__ = ["ITEM_TYPES", "index_item_urls", "item_to_markdown", "item_json_to_markdown",
           "directory_to_markdown", "clean_mdx", "qualify_title", "is_shadcn_url"]


def _demo(argv: Optional[list[str]] = None) -> Any:  # pragma: no cover
    import sys
    path = (argv or sys.argv[1:])[0]
    text = open(path).read()
    print(directory_to_markdown(text) if path.endswith("registries.json")
          else item_json_to_markdown(text))


if __name__ == "__main__":  # pragma: no cover
    _demo()
