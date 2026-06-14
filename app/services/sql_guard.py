"""SQL 가드 — 외부 AI가 보낸 SQL을 실행하기 전 다층 검증.

정책 (사용자 결정):
- 슈퍼유저 계정으로 실행하되, API 단에서 SELECT 이외는 사전 차단
- 다중 statement 금지 (세미콜론 2개 이상)
- 금지어(쓰기·DDL·시스템 함수 등) 블랙리스트 검출
- 허용 시작 키워드 화이트리스트: SELECT / WITH / VALUES / TABLE / SHOW / EXPLAIN(옵션 중 ANALYZE 금지)

실패 시 GuardError 발생 — 호출측이 400 응답으로 "조회 이외에는 사용 불가" 메시지 반환.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# ---------------------------------------------------------------------------
# 정책 상수
# ---------------------------------------------------------------------------

# 허용: 이 키워드로 시작하는 문장만
_ALLOWED_STARTS = {"SELECT", "WITH", "VALUES", "TABLE", "SHOW", "EXPLAIN"}

# 금지: 문장 어디에든 이 키워드가 단독 토큰으로 나타나면 차단
#   (문자열 리터럴·주석·식별자 안쪽은 제외 — 아래에서 정리 후 검사)
_DENY_TOKENS = {
    # DML 쓰기
    "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT", "REPLACE",
    # DDL
    "DROP", "CREATE", "ALTER", "TRUNCATE", "COMMENT", "RENAME",
    # 권한·세션·잠금
    "GRANT", "REVOKE", "SECURITY", "POLICY",
    "LOCK", "UNLOCK", "VACUUM", "REINDEX", "CLUSTER",
    # 서버측 실행·IO
    "COPY", "CALL", "DO", "LISTEN", "NOTIFY", "PREPARE", "EXECUTE", "DEALLOCATE",
    # 세션 상태 변경
    "SET", "RESET", "DISCARD", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT",
    # 파일/함수 악용
    "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR",
    "LO_EXPORT", "LO_IMPORT", "DBLINK", "DBLINK_EXEC",
}

# EXPLAIN ANALYZE는 실제 쿼리를 실행하므로 차단
_EXPLAIN_ANALYZE_RE = re.compile(r"\bEXPLAIN\s*\(\s*[^)]*\bANALYZE\b", re.IGNORECASE)
_EXPLAIN_ANALYZE_SIMPLE_RE = re.compile(r"\bEXPLAIN\s+ANALYZE\b", re.IGNORECASE)


class GuardError(ValueError):
    """SQL 검증 실패."""


@dataclass
class GuardReport:
    normalized_sql: str          # 주석 제거·양끝 공백/세미콜론 제거된 형태
    leading_keyword: str          # 첫 토큰 (대문자)
    statement_count: int          # 세미콜론으로 분리된 비어있지 않은 문장 수


# ---------------------------------------------------------------------------
# 주석·문자열 리터럴 제거 (토큰 검사용 정제)
# ---------------------------------------------------------------------------

_LINE_COMMENT_RE = re.compile(r"--[^\n\r]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SINGLE_QUOTED_RE = re.compile(r"'(?:''|[^'])*'")
_DOUBLE_QUOTED_RE = re.compile(r'"(?:""|[^"])*"')     # 식별자 — 토큰 검사엔 안전하게 치환
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", re.DOTALL)


def _strip_literals_for_scan(sql: str) -> str:
    """토큰 블랙리스트 검사용으로 문자열·주석·달러 태그 구간을 공백으로 치환.
    식별자("..."), 일반 스페이스·세미콜론·연산자는 그대로 유지."""
    s = sql
    s = _LINE_COMMENT_RE.sub(" ", s)
    s = _BLOCK_COMMENT_RE.sub(" ", s)
    s = _SINGLE_QUOTED_RE.sub("' '", s)
    s = _DOUBLE_QUOTED_RE.sub('"id"', s)

    # $$...$$ or $tag$...$tag$ 도 내용 제거
    def _strip_dollar(text: str) -> str:
        out: list[str] = []
        i = 0
        while i < len(text):
            m = _DOLLAR_TAG_RE.match(text, i)
            if m:
                tag = m.group(0)
                out.append(" ")
                end = text.find(tag, m.end())
                if end < 0:
                    out.append(" ")
                    i = len(text)
                    break
                out.append(" ")
                i = end + len(tag)
            else:
                out.append(text[i])
                i += 1
        return "".join(out)

    return _strip_dollar(s)


def _split_statements(scan_sql: str) -> List[str]:
    parts = [p.strip() for p in scan_sql.split(";")]
    return [p for p in parts if p]


def check(sql_raw: str) -> GuardReport:
    """검증 통과 시 GuardReport 반환, 실패 시 GuardError 발생.

    * 검사(토큰/금지어)는 리터럴 치환된 스캔 버전으로 수행
    * 실행에 쓰는 `normalized_sql`은 **원본 SQL**의 끝 세미콜론만 제거한 값
    """
    if not sql_raw or not sql_raw.strip():
        raise GuardError("빈 SQL은 허용되지 않습니다.")

    # --- 원본 기준 다중 문장 검사 ---
    scan = _strip_literals_for_scan(sql_raw)
    scan_parts = _split_statements(scan)
    if len(scan_parts) == 0:
        raise GuardError("유효한 SQL 문장이 없습니다.")
    if len(scan_parts) > 1:
        raise GuardError(
            "다중 쿼리는 허용되지 않습니다 — 세미콜론으로 분리된 2개 이상의 문장이 감지됨. "
            "한 번에 하나의 SELECT만 보낼 수 있습니다."
        )
    stmt_scan = scan_parts[0]

    # --- 실행용: 원본 그대로에서 양끝 공백·trailing 세미콜론만 제거 ---
    stmt_exec = sql_raw.strip()
    while stmt_exec.endswith(";"):
        stmt_exec = stmt_exec[:-1].rstrip()
    if not stmt_exec:
        raise GuardError("유효한 SQL 문장이 없습니다.")

    # 1) 허용 시작 키워드 화이트리스트 (스캔 기준)
    first_token_match = re.match(r"\s*([A-Za-z][A-Za-z0-9_]*)", stmt_scan)
    if not first_token_match:
        raise GuardError("SQL에서 시작 키워드를 식별할 수 없습니다.")
    first = first_token_match.group(1).upper()
    if first not in _ALLOWED_STARTS:
        raise GuardError(
            f"조회 이외에는 사용 불가 — '{first}'로 시작하는 문은 허용되지 않습니다. "
            f"허용 시작 키워드: {sorted(_ALLOWED_STARTS)}."
        )

    # 2) EXPLAIN ANALYZE 차단
    if _EXPLAIN_ANALYZE_RE.search(stmt_scan) or _EXPLAIN_ANALYZE_SIMPLE_RE.search(stmt_scan):
        raise GuardError("조회 이외에는 사용 불가 — EXPLAIN ANALYZE는 실제 실행을 수반하므로 차단됩니다.")

    # 3) 금지 토큰 블랙리스트 (스캔 기준 — 리터럴·식별자에 든 금지어는 무시)
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", stmt_scan))
    upper_tokens = {t.upper() for t in tokens}
    hit = upper_tokens & _DENY_TOKENS
    if hit:
        if first == "EXPLAIN" and hit == {"SET"}:
            pass
        else:
            raise GuardError(
                "조회 이외에는 사용 불가 — 금지된 키워드 포함: "
                + ", ".join(sorted(hit))
            )

    return GuardReport(
        normalized_sql=stmt_exec,
        leading_keyword=first,
        statement_count=1,
    )
