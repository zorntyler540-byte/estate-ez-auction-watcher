"""Extract items by SKU prefix into separate files (sold + not sold + tally)."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

PREFIXES = ("NK", "SA", "Z", "NL")
_LEADING_LETTERS = re.compile(r"^([A-Za-z]+)")
OUTPUT_COLUMNS = [
    "SKU",
    "Item Name",
    "Category Name",
    "Sale Price",
    "Reserve Price",
    "Met Reserve",
    "Buyers Premium",
    "Tax",
    "Total Collected",
    "Status",
    "Invoice Number",
]


def discover_sku_prefixes(input_path: Path) -> list[str]:
    """Find unique letter prefixes at the start of each SKU (e.g. NK800 -> NK).

    Returns prefixes sorted by item count (most common first), then alphabetically.
    SKUs with no leading letters are ignored.
    """
    raw = pd.read_csv(input_path, usecols=["SKU"])
    skus = raw["SKU"].astype(str).str.strip()
    prefixes = skus.str.extract(_LEADING_LETTERS, expand=False).dropna().str.upper()
    if prefixes.empty:
        return []
    counts = prefixes.value_counts()
    # sort by count desc, then name asc for a stable order
    ordered = sorted(counts.index.tolist(), key=lambda p: (-int(counts[p]), p))
    return ordered


def _prepare_items(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ("Sale Price", "Buyers Premium", "Tax"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
        else:
            out[col] = 0.0
    out["Total Collected"] = out["Sale Price"] + out["Buyers Premium"] + out["Tax"]
    if "Status" in out.columns:
        out["Status"] = out["Status"].astype(str).str.strip()
    else:
        out["Status"] = ""
    if "Reserve Price" in out.columns:
        out["Reserve Price"] = pd.to_numeric(out["Reserve Price"], errors="coerce").fillna(0)
    else:
        out["Reserve Price"] = 0.0
    existing_met = out["Met Reserve"].astype(str).str.strip() if "Met Reserve" in out.columns else pd.Series("", index=out.index)
    computed = pd.Series("", index=out.index)
    has_reserve = out["Reserve Price"] > 0
    computed.loc[has_reserve & (out["Sale Price"] >= out["Reserve Price"])] = "Yes"
    computed.loc[has_reserve & (out["Sale Price"] < out["Reserve Price"])] = "No"
    out["Met Reserve"] = existing_met.where(existing_met.isin(["Yes", "No"]), computed)
    return out


def _is_sold(status: str) -> bool:
    return status.lower() == "sold"


def extract_by_prefix(input_path: Path, output_dir: Path, prefixes: tuple[str, ...] = PREFIXES) -> dict[str, Path]:
    raw = pd.read_csv(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for prefix in prefixes:
        mask = raw["SKU"].astype(str).str.upper().str.startswith(prefix.upper())
        subset = _prepare_items(raw.loc[mask])

        sold = subset[subset["Status"].apply(_is_sold)].copy()
        not_sold = subset[~subset["Status"].apply(_is_sold)].copy()

        def _format_items(frame: pd.DataFrame) -> pd.DataFrame:
            if frame.empty:
                return pd.DataFrame(columns=OUTPUT_COLUMNS)
            out = frame.copy()
            for col in OUTPUT_COLUMNS:
                if col not in out.columns:
                    out[col] = ""
            return out[OUTPUT_COLUMNS].sort_values("SKU").reset_index(drop=True)

        sold_detail = _format_items(sold)
        not_sold_detail = _format_items(not_sold)

        tally_rows = pd.DataFrame(
            [
                {
                    "SKU": "[TALLY]",
                    "Item Name": f"Items sold: {len(sold_detail)}",
                    "Category Name": "",
                    "Sale Price": sold_detail["Sale Price"].sum() if not sold_detail.empty else 0,
                    "Reserve Price": "",
                    "Met Reserve": "",
                    "Buyers Premium": sold_detail["Buyers Premium"].sum() if not sold_detail.empty else 0,
                    "Tax": sold_detail["Tax"].sum() if not sold_detail.empty else 0,
                    "Total Collected": sold_detail["Total Collected"].sum() if not sold_detail.empty else 0,
                    "Status": "Summary",
                    "Invoice Number": "",
                },
                {
                    "SKU": "[TALLY]",
                    "Item Name": f"Items not sold: {len(not_sold_detail)}",
                    "Category Name": "",
                    "Sale Price": 0,
                    "Reserve Price": "",
                    "Met Reserve": "",
                    "Buyers Premium": 0,
                    "Tax": 0,
                    "Total Collected": 0,
                    "Status": "Summary",
                    "Invoice Number": "",
                },
                {
                    "SKU": "[TALLY]",
                    "Item Name": f"Total items: {len(subset)}",
                    "Category Name": "",
                    "Sale Price": "",
                    "Reserve Price": "",
                    "Met Reserve": "",
                    "Buyers Premium": "",
                    "Tax": "",
                    "Total Collected": "",
                    "Status": "Summary",
                    "Invoice Number": "",
                },
            ]
        )

        detail = pd.concat([tally_rows, sold_detail, not_sold_detail], ignore_index=True)

        out_path = output_dir / f"SKU_{prefix.upper()}.csv"
        detail.to_csv(out_path, index=False)
        written[prefix.upper()] = out_path

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract sale report by SKU prefix")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).resolve().parent / "sale-history-report (1).csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    args = parser.parse_args()

    paths = extract_by_prefix(args.input, args.output_dir)
    print(f"Source: {args.input}\n")
    for prefix, path in paths.items():
        df = pd.read_csv(path)
        tally = df[df["SKU"] == "[TALLY]"]
        sold_line = tally[tally["Item Name"].str.startswith("Items sold")]
        not_sold_line = tally[tally["Item Name"].str.startswith("Items not sold")]
        if not sold_line.empty and not not_sold_line.empty:
            print(
                f"SKU_{prefix}.csv — "
                f"{sold_line.iloc[0]['Item Name']} | {not_sold_line.iloc[0]['Item Name']} | "
                f"Sale price: ${float(sold_line.iloc[0]['Sale Price']):,.2f}"
            )
        else:
            print(f"SKU_{prefix}.csv — no items")
        print(f"  -> {path}")


if __name__ == "__main__":
    main()
