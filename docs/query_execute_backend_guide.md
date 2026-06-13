# `/query/execute` 백엔드 교체 가이드

`/query/execute` 엔드포인트는 AI가 생성한 SELECT SQL을 실제 데이터베이스에 대행 실행합니다.  
기본 백엔드는 **PostgreSQL(psycopg)**이며, 이 문서는 **MindsDB 또는 다른 SQL 인터페이스로 교체**하는 방법을 안내합니다.

---

## 1. 현재 구조 파악

```
app/
├── config.py          ← SOURCE_PG_* 환경변수 (접속 정보)
├── db.py              ← psycopg AsyncConnectionPool + source_conn() 컨텍스트 매니저
└── services/
    ├── query_runner.py ← source_conn() 사용, PG 방언 래퍼 포함
    └── sql_guard.py    ← SELECT 외 차단 (백엔드 무관, 공통 사용)
```

`query_runner.py`의 실행 흐름:

```
1. sql_guard.check(sql)          → SELECT 외 400 차단 (공통)
2. BEGIN READ ONLY               ← PG 전용
3. SET LOCAL statement_timeout   ← PG 전용
4. SET LOCAL lock_timeout        ← PG 전용
5. SET LOCAL search_path         ← PG 전용
6. 실제 SELECT 실행              → ANSI 표준 (백엔드 무관)
7. 결과 직렬화·감사 로그         → 공통
```

**핵심**: 1·6·7 단계는 모든 백엔드 공통. **2~5 단계(4줄)만 PG 전용.**

---

## 2. 교체 방법

### 2-1. MindsDB로 교체

MindsDB는 MySQL 프로토콜 호환 SQL 인터페이스를 제공합니다.

#### Step 1. 패키지 교체

```txt
# requirements.txt 변경
# 제거: psycopg[binary], psycopg-pool
# 추가:
aiomysql>=0.2.0
```

#### Step 2. 환경변수 변경 (`.env`)

```dotenv
# 기존 PG 설정 제거 또는 주석 처리
# SOURCE_PG_HOST=...

# MindsDB 접속 정보 추가
MINDSDB_HOST=127.0.0.1
MINDSDB_PORT=47335          # MindsDB MySQL 포트 기본값
MINDSDB_USER=mindsdb
MINDSDB_PASS=
MINDSDB_DB=mindsdb          # 대상 데이터베이스(프로젝트)명
```

#### Step 3. `config.py` 수정

```python
# SOURCE_PG_* 키 대신 MindsDB 접속 키 추가
mindsdb_host: str = _env("MINDSDB_HOST", "127.0.0.1")
mindsdb_port: int = int(_env("MINDSDB_PORT", "47335"))
mindsdb_user: str = _env("MINDSDB_USER", "mindsdb")
mindsdb_pass: str = _env("MINDSDB_PASS", "")
mindsdb_db:   str = _env("MINDSDB_DB",   "mindsdb")
```

#### Step 4. `db.py` 수정

```python
# psycopg 풀 대신 aiomysql 풀로 교체
import aiomysql

_source_pool = None

async def init_source_pool():
    global _source_pool
    _source_pool = await aiomysql.create_pool(
        host=settings.mindsdb_host,
        port=settings.mindsdb_port,
        user=settings.mindsdb_user,
        password=settings.mindsdb_pass,
        db=settings.mindsdb_db,
        minsize=1, maxsize=4,
    )

@asynccontextmanager
async def source_conn():
    async with _source_pool.acquire() as conn:
        yield conn
```

#### Step 5. `query_runner.py` 수정 — PG 전용 래퍼 제거

```python
# 변경 전 (PG 전용 4줄 제거)
async with source_conn() as conn:
    await conn.set_autocommit(False)
    async with conn.cursor(row_factory=tuple_row) as cur:
        await cur.execute("BEGIN READ ONLY")              # ← 제거
        await cur.execute(f"SET LOCAL statement_timeout = '{t_out}s'")  # ← 제거
        await cur.execute("SET LOCAL lock_timeout = '2s'")              # ← 제거
        await cur.execute(f'SET LOCAL search_path = "{settings.source_pg_schema}", public')  # ← 제거
        await cur.execute(report.normalized_sql)

# 변경 후 (MindsDB/ANSI SELECT 전용)
async with source_conn() as conn:
    async with conn.cursor() as cur:
        await cur.execute(report.normalized_sql)
        if cur.description:
            columns = [d[0] for d in cur.description]
            ...
```

> **참고**: MindsDB는 트랜잭션·타임아웃 제어를 자체적으로 처리합니다.  
> 타임아웃이 필요한 경우 Python `asyncio.wait_for()`로 감쌉니다.

---

### 2-2. 다른 PostgreSQL 호환 DB (예: Redshift, CockroachDB, Supabase)

이 경우 `psycopg` 드라이버를 그대로 유지하고 **접속 정보만 교체**합니다.

```dotenv
SOURCE_PG_HOST=your-redshift-cluster.amazonaws.com
SOURCE_PG_PORT=5439
SOURCE_PG_DB=dev
SOURCE_PG_USER=admin
SOURCE_PG_PASS=secret
SOURCE_PG_SCHEMA=public
```

PG 방언 4줄(`SET LOCAL` 등)은 대부분 PostgreSQL 호환 DB에서 지원하므로 코드 변경 불필요.

---

### 2-3. MySQL / MariaDB로 교체

MindsDB 교체와 동일한 절차(`aiomysql` 사용).  
포트만 `3306` (MySQL 기본값)으로 변경.

---

## 3. `sql_guard.py`는 변경 불필요

`sql_guard.py`는 SQL 텍스트를 정규식으로 검사하는 **백엔드 무관 레이어**입니다.  
어떤 백엔드로 교체하더라도 그대로 사용 가능합니다.

```
SELECT 허용  ✅   |  DROP/CREATE/INSERT 차단  ✅  (백엔드 무관)
```

---

## 4. 교체 체크리스트

| 항목 | PostgreSQL → MindsDB | PostgreSQL 호환 DB |
|------|----------------------|--------------------|
| `requirements.txt` | `aiomysql` 교체 | 변경 없음 |
| `.env` 접속 정보 | 변경 필요 | 변경 필요 |
| `config.py` 키 | 변경 필요 | 변경 없음 |
| `db.py` 드라이버 | `aiomysql` 교체 | 변경 없음 |
| `query_runner.py` PG 래퍼 4줄 | **제거** | 변경 없음 |
| `sql_guard.py` | 변경 없음 ✅ | 변경 없음 ✅ |

---

## 5. 빠른 검증

교체 후 아래 요청으로 정상 동작 확인:

```bash
curl -X POST http://localhost:8097/query/execute \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT 1 AS ping"}'
# 기대: {"status": "ok", "rows": [[1]], ...}

curl -X POST http://localhost:8097/query/execute \
  -H "Content-Type: application/json" \
  -d '{"sql": "DROP TABLE test"}'
# 기대: 400 (sql_guard 정상 차단)
```
