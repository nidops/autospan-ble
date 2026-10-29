"""Render the package README and validate local documentation paths and anchors."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from readme_renderer.markdown import render


def prose(markdown: str) -> str:
    return re.sub(r"^```[^\n]*\n.*?^```[^\n]*$", "", markdown, flags=re.M | re.S)


def anchors(path: Path) -> set[str]:
    result: set[str] = set()
    counts: dict[str, int] = {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", prose(path.read_text()), re.M):
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    result.update(re.findall(r'<(?:a|\w+)\b[^>]*\bid=["\']([^"\']+)', path.read_text()))
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"]
    readme = root / "README.md"
    markdown = readme.read_text(encoding="utf8")
    html = render(markdown)
    if not html or "<h1" not in html or "<pre" not in html:
        raise ValueError("README failed to render a complete package description")
    checked = 0
    documents = sorted(root.glob("*.md")) + sorted((root / "docs").rglob("*.md"))
    for document in documents:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", prose(document.read_text())):
            url = urlsplit(target)
            local: Path | None = None
            if url.scheme:
                prefix = f"/nidops/{project}/blob/"
                if url.netloc == "github.com" and url.path.startswith(prefix):
                    _, _, relative = url.path.removeprefix(prefix).partition("/")
                    local = root / unquote(relative)
            else:
                if document == readme and not target.startswith("#"):
                    raise ValueError(f"README relative link breaks in package metadata: {target}")
                local = document.parent / unquote(url.path) if url.path else document
            if local is None:
                continue
            if not local.exists():
                raise ValueError(f"{document.relative_to(root)}: missing link {target}")
            if (
                url.fragment
                and local.suffix == ".md"
                and unquote(url.fragment) not in anchors(local)
            ):
                raise ValueError(f"{document.relative_to(root)}: missing anchor {target}")
            checked += 1
    print(f"Package README renders; {checked} local documentation links/anchors resolve")


if __name__ == "__main__":
    main()
