import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def parse_single_kbart_file(file_path: Path) -> set[str]:
    """Reads a single KBART CSV/TSV file and returns extracted HTTP URLs."""
    urls = set()
    if not file_path.is_file():
        return urls

    delimiter = "," if file_path.suffix == ".csv" else "\t"
    try:
        with open(file_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                url = row.get("title_url") or row.get("location") or row.get("url")
                if url and url.strip().startswith("http"):
                    urls.add(url.strip())
    except Exception as e:
        print(f"Error reading {file_path.name}: {e}")

    return urls


def parse_kbart(input_path: Path, output_path: Path, max_workers: int = 8) -> int:
    """Parses KBART file(s) concurrently using a thread pool."""
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()

    if input_path.is_file():
        files_to_parse = [input_path]
    elif input_path.is_dir():
        files_to_parse = [
            f
            for f in input_path.glob("*")
            if f.is_file() and f.suffix in [".txt", ".tsv", ".csv"]
        ]
    else:
        print(f"Input path '{input_path}' does not exist.")
        return 0

    print(f"Parsing {len(files_to_parse)} KBART file(s) with {max_workers} workers...")

    all_urls = set()

    # Process KBART files concurrently across worker threads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {
            executor.submit(parse_single_kbart_file, f): f for f in files_to_parse
        }
        for future in as_completed(future_to_file):
            file_urls = future.result()
            all_urls.update(file_urls)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(sorted(all_urls)), encoding="utf-8")

    print(f"Successfully extracted {len(all_urls)} unique URLs to '{output_path}'.")
    return len(all_urls)


def main():
    parser = argparse.ArgumentParser(
        description="Extract paper URLs from KBART files concurrently."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="data/kbart",
        help="Path to KBART file or directory (default: data/kbart)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="data/discovered_urls.txt",
        help="Output URLs text file (default: data/discovered_urls.txt)",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=8,
        help="Number of concurrent worker threads (default: 8)",
    )
    args = parser.parse_args()

    parse_kbart(
        input_path=Path(args.input),
        output_path=Path(args.output),
        max_workers=args.workers,
    )


if __name__ == "__main__":
    main()
