from __future__ import annotations

import httpx
import pytest

from fantabuddy.db import database, ingest_listone
from fantabuddy.official import (
    download_official_listone,
    official_url,
    read_official_listone,
    season_slug,
)


def _official_html(season: str = "2021-22") -> bytes:
    return f"""
    <html><body>
      <h1>Quotazioni e FVM Fantacalcio Serie A 2021/22</h1>
      <table class="pills-table"><tbody>
        <tr class="player-row" data-filter-role-classic="a"
            data-filter-role-mantra="pc|a">
          <th class="player-name">
            <a class="player-link" href="https://www.fantacalcio.it/serie-a/squadre/inter/test/42/{season}">
              <span>Giocatore Test</span>
            </a>
            <span class="out-of-game">*</span>
          </th>
          <td data-col-key="sq">INT</td>
          <td data-col-key="c_qi">10</td><td data-col-key="c_qa">12</td>
          <td data-col-key="c_fvm">-</td>
          <td data-col-key="m_qi">11</td><td data-col-key="m_qa">13</td>
          <td data-col-key="m_fvm">-</td>
        </tr>
      </tbody></table>
    </body></html>
    """.encode()


def test_download_parse_and_ingest_official_listone(tmp_path) -> None:  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == official_url(2021)
        return httpx.Response(200, content=_official_html(), request=request)

    path, cached = download_official_listone(
        2021, tmp_path / "raw", transport=httpx.MockTransport(handler)
    )
    assert cached is False
    assert path.name == "quotazioni-2021-22.html"

    data = read_official_listone(path, 2021)
    assert data.season == "2021/22"
    assert data.source_url == official_url(2021)
    assert data.ceduti_count == 1
    assert data.records[0].fantacalcio_id == 42
    assert data.records[0].mantra_roles == "PC;A"
    assert data.records[0].quote_diff == 2
    assert data.records[0].fvm == 0
    assert data.records[0].fvm_available is False

    with database(tmp_path / "warehouse.duckdb") as connection:
        assert ingest_listone(connection, data) is True
        stored = connection.execute(
            """
            SELECT s.source_url, p.fvm_available
            FROM listone_snapshots s JOIN listone_players p USING(snapshot_id, season)
            """
        ).fetchone()
    assert stored == (official_url(2021), False)

    repeated, cached = download_official_listone(
        2021,
        tmp_path / "raw",
        transport=httpx.MockTransport(lambda _: pytest.fail("cache non usata")),
    )
    assert repeated == path
    assert cached is True


def test_official_archive_rejects_unsupported_seasons() -> None:
    with pytest.raises(ValueError, match="copre soltanto"):
        season_slug(2014)


def test_invalid_refresh_preserves_cached_source(tmp_path) -> None:  # type: ignore[no-untyped-def]
    raw = tmp_path / "raw"
    target = raw / "quotazioni-2021-22.html"
    raw.mkdir()
    target.write_bytes(_official_html())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<h1>Quotazioni e FVM Fantacalcio Serie A 2021/22</h1>",
            request=request,
        )

    with pytest.raises(ValueError, match="nessuna riga"):
        download_official_listone(
            2021,
            raw,
            refresh=True,
            transport=httpx.MockTransport(handler),
        )

    assert target.read_bytes() == _official_html()


def test_current_page_accepts_player_links_without_season(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "quotazioni-2026-27.html"
    payload = _official_html("2026-27").replace(b"/42/2026-27", b"/42")
    payload = payload.replace(b"Serie A 2021/22", b"Serie A 2026/27")
    path.write_bytes(payload)

    data = read_official_listone(path, 2026)

    assert data.season == "2026/27"
    assert data.records[0].fantacalcio_id == 42
