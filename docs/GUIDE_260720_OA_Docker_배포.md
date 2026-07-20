# robo-meta-api 260720 OA VM 163 배포

## 배포 위치와 포트

- 대상 VM: `10.40.4.163`
- 이미지: `robo-meta-api-v4:260720`
- 신규 API: host `8100` → container `8100`
- 기존 `kair-meta-api:8096`과 병행 운영
- Metadata Store: host `15433`
- MindsDB: host `47334`, `47335`

## 1. 반입·기동

```bash
cd /home/dhub_ontol
sha256sum -c nas_260720_metadata_bundle.tar.gz.sha256
tar -xzf nas_260720_metadata_bundle.tar.gz
cd 260720
sha256sum -c SHA256SUMS
bash scripts/load-images.sh

cp env.oa.example .env
vi .env
chmod 600 .env
bash scripts/up-oa.sh
```

OA에서는 build/pull을 하지 않고 반입 이미지로만 기동한다.

## 2. VM 163 GenOS 직접 연동

VM 163은 VM 152의 `genos-proxy`를 재사용하지 않는다.
`config/runtime-settings.oa.yaml`에서 chat과 embedding endpoint를 분리한다.

```yaml
embedding:
  base_url: http://10.40.4.215:30908/api/gateway/rep/serving/10/v1
  model: bge-m3
  dimensions: 1024

decision:
  analysis_base_url: http://10.40.4.215:30908/api/gateway/rep/serving/16/v1
  hyde_model: gpt-oss-120b
```

`.env`의 `OPENAI_API_KEY`에는 GenOS에서 사용하는 실제 Bearer를 입력한다.

## 3. MindsDB

VM 163 기본 가이드에는 MindsDB가 없으므로 260720 bundle에
`kair-mindsdb-neo4j:260720`을 포함한다.

```bash
docker compose up -d --no-build --pull never mindsdb
bash scripts/register-mindsdb-source.sh
```

Metastore는 dump 없이 빈 DB로 시작한다. OA에서 실제 사용할 원천 metadata를
수집·증강·승인·활성화하고 `.env`의 `SOURCE_DB_*`에도 같은 원천을 지정한다.
원천을 바꾸면 `runtime-settings.oa.yaml`의 integration, catalog, schema도 함께
변경한다.

## 4. API 검증

```bash
curl -fsS http://127.0.0.1:8100/health
curl -I http://127.0.0.1:8100/docs
docker compose logs --since 10m robo-meta-api
```

Swagger:

```text
http://10.40.4.163:8100/docs
```

Decision 요청:

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/data_decision \
  -H 'Content-Type: application/json; charset=utf-8' \
  -d '{
    "query": "사업장 코드와 사업장 이름을 보여줘",
    "include_matched_columns": true
  }'
```

주요 API:

| API | 역할 |
|---|---|
| `POST /v1/data_decision` | 의미·역할·테이블·JOIN·필터 계획 |
| `POST /v1/query_execute` | 검증된 SQL을 MindsDB로 실행 |
| `GET /health` | API backend 상태 |

## 5. query execution 조건

외부 생성형 AI는 decision의 `execution_context`를 이용해 SQL을 생성한다.
API는 SQLGlot 기반으로 다음을 검증한 후 MindsDB로 전달한다.

- 읽기 전용 SQL
- 허용 catalog/schema
- integration table 완전 수식
- execution context namespace
- timeout·행 수·응답 크기

현재 실행 dialect는 `mysql`이다.

## 6. 장애 확인

```bash
docker compose ps
docker compose logs --since 10m metadata-store
docker compose logs --since 10m mindsdb
docker compose logs --since 10m robo-meta-api
docker compose exec -T mindsdb \
  curl -fsS http://127.0.0.1:47334/api/status
```

- decision LLM 오류: serving 16과 Bearer 확인
- embedding 오류: serving 10, bge-m3, 1024 차원 확인
- query execution 오류: MindsDB integration과 metadata/source 일치 확인

## 7. 기존 서비스와 병행

```text
기존: http://10.40.4.163:8096
신규: http://10.40.4.163:8100
```

신규 검증 완료 전 기존 8096 서비스를 제거하지 않는다.
`docker compose down -v`와 기존 `kair-net` 삭제도 금지한다.

통합 기준:
`K_Water_v1/oa_work/docker_part/docs/GUIDE_VM_163_260720_메타데이터_Docker_배포.md`
