-- knowledge 스키마: ontology/*.yaml의 사본 (정본은 YAML).
-- scripts/load_ontology.py가 knowledge_writer 계정으로 실행한다. 다시 실행해도 안전하다.
-- 데이터는 적재 스크립트가 트랜잭션 하나로 지우고 다시 채운다. 손으로 고치지 않는다.

SET search_path = knowledge, public;

-- 공통 id 등록부: 개념·속성·관계·용어·규칙. 모든 참조 FK가 여기를 가리킨다.
CREATE TABLE IF NOT EXISTS item (
    id               text PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('concept', 'attribute', 'relation', 'term', 'rule')),
    label            text,
    definition       text,
    note             text,
    generalized_from text
);
COMMENT ON TABLE item IS '온톨로지 항목 공통 등록부 (YAML 사본)';

-- 근거: design.md 정책·결정, 1회성 프로파일링, schema_catalog
CREATE TABLE IF NOT EXISTS item_evidence (
    item_id     text NOT NULL REFERENCES item (id),
    seq         int  NOT NULL,
    source      text NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('policy', 'decision', 'profiling', 'catalog')),
    PRIMARY KEY (item_id, seq)
);

-- 골든 문항 추적(motivated_by). 검색 채점 전용: 렌더러·임베딩은 읽지 않고, 읽기 계정 권한도 회수한다.
CREATE TABLE IF NOT EXISTS item_motivation (
    item_id     text NOT NULL REFERENCES item (id),
    question_id text NOT NULL,
    PRIMARY KEY (item_id, question_id)
);
COMMENT ON TABLE item_motivation IS '골든 문항 추적. 에이전트 프롬프트에 넣지 않는다';

-- model.yaml
CREATE TABLE IF NOT EXISTS concept (
    id                  text PRIMARY KEY REFERENCES item (id),
    table_name          text NOT NULL,
    grain               text,
    identity            text[],
    parent_id           text REFERENCES concept (id),
    discriminator       text,
    discriminator_value text,
    derived_from        text REFERENCES concept (id)
);

CREATE TABLE IF NOT EXISTS attribute (
    id            text PRIMARY KEY REFERENCES item (id),  -- 'Concept.column'
    concept_id    text NOT NULL REFERENCES concept (id),
    column_name   text NOT NULL,
    data_type     text NOT NULL,
    unit          text,
    missing_means text,
    zero_means    text,
    UNIQUE (concept_id, column_name)
);

CREATE TABLE IF NOT EXISTS relation (
    id          text PRIMARY KEY REFERENCES item (id),
    from_id     text NOT NULL REFERENCES concept (id),
    to_id       text NOT NULL REFERENCES concept (id),
    cardinality text NOT NULL CHECK (cardinality IN ('1..1', '1..n', 'n..1', 'n..n')),
    via         text[] NOT NULL,
    derived     text
);

-- glossary.yaml
CREATE TABLE IF NOT EXISTS term (
    id           text PRIMARY KEY REFERENCES item (id),
    scope_tables text[] NOT NULL,
    in_data      boolean NOT NULL DEFAULT true,
    order_values text[],  -- 등급 순서. 배열 순서가 곧 순서다
    examples     text[]
);
-- 2026-10-30 추가 (이미 있는 테이블에도 붙도록 ALTER로 둔다)
ALTER TABLE term ADD COLUMN IF NOT EXISTS bundle boolean NOT NULL DEFAULT false;  -- 모든 표현이 canonical 값 전체
ALTER TABLE term ADD COLUMN IF NOT EXISTS order_up text CHECK (order_up IN ('first', 'last'));  -- "이상" 쪽 끝

CREATE TABLE IF NOT EXISTS term_canonical (
    term_id    text NOT NULL REFERENCES term (id),
    seq        int  NOT NULL,
    ref_id     text NOT NULL REFERENCES item (id),
    ref_values text[],  -- NULL이면 값 조건 없이 개념·컬럼 자체가 정본
    PRIMARY KEY (term_id, seq)
);

CREATE TABLE IF NOT EXISTS term_surface_form (
    term_id text NOT NULL REFERENCES term (id),
    form    text NOT NULL,
    PRIMARY KEY (term_id, form)
);

CREATE TABLE IF NOT EXISTS term_confusable (
    term_id     text NOT NULL REFERENCES term (id),
    seq         int  NOT NULL,
    target_id   text REFERENCES item (id),
    target_text text,  -- 온톨로지 항목이 아닌 대상(모델 밖 컬럼, 자유 표현)
    why         text NOT NULL,
    PRIMARY KEY (term_id, seq),
    CHECK ((target_id IS NULL) <> (target_text IS NULL))
);

-- rules.yaml
CREATE TABLE IF NOT EXISTS rule (
    id        text PRIMARY KEY REFERENCES item (id),
    rule_type text NOT NULL CHECK (rule_type IN ('조건', '시점', '예외', '계산')),
    when_text text NOT NULL,
    then_text text NOT NULL,
    disclose  text
);

CREATE TABLE IF NOT EXISTS rule_applies_to (
    rule_id   text NOT NULL REFERENCES rule (id),
    target_id text NOT NULL REFERENCES item (id),
    PRIMARY KEY (rule_id, target_id)
);

CREATE TABLE IF NOT EXISTS rule_link (
    rule_id        text NOT NULL REFERENCES rule (id),
    target_rule_id text NOT NULL REFERENCES rule (id),
    link_type      text NOT NULL CHECK (link_type IN ('overrides', 'depends_on')),
    PRIMARY KEY (rule_id, target_rule_id, link_type)
);

-- review_log.yaml: 추적용으로 통째 보관
CREATE TABLE IF NOT EXISTS review_entry (
    section text NOT NULL CHECK (section IN ('decisions', 'conflicts', 'profiling', 'open_questions', 'reviews')),
    id      text NOT NULL,  -- reviews는 id가 없어 'reviews-<순번>'
    body    jsonb NOT NULL,
    PRIMARY KEY (section, id)
);

-- 적재 정보 (1행)
CREATE TABLE IF NOT EXISTS source_version (
    singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    loaded_at   timestamptz NOT NULL,
    git_commit  text,
    git_dirty   boolean,
    file_sha256 jsonb NOT NULL,
    row_counts  jsonb NOT NULL
);

-- 검색용 임베딩 (scripts/embed_ontology.py가 채운다)
-- item FK 없음: load_ontology.py의 전체 삭제·재적재에도 벡터를 보존한다.
-- 사라진 항목은 embed_ontology.py가 지우고, content_sha가 바뀐 항목만 다시 임베딩한다.
CREATE TABLE IF NOT EXISTS item_embedding (
    item_id     text NOT NULL,
    model       text NOT NULL,
    variant     text NOT NULL,  -- 문서 조립 방식 (예: 'base')
    content_sha text NOT NULL,  -- sha256(model, variant, content)
    content     text NOT NULL,  -- 임베딩한 문서 원문
    vec         vector(1024) NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, model, variant)
);

-- 부모 개념의 속성을 자식 개념에 펼친다 (예: DomesticEtf ← DomesticListedProduct)
CREATE OR REPLACE VIEW attribute_resolved AS
WITH RECURSIVE lineage AS (
    SELECT id AS concept_id, id AS owner_id, 0 AS depth FROM concept
    UNION ALL
    SELECT l.concept_id, c.parent_id, l.depth + 1
    FROM lineage l JOIN concept c ON c.id = l.owner_id
    WHERE c.parent_id IS NOT NULL
)
SELECT l.concept_id,
       a.column_name,
       a.id AS attribute_id,
       l.owner_id AS defined_on,
       l.depth = 0 AS own,
       a.data_type, a.unit, a.missing_means, a.zero_means
FROM lineage l
JOIN attribute a ON a.concept_id = l.owner_id;

-- 읽기 계정: 스키마 기본 권한(ALTER DEFAULT PRIVILEGES)으로 테이블·뷰 SELECT가 열린다.
-- 문항 추적 테이블만 회수한다.
REVOKE ALL ON item_motivation FROM financial_agent_reader;
