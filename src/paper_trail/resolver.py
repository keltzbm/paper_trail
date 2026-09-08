# src/paper_trail/resolver.py

import argparse
from pathlib import Path

from paper_trail.metadata import fetch_crossref, search_crossref_by_title
from paper_trail.organizer import process_single_pdf


def resolve_unfiled(unfiled_dir: str, output_dir: str):
    """Interactively resolve PDFs sitting in the _unfiled directory."""
    unfiled_path = Path(unfiled_dir).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()

    pdf_files = list(unfiled_path.glob("*.pdf"))
    if not pdf_files:
        print(f"No unfiled PDFs found in {unfiled_path}")
        return

    print(f"=== Interactive Unfiled PDF Resolver ({len(pdf_files)} file(s)) ===")

    for pdf in pdf_files:
        print("\n" + "─" * 60)
        print(f"📄 File: {pdf.name}")

        user_input = input("Enter DOI, Search Title, or [s]kip / [d]elete: ").strip()

        if user_input.lower() in ("s", "skip", ""):
            continue
        elif user_input.lower() in ("d", "delete"):
            pdf.unlink()
            print("  🗑️ Deleted file.")
            continue

        # Check if user entered a DOI or a Search Query
        metadata = None
        if "10." in user_input:
            # Looks like a DOI
            doi = user_input[user_input.find("10.") :]
            metadata = fetch_crossref(doi)
        else:
            # Treat as title search
            print(f" Searching Crossref for: '{user_input}'...")
            metadata = search_crossref_by_title(user_input)

        if metadata:
            print(
                f"  Found: {metadata.get('year')} - {metadata.get('author')} - {metadata.get('title')}"
            )
            confirm = input("  Organize with this metadata? [Y/n]: ").strip()
            if confirm.lower() in ("", "y", "yes"):
                # Move and index PDF using organizer logic
                process_single_pdf(pdf, output_path, forced_metadata=metadata)
                print("  ✓ Successfully organized!")
        else:
            print("  ✗ No metadata found for that input.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactively resolve unfiled PDFs.")
    parser.add_argument(
        "--unfiled",
        default="/Users/keltzbm/pCloud Drive/research/_unfiled",
        help="Path to _unfiled directory",
    )
    parser.add_argument(
        "--output",
        default="/Users/keltzbm/pCloud Drive/research",
        help="Target organized library directory",
    )
    args = parser.parse_args()

    resolve_unfiled(args.unfiled, args.output)
