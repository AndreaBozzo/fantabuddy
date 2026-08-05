from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from openpyxl import Workbook

from fantabuddy.excel import EXPECTED_HEADERS, EXPECTED_SHEETS


def write_listone(
    path: Path,
    season: str,
    records: Iterable[dict[str, object]],
    ceduti: Iterable[dict[str, object]] = (),
) -> Path:
    workbook = Workbook()
    first = workbook.active
    first.title = EXPECTED_SHEETS[0]
    for sheet_name in EXPECTED_SHEETS[1:]:
        workbook.create_sheet(sheet_name)
    title = f"Quotazioni Fantacalcio Stagione {season.replace('/', ' ')}"
    rows = {"Tutti": list(records), "Ceduti": list(ceduti)}
    for sheet_name in EXPECTED_SHEETS:
        sheet = workbook[sheet_name]
        sheet.append([title if sheet_name != "Ceduti" else f"Calciatori Ceduti {season}"])
        sheet.append(list(EXPECTED_HEADERS))
        for record in rows.get(sheet_name, []):
            current = int(record.get("quote_current", 10))
            initial = int(record.get("quote_initial", 8))
            mantra_current = int(record.get("mantra_quote_current", current))
            mantra_initial = int(record.get("mantra_quote_initial", initial))
            sheet.append(
                [
                    record["id"],
                    record["role"],
                    record.get("mantra_roles", "Por" if record["role"] == "P" else "C"),
                    record["name"],
                    record.get("team", "Test FC"),
                    current,
                    initial,
                    current - initial,
                    mantra_current,
                    mantra_initial,
                    mantra_current - mantra_initial,
                    record.get("fvm", current * 5),
                    record.get("fvm_mantra", current * 5),
                ]
            )
    workbook.save(path)
    return path
