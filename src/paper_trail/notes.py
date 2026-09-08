"""Generates Obsidian-ready markdown notes to sit alongside organized papers."""

from pathlib import Path


def generate_markdown_note(
    dest_dir: Path,
    folder_name: str,
    year: str | int | None,
    author: str | None,
    title: str | None,
    doi: str | None = None,
    resolved_via: str | None = None,
) -> None:
    """Writes a frontmatter-style markdown note co-located with a paper.

    Does nothing if a note already exists at the destination, so any notes
    the user has since edited by hand are preserved. ``resolved_via`` (if
    given) records which metadata-resolution method matched this paper —
    e.g. "isbn" vs "ocr_doi" vs "fuzzy_title_search" — so lower-confidence
    matches can be found and spot-checked later by searching the vault for
    that field, rather than every note looking equally certain.
    """
    note_path = dest_dir / f"{folder_name}.md"
    if note_path.exists():
        return  # Preserve existing user notes

    frontmatter = [
        "---",
        f'title: "{title or ""}"',
        f'author: "{author or ""}"',
        f"year: {year or ''}",
    ]
    if doi:
        frontmatter.append(f'doi: "{doi}"')
    if resolved_via:
        frontmatter.append(f"resolved_via: {resolved_via}")
    frontmatter.extend(["type: paper", "---", "", f"# {title or folder_name}", ""])

    note_path.write_text("\n".join(frontmatter), encoding="utf-8")
