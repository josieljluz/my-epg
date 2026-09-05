#!/usr/bin/env python3

from __future__ import annotations

import gzip
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

OUTPUT_DIR = BASE_DIR / "output"
CONFIG_DIR = BASE_DIR / "config"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


EPG_XML = OUTPUT_DIR / "epg.xml"
EPG_GZ = OUTPUT_DIR / "epg.xml.gz"
PLAYLIST_M3U = OUTPUT_DIR / "playlist.m3u"
ALIASES_JSON = OUTPUT_DIR / "epg_aliases.json"

CHANNEL_CONFIG = CONFIG_DIR / "channels.json"


MITV_BASE = "https://mi.tv/br"

GUIA_BASE = "https://www.guiadetv.com"

IPTV_ORG_CHANNELS = (
    "https://iptv-org.github.io/api/channels.json"
)

IPTV_ORG_LOGOS = (
    "https://iptv-org.github.io/api/logos.json"
)

IPTV_ORG_STREAMS = (
    "https://iptv-org.github.io/api/streams.json"
)


USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

REQUEST_TIMEOUT = int(
    os.getenv("REQUEST_TIMEOUT", "30")
)

DAYS_AHEAD = int(
    os.getenv("DAYS_AHEAD", "3")
)

REQUEST_DELAY = float(
    os.getenv("REQUEST_DELAY", "0.5")
)

MIN_PROGRAMS = int(
    os.getenv("MIN_PROGRAMS", "1")
)

LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO"
).upper()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("EPG")


# ============================================================
# HTTP
# ============================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
)


def get_url(
    url: str,
    *,
    timeout: int = REQUEST_TIMEOUT,
) -> Optional[requests.Response]:

    try:

        response = SESSION.get(
            url,
            timeout=timeout,
        )

        response.raise_for_status()

        return response

    except requests.RequestException as exc:

        log.warning(
            "Falha ao acessar %s: %s",
            url,
            exc,
        )

        return None


def get_json(url: str):

    response = get_url(url)

    if not response:
        return None

    try:
        return response.json()

    except ValueError as exc:

        log.warning(
            "JSON inválido: %s",
            url,
        )

        return None


# ============================================================
# MODELOS
# ============================================================

@dataclass
class Program:

    channel_id: str
    channel_name: str

    title: str

    description: str = ""

    start: Optional[datetime] = None
    stop: Optional[datetime] = None

    category: str = ""

    source: str = ""


@dataclass
class Channel:

    id: str
    name: str

    logo: str = ""

    stream_url: str = ""

    tvg_id: str = ""

    tvg_name: str = ""

    group: str = "Brasil"

    country: str = "BR"

    aliases: List[str] = None

    website: str = ""

    def __post_init__(self):

        if self.aliases is None:
            self.aliases = []


# ============================================================
# NORMALIZAÇÃO
# ============================================================

def normalize(text: str) -> str:

    if not text:
        return ""

    text = unicodedata.normalize(
        "NFKD",
        text,
    )

    text = "".join(
        c
        for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = text.replace("&", " e ")

    text = re.sub(
        r"\bhd\b",
        "",
        text,
    )

    text = re.sub(
        r"\bbrasil\b",
        "",
        text,
    )

    text = re.sub(
        r"\btv\b",
        " ",
        text,
    )

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def compact(text: str) -> str:

    return normalize(text).replace(
        " ",
        "",
    )


# ============================================================
# CONFIGURAÇÃO LOCAL
# ============================================================

def load_local_channels() -> Dict[str, dict]:

    if not CHANNEL_CONFIG.exists():

        return {}

    try:

        with open(
            CHANNEL_CONFIG,
            "r",
            encoding="utf-8",
        ) as fp:

            data = json.load(fp)

        result = {}

        for item in data:

            name = item.get("name", "")

            if not name:
                continue

            result[normalize(name)] = item

        return result

    except Exception as exc:

        log.warning(
            "Erro lendo channels.json: %s",
            exc,
        )

        return {}


# ============================================================
# IPTV-ORG
# ============================================================

class IPTVOrg:

    def __init__(self):

        self.channels = []
        self.logos = []
        self.streams = []

        self.channel_by_id = {}
        self.logo_by_channel = {}
        self.stream_by_channel = {}

    def load(self):

        log.info(
            "Carregando canais do iptv-org..."
        )

        channels = get_json(
            IPTV_ORG_CHANNELS
        )

        logos = get_json(
            IPTV_ORG_LOGOS
        )

        streams = get_json(
            IPTV_ORG_STREAMS
        )

        self.channels = channels or []
        self.logos = logos or []
        self.streams = streams or []

        for channel in self.channels:

            channel_id = channel.get("id")

            if channel_id:
                self.channel_by_id[
                    channel_id
                ] = channel

        for logo in self.logos:

            if not logo.get("in_use", True):
                continue

            channel_id = logo.get("channel")

            if channel_id:

                self.logo_by_channel[
                    channel_id
                ] = logo.get("url", "")

        for stream in self.streams:

            channel_id = stream.get("channel")

            url = stream.get("url")

            if not channel_id or not url:
                continue

            if channel_id not in self.stream_by_channel:

                self.stream_by_channel[
                    channel_id
                ] = stream

        log.info(
            "iptv-org: %d canais | %d logos | %d streams",
            len(self.channels),
            len(self.logos),
            len(self.streams),
        )

    def find_channel(
        self,
        name: str,
    ) -> Optional[dict]:

        target = normalize(name)

        if not target:
            return None

        # Primeiro tenta correspondência exata
        for channel in self.channels:

            channel_name = channel.get(
                "name",
                "",
            )

            if normalize(channel_name) == target:

                return channel

        # Depois alt_names
        for channel in self.channels:

            names = [
                channel.get("name", "")
            ]

            names.extend(
                channel.get(
                    "alt_names",
                    [],
                )
            )

            for candidate in names:

                if normalize(candidate) == target:

                    return channel

        # Correspondência compacta
        target_compact = compact(name)

        if not target_compact:
            return None

        for channel in self.channels:

            names = [
                channel.get("name", "")
            ]

            names.extend(
                channel.get(
                    "alt_names",
                    [],
                )
            )

            for candidate in names:

                if compact(candidate) == target_compact:

                    return channel

        return None

    def get_logo(
        self,
        channel_id: str,
    ) -> str:

        return self.logo_by_channel.get(
            channel_id,
            "",
        )

    def get_stream(
        self,
        channel_id: str,
    ) -> str:

        stream = self.stream_by_channel.get(
            channel_id
        )

        if not stream:
            return ""

        return stream.get(
            "url",
            "",
        )


# ============================================================
# GUIA DE TV
# ============================================================

class GuiaDeTV:

    URL = (
        "https://www.guiadetv.com/programacao"
    )

    def fetch(
        self,
        day: datetime,
    ) -> List[Program]:

        log.info(
            "Consultando Guia de TV: %s",
            day.date(),
        )

        response = get_url(
            self.URL
        )

        if not response:
            return []

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        programs = []

        # O site pode mudar a estrutura.
        # Procuramos estruturas sem depender
        # de uma única classe CSS.

        for block in soup.find_all(
            ["article", "section", "div"]
        ):

            text = block.get_text(
                " ",
                strip=True,
            )

            if not text:
                continue

            # Procura horários
            matches = re.findall(
                r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
                text,
            )

            if not matches:
                continue

            # Tentativa de descobrir o canal
            channel_name = ""

            heading = block.find(
                ["h1", "h2", "h3", "h4", "strong"]
            )

            if heading:

                channel_name = heading.get_text(
                    " ",
                    strip=True,
                )

            if not channel_name:
                continue

            times = [
                f"{h}:{m}"
                for h, m in matches
            ]

            for index, start_time in enumerate(times):

                title = text

                title = re.sub(
                    r"\b\d{1,2}:\d{2}\b",
                    "",
                    title,
                )

                title = re.sub(
                    r"\s+",
                    " ",
                    title,
                ).strip()

                if not title:
                    continue

                try:

                    hour, minute = map(
                        int,
                        start_time.split(":"),
                    )

                    start = datetime(
                        day.year,
                        day.month,
                        day.day,
                        hour,
                        minute,
                    )

                    if index + 1 < len(times):

                        next_hour, next_minute = map(
                            int,
                            times[index + 1].split(":"),
                        )

                        stop = datetime(
                            day.year,
                            day.month,
                            day.day,
                            next_hour,
                            next_minute,
                        )

                        if stop <= start:

                            stop += timedelta(
                                days=1
                            )

                    else:

                        stop = start + timedelta(
                            minutes=60
                        )

                    programs.append(
                        Program(
                            channel_id="",
                            channel_name=channel_name,
                            title=title,
                            start=start,
                            stop=stop,
                            source="guiadetv",
                        )
                    )

                except ValueError:
                    continue

        return programs


# ============================================================
# MI.TV
# ============================================================

class MiTV:

    BASE = "https://mi.tv/br/programacao"

    def fetch(
        self,
        day: datetime,
    ) -> List[Program]:

        date_string = day.strftime(
            "%Y-%m-%d"
        )

        url = (
            f"{self.BASE}/{date_string}"
        )

        log.info(
            "Consultando mi.tv: %s",
            url,
        )

        response = get_url(
            url
        )

        if not response:
            return []

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        programs = []

        # Estratégia genérica para suportar
        # alterações menores no HTML.

        text_nodes = soup.find_all(
            string=re.compile(
                r"\b\d{1,2}:\d{2}\b"
            )
        )

        for node in text_nodes:

            parent = node.parent

            if not parent:
                continue

            text = parent.get_text(
                " ",
                strip=True,
            )

            if not text:
                continue

            time_match = re.search(
                r"\b(\d{1,2}):(\d{2})\b",
                text,
            )

            if not time_match:
                continue

            hour = int(
                time_match.group(1)
            )

            minute = int(
                time_match.group(2)
            )

            if hour > 23:
                continue

            title = re.sub(
                r"\b\d{1,2}:\d{2}\b",
                "",
                text,
            )

            title = re.sub(
                r"\s+",
                " ",
                title,
            ).strip()

            if not title:
                continue

            # Tenta encontrar o nome do canal
            # nos elementos pais.

            channel_name = ""

            current = parent

            for _ in range(5):

                if not current:
                    break

                heading = current.find(
                    ["h1", "h2", "h3", "h4"]
                )

                if heading:

                    channel_name = heading.get_text(
                        " ",
                        strip=True,
                    )

                    break

                current = current.parent

            if not channel_name:
                continue

            start = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
            )

            stop = start + timedelta(
                minutes=60
            )

            programs.append(
                Program(
                    channel_id="",
                    channel_name=channel_name,
                    title=title,
                    start=start,
                    stop=stop,
                    source="mitv",
                )
            )

        return programs


# ============================================================
# DEDUPLICAÇÃO
# ============================================================

def deduplicate_programs(
    programs: List[Program],
) -> List[Program]:

    result = {}

    for program in programs:

        if not program.start:
            continue

        key = (
            normalize(program.channel_name),
            program.start.isoformat(),
            normalize(program.title),
        )

        if key not in result:

            result[key] = program

        else:

            current = result[key]

            # mi.tv tem prioridade
            if (
                current.source != "mitv"
                and program.source == "mitv"
            ):

                result[key] = program

    return sorted(
        result.values(),
        key=lambda p: (
            normalize(p.channel_name),
            p.start or datetime.min,
        ),
    )


# ============================================================
# NORMALIZAÇÃO DE HORÁRIOS
# ============================================================

def fix_program_times(
    programs: List[Program],
):

    grouped = {}

    for program in programs:

        key = normalize(
            program.channel_name
        )

        grouped.setdefault(
            key,
            []
        ).append(program)

    result = []

    for _, items in grouped.items():

        items.sort(
            key=lambda p: p.start or datetime.min
        )

        for index, program in enumerate(items):

            if index + 1 < len(items):

                next_program = items[index + 1]

                if (
                    program.start
                    and next_program.start
                    and next_program.start > program.start
                ):

                    program.stop = next_program.start

            if (
                program.stop
                and program.start
                and program.stop <= program.start
            ):

                program.stop = (
                    program.start
                    + timedelta(minutes=60)
                )

            result.append(program)

    return result


# ============================================================
# MATCHING
# ============================================================

def resolve_channel(
    name: str,
    iptv: IPTVOrg,
    local: Dict[str, dict],
) -> Optional[Channel]:

    normalized = normalize(name)

    local_data = local.get(
        normalized
    )

    iptv_channel = iptv.find_channel(
        name
    )

    if local_data:

        channel_id = local_data.get(
            "id",
            "",
        )

        if not channel_id and iptv_channel:

            channel_id = iptv_channel.get(
                "id",
                "",
            )

        return Channel(
            id=channel_id or normalized.replace(
                " ",
                ".",
            ),
            name=local_data.get(
                "name",
                name,
            ),
            logo=local_data.get(
                "logo",
                "",
            ),
            stream_url=local_data.get(
                "stream_url",
                "",
            ),
            tvg_id=local_data.get(
                "tvg_id",
                channel_id or normalized,
            ),
            tvg_name=local_data.get(
                "tvg_name",
                name,
            ),
            group=local_data.get(
                "group",
                "Brasil",
            ),
            aliases=local_data.get(
                "aliases",
                [],
            ),
            website=local_data.get(
                "website",
                "",
            ),
        )

    if not iptv_channel:

        # Ainda cria um canal mesmo sem
        # correspondência no iptv-org.

        channel_id = (
            normalized.replace(
                " ",
                ".",
            )
            + ".br"
        )

        return Channel(
            id=channel_id,
            name=name,
            tvg_id=channel_id,
            tvg_name=name,
            group="Brasil",
        )

    channel_id = iptv_channel.get(
        "id",
        normalized.replace(" ", "."),
    )

    logo = iptv.get_logo(
        channel_id
    )

    stream = iptv.get_stream(
        channel_id
    )

    return Channel(
        id=channel_id,
        name=iptv_channel.get(
            "name",
            name,
        ),
        logo=logo,
        stream_url=stream,
        tvg_id=channel_id,
        tvg_name=iptv_channel.get(
            "name",
            name,
        ),
        group="Brasil",
        aliases=iptv_channel.get(
            "alt_names",
            [],
        ),
        website=iptv_channel.get(
            "website",
            "",
        ),
    )


# ============================================================
# XMLTV
# ============================================================

def xml_escape(
    value: str,
) -> str:

    return html.escape(
        value or "",
        quote=False,
    )


def xml_datetime(
    value: datetime,
) -> str:

    return value.strftime(
        "%Y%m%d%H%M%S -0300"
    )


def generate_xmltv(
    channels: Dict[str, Channel],
    programs: List[Program],
):

    lines = []

    lines.append(
        '<?xml version="1.0" encoding="UTF-8"?>'
    )

    lines.append(
        '<tv generator-info-name="GitHub EPG Generator" '
        'generator-info-url="https://github.com/">'
    )

    for channel in channels.values():

        lines.append(
            f'  <channel id="{html.escape(channel.id)}">'
        )

        lines.append(
            f'    <display-name lang="pt">'
            f'{xml_escape(channel.name)}'
            f'</display-name>'
        )

        if channel.logo:

            lines.append(
                f'    <icon src="{html.escape(channel.logo)}"/>'
            )

        lines.append(
            "  </channel>"
        )

    for program in programs:

        if not program.start:
            continue

        channel = channels.get(
            program.channel_id
        )

        if not channel:
            continue

        start = xml_datetime(
            program.start
        )

        stop = xml_datetime(
            program.stop
        ) if program.stop else xml_datetime(
            program.start + timedelta(hours=1)
        )

        lines.append(
            f'  <programme '
            f'channel="{html.escape(channel.id)}" '
            f'start="{start}" '
            f'stop="{stop}">'
        )

        lines.append(
            f'    <title lang="pt">'
            f'{xml_escape(program.title)}'
            f'</title>'
        )

        if program.description:

            lines.append(
                f'    <desc lang="pt">'
                f'{xml_escape(program.description)}'
                f'</desc>'
            )

        if program.category:

            lines.append(
                f'    <category lang="pt">'
                f'{xml_escape(program.category)}'
                f'</category>'
            )

        lines.append(
            "  </programme>"
        )

    lines.append(
        "</tv>"
    )

    content = "\n".join(lines)

    EPG_XML.write_text(
        content,
        encoding="utf-8",
    )

    with gzip.open(
        EPG_GZ,
        "wb",
        compresslevel=9,
    ) as gz:

        gz.write(
            content.encode(
                "utf-8"
            )
        )


# ============================================================
# M3U
# ============================================================

def generate_playlist(
    channels: Dict[str, Channel],
):

    lines = [
        "#EXTM3U"
    ]

    for channel in sorted(
        channels.values(),
        key=lambda c: normalize(c.name),
    ):

        if not channel.stream_url:
            continue

        attrs = [
            f'tvg-id="{html.escape(channel.tvg_id)}"',
            f'tvg-name="{html.escape(channel.tvg_name)}"',
        ]

        if channel.logo:

            attrs.append(
                f'tvg-logo="{html.escape(channel.logo)}"'
            )

        attrs.append(
            f'group-title="{html.escape(channel.group)}"'
        )

        lines.append(
            "#EXTINF:-1 "
            + " ".join(attrs)
            + ","
            + channel.name
        )

        lines.append(
            channel.stream_url
        )

    PLAYLIST_M3U.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


# ============================================================
# ALIASES
# ============================================================

def generate_aliases(
    channels: Dict[str, Channel],
):

    aliases = {}

    for channel in channels.values():

        values = set()

        values.add(
            channel.name
        )

        values.add(
            normalize(channel.name)
        )

        values.add(
            compact(channel.name)
        )

        for alias in channel.aliases:

            values.add(alias)

            values.add(
                normalize(alias)
            )

            values.add(
                compact(alias)
            )

        for value in values:

            if value:

                aliases[
                    normalize(value)
                ] = channel.id

    data = {
        "generated_at": datetime.utcnow().isoformat()
        + "Z",
        "channels": aliases,
    }

    ALIASES_JSON.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# PIPELINE
# ============================================================

def main():

    started = time.time()

    log.info(
        "=========================================="
    )

    log.info(
        "        GERADOR XMLTV / IPTV"
    )

    log.info(
        "=========================================="
    )

    log.info(
        "Dias: %d",
        DAYS_AHEAD,
    )

    # --------------------------------------------------------
    # IPTV-ORG
    # --------------------------------------------------------

    iptv = IPTVOrg()

    iptv.load()

    # --------------------------------------------------------
    # Configuração local
    # --------------------------------------------------------

    local_channels = load_local_channels()

    # --------------------------------------------------------
    # Fontes
    # --------------------------------------------------------

    mitv = MiTV()
    guia = GuiaDeTV()

    all_programs = []

    today = datetime.now()

    for offset in range(
        DAYS_AHEAD
    ):

        day = today + timedelta(
            days=offset
        )

        # ----------------------------------------------------
        # mi.tv
        # ----------------------------------------------------

        try:

            programs = mitv.fetch(
                day
            )

            log.info(
                "mi.tv: %d programas",
                len(programs),
            )

            all_programs.extend(
                programs
            )

        except Exception as exc:

            log.exception(
                "Erro mi.tv: %s",
                exc,
            )

        time.sleep(
            REQUEST_DELAY
        )

        # ----------------------------------------------------
        # Guia de TV
        # ----------------------------------------------------

        try:

            programs = guia.fetch(
                day
            )

            log.info(
                "Guia de TV: %d programas",
                len(programs),
            )

            all_programs.extend(
                programs
            )

        except Exception as exc:

            log.exception(
                "Erro Guia de TV: %s",
                exc,
            )

        time.sleep(
            REQUEST_DELAY
        )

    # --------------------------------------------------------
    # Deduplicação
    # --------------------------------------------------------

    all_programs = deduplicate_programs(
        all_programs
    )

    all_programs = fix_program_times(
        all_programs
    )

    log.info(
        "Programas após limpeza: %d",
        len(all_programs),
    )

    # --------------------------------------------------------
    # Criar canais
    # --------------------------------------------------------

    channels = {}

    for program in all_programs:

        channel = resolve_channel(
            program.channel_name,
            iptv,
            local_channels,
        )

        if not channel:
            continue

        channels[
            channel.id
        ] = channel

        program.channel_id = (
            channel.id
        )

    # --------------------------------------------------------
    # Filtrar canais sem programação
    # --------------------------------------------------------

    valid_programs = []

    for program in all_programs:

        if program.channel_id not in channels:
            continue

        if not program.title:
            continue

        valid_programs.append(
            program
        )

    # --------------------------------------------------------
    # Gerar arquivos
    # --------------------------------------------------------

    log.info(
        "Canais: %d",
        len(channels),
    )

    log.info(
        "Programas: %d",
        len(valid_programs),
    )

    generate_xmltv(
        channels,
        valid_programs,
    )

    generate_playlist(
        channels
    )

    generate_aliases(
        channels
    )

    # --------------------------------------------------------
    # Relatório
    # --------------------------------------------------------

    elapsed = time.time() - started

    log.info(
        "------------------------------------------"
    )

    log.info(
        "EPG XML:       %s",
        EPG_XML,
    )

    log.info(
        "EPG GZIP:      %s",
        EPG_GZ,
    )

    log.info(
        "Playlist M3U:  %s",
        PLAYLIST_M3U,
    )

    log.info(
        "Aliases:       %s",
        ALIASES_JSON,
    )

    log.info(
        "Tempo: %.2fs",
        elapsed,
    )

    log.info(
        "=========================================="
    )

    if len(valid_programs) < MIN_PROGRAMS:

        log.error(
            "Poucos programas foram encontrados: %d",
            len(valid_programs),
        )

        return 1

    return 0


if __name__ == "__main__":

    sys.exit(
        main()
    )