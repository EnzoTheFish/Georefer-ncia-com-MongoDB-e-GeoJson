# Coletor GeoJSON de aerodromos brasileiros

O script baixa o CSV oficial de aerodromos do SIROS/ANAC, filtra os registros do
Brasil, trata latitude e longitude, gera pontos GeoJSON no CRS `EPSG:4326` e faz
`upsert` de cada `Feature` no MongoDB. A colecao recebe um indice unico de origem
e um indice geoespacial `2dsphere`.

Fontes oficiais:

- Catalogo nacional (categoria relacionada): <https://dados.gov.br/dados/conjuntos-dados/aerodromos-publicos---pistas-de-taxi>
- Metadados da fonte: <https://www.anac.gov.br/acesso-a-informacao/dados-abertos/areas-de-atuacao/aerodromos/lista-de-aerodromos-publicos/2-lista-de-aerodromos-publicos>
- CSV: <https://siros.anac.gov.br/siros/registros/aerodromo/aerodromos.csv>

## Instalacao e execucao

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python coletor_aerodromos_geojson.py
```

Por padrao, o MongoDB deve estar em `mongodb://localhost:27017`. As configuracoes
podem ser alteradas pelas variaveis mostradas em `.env.example` (o script le
variaveis do ambiente; ele nao carrega o arquivo `.env` automaticamente).

Teste a coleta e a geracao do GeoJSON sem MongoDB:

```powershell
python coletor_aerodromos_geojson.py --sem-mongo --limite 10
```

Exemplo de consulta geoespacial no `mongosh` (aeroportos em ate 50 km do ponto):

```javascript
use dados_governo
db.aerodromos_brasil.find({
  geometry: {
    $near: {
      $geometry: { type: "Point", coordinates: [-47.9186, -15.8697] },
      $maxDistance: 50000
    }
  }
})
```

No GeoJSON e no MongoDB a ordem das coordenadas e sempre
`[longitude, latitude]`.
