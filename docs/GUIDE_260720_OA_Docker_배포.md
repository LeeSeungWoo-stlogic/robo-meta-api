# robo-meta-api 260720 OA Docker 배포 가이드

## 범위

- 이미지: `robo-meta-api-v4:260720`
- 내부 포트: `8100`
- OA 기본 호스트 포트: `8100`
- 대상: Rocky Linux 9 `linux/amd64`, OA 폐쇄망 VM 152
- 전체 bundle: `nas_260720_metadata_bundle.tar.gz`

## 1. Docker 설치 확인

```bash
docker version
docker compose version
systemctl is-active docker
docker info | grep 'Docker Root Dir'
```

Docker가 없다면 인터넷 연결된 동일 Rocky Linux 9 PC에서 다음 패키지와 의존 RPM을
`dnf download --resolve --alldeps`로 준비해 반입한다.

```text
docker-ce
docker-ce-cli
containerd.io
docker-buildx-plugin
docker-compose-plugin
```

OA 설치:

```bash
sudo dnf install -y --disablerepo='*' ./docker-rpms/*.rpm
sudo systemctl enable --now docker
sudo usermod -aG docker dhub_ontol
```

VM 152의 기존 `/DATA/docker-data`와 `nk-net` 설정은 유지한다.

## 2. bundle 반입·이미지 load

```bash
cd /home/dhub_ontol
sha256sum -c nas_260720_metadata_bundle.tar.gz.sha256
tar -xzf nas_260720_metadata_bundle.tar.gz
cd 260720
sha256sum -c SHA256SUMS
bash scripts/load-images.sh
```

OA에서는 build/pull 없이 load된 이미지로만 기동한다.

## 3. 런타임 설정

```bash
cp env.oa.example .env
vi .env
chmod 600 .env
```

주요 값:

```env
METADATA_PG_PASSWORD=<현장 비밀번호>
ROBO_META_API_PORT=8100
ROBO_RUNTIME_CONFIG=./config/runtime-settings.oa.yaml
KAIR_METADATA_NETWORK=nk-net
OPENAI_API_KEY=genos-via-proxy
```

`config/runtime-settings.oa.yaml` 기준 의존성:

- Metadata Store: `metadata-store:5432/t2s`
- LLM/embedding: `http://genos-proxy/v1`
- Query execution: `http://mindsdb:47334/api/sql/query`
- MindsDB integration: `rwis_postgres_active`

기반 서비스 확인:

```bash
docker network inspect nk-net
docker ps --format '{{.Names}} {{.Status}}' |
  grep -E 'nk-mindsdb|nk-genos-proxy'
```

genos-proxy에는 최소한 `gpt-4o-mini`와 `text-embedding-3-small` 라우팅이 필요하다.

## 4. 단계별 기동

```bash
cd /home/dhub_ontol/260720
bash scripts/up-oa.sh
```

수동 기동:

```bash
docker compose up -d --no-build --pull never metadata-store
bash scripts/restore-metadata.sh
docker compose up -d --no-build --pull never robo-meta-api
```

## 5. API 검증

```bash
curl -fsS http://127.0.0.1:8100/health
curl -I http://127.0.0.1:8100/docs
docker compose logs --since 10m robo-meta-api
```

Swagger:

```text
http://10.40.4.152:8100/docs
```

주요 API:

| API | 역할 |
|---|---|
| `POST /v1/data_decision` | 자연어 의미·역할·테이블·JOIN·필터 계획 반환 |
| `POST /v1/query_execute` | 검증된 조회 SQL을 MindsDB로 실행 |
| `GET /health` | metadata 및 execution backend 상태 |

간단한 decision 요청:

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/data_decision \
  -H 'Content-Type: application/json; charset=utf-8' \
  -d '{
    "query": "사업장 코드와 사업장 이름을 보여줘",
    "include_matched_columns": true
  }'
```

`query_plan.completeness`, `required_tables`, `join_paths`, `filters`를 확인한다.

## 6. query execution 주의

외부 생성형 AI가 `/v1/data_decision`의 실행 계획을 참고해 SQL을 생성하고
`/v1/query_execute`로 전달한다.

API는 SQLGlot 기반으로 다음을 검증한다.

- 읽기 전용 SQL
- 허용 catalog/schema
- 완전 수식된 MindsDB integration table
- timeout·최대 행·응답 크기
- 제공된 execution context와 namespace 일치

현재 MindsDB 실행 dialect는 `mysql`이다. 단순한 “ANSI SQL 변환 서비스”가 아니다.

## 7. 장애 확인 순서

```bash
docker compose ps
docker compose logs --since 10m metadata-store
docker compose logs --since 10m robo-meta-api
docker compose exec -T robo-meta-api \
  curl -fsS http://genos-proxy/v1/models
```

판단 기준:

- `/health` 실패: runtime config, Metastore 연결, 비밀번호 확인
- decision의 LLM 오류: genos-proxy chat/embedding 라우팅 확인
- query execution 오류: MindsDB integration과 namespace 확인

## 8. 중지·재기동

```bash
docker compose stop robo-meta-api
docker compose start robo-meta-api
```

Metastore volume 보존을 위해 `docker compose down -v`를 사용하지 않는다.

통합 설치 절차는
`K_Water_v1/oa_work/docker_part/docs/GUIDE_VM_152_260720_메타데이터_Docker_배포.md`
를 기준으로 한다.
