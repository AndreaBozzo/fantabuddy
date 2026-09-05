from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

from fantabuddy.excel import (
    ListoneImport,
    ListoneRecord,
    file_sha256,
    normalize_mantra_roles,
    normalize_season,
)

OFFICIAL_ARCHIVE_START = 2015
OFFICIAL_ARCHIVE_END = 2026
OFFICIAL_BASE_URL = "https://www.fantacalcio.it/quotazioni-fantacalcio"
PLAYER_ID_PATTERN = re.compile(r"/(\d+)(?:/(\d{4}-\d{2}))?/?$")
USER_AGENT = "fantabuddy/0.4 (+https://github.com/AndreaBozzo/fantabuddy)"


def season_slug(start_year: int) -> str:
    if not OFFICIAL_ARCHIVE_START <= start_year <= OFFICIAL_ARCHIVE_END:
        raise ValueError(
            "l'archivio ufficiale verificato copre soltanto le stagioni "
            f"{OFFICIAL_ARCHIVE_START}/{str(OFFICIAL_ARCHIVE_START + 1)[-2:]}–"
            f"{OFFICIAL_ARCHIVE_END}/{str(OFFICIAL_ARCHIVE_END + 1)[-2:]}"
        )
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def official_url(start_year: int) -> str:
    return f"{OFFICIAL_BASE_URL}/{season_slug(start_year)}"


def download_official_listone(
    start_year: int,
    output_dir: Path,
    *,
    refresh: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> tuple[Path, bool]:
    slug = season_slug(start_year)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"quotazioni-{slug}.html"
    if target.is_file() and not refresh:
        return target, True

    url = official_url(start_year)
    with httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
    if str(response.url).rstrip("/") != url:
        raise ValueError(f"la pagina ufficiale {url} reindirizza a {response.url}")
    if f"Serie A {start_year}/{str(start_year + 1)[-2:]}" not in response.text:
        raise ValueError(f"la pagina ufficiale non dichiara la stagione attesa: {url}")

    temporary = target.with_suffix(".html.tmp")
    temporary.write_bytes(response.content)
    try:
        read_official_listone(temporary, start_year)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target, False


def _text(row: Tag, selector: str, field: str) -> str:
    cell = row.select_one(selector)
    if cell is None:
        raise ValueError(f"campo {field} assente nella riga ufficiale")
    value = cell.get_text(" ", strip=True)
    if not value:
        raise ValueError(f"campo {field} vuoto nella riga ufficiale")
    return value


def _price(row: Tag, selector: str, field: str) -> int:
    value = _text(row, selector, field)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"valore non intero per {field}: {value!r}") from exc


def read_official_listone(path: Path, start_year: int) -> ListoneImport:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    slug = season_slug(start_year)
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    rows = soup.select("table.pills-table tbody tr.player-row")
    if not rows:
        raise ValueError(f"nessuna riga giocatore nella pagina ufficiale {path.name}")

    records: list[ListoneRecord] = []
    for row_number, row in enumerate(rows, start=1):
        link = row.select_one("a.player-link")
        href = str(link.get("href", "")) if isinstance(link, Tag) else ""
        match = PLAYER_ID_PATTERN.search(href)
        if match is None or (match.group(2) is not None and match.group(2) != slug):
            raise ValueError(f"URL giocatore inatteso alla riga {row_number}: {href!r}")
        classic_role = str(row.get("data-filter-role-classic", "")).upper()
        if classic_role not in {"P", "D", "C", "A"}:
            raise ValueError(f"ruolo Classic non valido alla riga {row_number}: {classic_role!r}")
        mantra_roles = normalize_mantra_roles(row.get("data-filter-role-mantra", ""), row_number)
        quote_initial = _price(row, "[data-col-key='c_qi']", "QI Classic")
        quote_current = _price(row, "[data-col-key='c_qa']", "QA Classic")
        mantra_initial = _price(row, "[data-col-key='m_qi']", "QI Mantra")
        mantra_current = _price(row, "[data-col-key='m_qa']", "QA Mantra")
        classic_fvm = _text(row, "[data-col-key='c_fvm']", "FVM Classic")
        mantra_fvm = _text(row, "[data-col-key='m_fvm']", "FVM Mantra")
        classic_fvm_available = classic_fvm != "-"
        mantra_fvm_available = mantra_fvm != "-"
        if classic_fvm_available != mantra_fvm_available:
            raise ValueError(f"disponibilità FVM incoerente alla riga {row_number}")
        fvm_available = classic_fvm_available
        records.append(
            ListoneRecord(
                fantacalcio_id=int(match.group(1)),
                classic_role=classic_role,
                mantra_roles=mantra_roles,
                name=_text(row, "th.player-name a span", "nome"),
                team=_text(row, "[data-col-key='sq']", "squadra"),
                quote_current=quote_current,
                quote_initial=quote_initial,
                quote_diff=quote_current - quote_initial,
                mantra_quote_current=mantra_current,
                mantra_quote_initial=mantra_initial,
                mantra_quote_diff=mantra_current - mantra_initial,
                fvm=int(classic_fvm) if fvm_available else 0,
                fvm_mantra=int(mantra_fvm) if fvm_available else 0,
                fvm_available=fvm_available,
                status="ceduto" if row.select_one(".out-of-game") else "active",
                source_sheet="official-web",
                source_row=row_number,
            )
        )

    ids = [record.fantacalcio_id for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"ID duplicati nella pagina ufficiale {path.name}")
    checksum = file_sha256(path)
    season = normalize_season(start_year, start_year + 1)
    return ListoneImport(
        snapshot_id=f"listone-{season.replace('/', '-')}-{checksum[:16]}",
        season=season,
        source_path=path,
        source_filename=path.name,
        checksum=checksum,
        source_modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        source_url=official_url(start_year),
        records=records,
    )
