"""Disponibilità editoriale ufficiale Fantacalcio (fonte gratuita, senza API a pagamento).

La pagina ``indisponibili-serie-a`` è redazionale: dichiara chi è indisponibile e una
finestra di rientro stimata. Le finestre sono stime, non date certe, e vengono conservate
sia come testo originale sia come data derivata, con lo stato di derivazione esplicito.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import duckdb
import httpx
from bs4 import BeautifulSoup, Tag

EDITORIAL_BASE_URL = "https://www.fantacalcio.it"
EDITORIAL_AVAILABILITY_PATH = "/indisponibili-serie-a"
EDITORIAL_AVAILABILITY_URL = f"{EDITORIAL_BASE_URL}{EDITORIAL_AVAILABILITY_PATH}"
USER_AGENT = "fantabuddy/0.4 (+https://github.com/AndreaBozzo/fantabuddy)"

CATEGORY_LABELS = {
    "Infortunati": "infortunio",
    "Squalificati": "squalifica",
    "Diffidati": "diffida",
}

MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

# Giorno rappresentativo per ciascun qualificatore redazionale. Sono convenzioni di
# lettura dichiarate, non date comunicate dal club.
QUALIFIERS: tuple[tuple[str, int], ...] = (
    ("seconda meta", 23),
    ("seconda parte", 23),
    ("prima meta", 8),
    ("prima parte", 8),
    ("inizio", 5),
    ("meta", 15),
    ("fine", 28),
)

_RETURN_TRIGGERS = (
    "recuper",
    "rientro",
    "tornare",
    "torna",
    "rivederlo",
    "riaverlo",
    "arruolabil",
    "convocabil",
    "riprendere",
    "ritorno",
    "fino a",
    "disposizione",
)


@dataclass(frozen=True)
class EditorialRecord:
    team_name: str
    category: str
    player_name: str
    note: str


@dataclass(frozen=True)
class EditorialSnapshot:
    snapshot_id: str
    source_path: Path
    source_url: str
    checksum: str
    observed_at: datetime
    records: tuple[EditorialRecord, ...]


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_editorial_availability(
    output_dir: Path,
    *,
    refresh: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> tuple[Path, bool]:
    """Scarica la pagina ufficiale degli indisponibili conservando il payload grezzo."""
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "indisponibili-serie-a.html"
    if target.is_file() and not refresh:
        return target, True

    with httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        response = client.get(EDITORIAL_AVAILABILITY_URL)
        response.raise_for_status()
    if str(response.url).rstrip("/") != EDITORIAL_AVAILABILITY_URL:
        raise ValueError(
            f"la pagina ufficiale {EDITORIAL_AVAILABILITY_URL} reindirizza a {response.url}"
        )

    temporary = target.with_suffix(".html.tmp")
    temporary.write_bytes(response.content)
    try:
        read_editorial_availability(temporary)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target, False


def _category_of(header: Tag) -> str | None:
    text = header.get_text(" ", strip=True)
    for label, category in CATEGORY_LABELS.items():
        if label in text:
            return category
    return None


def read_editorial_availability(path: Path) -> EditorialSnapshot:
    """Estrae le segnalazioni per club dalla pagina ufficiale, senza inferire nulla."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    soup = BeautifulSoup(path.read_bytes(), "html.parser")

    records: list[EditorialRecord] = []
    seen: set[tuple[str, str, str]] = set()
    teams_seen = 0
    for span in soup.select("span.team-name"):
        header = span.parent
        block = header.parent if header is not None else None
        if block is None:
            continue
        columns = block.select("div.col")
        if not columns:
            continue
        teams_seen += 1
        team_name = span.get_text(strip=True)
        for column in columns:
            category: str | None = None
            for child in column.children:
                if not isinstance(child, Tag):
                    continue
                if child.name == "header":
                    category = _category_of(child) or category
                    continue
                if child.name != "ul":
                    continue
                if category is None:
                    raise ValueError(
                        f"elenco senza intestazione riconosciuta per {team_name} in {path.name}"
                    )
                for item in child.select("li"):
                    name_tag = item.select_one("strong.item-name")
                    if name_tag is None:
                        continue
                    player_name = name_tag.get_text(strip=True)
                    if not player_name:
                        continue
                    description = item.select_one(".item-description")
                    note = description.get_text(" ", strip=True) if description else ""
                    key = (team_name, category, player_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(
                        EditorialRecord(
                            team_name=team_name,
                            category=category,
                            player_name=player_name,
                            note=note,
                        )
                    )
    if teams_seen < 20:
        raise ValueError(
            f"la pagina ufficiale dichiara solo {teams_seen} club in {path.name}: "
            "struttura inattesa"
        )

    checksum = file_sha256(path)
    return EditorialSnapshot(
        snapshot_id=f"indisponibili-{checksum[:16]}",
        source_path=path,
        source_url=EDITORIAL_AVAILABILITY_URL,
        checksum=checksum,
        observed_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        records=tuple(records),
    )


def _shift_year(month: int, reference: date) -> int:
    return reference.year if month >= reference.month else reference.year + 1


def parse_return_window(note: str, as_of: date) -> tuple[str | None, date | None]:
    """Deriva una finestra di rientro dal testo redazionale.

    Restituisce l'etichetta testuale e la data rappresentativa. Quando il testo non
    dichiara un mese, la data resta ``None``: il rientro va verificato a mano.
    """
    folded = _fold(note)
    if not folded:
        return None, None

    best: tuple[int, str, date] | None = None
    for match in re.finditer(r"\b(" + "|".join(MONTHS) + r")\b", folded):
        month = MONTHS[match.group(1)]
        prefix = folded[max(0, match.start() - 40) : match.start()]
        # Un mese citato come data dell'infortunio non è una finestra di rientro.
        context = folded[max(0, match.start() - 120) : match.start()]
        if not any(trigger in context for trigger in _RETURN_TRIGGERS):
            continue
        day = 15
        qualifier = ""
        for name, qualifier_day in QUALIFIERS:
            if name in prefix:
                day = qualifier_day
                qualifier = name
                break
        candidate = date(_shift_year(month, as_of), month, day)
        label = f"{qualifier} {match.group(1)}".strip()
        if best is None or candidate > best[2]:
            best = (month, label, candidate)

    if best is None:
        return None, None
    return best[1], best[2]


def _team_codes(connection: duckdb.DuckDBPyConnection, season: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT team FROM latest_listone_players WHERE season = ?", [season]
        ).fetchall()
    }


def import_editorial_availability(
    connection: duckdb.DuckDBPyConnection,
    snapshot: EditorialSnapshot,
    season: str,
    as_of: date,
) -> dict[str, Any]:
    """Registra le segnalazioni collegandole al listone senza accettare match ambigui."""
    codes = _team_codes(connection, season)
    listone = connection.execute(
        """
        SELECT fantacalcio_id, name, team
        FROM latest_listone_players
        WHERE season = ? AND status = 'active'
        """,
        [season],
    ).fetchall()
    by_key: dict[tuple[str, str], list[int]] = {}
    for listone_id, name, team in listone:
        by_key.setdefault((_fold(str(name)), str(team)), []).append(int(listone_id))

    connection.execute(
        "DELETE FROM editorial_availability WHERE snapshot_id = ?", [snapshot.snapshot_id]
    )

    matched = 0
    dated_returns = 0
    unmatched: list[dict[str, str]] = []
    for record in snapshot.records:
        prefix = _fold(record.team_name)[:3].upper()
        candidates = [code for code in codes if code == prefix]
        team_code = candidates[0] if len(candidates) == 1 else None
        fantacalcio_id: int | None = None
        if team_code is not None:
            ids = by_key.get((_fold(record.player_name), team_code), [])
            if len(ids) == 1:
                fantacalcio_id = ids[0]
        if fantacalcio_id is None:
            unmatched.append({"team": record.team_name, "name": record.player_name})
        else:
            matched += 1
        return_label, return_date = parse_return_window(record.note, as_of)
        dated_returns += return_date is not None
        connection.execute(
            """
            INSERT INTO editorial_availability VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                snapshot.snapshot_id,
                season,
                fantacalcio_id,
                record.player_name,
                record.team_name,
                team_code,
                record.category,
                record.note or None,
                return_label,
                return_date,
                "matched" if fantacalcio_id is not None else "pending",
                snapshot.source_url,
                snapshot.checksum,
                snapshot.observed_at,
            ],
        )

    counts = {
        category: sum(1 for record in snapshot.records if record.category == category)
        for category in sorted({record.category for record in snapshot.records})
    }
    return {
        "snapshot_id": snapshot.snapshot_id,
        "season": season,
        "records": len(snapshot.records),
        "matched": matched,
        "pending": len(unmatched),
        "pending_players": unmatched,
        "by_category": counts,
        "dated_returns": dated_returns,
        "source_url": snapshot.source_url,
        "checksum": snapshot.checksum,
        "observed_at": snapshot.observed_at.isoformat(),
    }


def latest_snapshot_id(
    connection: duckdb.DuckDBPyConnection, season: str
) -> str | None:
    row = connection.execute(
        """
        SELECT snapshot_id
        FROM editorial_availability
        WHERE season = ?
        QUALIFY row_number() OVER (ORDER BY observed_at DESC, snapshot_id) = 1
        """,
        [season],
    ).fetchone()
    return None if row is None else str(row[0])
