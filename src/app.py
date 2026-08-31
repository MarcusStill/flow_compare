import json
import re
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Optional
import os
import psycopg2
from dotenv import load_dotenv

import pandas as pd
import streamlit as st

load_dotenv()
root_dir = Path(__file__).resolve().parent.parent
DATA_DIR = root_dir / "data"

ADH1_PATH = DATA_DIR / "adh1_new.json"
ADH3_PATH = DATA_DIR / "adh3_new.json"
RESULT_PATH = DATA_DIR / "manual_dependency_review_new_json.csv"
PARENT_WORKFLOW_URL_TEMPLATE = "http://adp-eiap-app1.adp.local/#/workflows/{flow_name}/tasks"


# Типы зависимостей, у которых нет parentName/parentTsk,
# но есть смысловое значение в depValue.
# Чтобы добавить новый тип, допиши его сюда:
# "someType": {"label": "someType", "unit": "..."}
SPECIAL_DEP_VALUE_TYPES = {
    "lastRunTime": {"label": "lastRunTime", "unit": "мин"},
}

# Типы зависимостей, у которых нет родительского workflow/task,
# но есть список сущностей внутри rcDesc/rcBody.
# Для таких зависимостей в parentNmeUnq показываем короткое описание,
# а полную детализацию оставляем в rcDesc/rcBody.
SPECIAL_ENTITY_DEP_TYPES = {
    "parentIsNotWorkingByEntity": {
        "label": "target entities",
        "source_phrase": "целевыми таблицами",
    },
}

# Типы зависимостей, которые нужно развернуть в отдельные строки
# по каждой сущности/таблице.
# Если появится похожий тип, добавь его сюда.
SPLIT_ENTITY_DEP_TYPES = {
    "parentIsNotWorkingByEntity",
}


# ------------------------------------------------------------
# JSON loading
# ------------------------------------------------------------

def load_json_values(path: str) -> list[Any]:
    text = Path(path).read_text(encoding="utf-8").strip()

    if not text:
        return []

    try:
        return [json.loads(text)]
    except JSONDecodeError:
        pass

    values = []
    decoder = json.JSONDecoder()
    idx = 0

    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1

        if idx >= len(text):
            break

        try:
            value, end = decoder.raw_decode(text, idx)
        except JSONDecodeError as exc:
            raise ValueError(
                f"Не удалось распарсить JSON в файле {path}. "
                f"Ошибка около позиции {idx}: {exc}"
            ) from exc

        values.append(value)
        idx = end

    return values


def try_parse_json_string(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        return value

    if not isinstance(value, str):
        return None

    stripped = value.strip()

    if not stripped.startswith("{"):
        return None

    try:
        parsed = json.loads(stripped)
    except JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def get_first_string_value(obj: dict, keys: list[str]) -> Optional[str]:
    for key in keys:
        value = obj.get(key)

        if value is None:
            continue

        if isinstance(value, str) and value.strip():
            return value.strip()

        if isinstance(value, (int, float)):
            return str(value)

    return None


def split_comma_entities(value: str) -> list[str]:
    return [
        entity.strip()
        for entity in value.split(",")
        if entity.strip()
    ]


def extract_entities_from_text(text: Any) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []

    patterns = [
        # Описание вида:
        # "Проверка отсутствия активных task с целевыми таблицами: a, b, c"
        r"целевыми таблицами:\s*(.+?)(?:\n|$)",
        r"target tables:\s*(.+?)(?:\n|$)",
        r"entities:\s*(.+?)(?:\n|$)",

        # SQL вида:
        # lower('{a, b, c}')::text[]
        r"lower\('\{(.+?)\}'\)::text\[\]",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)

        if match:
            return split_comma_entities(match.group(1))

    return []


def extract_entities_from_dependency(item: dict) -> list[str]:
    """
    Достает список сущностей/таблиц для зависимостей типа
    parentIsNotWorkingByEntity.

    Сначала пробуем rcDesc, потом rcBody. Обычно нужный список есть
    в обоих местах, но rcDesc короче и удобнее.
    """
    return (
        extract_entities_from_text(item.get("rcDesc"))
        or extract_entities_from_text(item.get("rcBody"))
    )


def make_short_entity_name(dep_type: str, entities: list[str]) -> str:
    meta = SPECIAL_ENTITY_DEP_TYPES.get(dep_type, {})
    label = meta.get("label", "entities")

    if not entities:
        return f"{dep_type}: {label}"

    first_entity = entities[0]

    if len(entities) == 1:
        return f"{dep_type}: {first_entity}"

    return f"{dep_type}: {len(entities)} {label}; first={first_entity}"


def extract_special_dependency_name(
    item: dict,
    dep_typ: Optional[str],
    rc_typ_unq: Optional[str],
) -> Optional[str]:
    """
    Возвращает человекочитаемое имя для зависимостей без родителя.

    Примеры:
    - lastRunTime + depValue=10080 -> lastRunTime: 10080 мин
    - parentIsNotWorkingByEntity -> parentIsNotWorkingByEntity: 11 target entities; first=...
    """
    dep_type = rc_typ_unq or dep_typ

    if not dep_type:
        return None

    if dep_type in SPECIAL_DEP_VALUE_TYPES:
        dep_value = item.get("depValue")

        if dep_value is not None:
            meta = SPECIAL_DEP_VALUE_TYPES[dep_type]
            label = meta.get("label", dep_type)
            unit = meta.get("unit", "")

            if unit:
                return f"{label}: {dep_value} {unit}"

            return f"{label}: {dep_value}"

    if dep_type in SPECIAL_ENTITY_DEP_TYPES:
        entities = (
            extract_entities_from_text(item.get("rcDesc"))
            or extract_entities_from_text(item.get("rcBody"))
        )

        return make_short_entity_name(dep_type, entities)

    return None


# ------------------------------------------------------------
# Extract parent / child names
# ------------------------------------------------------------

def extract_parent_name(item: dict) -> Optional[str]:
    direct_parent = get_first_string_value(
        item,
        [
            "parentName",
            "parentNmeUnq",
            "parentTsk",
            "parentWf",
            "parent",
            "obj_name",
        ],
    )

    if direct_parent:
        return direct_parent

    body = try_parse_json_string(item.get("rcBody"))

    if body:
        body_parent = get_first_string_value(
            body,
            [
                "parentTsk",
                "parentWf",
                "parent",
                "obj_name",
            ],
        )

        if body_parent:
            return body_parent

        parents = body.get("parents")

        if isinstance(parents, list):
            for parent in parents:
                if isinstance(parent, dict):
                    parent_name = get_first_string_value(
                        parent,
                        ["obj_name", "name", "parentName", "parentNmeUnq"],
                    )

                    if parent_name:
                        return parent_name

    rc_desc = item.get("rcDesc")

    if isinstance(rc_desc, str):
        patterns = [
            r"родительской задачи\s+([A-Za-z0-9_\[\]\-]+)",
            r"workflow-предшественника\s+([A-Za-z0-9_\[\]\-]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, rc_desc, flags=re.IGNORECASE)

            if match:
                return match.group(1)

    return None


def extract_child_flow_from_item(item: dict) -> Optional[str]:
    direct_child = get_first_string_value(
        item,
        [
            "flowName",
            "flow_name",
            "workflowName",
            "workflow_name",
            "wfNmeUnq",
            "wf_nme_unq",
            "childWf",
            "child",
        ],
    )

    if direct_child:
        return direct_child

    body = try_parse_json_string(item.get("rcBody"))

    if body:
        body_child = get_first_string_value(
            body,
            [
                "childWf",
                "child",
                "workflowName",
                "flowName",
            ],
        )

        if body_child:
            return body_child

    rc_desc = item.get("rcDesc")

    if isinstance(rc_desc, str):
        patterns = [
            r"даты запуска workflow\s+([A-Za-z0-9_\[\]\-]+)",
            r"workflow\s+([A-Za-z0-9_\[\]\-]+)\s*$",
        ]

        for pattern in patterns:
            match = re.search(pattern, rc_desc, flags=re.IGNORECASE)

            if match:
                return match.group(1)

    return None


# ------------------------------------------------------------
# Flow extraction for new JSON
# ------------------------------------------------------------

def is_condition_like_list(value: list) -> bool:
    return (
        bool(value)
        and all(
            isinstance(item, dict)
            and (
                "depTyp" in item
                or "depParents" in item
                or "rcTypUnq" in item
            )
            for item in value
        )
    )


def infer_flow_name_from_conditions(
    conditions: list[dict],
    fallback_name: str,
) -> str:
    for condition in conditions:
        dep_parents = condition.get("depParents")

        if isinstance(dep_parents, list):
            for parent in dep_parents:
                child_flow = extract_child_flow_from_item(parent)

                if child_flow:
                    return child_flow

        child_flow = extract_child_flow_from_item(condition)

        if child_flow:
            return child_flow

    return fallback_name


def extract_flow_records(value: Any, fallback_name: str) -> list[dict]:
    records = []

    if isinstance(value, list):
        if is_condition_like_list(value):
            records.append({
                "flow_name": fallback_name,
                "conditions": value,
            })
            return records

        for idx, item in enumerate(value, start=1):
            records.extend(
                extract_flow_records(
                    item,
                    fallback_name=f"{fallback_name}_{idx}",
                )
            )

        return records

    if not isinstance(value, dict):
        return records

    explicit_flow_name = get_first_string_value(
        value,
        [
            "flowName",
            "flow_name",
            "workflowName",
            "workflow_name",
            "wfNmeUnq",
            "wf_nme_unq",
            "name",
            "nmeUnq",
        ],
    )

    if "conditions" in value and isinstance(value["conditions"], list):
        flow_name = explicit_flow_name or infer_flow_name_from_conditions(
            value["conditions"],
            fallback_name=fallback_name,
        )

        records.append({
            "flow_name": flow_name,
            "conditions": value["conditions"],
        })

        return records

    container_keys = [
        "workflows",
        "flows",
        "items",
        "data",
        "result",
        "results",
    ]

    for container_key in container_keys:
        container_value = value.get(container_key)

        if isinstance(container_value, (list, dict)):
            nested_records = extract_flow_records(
                container_value,
                fallback_name=fallback_name,
            )

            if nested_records:
                records.extend(nested_records)

    if records:
        return records

    for key, nested_value in value.items():
        if key in container_keys:
            continue

        if isinstance(nested_value, dict):
            nested_records = extract_flow_records(
                nested_value,
                fallback_name=str(key),
            )

            if nested_records:
                for record in nested_records:
                    if not record.get("flow_name"):
                        record["flow_name"] = str(key)

                records.extend(nested_records)

        elif isinstance(nested_value, list) and is_condition_like_list(nested_value):
            records.append({
                "flow_name": str(key),
                "conditions": nested_value,
            })

    return records


# ------------------------------------------------------------
# Flatten new JSON
# ------------------------------------------------------------

def flatten_new_conditions(
    conditions: list[dict],
    cluster: str,
    flow_name: str,
) -> list[dict]:
    rows = []

    for condition_index, condition in enumerate(conditions, start=1):
        dep_typ = condition.get("depTyp") or condition.get("rcTypUnq")
        dep_parents = condition.get("depParents")

        if isinstance(dep_parents, list) and dep_parents:
            parent_items = dep_parents
            is_group_condition = True
        else:
            parent_items = [condition]
            is_group_condition = False

        for parent_index, item in enumerate(parent_items, start=1):
            rc_typ_unq = (
                item.get("rcTypUnq")
                or condition.get("rcTypUnq")
                or condition.get("depTyp")
                or dep_typ
            )

            dep_value = item.get("depValue")

            # Спец-логика: одну зависимость с набором сущностей/таблиц
            # разворачиваем в несколько строк — по одной на каждую entity.
            # Так каждую сущность можно отдельно отметить как matched / need_review /
            # unnecessary_on_adh3 и т.д.
            if rc_typ_unq in SPLIT_ENTITY_DEP_TYPES:
                entities = extract_entities_from_dependency(item)

                if entities:
                    for entity_index, entity_name in enumerate(entities, start=1):
                        rows.append({
                            "cluster": cluster,
                            "flow_name": flow_name,

                            "condition_index": condition_index,
                            "parent_index": parent_index,
                            "entity_index": entity_index,
                            "is_group_condition": is_group_condition,

                            "depTyp": dep_typ,
                            "rcTypUnq": rc_typ_unq,
                            "depValue": dep_value,

                            "depndId": item.get("depndId"),
                            "parentId": item.get("parentId"),
                            "parentTyp": "entity",
                            "parentNmeUnq": entity_name,

                            "rcId": item.get("rcId"),
                            "rcTyp": item.get("rcTyp"),
                            "rcBody": item.get("rcBody"),
                            "rcDesc": item.get("rcDesc"),

                            "parentIsNotFoundFlg": item.get("parentIsNotFoundFlg"),
                            "depndIsComm": item.get("depndIsComm"),

                            "rrRcStatus": item.get("rrRcStatus"),
                            "rcPriorVal": item.get("rcPriorVal"),
                            "rcNmeUnq": item.get("rcNmeUnq"),
                            "dependencyStatus": item.get("dependencyStatus"),
                        })

                    continue

            parent_name = extract_parent_name(item)

            if not parent_name:
                parent_name = extract_special_dependency_name(
                    item=item,
                    dep_typ=dep_typ,
                    rc_typ_unq=rc_typ_unq,
                )

            rows.append({
                "cluster": cluster,
                "flow_name": flow_name,

                "condition_index": condition_index,
                "parent_index": parent_index,
                "entity_index": None,
                "is_group_condition": is_group_condition,

                "depTyp": dep_typ,
                "rcTypUnq": rc_typ_unq,
                "depValue": dep_value,

                "depndId": item.get("depndId"),
                "parentId": item.get("parentId"),
                "parentTyp": item.get("parentTyp"),
                "parentNmeUnq": parent_name,

                "rcId": item.get("rcId"),
                "rcTyp": item.get("rcTyp"),
                "rcBody": item.get("rcBody"),
                "rcDesc": item.get("rcDesc"),

                "parentIsNotFoundFlg": item.get("parentIsNotFoundFlg"),
                "depndIsComm": item.get("depndIsComm"),

                "rrRcStatus": item.get("rrRcStatus"),
                "rcPriorVal": item.get("rcPriorVal"),
                "rcNmeUnq": item.get("rcNmeUnq"),
                "dependencyStatus": item.get("dependencyStatus"),
            })

    return rows


def flatten_conditions_from_file(path: str, cluster: str) -> pd.DataFrame:
    json_values = load_json_values(path)

    rows = []

    for obj_idx, value in enumerate(json_values, start=1):
        flow_records = extract_flow_records(
            value,
            fallback_name=f"{cluster}_json_{obj_idx}",
        )

        for flow_idx, flow_record in enumerate(flow_records, start=1):
            flow_name = flow_record.get("flow_name") or f"{cluster}_flow_{flow_idx}"
            conditions = flow_record.get("conditions") or []

            rows.extend(
                flatten_new_conditions(
                    conditions=conditions,
                    cluster=cluster,
                    flow_name=flow_name,
                )
            )

    columns = [
        "cluster",
        "flow_name",

        "condition_index",
        "parent_index",
        "entity_index",
        "is_group_condition",

        "depTyp",
        "rcTypUnq",
        "depValue",

        "depndId",
        "parentId",
        "parentTyp",
        "parentNmeUnq",

        "rcId",
        "rcTyp",
        "rcBody",
        "rcDesc",

        "parentIsNotFoundFlg",
        "depndIsComm",

        "rrRcStatus",
        "rcPriorVal",
        "rcNmeUnq",
        "dependencyStatus",
    ]

    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame(rows, columns=columns)


# ------------------------------------------------------------
# Reviews
# ------------------------------------------------------------

def load_reviews() -> pd.DataFrame:
    if Path(RESULT_PATH).exists():
        return pd.read_csv(RESULT_PATH)

    return pd.DataFrame(columns=[
        "review_key",
        "adh1_review_key",
        "adh3_review_key",
        "rcTypUnq",

        "adh1_flow_name",
        "adh1_depndId",
        "adh1_parentTyp",
        "adh1_parentNmeUnq",
        "adh1_depValue",

        "adh3_flow_name",
        "adh3_depndId",
        "adh3_parentTyp",
        "adh3_parentNmeUnq",
        "adh3_depValue",

        "manual_status",
        "comment",
    ])


def is_valid_key(value: Any) -> bool:
    if value is None:
        return False

    try:
        if pd.isna(value):
            return False
    except TypeError:
        pass

    return bool(str(value).strip())


def get_review_map(result: pd.DataFrame) -> dict:
    """
    Возвращает словарь:
        review_key зависимости -> manual_status

    Важно:
    - старые строки с одним review_key тоже поддерживаются;
    - новые строки с adh1_review_key и adh3_review_key отмечают проверенными обе стороны.
    """
    if result.empty:
        return {}

    review_map = {}

    for _, row in result.iterrows():
        status = row.get("manual_status")

        for key_col in ["review_key", "adh1_review_key", "adh3_review_key"]:
            if key_col not in result.columns:
                continue

            key = row.get(key_col)

            if is_valid_key(key):
                review_map[str(key)] = status

    return review_map


def safe_key_part(value: Any) -> str:
    if value is None:
        return "NULL"

    try:
        if pd.isna(value):
            return "NULL"
    except TypeError:
        pass

    return str(value).strip()


def make_review_key(row: pd.Series) -> str:
    """
    Ключ проверки для одной зависимости.

    flow_name добавлен обязательно, чтобы одинаковые зависимости
    в разных потоках не считались одной проверкой.
    """
    parts = [
        row.get("cluster"),
        row.get("flow_name"),
        row.get("rcTypUnq"),
        row.get("parentTyp"),
        row.get("parentNmeUnq"),
        row.get("depndId"),
        row.get("rcId"),
    ]

    return "__".join(safe_key_part(part) for part in parts)


def get_review_keys_from_row(row: pd.Series | dict) -> set[str]:
    keys = set()

    for key_col in ["review_key", "adh1_review_key", "adh3_review_key"]:
        if isinstance(row, pd.Series):
            if key_col not in row.index:
                continue
            key = row.get(key_col)
        else:
            key = row.get(key_col)

        if is_valid_key(key):
            keys.add(str(key))

    return keys


def save_review(row: dict):
    if Path(RESULT_PATH).exists():
        result = pd.read_csv(RESULT_PATH)
    else:
        result = pd.DataFrame()

    new_keys = get_review_keys_from_row(row)

    if not new_keys:
        raise ValueError("review_key is required")

    if not result.empty:
        rows_to_keep = []

        for _, old_row in result.iterrows():
            old_keys = get_review_keys_from_row(old_row)
            rows_to_keep.append(not bool(old_keys & new_keys))

        result = result[pd.Series(rows_to_keep, index=result.index)]

    result = pd.concat([result, pd.DataFrame([row])], ignore_index=True)
    result.to_csv(RESULT_PATH, index=False)


# ------------------------------------------------------------
# Display helpers
# ------------------------------------------------------------

def status_prefix(status) -> str:
    if pd.isna(status) or status is None:
        return "⬜ not_checked"

    if status == "matched":
        return "✅ matched"

    if status == "matched_with_changes":
        return "🟡 matched_with_changes"

    if status == "missing_on_adh3":
        return "❌ missing_on_adh3"

    if status == "unnecessary_on_adh3":
        return "❌ unnecessary_on_adh3"

    if status == "ignored":
        return "🚫 ignored"

    if status == "need_review":
        return "❓ need_review"

    return f"☑️ {status}"


def make_left_label(row: pd.Series) -> str:
    prefix = status_prefix(row.get("manual_status"))

    return (
        f"{prefix} | "
        f"depndId={row.get('depndId')} | "
        f"{row.get('parentTyp')} | "
        f"{row.get('parentNmeUnq')}"
    )


def make_right_label(row: pd.Series) -> str:
    prefix = status_prefix(row.get("manual_status"))

    return (
        f"{prefix} | "
        f"depndId={row.get('depndId')} | "
        f"{row.get('parentTyp')} | "
        f"{row.get('parentNmeUnq')}"
    )


def highlight_status(row):
    status = row.get("manual_status")

    if status == "matched":
        return ["background-color: #d4edda"] * len(row)

    if status == "matched_with_changes":
        return ["background-color: #fff3cd"] * len(row)

    if status in ("missing_on_adh3", "unnecessary_on_adh3"):
        return ["background-color: #f8d7da"] * len(row)

    if status == "ignored":
        return ["background-color: #e2e3e5"] * len(row)

    if status == "need_review":
        return ["background-color: #d1ecf1"] * len(row)

    return [""] * len(row)


def is_not_empty_text(value: Any) -> bool:
    if value is None:
        return False

    try:
        if pd.isna(value):
            return False
    except TypeError:
        pass

    text = str(value).strip()
    return bool(text) and text.lower() not in {"none", "nan", "null"}


def can_open_parent_workflow(row: Optional[pd.Series]) -> bool:
    if row is None:
        return False

    parent_typ = str(row.get("parentTyp") or "").strip().lower()
    parent_name = row.get("parentNmeUnq")

    return parent_typ == "workflow" and is_not_empty_text(parent_name)


def can_open_parent_task(row: pd.Series) -> bool:
    parent_typ = row.get("parentTyp")
    parent_name = row.get("parentNmeUnq")

    return (
        str(parent_typ).lower() == "task"
        and is_not_empty_text(parent_name)
    )


def is_entity_dependency(row: pd.Series) -> bool:
    parent_typ = row.get("parentTyp")
    parent_name = row.get("parentNmeUnq")

    return (
        str(parent_typ).lower() == "entity"
        and is_not_empty_text(parent_name)
    )


def build_parent_workflow_url(parent_name: str) -> str:
    return PARENT_WORKFLOW_URL_TEMPLATE.format(flow_name=str(parent_name).strip())


def build_parent_task_url(task_name: str) -> str:
    return f"http://adp-eiap-app1.adp.local/#/tasks/{task_name}/info/"


@st.cache_resource
def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("MDB_POSTGRES_HOST"),
        port=os.getenv("MDB_POSTGRES_PORT"),
        dbname=os.getenv("MDB_POSTGRES_DATABASE"),
        user=os.getenv("MDB_POSTGRES_USERNAME"),
        password=os.getenv("MDB_POSTGRES_PASSWORD"),
    )


@st.cache_data(ttl=300)
def get_parent_flow_analog(parent_flow_name: str) -> str | None:
    """
    Возвращает аналог parent_flow_name на другом кластере
    """

    if not parent_flow_name:
        return None

    query = """
        with get_wf_info as (
            select 
                * 
            from
                mdb.workflow 
            where wf_nme_unq = %s
        )
        select
            t2.wf_nme_unq as analog_wf_nme_unq
        from
            get_wf_info t1
        left join 
            mdb.workflow t2 on t1.wf_desc = t2.wf_desc
                           and t1.wf_nme_unq <> t2.wf_nme_unq
    """

    conn = get_pg_connection()

    with conn.cursor() as cur:
        cur.execute(query, (parent_flow_name,))
        row = cur.fetchone()

    if not row:
        return None

    return row[0]

@st.cache_data(ttl=300)
def get_entity_analog(entity_name: str) -> str | None:
    """
    Возвращает аналог entity на другом кластере.
    """

    if not entity_name:
        return None

    query = """
        with get_ent_info as (
            select 
                entity_nme_unq
                , regexp_replace(
                    entity_nme_unq,
                    '^.*__(.*__.*)$',
                    '\\1'
                ) AS short_name
            FROM mdb.entity
        )
        select
            t2.entity_nme_unq as analog_entity_nme_unq
        from
            get_ent_info t1
        left join 
            get_ent_info t2 on lower(t1.short_name) = lower(t2.short_name)
                           and t1.entity_nme_unq <> t2.entity_nme_unq	
        where t1.entity_nme_unq = %s	
    """

    conn = get_pg_connection()

    with conn.cursor() as cur:
        cur.execute(query, (entity_name,))
        row = cur.fetchone()

    if not row:
        return None

    return row[0]


def show_dependency_details(title: str, row: Optional[pd.Series]):
    st.subheader(title)

    if row is None:
        st.warning("Зависимость не выбрана")
        return

    parent_name = row.get("parentNmeUnq")
    parent_name_for_display = "" if not is_not_empty_text(parent_name) else str(parent_name)

    st.markdown("**parentNmeUnq / dependency value:**")
    st.code(parent_name_for_display or "Нет родительского объекта")

    is_workflow_parent = can_open_parent_workflow(row)
    is_task_parent = can_open_parent_task(row)
    is_entity_parent = is_entity_dependency(row)

    if is_workflow_parent:
        parent_url = build_parent_workflow_url(parent_name_for_display)
        parent_button_label = "Открыть родительский поток"
        parent_button_enabled = True

    elif is_task_parent:
        parent_url = build_parent_task_url(parent_name_for_display)
        parent_button_label = "Открыть родительскую task"
        parent_button_enabled = True

    else:
        parent_url = "#"
        parent_button_label = "Открыть родительский объект"
        parent_button_enabled = False

    st.link_button(
        parent_button_label,
        parent_url,
        disabled=not parent_button_enabled,
        use_container_width=True,
    )

    if is_workflow_parent:
        try:
            analog_parent_name = get_parent_flow_analog(parent_name_for_display)
        except Exception as exc:
            analog_parent_name = None
            st.warning(f"Не удалось получить аналог родительского потока: {exc}")

        st.markdown("**Аналог родительского потока на другом кластере:**")

        if is_not_empty_text(analog_parent_name):
            analog_parent_name = str(analog_parent_name)

            st.code(analog_parent_name)

            st.link_button(
                "Открыть аналог родительского потока",
                build_parent_workflow_url(analog_parent_name),
                use_container_width=True,
            )
        else:
            st.info("Аналог родительского потока не найден.")

    elif is_entity_parent:
        try:
            analog_entity_name = get_entity_analog(parent_name_for_display)
        except Exception as exc:
            analog_entity_name = None
            st.warning(f"Не удалось получить аналог entity: {exc}")

        st.markdown("**Аналог entity на другом кластере:**")

        if is_not_empty_text(analog_entity_name):
            st.code(str(analog_entity_name))
        else:
            st.info("Аналог entity не найден.")

    elif is_task_parent:
        try:
            analog_parent_name = get_parent_flow_analog(parent_name_for_display.split('__')[0])
        except Exception as exc:
            analog_parent_name = None
            st.warning(f"Не удалось получить аналог родительского потока: {exc}")

        st.markdown("**Аналог родительского потока на другом кластере:**")

        if is_not_empty_text(analog_parent_name):
            analog_parent_name = str(analog_parent_name)

            st.code(analog_parent_name)

            st.link_button(
                "Открыть аналог родительского потока",
                build_parent_workflow_url(analog_parent_name),
                use_container_width=True,
            )
        else:
            st.info("Аналог родительского потока не найден.")

    else:
        st.caption("Аналог не ищем: зависимость не является workflow/task/entity.")

    st.markdown("**Основные поля:**")
    st.write({
        "flow_name": row.get("flow_name"),
        "rcTypUnq": row.get("rcTypUnq"),
        "depValue": row.get("depValue"),
        "parentTyp": row.get("parentTyp"),
        "entity_index": row.get("entity_index"),
    })

    with st.expander("rcDesc"):
        st.write(row.get("rcDesc"))

    with st.expander("rcBody"):
        st.code(str(row.get("rcBody")))


def build_review_row(
    selected_type: str,
    selected_left: Optional[pd.Series],
    selected_right: Optional[pd.Series],
    manual_status: str,
    comment: str,
) -> dict:
    """
    Строка результата проверки.

    Если выбраны ADH1 и ADH3 одновременно, сохраняем оба ключа:
    - ADH1 зависимость станет проверенной;
    - ADH3 зависимость тоже станет проверенной.

    Это особенно важно для статусов matched / matched_with_changes.
    """
    adh1_review_key = None if selected_left is None else selected_left.get("review_key")
    adh3_review_key = None if selected_right is None else selected_right.get("review_key")

    review_key = adh1_review_key or adh3_review_key

    return {
        "review_key": review_key,
        "adh1_review_key": adh1_review_key,
        "adh3_review_key": adh3_review_key,
        "rcTypUnq": selected_type,

        "adh1_flow_name": None if selected_left is None else selected_left.get("flow_name"),
        "adh1_depndId": None if selected_left is None else selected_left.get("depndId"),
        "adh1_parentTyp": None if selected_left is None else selected_left.get("parentTyp"),
        "adh1_parentNmeUnq": None if selected_left is None else selected_left.get("parentNmeUnq"),
        "adh1_depValue": None if selected_left is None else selected_left.get("depValue"),

        "adh3_flow_name": None if selected_right is None else selected_right.get("flow_name"),
        "adh3_depndId": None if selected_right is None else selected_right.get("depndId"),
        "adh3_parentTyp": None if selected_right is None else selected_right.get("parentTyp"),
        "adh3_parentNmeUnq": None if selected_right is None else selected_right.get("parentNmeUnq"),
        "adh3_depValue": None if selected_right is None else selected_right.get("depValue"),

        "manual_status": manual_status,
        "comment": comment,
    }


# ------------------------------------------------------------
# Streamlit app
# ------------------------------------------------------------

st.set_page_config(
    page_title="Dependency Review: new JSON",
    layout="wide",
)

st.title("Dependency Review: ADH1 vs ADH3, new JSON format")

if not Path(ADH1_PATH).exists():
    st.error(f"Не найден файл {ADH1_PATH}")
    st.stop()

if not Path(ADH3_PATH).exists():
    st.error(f"Не найден файл {ADH3_PATH}")
    st.stop()

try:
    adh1_all = flatten_conditions_from_file(ADH1_PATH, "adh1")
    adh3_all = flatten_conditions_from_file(ADH3_PATH, "adh3")
except Exception as exc:
    st.exception(exc)
    st.stop()

if adh1_all.empty and adh3_all.empty:
    st.error("В обоих JSON не найдено conditions.")
    st.stop()

reviews = load_reviews()
review_map = get_review_map(reviews)

st.divider()

st.subheader("Выбор потоков")

flow_col1, flow_col2 = st.columns(2)

adh1_flows = adh1_all["flow_name"].dropna().astype(str).unique().tolist()
adh3_flows = adh3_all["flow_name"].dropna().astype(str).unique().tolist()

with flow_col1:
    if not adh1_flows:
        st.error("В ADH1-файле не найдено потоков.")
        st.stop()

    selected_adh1_flow = st.selectbox(
        "Поток ADH1",
        adh1_flows,
    )

    st.link_button(
        "Открыть поток ADH1",
        f"http://adp-eiap-app1.adp.local/#/workflows/{selected_adh1_flow}/tasks",
        use_container_width=True,
    )

with flow_col2:
    if not adh3_flows:
        st.error("В ADH3-файле не найдено потоков.")
        st.stop()

    selected_adh3_flow = st.selectbox(
        "Поток ADH3",
        adh3_flows,
    )

    st.link_button(
        "Открыть поток ADH3",
        f"http://adp-eiap-app1.adp.local/#/workflows/{selected_adh3_flow}/tasks",
        use_container_width=True,
    )

adh1 = adh1_all[adh1_all["flow_name"].astype(str) == selected_adh1_flow].copy()
adh3 = adh3_all[adh3_all["flow_name"].astype(str) == selected_adh3_flow].copy()

all_types = sorted(
    set(adh1["rcTypUnq"].dropna().astype(str).tolist())
    | set(adh3["rcTypUnq"].dropna().astype(str).tolist())
)

if not all_types:
    st.warning("Для выбранной пары потоков не найдено rcTypUnq.")
    st.stop()

selected_type = st.selectbox(
    "Тип зависимости rcTypUnq",
    all_types,
)

left_df = adh1[adh1["rcTypUnq"].astype(str) == selected_type].copy()
right_df = adh3[adh3["rcTypUnq"].astype(str) == selected_type].copy()

if not left_df.empty:
    left_df["review_key"] = left_df.apply(make_review_key, axis=1)
    left_df["manual_status"] = left_df["review_key"].astype(str).map(review_map)
    left_df["is_reviewed"] = left_df["manual_status"].notna()
    left_df["label"] = left_df.apply(make_left_label, axis=1)
else:
    left_df["review_key"] = []
    left_df["manual_status"] = []
    left_df["is_reviewed"] = []
    left_df["label"] = []

if not right_df.empty:
    right_df["review_key"] = right_df.apply(make_review_key, axis=1)
    right_df["manual_status"] = right_df["review_key"].astype(str).map(review_map)
    right_df["is_reviewed"] = right_df["manual_status"].notna()
    right_df["label"] = right_df.apply(make_right_label, axis=1)
else:
    right_df["review_key"] = []
    right_df["manual_status"] = []
    right_df["is_reviewed"] = []
    right_df["label"] = []

st.divider()

metric_col1, metric_col2, metric_col3, metric_col4, metric_col5, metric_col6 = st.columns(6)

checked_count_adh1 = int(left_df["is_reviewed"].sum()) if not left_df.empty else 0
unchecked_count_adh1 = int((~left_df["is_reviewed"]).sum()) if not left_df.empty else 0

checked_count_adh3 = int(right_df["is_reviewed"].sum()) if not right_df.empty else 0
unchecked_count_adh3 = int((~right_df["is_reviewed"]).sum()) if not right_df.empty else 0

with metric_col1:
    st.metric("ADH1 dependencies", len(left_df))

with metric_col2:
    st.metric("ADH3 dependencies", len(right_df))

with metric_col3:
    st.metric("Проверено ADH1", checked_count_adh1)

with metric_col4:
    st.metric("Осталось ADH1", unchecked_count_adh1)

with metric_col5:
    st.metric("Проверено ADH3", checked_count_adh3)

with metric_col6:
    st.metric("Осталось ADH3", unchecked_count_adh3)

if (not left_df.empty and checked_count_adh1 > 0) or (not right_df.empty and checked_count_adh3 > 0):
    with st.expander("Статусы по выбранному rcTypUnq"):
        status_counts = []

        if not left_df.empty:
            left_status = (
                left_df["manual_status"]
                .fillna("not_checked")
                .astype(str)
            )

            left_counts = (
                left_status
                .value_counts()
                .rename_axis("manual_status")
                .reset_index(name="adh1_count")
            )
            status_counts.append(left_counts)

        if not right_df.empty:
            right_status = (
                right_df["manual_status"]
                .fillna("not_checked")
                .astype(str)
            )

            right_counts = (
                right_status
                .value_counts()
                .rename_axis("manual_status")
                .reset_index(name="adh3_count")
            )
            status_counts.append(right_counts)

        if len(status_counts) == 2:
            st.write(
                pd.merge(
                    status_counts[0],
                    status_counts[1],
                    on="manual_status",
                    how="outer",
                ).fillna(0)
            )
        else:
            st.write(status_counts[0])

filter_col1, filter_col2 = st.columns(2)

with filter_col1:
    show_only_unchecked = st.checkbox(
        "Показывать только непроверенные зависимости ADH1",
        value=True,
    )

with filter_col2:
    show_only_unchecked_adh3 = st.checkbox(
        "Показывать только непроверенные зависимости ADH3",
        value=True,
    )

if show_only_unchecked and not left_df.empty:
    left_df_for_select = left_df[~left_df["is_reviewed"]].copy()
else:
    left_df_for_select = left_df.copy()

if show_only_unchecked_adh3 and not right_df.empty:
    right_df_for_select = right_df[~right_df["is_reviewed"]].copy()
else:
    right_df_for_select = right_df.copy()

st.divider()

col1, col2 = st.columns(2)

selected_left = None
selected_right = None

with col1:
    st.subheader("ADH1")

    if left_df.empty:
        st.warning("Нет зависимостей этого типа на ADH1.")
    elif left_df_for_select.empty:
        if show_only_unchecked:
            st.success("Все зависимости этого типа на ADH1 уже проверены.")
        else:
            st.warning("Нет зависимостей этого типа на ADH1.")
    else:
        selected_left_label = st.selectbox(
            "Зависимость ADH1",
            left_df_for_select["label"].tolist(),
        )

        selected_left = left_df_for_select[
            left_df_for_select["label"] == selected_left_label
        ].iloc[0]

        if selected_left.get("is_reviewed"):
            st.info(
                f"Эта ADH1-зависимость уже проверена: "
                f"{selected_left.get('manual_status')}"
            )

        show_dependency_details("Детали ADH1", selected_left)

with col2:
    st.subheader("ADH3")

    if right_df.empty:
        st.warning("Нет зависимостей этого типа на ADH3.")
    elif right_df_for_select.empty:
        if show_only_unchecked_adh3:
            st.success("Все зависимости этого типа на ADH3 уже проверены.")
        else:
            st.warning("Нет зависимостей этого типа на ADH3.")
    else:
        right_options = ["<нет соответствия>"] + right_df_for_select["label"].tolist()

        selected_right_label = st.selectbox(
            "Зависимость ADH3",
            right_options,
        )

        if selected_right_label != "<нет соответствия>":
            selected_right = right_df_for_select[
                right_df_for_select["label"] == selected_right_label
            ].iloc[0]

        if selected_right is not None and selected_right.get("is_reviewed"):
            st.info(
                f"Эта ADH3-зависимость уже проверена: "
                f"{selected_right.get('manual_status')}"
            )

        show_dependency_details("Детали ADH3", selected_right)

st.divider()

st.subheader("Ручная проверка")

manual_status = st.selectbox(
    "Статус ручной проверки",
    [
        "matched",
        "matched_with_changes",
        "missing_on_adh3",
        "unnecessary_on_adh3",
        "need_review",
        "ignored",
    ],
)

comment = st.text_area("Комментарий")

save_col1, save_col2 = st.columns(2)

with save_col1:
    save_pair_disabled = selected_left is None

    if st.button(
        "Сохранить проверку ADH1 → ADH3",
        disabled=save_pair_disabled,
    ):
        save_review(
            build_review_row(
                selected_type=selected_type,
                selected_left=selected_left,
                selected_right=selected_right,
                manual_status=manual_status,
                comment=comment,
            )
        )

        st.success("Сохранено как проверка ADH1")
        st.rerun()

with save_col2:
    save_adh3_disabled = selected_right is None

    if st.button(
        "Сохранить проверку только ADH3",
        disabled=save_adh3_disabled,
    ):
        save_review(
            build_review_row(
                selected_type=selected_type,
                selected_left=None,
                selected_right=selected_right,
                manual_status=manual_status,
                comment=comment,
            )
        )

        st.success("Сохранено как проверка ADH3")
        st.rerun()

st.divider()

st.subheader("Текущие результаты ручной проверки")

reviews = load_reviews()

if reviews.empty:
    st.info("Пока нет сохраненных результатов.")
else:
    visible_columns = [
        "rcTypUnq",
        "adh1_flow_name",
        "adh1_parentTyp",
        "adh1_parentNmeUnq",
        "adh3_flow_name",
        "adh3_parentTyp",
        "adh3_parentNmeUnq",
        "manual_status",
        "comment",
    ]

    visible_reviews = reviews[
        [col for col in visible_columns if col in reviews.columns]
    ].copy()

    st.dataframe(
        visible_reviews.style.apply(highlight_status, axis=1),
        use_container_width=True,
    )

    st.download_button(
        "Скачать короткий CSV",
        data=visible_reviews.to_csv(index=False).encode("windows-1251"),
        file_name="manual_dependency_review_new_json_short.csv",
        mime="text/csv",
    )

    st.download_button(
        "Скачать полный технический CSV",
        data=reviews.to_csv(index=False).encode("utf-8"),
        file_name="manual_dependency_review_new_json_full.csv",
        mime="text/csv",
    )

    if st.button("Очистить все результаты проверки"):
        Path(RESULT_PATH).unlink(missing_ok=True)
        st.success("Результаты очищены")
        st.rerun()
