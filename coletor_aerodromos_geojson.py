"""Coleta aerodromos brasileiros da ANAC e persiste GeoJSON no MongoDB.

Fonte oficial: Sistema de Registro de Operacoes (SIROS/ANAC).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from pymongo import ASCENDING, GEOSPHERE, MongoClient, ReplaceOne
from pymongo.errors import PyMongoError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


URL_FONTE_PADRAO = (
    "https://siros.anac.gov.br/siros/registros/aerodromo/aerodromos.csv"
)
URL_CATALOGO = (
    "https://www.anac.gov.br/acesso-a-informacao/dados-abertos/areas-de-atuacao/"
    "aerodromos/lista-de-aerodromos-publicos/2-lista-de-aerodromos-publicos"
)
BANCO_PADRAO = "dados_governo"
COLECAO_PADRAO = "aerodromos_brasil"

RENOMEAR_COLUNAS = {
    "sigla_icao_aerodromo": "codigo_icao",
    "sigla_iata_aerodromo": "codigo_iata",
    "nome_aerodromo": "nome",
    "municipio_aerodromo": "municipio",
    "estado_aerodromo": "estado",
    "pais_aerodromo": "pais",
    "aeronave_critica": "aeronave_critica",
    "latitude": "latitude",
    "longitude": "longitude",
}


def normalizar_texto(valor: Any) -> str:
    """Remove acentos e uniformiza um texto para comparacoes."""
    texto = "" if pd.isna(valor) else str(valor)
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c)).strip().upper()


def normalizar_nome_coluna(nome: str) -> str:
    nome = unicodedata.normalize("NFKD", str(nome))
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", nome.lower()).strip("_")


def criar_sessao_http() -> requests.Session:
    repeticao = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    adaptador = HTTPAdapter(max_retries=repeticao)
    sessao = requests.Session()
    sessao.headers.update(
        {"User-Agent": "ColetorGeoJSON-ANAC/1.0 (atividade academica)"}
    )
    sessao.mount("https://", adaptador)
    return sessao


def baixar_csv(url: str, timeout: int = 60) -> bytes:
    logging.info("Baixando dados oficiais: %s", url)
    with criar_sessao_http() as sessao:
        resposta = sessao.get(url, timeout=timeout)
        resposta.raise_for_status()
        if not resposta.content:
            raise ValueError("A fonte respondeu sem conteudo.")
        return resposta.content


def ler_csv(conteudo: bytes) -> pd.DataFrame:
    """Le o CSV da ANAC, que usa ponto e virgula e UTF-8 com BOM."""
    quadro = pd.read_csv(
        io.BytesIO(conteudo),
        sep=";",
        encoding="utf-8-sig",
        dtype="string",
        keep_default_na=True,
    )
    quadro.columns = [normalizar_nome_coluna(coluna) for coluna in quadro.columns]
    quadro = quadro.rename(columns=RENOMEAR_COLUNAS)
    obrigatorias = {"latitude", "longitude", "pais", "nome"}
    ausentes = obrigatorias.difference(quadro.columns)
    if ausentes:
        raise ValueError(
            "O esquema da fonte mudou; colunas ausentes: " + ", ".join(sorted(ausentes))
        )
    return quadro


def _id_estavel(linha: pd.Series) -> str:
    codigo_icao = normalizar_texto(linha.get("codigo_icao"))
    if re.fullmatch(r"[A-Z]{4}", codigo_icao):
        return f"ANAC-SIROS:{codigo_icao}"

    partes = [
        linha.get("nome", ""),
        linha.get("municipio", ""),
        linha.get("estado", ""),
        linha.get("latitude", ""),
        linha.get("longitude", ""),
    ]
    base = "|".join(normalizar_texto(valor) for valor in partes)
    return "ANAC-SIROS:" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]


def tratar_dados(
    quadro: pd.DataFrame, fonte_url: str = URL_FONTE_PADRAO
) -> gpd.GeoDataFrame:
    total = len(quadro)
    pais_normalizado = quadro["pais"].map(normalizar_texto)
    brasil = quadro.loc[pais_normalizado.isin({"BRASIL", "BRAZIL"})].copy()

    for coluna in ("latitude", "longitude"):
        brasil[coluna] = pd.to_numeric(
            brasil[coluna].str.strip().str.replace(",", ".", regex=False),
            errors="coerce",
        )

    brasil = brasil.dropna(subset=["latitude", "longitude", "nome"])
    brasil = brasil.loc[
        brasil["latitude"].between(-90, 90)
        & brasil["longitude"].between(-180, 180)
    ].copy()

    for coluna in brasil.select_dtypes(include=["string"]).columns:
        brasil[coluna] = brasil[coluna].str.strip()

    brasil["source_id"] = brasil.apply(_id_estavel, axis=1)
    antes_duplicatas = len(brasil)
    brasil = brasil.drop_duplicates(subset="source_id", keep="last")

    coletado_em = datetime.now(timezone.utc).isoformat()
    brasil["fonte"] = "Agencia Nacional de Aviacao Civil (ANAC) - SIROS"
    brasil["fonte_url"] = fonte_url
    brasil["catalogo_url"] = URL_CATALOGO
    brasil["coletado_em"] = coletado_em

    geometria = gpd.points_from_xy(brasil["longitude"], brasil["latitude"])
    geodados = gpd.GeoDataFrame(brasil, geometry=geometria, crs="EPSG:4326")
    logging.info(
        "Tratamento: %d linhas recebidas, %d brasileiras validas, %d duplicatas removidas.",
        total,
        len(geodados),
        antes_duplicatas - len(geodados),
    )
    if geodados.empty:
        raise ValueError("Nenhum aerodromo brasileiro com coordenadas validas foi encontrado.")
    return geodados


def gerar_feature_collection(geodados: gpd.GeoDataFrame) -> dict[str, Any]:
    """Usa a serializacao do GeoPandas para obter GeoJSON valido e sem NaN."""
    return json.loads(geodados.to_json(drop_id=True, ensure_ascii=False))


def salvar_geojson(feature_collection: dict[str, Any], caminho: Path) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(feature_collection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logging.info("GeoJSON salvo em %s", caminho.resolve())


def persistir_mongodb(
    feature_collection: dict[str, Any],
    uri: str,
    banco: str,
    colecao: str,
) -> tuple[int, int]:
    """Faz upsert de cada Feature, mantendo documentos consultaveis por $near."""
    cliente = MongoClient(uri, serverSelectionTimeoutMS=8_000)
    try:
        cliente.admin.command("ping")
        destino = cliente[banco][colecao]
        destino.create_index(
            [("properties.source_id", ASCENDING)],
            unique=True,
            name="source_id_unico",
        )
        destino.create_index(
            [("geometry", GEOSPHERE)],
            name="geometry_2dsphere",
        )

        operacoes = [
            ReplaceOne(
                {"properties.source_id": feature["properties"]["source_id"]},
                feature,
                upsert=True,
            )
            for feature in feature_collection["features"]
        ]
        if not operacoes:
            return (0, 0)
        resultado = destino.bulk_write(operacoes, ordered=False)
        return (resultado.upserted_count, resultado.modified_count)
    finally:
        cliente.close()


def criar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta aerodromos brasileiros da ANAC e grava GeoJSON no MongoDB."
    )
    parser.add_argument("--url", default=os.getenv("ANAC_CSV_URL", URL_FONTE_PADRAO))
    parser.add_argument(
        "--mongo-uri", default=os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    )
    parser.add_argument("--banco", default=os.getenv("MONGODB_DATABASE", BANCO_PADRAO))
    parser.add_argument(
        "--colecao", default=os.getenv("MONGODB_COLLECTION", COLECAO_PADRAO)
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=Path("dados/aerodromos_brasil.geojson"),
        help="Arquivo GeoJSON local para auditoria.",
    )
    parser.add_argument(
        "--sem-mongo",
        action="store_true",
        help="Coleta e gera o arquivo, mas nao conecta ao MongoDB.",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=None,
        help="Limita o numero de Features (util para teste).",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    argumentos = criar_argumentos()
    if argumentos.limite is not None and argumentos.limite < 1:
        logging.error("--limite deve ser maior que zero.")
        return 2

    try:
        conteudo = baixar_csv(argumentos.url)
        geodados = tratar_dados(ler_csv(conteudo), argumentos.url)
        if argumentos.limite:
            geodados = geodados.head(argumentos.limite).copy()

        feature_collection = gerar_feature_collection(geodados)
        salvar_geojson(feature_collection, argumentos.saida)

        if argumentos.sem_mongo:
            logging.info("MongoDB ignorado por --sem-mongo.")
        else:
            inseridos, atualizados = persistir_mongodb(
                feature_collection,
                argumentos.mongo_uri,
                argumentos.banco,
                argumentos.colecao,
            )
            logging.info(
                "MongoDB: %d inseridos e %d atualizados em %s.%s.",
                inseridos,
                atualizados,
                argumentos.banco,
                argumentos.colecao,
            )
        logging.info("Concluido: %d Features.", len(feature_collection["features"]))
        return 0
    except (requests.RequestException, ValueError, OSError, PyMongoError) as erro:
        logging.error("Falha no processamento: %s", erro)
        return 1


if __name__ == "__main__":
    sys.exit(main())
